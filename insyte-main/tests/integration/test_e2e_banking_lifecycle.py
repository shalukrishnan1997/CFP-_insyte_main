"""End-to-end banking lifecycle for slip-based payment types.

FIN-E2E-BANKING-* test cases. Drives an 8-donor slice through the full
post-QA banking lifecycle for each slip-eligible payment method
(``cheque`` / ``cash`` / ``caf`` / ``postal_order``):

1. Build a ``DonationBatch`` with mixed slip-eligible donations linked
   to a ``ScanPlaceholder`` so the QA approval path mirrors the real
   scanner pipeline.
2. ``force_login`` a staff user and POST ``custom_admin:qa_approve_batch``
   to flip the batch + cascade donations to ``approved``.
3. Create a ``PayingInSlip`` per payment type via ``PayingInSlipFactory``
   and link the relevant donations so ``paying_in_slip`` is set.
4. Drive ``custom_admin:slip_record_processing`` for the success path
   (``completion_status='full_success'``) — assert donations stay
   ``completed`` and no reversal audit row is written.
5. Drive a separate slip through bounce / partial-success and assert
   every settled donation flips to ``reversed`` with an ``AuditLog``
   row carrying a ``payment_status`` diff.

This module covers the regression that originally shipped in commit
``4ae9564`` ("auto-reverse donation payments on cheque bounce / cash
discrepancy") from the operator's perspective rather than the service
unit-test perspective.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from django.test import Client
from django.urls import reverse

from audit.models import AuditLog
from banking.models import PayingInSlip
from banking.services import BankingService
from donations.models import Donation, DonationBatch
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    PayingInSlipFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from clients.models import Client as ClientModel
    from core.models import User

# Slip-eligible payment methods. The card path goes through Stripe and is
# excluded from the slip-based banking lifecycle.
_SLIP_PAYMENT_METHODS: tuple[str, ...] = ("cheque", "cash", "caf", "postal_order")


def _build_eight_donor_slice(
    client_obj: ClientModel,
) -> tuple[DonationBatch, list[Donation]]:
    """Construct a QA-pending batch with eight slip-eligible donations.

    Two donations per slip-eligible payment method (cheque/cash/caf/
    postal_order) so each slip in the bounce path has a non-trivial
    multi-row reversal target.

    Args:
        client_obj: Client that owns the campaign.

    Returns:
        Tuple of (batch, donations). All donations are ``qa_status=pending``,
        ``payment_status=pending`` so the QA-approve path will cascade them
        to ``approved``.
    """
    campaign = CampaignFactory(client=client_obj, status="active")
    batch = DonationBatchFactory(
        campaign=campaign,
        status=DonationBatch.STATUS_PENDING_QA,
        default_payment_method="cheque",
    )
    donations: list[Donation] = []
    for method in _SLIP_PAYMENT_METHODS:
        for _ in range(2):
            donations.append(
                DonationFactory(
                    campaign=campaign,
                    batch=batch,
                    payment_method=method,
                    payment_status=Donation.PAYMENT_STATUS_PENDING,
                    qa_status=Donation.QA_STATUS_PENDING,
                    amount=Decimal("25.00"),
                )
            )
    assert len(donations) == 8
    return batch, donations


def _approve_batch_via_qa_view(client: Client, batch: DonationBatch) -> None:
    """POST to ``qa_approve_batch`` and assert the cascade ran.

    The view redirects to the QA dashboard on success. After the
    redirect the batch must be ``APPROVED`` and every donation must be
    ``QA_STATUS_APPROVED`` so the slip-eligibility filter in the
    Daily Banking dashboard accepts them.

    Args:
        client: A logged-in staff Django ``Client``.
        batch: The pending-QA donation batch to approve.
    """
    response = client.post(
        reverse("custom_admin:qa_approve_batch", kwargs={"batch_id": batch.pk}),
    )
    # 302 redirect to QA dashboard on success.
    assert response.status_code == 302, response.content
    batch.refresh_from_db()
    assert batch.status == DonationBatch.STATUS_APPROVED
    assert (
        batch.donations.filter(qa_status=Donation.QA_STATUS_APPROVED).count()
        == batch.donations.count()
    )


def _link_donations_to_slip(donations: list[Donation], slip: PayingInSlip) -> None:
    """Attach each donation to ``slip`` and mark its payment as settled.

    For non-card payment methods, donations carry ``payment_status='pending'``
    out of the QA cascade — banking flips them to ``completed`` once the
    cash/cheque/voucher is in hand and the slip is built. The reversal
    contract only operates on ``completed`` rows, so we bridge the gap
    here the same way the real slip-creation flow does.

    Args:
        donations: Donations to attach to the slip.
        slip: Target paying-in slip.
    """
    Donation.objects.filter(pk__in=[d.pk for d in donations]).update(
        paying_in_slip=slip,
        payment_status=Donation.PAYMENT_STATUS_COMPLETED,
    )


def _post_record_processing(
    client: Client,
    slip: PayingInSlip,
    *,
    completion_status: str,
    processed_amount: str,
    processing_issues: list[str] | None = None,
    custom_issue: str = "",
) -> dict[str, object]:
    """POST to ``slip_record_processing`` and parse the JSON response.

    Args:
        client: Logged-in staff client.
        slip: Slip whose processing results are being recorded.
        completion_status: One of the slip ``COMPLETION_STATUS_CHOICES``.
        processed_amount: Decimal amount the bank credited (string-form
            so the form-encoded POST round-trips identically to the UI).
        processing_issues: Picklist issue codes (defaults to empty list).
        custom_issue: Free-text reason supplied by the operator.

    Returns:
        Decoded JSON payload from the view.
    """
    response = client.post(
        reverse("custom_admin:slip_record_processing", kwargs={"slip_id": slip.pk}),
        {
            "processed_amount": processed_amount,
            "completion_status": completion_status,
            "bank_processed_date": "2026-04-29",
            "processing_issues": json.dumps(processing_issues or []),
            "custom_issue": custom_issue,
        },
    )
    assert response.status_code == 200, response.content
    payload = json.loads(response.content)
    return payload


@pytest.fixture()
def staff_client_pair() -> tuple[Client, User]:
    """Return a logged-in staff ``Client`` and the underlying ``User``.

    Uses ``force_login`` to bypass the project's mandatory 2FA — the
    banking-lifecycle assertions don't exercise the OTP flow, only
    the post-auth permission gate on each view.
    """
    user = UserFactory(is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(user)
    return client, user


# ═══════════════════════════════════════════════════════════════
# FIN-E2E-BANKING-001: full_success keeps donations completed
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
def test_full_success_lifecycle_keeps_all_eight_donations_completed(
    staff_client_pair: tuple[Client, User],
) -> None:
    """FIN-E2E-BANKING-001: clean banking pass leaves every donation settled.

    QA approves an 8-donor batch, the operator builds one slip per
    payment type and records ``full_success``. No donation should be
    reversed and no ``payment_status`` audit diff should be written.
    """
    client, _ = staff_client_pair
    client_obj = ClientFactory()
    batch, donations = _build_eight_donor_slice(client_obj)

    _approve_batch_via_qa_view(client, batch)

    by_method: dict[str, list[Donation]] = {m: [] for m in _SLIP_PAYMENT_METHODS}
    for donation in donations:
        by_method[donation.payment_method].append(donation)

    for method, method_donations in by_method.items():
        slip = PayingInSlipFactory(
            client=client_obj,
            payment_type=method,
            status="ready",
        )
        _link_donations_to_slip(method_donations, slip)
        # Recompute totals so processed_amount<=total passes view validation.
        BankingService.recalculate_slip_totals(slip)
        assert slip.total_items == len(method_donations)

        payload = _post_record_processing(
            client,
            slip,
            completion_status="full_success",
            processed_amount=str(slip.total_amount),
        )
        assert payload["success"] is True
        assert payload["reversed_donation_count"] == 0
        slip.refresh_from_db()
        assert slip.completion_status == "full_success"
        assert slip.status == "processed"

    # Every donation stays ``completed``; no reversal payment_status diffs.
    for donation in donations:
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED
        assert donation.payment_reversal_reason == ""

    reversal_logs = AuditLog.objects.filter(
        model_name="Donation",
        action="UPDATE",
        object_id__in=[str(d.pk) for d in donations],
    )
    for log in reversal_logs:
        diff = log.changes or {}
        ps_diff = diff.get("payment_status") if isinstance(diff, dict) else None
        if isinstance(ps_diff, dict):
            assert ps_diff.get("new") != Donation.PAYMENT_STATUS_REVERSED


# ═══════════════════════════════════════════════════════════════
# FIN-E2E-BANKING-002: cheque bounce reverses + audits all linked donations
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
def test_cheque_bounce_lifecycle_reverses_linked_cheque_donations(
    staff_client_pair: tuple[Client, User],
) -> None:
    """FIN-E2E-BANKING-002: ``completion_status='issues'`` flips donations to reversed.

    Cheque slip rejected by the bank. All cheque donations on that slip
    must end up ``reversed``, ``payment_reversal_reason`` records the
    operator's free-text + picklist code, and an ``AuditLog`` row
    captures the ``payment_status`` diff per donation.
    """
    client, _ = staff_client_pair
    client_obj = ClientFactory()
    batch, donations = _build_eight_donor_slice(client_obj)

    _approve_batch_via_qa_view(client, batch)

    cheque_donations = [
        d for d in donations if d.payment_method == Donation.PAYMENT_METHOD_CHEQUE
    ]
    other_donations = [
        d for d in donations if d.payment_method != Donation.PAYMENT_METHOD_CHEQUE
    ]

    slip = PayingInSlipFactory(
        client=client_obj,
        payment_type="cheque",
        status="ready",
    )
    _link_donations_to_slip(cheque_donations, slip)
    BankingService.recalculate_slip_totals(slip)

    payload = _post_record_processing(
        client,
        slip,
        completion_status="issues",
        processed_amount="0.00",
        processing_issues=["invalid_cheque"],
        custom_issue="All cheques bounced — drawer's bank refused payment",
    )

    assert payload["success"] is True
    assert payload["reversed_donation_count"] == len(cheque_donations)

    for donation in cheque_donations:
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_REVERSED
        assert "invalid_cheque" in donation.payment_reversal_reason
        assert "drawer" in donation.payment_reversal_reason

    # Donations on other slip types are completely untouched.
    for donation in other_donations:
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING
        assert donation.payment_reversal_reason == ""

    reversal_logs = AuditLog.objects.filter(
        model_name="Donation",
        action="UPDATE",
        object_id__in=[str(d.pk) for d in cheque_donations],
    )
    seen_with_payment_status_diff = {
        log.object_id
        for log in reversal_logs
        if "payment_status" in (log.changes or {})
    }
    assert seen_with_payment_status_diff == {str(d.pk) for d in cheque_donations}


# ═══════════════════════════════════════════════════════════════
# FIN-E2E-BANKING-003: cash partial-success reverses every settled cash donation
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
def test_cash_partial_success_lifecycle_reverses_all_settled_cash_donations(
    staff_client_pair: tuple[Client, User],
) -> None:
    """FIN-E2E-BANKING-003: ``partial_success`` reverses every settled row on the slip.

    Banking can't cherry-pick which specific cash donations made up the
    shortfall, so the safe default is to reverse all of them and let
    operators re-bank the verified subset (matches the
    ``test_partial_success_completion_status_reverses_donations``
    contract in ``test_banking_service.py``).
    """
    client, _ = staff_client_pair
    client_obj = ClientFactory()
    batch, donations = _build_eight_donor_slice(client_obj)

    _approve_batch_via_qa_view(client, batch)

    cash_donations = [
        d for d in donations if d.payment_method == Donation.PAYMENT_METHOD_CASH
    ]
    slip = PayingInSlipFactory(
        client=client_obj,
        payment_type="cash",
        status="ready",
    )
    _link_donations_to_slip(cash_donations, slip)
    BankingService.recalculate_slip_totals(slip)
    # Bank credited half the cash; partial_success captures the discrepancy.
    half_total = (slip.total_amount / Decimal("2")).quantize(Decimal("0.01"))

    payload = _post_record_processing(
        client,
        slip,
        completion_status="partial_success",
        processed_amount=str(half_total),
        processing_issues=["incorrect_amount"],
        custom_issue="Cash count short on deposit",
    )

    assert payload["success"] is True
    assert payload["reversed_donation_count"] == len(cash_donations)

    for donation in cash_donations:
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_REVERSED
        assert "incorrect_amount" in donation.payment_reversal_reason
        assert "short" in donation.payment_reversal_reason

    slip.refresh_from_db()
    assert slip.completion_status == "partial_success"
    assert slip.status == "partially_processed"


# ═══════════════════════════════════════════════════════════════
# FIN-E2E-BANKING-004: caf + postal_order failures both reverse linked donations
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("payment_method", "completion_status", "issue_code"),
    [
        ("caf", "issues", "expired_voucher"),
        ("postal_order", "failed", "invalid_cheque"),
    ],
)
@pytest.mark.django_db()
def test_caf_and_postal_order_failures_reverse_linked_donations(
    staff_client_pair: tuple[Client, User],
    payment_method: str,
    completion_status: str,
    issue_code: str,
) -> None:
    """FIN-E2E-BANKING-004: every slip-based payment type gets its reversal.

    CAF voucher expiring or postal order being rejected must trigger the
    same reversal path as cheque bounce — the slip's payment type is
    irrelevant to the contract, only the ``completion_status`` is.
    """
    client, _ = staff_client_pair
    client_obj = ClientFactory()
    batch, donations = _build_eight_donor_slice(client_obj)

    _approve_batch_via_qa_view(client, batch)

    target_donations = [d for d in donations if d.payment_method == payment_method]
    slip = PayingInSlipFactory(
        client=client_obj,
        payment_type=payment_method,
        status="ready",
    )
    _link_donations_to_slip(target_donations, slip)
    BankingService.recalculate_slip_totals(slip)

    payload = _post_record_processing(
        client,
        slip,
        completion_status=completion_status,
        processed_amount="0.00",
        processing_issues=[issue_code],
        custom_issue=f"{payment_method} rejected by bank",
    )

    assert payload["success"] is True
    assert payload["reversed_donation_count"] == len(target_donations)

    for donation in target_donations:
        donation.refresh_from_db()
        assert donation.payment_status == Donation.PAYMENT_STATUS_REVERSED
        assert issue_code in donation.payment_reversal_reason

    slip.refresh_from_db()
    expected_slip_status = (
        "failed" if completion_status == "failed" else "partially_processed"
    )
    assert slip.status == expected_slip_status
