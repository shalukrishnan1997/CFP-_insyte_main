"""End-to-end integration tests for non-Stripe / non-banking donation paths.

Covers two ``Donation.payment_method`` values that bypass *both* the Stripe
batch payment service *and* the paying-in-slip banking workflow:

* ``direct_debit`` — donor authorises a UK direct debit. The bureau records
  ``sort_code`` and ``account_number`` (encrypted at rest via
  ``django-fernet-encrypted-fields``) but does not auto-charge anything.
* ``non_financial`` — pledges, in-kind goods, volunteer hours. No money
  changes hands so neither rail (Stripe nor banking) ever sees the row.

For each method the test drives a four-donor slice through the QA approval
HTTP endpoint, asserts the encrypted fields round-trip cleanly, asserts the
ciphertext on disk is unreadable, and confirms the approved donations are
eligible for the letter generation queryset and skipped by the credit card
batch payment selector.

The fixture PDF (``tests/fixtures/000015.pdf``) is staged under source
control so downstream e2e tests in this batch can attach it as a scan input
without re-uploading the binary on every run.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client
from django.urls import reverse

from banking.utils import BANKABLE_PAYMENT_TYPES
from donations.models import Donation, DonationBatch
from letters.tasks import build_letter_generation_queryset
from payments.batch_payment import BatchPaymentService
from payments.models import StripePayment
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Path to the donation form PDF used as upstream scan input across this batch
#: of e2e tests. Staged in ``tests/fixtures/`` so each unit's worktree picks
#: up the same binary.
FIXTURE_PDF = Path(__file__).resolve().parent.parent / "fixtures" / "000015.pdf"


@pytest.fixture()
def staff_client() -> tuple[Client, Any]:
    """Return an authenticated staff client and the underlying user.

    The user has admin group + superuser flags so the QA approval permission
    decorators (``is_authenticated_and_is_staff`` /
    ``has_permission_or_is_staff``) accept the request without explicit
    perm assignment.

    Returns:
        Tuple of ``(authenticated_client, staff_user)``.
    """
    user = UserFactory(is_staff=True, is_superuser=True)
    admin_group, _ = Group.objects.get_or_create(name="admin")
    user.groups.add(admin_group)

    client = Client()
    client.force_login(user)
    return client, user


def _make_batch(
    payment_method: str, *, count: int = 4
) -> tuple[DonationBatch, list[Donation]]:
    """Create a pending-QA batch with ``count`` donors of ``payment_method``.

    Each donor gets a unique URN, a stable ``SystemDonor`` (via the factory's
    LazyAttribute), and the donation lands in the same batch + campaign so
    the QA approval cascade flips them all in one POST.

    Args:
        payment_method: One of ``Donation.PAYMENT_METHOD_CHOICES``.
        count: Number of donor donations to create. Defaults to four to
            match the batch slice expected by the parallel e2e units.

    Returns:
        Tuple ``(batch, donations)`` where ``donations`` is in creation order.
    """
    bureau = ClientFactory(name=f"E2E {payment_method.title()} Charity")
    campaign = CampaignFactory(
        client=bureau,
        name=f"E2E {payment_method.title()} Campaign",
        status="active",
    )
    batch = DonationBatchFactory(
        campaign=campaign,
        batch_name=f"E2E-{payment_method.upper()}-001",
        status=DonationBatch.STATUS_PENDING_QA,
        default_payment_method=payment_method,
    )

    donations: list[Donation] = []
    for index in range(count):
        donor = DonorFactory(urn=f"E2E-{payment_method.upper()}-{index:03d}")
        kwargs: dict[str, Any] = {
            "campaign": campaign,
            "batch": batch,
            "donor": donor,
            "payment_method": payment_method,
            "amount": Decimal("25.00") + Decimal(index),
            "qa_status": Donation.QA_STATUS_PENDING,
            "letter_status": "pending",
            "payment_status": Donation.PAYMENT_STATUS_PENDING,
        }
        if payment_method == Donation.PAYMENT_METHOD_DIRECT_DEBIT:
            # Six-digit sort code + eight-digit account number per UK Faster
            # Payments format. Distinct values per donor so the per-row
            # ciphertexts are guaranteed unique.
            kwargs["sort_code"] = f"12-34-{index:02d}"
            kwargs["account_number"] = f"1234{index:04d}"
            kwargs["direct_debit_start_date"] = date(2026, 5, 1)
        elif payment_method == Donation.PAYMENT_METHOD_NON_FINANCIAL:
            kwargs["amount"] = Decimal("0.00")
            kwargs["non_financial_reason"] = "In-Kind"
            kwargs["non_financial_notes"] = f"Donor {index} pledged volunteer hours"
        donations.append(DonationFactory(**kwargs))

    return batch, donations


def _read_raw_bank_columns(
    donation: Donation,
) -> tuple[str | None, str | None]:
    """Return the raw on-disk ``(sort_code, account_number)`` for a donation.

    Bypasses the ``EncryptedCharField`` descriptor's ``from_db_value`` so the
    caller sees the Fernet ciphertext (or NULL) actually persisted. The UUID
    primary key is shaped per-backend: SQLite stores UUIDs as a 32-char hex
    string with no dashes, while Postgres accepts the canonical form.

    Args:
        donation: A persisted ``Donation`` whose row should be read raw.

    Returns:
        A 2-tuple of nullable strings — the raw values from the column.
    """
    pk_param = donation.id.hex if connection.vendor == "sqlite" else str(donation.id)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT sort_code, account_number FROM donations_donation WHERE id = %s",
            [pk_param],
        )
        row = cursor.fetchone()
    if row is None:
        raise AssertionError(
            f"Donation row {donation.pk} not found in donations_donation"
        )
    return row[0], row[1]


def _approve_batch(client: Client, batch: DonationBatch) -> int:
    """POST to the QA approve-batch endpoint and return the response code.

    Args:
        client: Authenticated staff Django test client.
        batch: Batch to approve.

    Returns:
        HTTP status code from the redirect response.
    """
    url = reverse("custom_admin:qa_approve_batch", kwargs={"batch_id": batch.id})
    response = client.post(url, data={"batch_notes": "E2E auto-approve"})
    return response.status_code


# ---------------------------------------------------------------------------
# Fixture sanity check
# ---------------------------------------------------------------------------


class TestFixtureStaging:
    """Guard the fixture PDF presence so other units can rely on it."""

    def test_pdf_fixture_is_staged(self) -> None:
        """The 000015.pdf fixture must be checked in under tests/fixtures/."""
        assert FIXTURE_PDF.exists(), (
            f"Expected fixture at {FIXTURE_PDF}; copy from repo root before running tests."
        )
        # Sanity-check the binary header so a zero-byte placeholder is rejected.
        with FIXTURE_PDF.open("rb") as handle:
            header = handle.read(5)
        assert header == b"%PDF-", (
            "Fixture file is not a valid PDF (missing %PDF- magic)"
        )


# ---------------------------------------------------------------------------
# Direct debit
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestDirectDebitE2E:
    """``payment_method='direct_debit'`` — encrypted bank fields, no auto-charge."""

    def test_full_direct_debit_lifecycle(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Approve → letters eligible, no Stripe row, no banking slip, encrypted at rest."""
        client, _user = staff_client
        batch, donations = _make_batch(Donation.PAYMENT_METHOD_DIRECT_DEBIT)

        # Banking handles cash/cheque/postal_order/CAF only.
        assert Donation.PAYMENT_METHOD_DIRECT_DEBIT not in BANKABLE_PAYMENT_TYPES

        for index, donation in enumerate(donations):
            donation.refresh_from_db()
            assert donation.sort_code == f"12-34-{index:02d}"
            assert donation.account_number == f"1234{index:04d}"

        assert _approve_batch(client, batch) == 302

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        for donation in donations:
            donation.refresh_from_db()
            assert donation.qa_status == Donation.QA_STATUS_APPROVED
            assert not StripePayment.objects.filter(donation=donation).exists()
            assert donation.paying_in_slip is None

        # Letters: build_letter_generation_queryset filters on qa_status only,
        # so DD donations should be eligible immediately after approval.
        eligible = build_letter_generation_queryset(
            campaign=batch.campaign,
            donation_filter="all",
            regenerate_mode=False,
            source_donation_batch_id=batch.id,
        )
        eligible_ids = set(eligible.values_list("id", flat=True))
        assert eligible_ids == {donation.id for donation in donations}

        # BatchPaymentService skips DD entirely — its credit card selector
        # filters on ``payment_method__in={"card"}``.
        cc_only = BatchPaymentService.get_credit_card_donations(batch)
        assert cc_only.count() == 0

    def test_encrypted_fields_persist_ciphertext_in_database(self) -> None:
        """Raw DB column for ``sort_code`` is ciphertext; ORM read decrypts it.

        Encryption uses Fernet via ``django-fernet-encrypted-fields``. The
        column type is ``TEXT`` (not the declared CharField max_length), and
        the value at rest is a urlsafe-base64 token that bears no resemblance
        to the plaintext.
        """
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_DIRECT_DEBIT,
            sort_code="65-43-21",
            account_number="98765432",
            amount=Decimal("10.00"),
        )

        raw_sort, raw_account = _read_raw_bank_columns(donation)
        assert raw_sort != "65-43-21"
        assert raw_account != "98765432"
        # Fernet tokens are urlsafe base64 prefixed with ``gAAAAA``.
        assert raw_sort is not None and raw_sort.startswith("gAAAAA")
        assert raw_account is not None and raw_account.startswith("gAAAAA")

        donation.refresh_from_db()
        assert donation.sort_code == "65-43-21"
        assert donation.account_number == "98765432"

    def test_blank_bank_fields_store_null(self) -> None:
        """Empty sort_code/account_number persist as SQL NULL.

        The encrypted field's ``get_prep_value`` returns ``None`` for empty
        strings, so the column is NULL rather than a Fernet token of "". The
        model defines ``null=True`` to accommodate this.
        """
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_DIRECT_DEBIT,
            sort_code="",
            account_number="",
        )
        raw_sort, raw_account = _read_raw_bank_columns(donation)
        assert raw_sort is None
        assert raw_account is None


