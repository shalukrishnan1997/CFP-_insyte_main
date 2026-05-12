"""End-to-end mixed-payment-method batch routing test.

Unit 9 — verifies that a single ``DonationBatch`` containing one donation per
``Donation.PAYMENT_METHOD_*`` value routes each donation to the correct
downstream system (Stripe / banking slip / letter) without cross-contamination.

After QA approval:

* ``card`` → exactly one ``StripePayment`` row created when
  ``BatchPaymentService.process_batch_payments`` runs; never linked to a
  ``PayingInSlip``.
* ``cash`` / ``caf`` / ``cheque`` / ``postal_order`` → no ``StripePayment``;
  eligible for ``PayingInSlip`` linkage; assigning to a slip works.
* ``direct_debit`` → no ``StripePayment``, never bankable, encrypted
  ``sort_code`` and ``account_number`` round-trip through the DB.
* ``non_financial`` → no ``StripePayment``, never bankable, immediately
  eligible for thank-you letter generation.

The fixture PDF (``tests/fixtures/000015.pdf``) is the realistic donation form
upstream Document AI ingests; this test bypasses OCR and seeds the resulting
``DonationBatch`` directly so the assertions stay deterministic and fast.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from banking.utils import BANKABLE_PAYMENT_TYPES
from donations.models import Donation, DonationBatch
from letters.tasks import build_letter_generation_queryset
from payments.batch_payment import BatchPaymentService
from payments.models import StripePayment
from tests.factories import (
    DonationBatchFactory,
    DonationFactory,
    PayingInSlipFactory,
    StripeCustomerFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from core.models import User

# All seven payment-method values we drive through the pipeline.
ALL_PAYMENT_METHODS: tuple[str, ...] = (
    Donation.PAYMENT_METHOD_CARD,
    Donation.PAYMENT_METHOD_DIRECT_DEBIT,
    Donation.PAYMENT_METHOD_CASH,
    Donation.PAYMENT_METHOD_CAF,
    Donation.PAYMENT_METHOD_CHEQUE,
    Donation.PAYMENT_METHOD_POSTAL_ORDER,
    Donation.PAYMENT_METHOD_NON_FINANCIAL,
)

# Slip-eligible methods per ``banking.utils.BANKABLE_PAYMENT_TYPES``.
SLIP_PAYMENT_METHODS: tuple[str, ...] = (
    Donation.PAYMENT_METHOD_CASH,
    Donation.PAYMENT_METHOD_CAF,
    Donation.PAYMENT_METHOD_CHEQUE,
    Donation.PAYMENT_METHOD_POSTAL_ORDER,
)

# Methods that must NEVER appear on a paying-in slip.
NON_SLIP_PAYMENT_METHODS: tuple[str, ...] = (
    Donation.PAYMENT_METHOD_CARD,
    Donation.PAYMENT_METHOD_DIRECT_DEBIT,
    Donation.PAYMENT_METHOD_NON_FINANCIAL,
)


_FIXTURE_PDF_PATH: Path = (
    Path(__file__).resolve().parent.parent / "fixtures" / "000015.pdf"
)


@contextlib.contextmanager
def _stub_stripe_for_routing(
    intent_id: str, charge_id: str, customer_client: object
) -> Iterator[None]:
    """Patch the Stripe SDK seams so card-payment processing stays offline.

    Args:
        intent_id: ``stripe.PaymentIntent`` id the stub returns.
        charge_id: ``latest_charge`` value on the returned intent.
        customer_client: ``Client`` to attach to the stubbed ``StripeCustomer``.
    """
    mock_pi = MagicMock()
    mock_pi.id = intent_id
    mock_pi.status = "succeeded"
    mock_pi.latest_charge = charge_id
    mock_pi.to_dict.return_value = {}

    with (
        patch("stripe.PaymentIntent.create", return_value=mock_pi),
        patch(
            "payments.services.StripePaymentService.get_or_create_customer",
            return_value=StripeCustomerFactory(client=customer_client),
        ),
        patch(
            "payments.services.StripePaymentService._get_api_key",
            return_value="sk_test_routing",
        ),
        patch(
            "payments.campaign_payment.CampaignPaymentService."
            "update_campaign_payment_status",
        ),
    ):
        yield


def _make_donation(
    batch: DonationBatch,
    payment_method: str,
    qa_status: str = Donation.QA_STATUS_PENDING,
) -> Donation:
    """Build a donation with the per-method extra fields filled in.

    Args:
        batch: Parent ``DonationBatch``.
        payment_method: One of the seven ``Donation.PAYMENT_METHOD_*`` values.
        qa_status: Initial QA status for the donation.

    Returns:
        Persisted ``Donation`` instance.
    """
    extras: dict[str, object] = {}
    if payment_method == Donation.PAYMENT_METHOD_DIRECT_DEBIT:
        # Encrypted at rest — see ``Donation.audit_exclude_fields``.
        extras["sort_code"] = "123456"
        extras["account_number"] = "12345678"
    elif payment_method == Donation.PAYMENT_METHOD_CHEQUE:
        extras["cheque_number"] = "001234"
    elif payment_method == Donation.PAYMENT_METHOD_CAF:
        extras["caf_voucher_number"] = "CAF-001"
    elif payment_method == Donation.PAYMENT_METHOD_POSTAL_ORDER:
        extras["postal_order_number"] = "PO-001"
        extras["postal_issuer"] = "Royal Mail"
    elif payment_method == Donation.PAYMENT_METHOD_NON_FINANCIAL:
        extras["non_financial_reason"] = "In-Kind"

    return DonationFactory(
        batch=batch,
        campaign=batch.campaign,
        payment_method=payment_method,
        qa_status=qa_status,
        amount=Decimal("25.00"),
        **extras,
    )


@pytest.fixture()
def staff_client() -> tuple[Client, User]:
    """Authenticated staff client + the underlying ``User``.

    Returns:
        Tuple of ``(client, user)``. The user has ``is_staff=is_superuser=True``
        so it bypasses the granular permission helpers in
        ``responsehandling/permissions.py``.
    """
    user = UserFactory(is_staff=True, is_superuser=True)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = Client()
    client.force_login(user)
    return client, user


@pytest.mark.django_db()
class TestMixedPaymentBatchRouting:
    """Verify per-payment-method routing isolation after batch QA approval."""

    def test_pdf_fixture_is_staged(self) -> None:
        """The 000015.pdf donation-form fixture must be staged for this suite."""
        assert _FIXTURE_PDF_PATH.exists(), (
            f"Expected donation-form PDF fixture at {_FIXTURE_PDF_PATH}; "
            "Unit 9 must stage it before running."
        )
        assert _FIXTURE_PDF_PATH.stat().st_size > 0, "Fixture PDF must not be empty."

    def test_card_donation_creates_stripe_payment_only(self) -> None:
        """A card donation drives exactly one StripePayment via BatchPaymentService.

        Stubs the Stripe SDK so no network call goes out, then runs
        ``BatchPaymentService.process_batch_payments`` and asserts that:

        * Exactly one ``StripePayment`` row exists for the card donation.
        * No ``PayingInSlip`` linkage was made.
        * The donation's ``payment_status`` flipped to ``completed``.
        """
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_APPROVED,
            default_payment_method=Donation.PAYMENT_METHOD_CARD,
        )
        donation = _make_donation(
            batch,
            Donation.PAYMENT_METHOD_CARD,
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        with _stub_stripe_for_routing(
            intent_id="pi_test_card_routing",
            charge_id="ch_test_card_routing",
            customer_client=batch.campaign.client,
        ):
            user = UserFactory(is_staff=True)
            result = BatchPaymentService.process_batch_payments(batch, user)

        assert result["success"] is True
        assert result["successful"] == 1
        assert result["failed"] == 0

        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED
        assert donation.paying_in_slip is None

        payments = list(StripePayment.objects.filter(donation=donation))
        assert len(payments) == 1, (
            f"Expected exactly one StripePayment for card donation, got {len(payments)}"
        )
        assert payments[0].status == StripePayment.STATUS_SUCCEEDED

    @pytest.mark.parametrize("payment_method", SLIP_PAYMENT_METHODS)
    def test_slip_based_donations_eligible_for_slip_linkage(
        self, payment_method: str
    ) -> None:
        """Slip-based donations route to a PayingInSlip, never to Stripe.

        Args:
            payment_method: One of cash/caf/cheque/postal_order.
        """
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_APPROVED,
            default_payment_method=payment_method,
        )
        donation = _make_donation(
            batch, payment_method, qa_status=Donation.QA_STATUS_APPROVED
        )

        # Sanity: BatchPaymentService refuses non-card methods up front.
        user = UserFactory(is_staff=True)
        result = BatchPaymentService.process_donation_payment(donation, user)
        assert result["success"] is False
        assert "not a card payment" in result["error"]
        assert not StripePayment.objects.filter(donation=donation).exists()

        # Slip linkage works for every method in ``SLIP_PAYMENT_METHODS``.
        slip = PayingInSlipFactory(client=batch.campaign.client)
        donation.paying_in_slip = slip
        donation.save(update_fields=["paying_in_slip"])

        donation.refresh_from_db()
        assert donation.paying_in_slip_id == slip.id

    def test_direct_debit_skips_payment_and_banking(self) -> None:
        """Direct debit donations stay off Stripe and off paying-in slips.

        Also verifies the encrypted bank fields round-trip — this is the
        core PII protection contract for direct-debit donations.
        """
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_APPROVED,
            default_payment_method=Donation.PAYMENT_METHOD_DIRECT_DEBIT,
        )
        donation = _make_donation(
            batch,
            Donation.PAYMENT_METHOD_DIRECT_DEBIT,
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        # No Stripe processing for direct debit.
        user = UserFactory(is_staff=True)
        result = BatchPaymentService.process_donation_payment(donation, user)
        assert result["success"] is False
        assert "not a card payment" in result["error"]
        assert not StripePayment.objects.filter(donation=donation).exists()

        # Direct debit is not a bankable type — never goes on a slip.
        assert Donation.PAYMENT_METHOD_DIRECT_DEBIT not in BANKABLE_PAYMENT_TYPES

        # Encrypted fields round-trip through the DB.
        reloaded = Donation.objects.get(pk=donation.pk)
        assert reloaded.sort_code == "123456"
        assert reloaded.account_number == "12345678"
        assert reloaded.paying_in_slip is None

    def test_non_financial_immediately_letter_eligible(self) -> None:
        """A non-financial donation is letter-eligible right after QA approval.

        It must never reach Stripe or a paying-in slip; the only downstream
        system it touches is the letter-generation queryset.
        """
        batch = DonationBatchFactory(
            status=DonationBatch.STATUS_APPROVED,
            default_payment_method=Donation.PAYMENT_METHOD_NON_FINANCIAL,
        )
        donation = _make_donation(
            batch,
            Donation.PAYMENT_METHOD_NON_FINANCIAL,
            qa_status=Donation.QA_STATUS_APPROVED,
        )

        # Not a card → BatchPaymentService refuses.
        user = UserFactory(is_staff=True)
        result = BatchPaymentService.process_donation_payment(donation, user)
        assert result["success"] is False
        assert not StripePayment.objects.filter(donation=donation).exists()

        # Not bankable.
        assert Donation.PAYMENT_METHOD_NON_FINANCIAL not in BANKABLE_PAYMENT_TYPES
        assert donation.paying_in_slip is None

        # Letter-eligible queryset includes it.
        eligible = build_letter_generation_queryset(
            campaign=batch.campaign,
            donation_filter="all",
            regenerate_mode=False,
            source_donation_batch_id=batch.id,
        )
        eligible_ids = {str(pk) for pk in eligible.values_list("id", flat=True)}
        assert str(donation.id) in eligible_ids

    def test_no_cross_contamination_in_mixed_batch(
        self, staff_client: tuple[Client, User]
    ) -> None:
        """One batch, all 7 payment methods, single QA approval → clean routing.

        End-to-end check: drives the real ``qa_approve_batch`` view (so the
        batch ``post_save`` cascade fires under the production middleware
        stack), then runs ``BatchPaymentService.process_batch_payments``
        against the approved batch and asserts row counts per downstream
        system. Contract:

        * StripePayment count == 1 (card only).
        * No donation other than the card one ever reaches Stripe.
        * Slip linkage works only for the four bankable methods.
        * Direct-debit and non-financial donations remain untouched by
          both Stripe and the banking flow.
        """
        client, _user = staff_client

        batch = DonationBatchFactory(status=DonationBatch.STATUS_PENDING_QA)
        # Card donations must be individually QA-approved before batch
        # approval — ``_batch_approval_blocked_response`` blocks the batch
        # while a card donation is still pending (it requires per-donation
        # secure-payment QA). The non-card donations stay pending so the
        # cascade in ``_commit_batch_status`` auto-approves them on batch
        # approval, mirroring the production flow.
        donations: dict[str, Donation] = {}
        for method in ALL_PAYMENT_METHODS:
            initial_qa = (
                Donation.QA_STATUS_APPROVED
                if method == Donation.PAYMENT_METHOD_CARD
                else Donation.QA_STATUS_PENDING
            )
            donations[method] = _make_donation(batch, method, qa_status=initial_qa)

        # 1. QA approve via the real view — this triggers the cascade that
        #    auto-approves remaining pending donations in one go.
        response = client.post(
            reverse("custom_admin:qa_approve_batch", kwargs={"batch_id": batch.id}),
        )
        assert response.status_code == 302, (
            f"Expected redirect after batch approve, got {response.status_code}"
        )

        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_APPROVED
        for method, donation in donations.items():
            donation.refresh_from_db()
            assert donation.qa_status == Donation.QA_STATUS_APPROVED, (
                f"{method} donation should be auto-approved by batch cascade"
            )

        # 2. Run the batch payment service. Stub Stripe so no network call.
        with _stub_stripe_for_routing(
            intent_id="pi_test_mixed_routing",
            charge_id="ch_test_mixed_routing",
            customer_client=batch.campaign.client,
        ):
            payer = UserFactory(is_staff=True)
            result = BatchPaymentService.process_batch_payments(batch, payer)

        # 3. Assertions on row counts and per-donation state.
        assert result["successful"] == 1, (
            "Only the single card donation should drive a Stripe charge; "
            f"got successful={result['successful']}"
        )
        assert result["failed"] == 0
        assert result["total"] == 1, (
            f"Stripe-eligible donation count must be 1 (card only); "
            f"got total={result['total']}"
        )

        # Exactly one StripePayment in the entire batch.
        all_payments = list(
            StripePayment.objects.filter(donation__batch=batch).order_by("id")
        )
        assert len(all_payments) == 1, (
            f"Expected exactly 1 StripePayment for the mixed batch, got "
            f"{len(all_payments)}: "
            f"{[(p.donation.payment_method, p.status) for p in all_payments]}"
        )
        card_payment = all_payments[0]
        assert card_payment.donation_id == donations[Donation.PAYMENT_METHOD_CARD].id, (
            "The single StripePayment must belong to the card donation"
        )
        assert card_payment.status == StripePayment.STATUS_SUCCEEDED

        # Card donation: completed; no slip.
        card_donation = donations[Donation.PAYMENT_METHOD_CARD]
        card_donation.refresh_from_db()
        assert card_donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED
        assert card_donation.paying_in_slip is None

        # Non-card donations: never on Stripe.
        for method in ALL_PAYMENT_METHODS:
            if method == Donation.PAYMENT_METHOD_CARD:
                continue
            assert not StripePayment.objects.filter(
                donation=donations[method]
            ).exists(), f"{method} donation must never have a StripePayment row"

        # 4. Slip linkage works for the four bankable methods only.
        slip = PayingInSlipFactory(client=batch.campaign.client)
        for method in SLIP_PAYMENT_METHODS:
            donation = donations[method]
            donation.paying_in_slip = slip
            donation.save(update_fields=["paying_in_slip"])

        slip_donations = set(slip.donations.values_list("payment_method", flat=True))
        assert slip_donations == set(SLIP_PAYMENT_METHODS), (
            f"Slip should hold only the 4 bankable methods, got {slip_donations}"
        )

        # 5. Non-bankable methods stay off the slip.
        for method in NON_SLIP_PAYMENT_METHODS:
            donations[method].refresh_from_db()
            assert donations[method].paying_in_slip is None, (
                f"{method} must never be linked to a PayingInSlip"
            )