# ---------------------------------------------------------------------------
# Non-financial
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestNonFinancialE2E:
    """``payment_method='non_financial'`` — pledges/in-kind/volunteer hours."""

    def test_full_non_financial_lifecycle(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Approve → letters eligible, no Stripe, no banking, no payment status flip."""
        client, _user = staff_client
        batch, donations = _make_batch(Donation.PAYMENT_METHOD_NON_FINANCIAL)

        assert Donation.PAYMENT_METHOD_NON_FINANCIAL not in BANKABLE_PAYMENT_TYPES

        assert _approve_batch(client, batch) == 302

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED

        for donation in donations:
            donation.refresh_from_db()
            assert donation.qa_status == Donation.QA_STATUS_APPROVED
            assert donation.amount == Decimal("0.00")
            assert donation.non_financial_reason == "In-Kind"
            assert not StripePayment.objects.filter(donation=donation).exists()
            assert donation.paying_in_slip is None
            # ``payment_status`` stays at the default sentinel — no completion
            # flip because no real payment ever happened.
            assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING

        # Letters are eligible immediately after QA approval.
        eligible = build_letter_generation_queryset(
            campaign=batch.campaign,
            donation_filter="all",
            regenerate_mode=False,
            source_donation_batch_id=batch.id,
        )
        eligible_ids = set(eligible.values_list("id", flat=True))
        assert eligible_ids == {donation.id for donation in donations}

        # No card payments to process.
        assert BatchPaymentService.get_credit_card_donations(batch).count() == 0

    def test_non_financial_donation_skipped_by_card_payment_service(self) -> None:
        """``BatchPaymentService.process_donation_payment`` rejects non-card methods.

        Confirms the service-level guard is in place even if a non-financial
        donation slips into the card retry queue by mistake.
        """
        donation = DonationFactory(
            payment_method=Donation.PAYMENT_METHOD_NON_FINANCIAL,
            qa_status=Donation.QA_STATUS_APPROVED,
            amount=Decimal("0.00"),
        )
        result = BatchPaymentService.process_donation_payment(
            donation,
            user=donation.filled_by,
            require_qa_approved=True,
        )
        assert result == {"success": False, "error": "Donation is not a card payment"}
