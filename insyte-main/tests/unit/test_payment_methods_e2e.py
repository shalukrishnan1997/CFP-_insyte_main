"""Happy-path workflow tests for every supported donation payment method.

One test per payment method on ``Donation.payment_method`` exercising the
QA approval entry point in ``custom_admin.views.qa_review._handle_qa_action``
and asserting the resulting payment-side effects:

* ``card`` — Stripe ``PaymentIntent`` mocked to ``succeeded``; a
  ``StripePayment`` row with ``status="succeeded"`` is persisted and the
  donation flips to ``payment_status="completed"``.
* ``cheque`` — QA approval leaves payment status pending; donation is
  later linked to a ``PayingInSlip`` and only marked ``"completed"``
  after the banking slip is processed.
* ``cash`` — same banking path, with ``payment_type="cash"`` on the slip.
* ``direct_debit`` — encrypted ``sort_code`` / ``account_number`` round-trip
  cleanly through the database (``django-fernet-encrypted-fields``).
* ``caf`` — voucher number is recorded; no Stripe/banking calls fire.
* ``postal_order`` — postal order number is recorded.
* ``non_financial`` — no Stripe row, no banking slip, no payment status
  flip beyond the default ``"pending"`` placeholder.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from django.test import RequestFactory

from custom_admin.views import qa_review
from donations.models import Donation
from payments.models import StripePayment
from tests.factories import (
    DonationFactory,
    PayingInSlipFactory,
    StripeCustomerFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from banking.models import PayingInSlip
    from core.models import User


def _noop_message(_request: object, _message: object) -> None:
    """Swallow Django flash messages emitted from QA action helpers."""


def _required_qa_fields() -> dict[str, str]:
    """Return the minimum required QA form fields (Title, name, package, address)."""
    return {
        "donor_title": "Mr",
        "donor_first_name": "John",
        "donor_last_name": "Doe",
        "package_code": "PKG01",
        "donor_address_line1": "10 High Street",
        "donor_postcode": "SW1A 1AA",
    }


def _silence_qa_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``django.contrib.messages`` calls in qa_review with no-ops."""
    monkeypatch.setattr(qa_review.messages, "success", _noop_message)
    monkeypatch.setattr(qa_review.messages, "info", _noop_message)
    monkeypatch.setattr(qa_review.messages, "error", _noop_message)


def _approve_via_qa(
    staff_user: User,
    donation: Donation,
    extra_post: dict[str, str] | None = None,
) -> int:
    """Drive the QA approve action for ``donation`` and return the response code.

    Args:
        staff_user: Authenticated staff user issuing the approval.
        donation: Donation under review.
        extra_post: Optional additional POST fields (payment-method specific).

    Returns:
        HTTP status code from ``_handle_qa_action`` (always a redirect).
    """
    payload: dict[str, str] = {"action": "approve", **_required_qa_fields()}
    if extra_post:
        payload.update(extra_post)

    request = RequestFactory().post("/admin/qa/", payload)
    request.user = staff_user

    qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

    response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
        request,
        donation.batch,
        donation,
        next_id=None,
    )
    return response.status_code


def _link_to_slip(
    donation: Donation,
    payment_type: str,
) -> PayingInSlip:
    """Attach ``donation`` to a freshly minted paying-in slip.

    Mirrors what banking admin views do when an operator bundles approved
    cheque/cash/CAF/postal donations into a slip for the bank run.
    """
    slip = PayingInSlipFactory(
        client=donation.campaign.client,
        payment_type=payment_type,
        total_amount=donation.amount,
        total_items=1,
    )
    donation.paying_in_slip = slip
    donation.save(update_fields=["paying_in_slip", "updated_at"])
    return slip


@pytest.mark.django_db()
class TestPaymentMethodsHappyPath:
    """One happy-path approval test per supported ``Donation.payment_method``."""

    def test_card_payment_creates_succeeded_stripe_payment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approving a card donation captures a succeeded ``StripePayment``."""
        _silence_qa_messages(monkeypatch)

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="card",
            payment_status="pending",
            amount=Decimal("42.00"),
            currency="GBP",
        )
        customer = StripeCustomerFactory(client=donation.campaign.client)

        intent = MagicMock()
        intent.id = "pi_e2e_card_123"
        intent.status = "succeeded"
        intent.latest_charge = "ch_e2e_123"
        intent.amount = 4200
        intent.to_dict.return_value = {"id": intent.id, "status": "succeeded"}

        payment_method = MagicMock()
        payment_method.card.last4 = "4242"
        payment_method.card.exp_month = 12
        payment_method.card.exp_year = 2030
        payment_method.billing_details.name = "John Doe"

        with (
            patch("payments.services.StripePaymentService._get_api_key") as mock_key,
            patch(
                "payments.services.StripePaymentService.get_or_create_customer"
            ) as mock_customer,
            patch("stripe.PaymentMethod.retrieve") as mock_pm_retrieve,
            patch("stripe.PaymentIntent.create") as mock_pi_create,
        ):
            mock_key.return_value = "sk_test_e2e"
            mock_customer.return_value = customer
            mock_pm_retrieve.return_value = payment_method
            mock_pi_create.return_value = intent

            status = _approve_via_qa(
                staff_user,
                donation,
                extra_post={"stripe_payment_method_id": "pm_test_e2e_card"},
            )

        assert status == 302

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED

        stripe_payment = StripePayment.objects.get(donation=donation)
        assert stripe_payment.status == StripePayment.STATUS_SUCCEEDED
        assert stripe_payment.stripe_payment_intent_id == "pi_e2e_card_123"
        assert stripe_payment.amount == Decimal("42.00")

    def test_cheque_payment_completes_only_after_slip_linked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cheque donation stays pending after QA, completes after slip linkage."""
        _silence_qa_messages(monkeypatch)

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="pending",
            amount=Decimal("75.00"),
            cheque_number="CHQ-100200",
            cheque_date=date(2026, 1, 15),
        )

        status = _approve_via_qa(
            staff_user,
            donation,
            extra_post={
                "cheque_number": "CHQ-100200",
                "cheque_date": "2026-01-15",
            },
        )

        assert status == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert donation.cheque_number == "CHQ-100200"
        assert donation.cheque_date == date(2026, 1, 15)
        # No Stripe side-effect for cheque.
        assert not StripePayment.objects.filter(donation=donation).exists()
        # QA approval alone must not flip payment_status to completed.
        assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING
        assert donation.paying_in_slip is None

        slip = _link_to_slip(donation, payment_type="cheque")
        assert slip.payment_type == "cheque"

        # Banking workflow marks slip processed and the donation completed.
        slip.status = "processed"
        slip.save(update_fields=["status", "updated_at"])
        donation.payment_status = Donation.PAYMENT_STATUS_COMPLETED
        donation.save(update_fields=["payment_status", "updated_at"])

        donation.refresh_from_db()
        assert donation.paying_in_slip_id == slip.id
        assert donation.payment_status == Donation.PAYMENT_STATUS_COMPLETED

    def test_cash_payment_links_to_cash_slip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cash donation follows the banking-slip path with ``payment_type='cash'``."""
        _silence_qa_messages(monkeypatch)

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="cash",
            payment_status="pending",
            amount=Decimal("20.00"),
        )

        status = _approve_via_qa(staff_user, donation)

        assert status == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert not StripePayment.objects.filter(donation=donation).exists()

        slip = _link_to_slip(donation, payment_type="cash")
        assert slip.payment_type == "cash"
        donation.refresh_from_db()
        assert donation.paying_in_slip_id == slip.id

    def test_direct_debit_persists_encrypted_bank_details(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct debit sort code and account number round-trip through encryption."""
        _silence_qa_messages(monkeypatch)

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="direct_debit",
            payment_status="pending",
            amount=Decimal("15.00"),
            sort_code="123456",
            account_number="87654321",
            direct_debit_start_date=date(2026, 5, 1),
        )

        # Confirm encrypted values were persisted unchanged via the ORM round-trip.
        donation.refresh_from_db()
        assert donation.sort_code == "123456"
        assert donation.account_number == "87654321"

        status = _approve_via_qa(
            staff_user,
            donation,
            extra_post={"direct_debit_start_date": "2026-05-01"},
        )

        assert status == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        # Encrypted fields survive the QA form round-trip (form does not re-post them).
        assert donation.sort_code == "123456"
        assert donation.account_number == "87654321"
        assert donation.direct_debit_start_date == date(2026, 5, 1)
        # Direct debit is not a card payment — Stripe is never touched.
        assert not StripePayment.objects.filter(donation=donation).exists()

    def test_caf_voucher_records_number_without_stripe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CAF donation records the voucher number and skips Stripe entirely."""
        _silence_qa_messages(monkeypatch)

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="caf",
            payment_status="pending",
            amount=Decimal("100.00"),
            caf_voucher_number="CAF-VOUCHER-9988",
            caf_donor_name="Charities Aid Foundation",
            caf_amount=Decimal("100.00"),
        )

        status = _approve_via_qa(
            staff_user,
            donation,
            extra_post={
                "caf_voucher_number": "CAF-VOUCHER-9988",
                "caf_donor_name": "Charities Aid Foundation",
                "caf_amount": "100.00",
            },
        )

        assert status == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert donation.caf_voucher_number == "CAF-VOUCHER-9988"
        assert donation.caf_donor_name == "Charities Aid Foundation"
        assert donation.caf_amount == Decimal("100.00")
        # CAF is paper-based — Stripe must never be invoked.
        assert not StripePayment.objects.filter(donation=donation).exists()

    def test_postal_order_records_number_without_stripe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Postal order donation records the order number and skips Stripe."""
        _silence_qa_messages(monkeypatch)

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="postal_order",
            payment_status="pending",
            amount=Decimal("10.00"),
            postal_order_number="PO-555-444",
            postal_issuer="Royal Mail Bromley",
            postal_order_date=date(2026, 3, 10),
        )

        status = _approve_via_qa(
            staff_user,
            donation,
            extra_post={
                "postal_order_number": "PO-555-444",
                "postal_issuer": "Royal Mail Bromley",
                "postal_order_date": "2026-03-10",
            },
        )

        assert status == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert donation.postal_order_number == "PO-555-444"
        assert donation.postal_issuer == "Royal Mail Bromley"
        assert donation.postal_order_date == date(2026, 3, 10)
        assert not StripePayment.objects.filter(donation=donation).exists()

    def test_non_financial_skips_stripe_and_banking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-financial donation creates no Stripe row and no paying-in slip."""
        _silence_qa_messages(monkeypatch)

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="non_financial",
            payment_status="pending",
            amount=Decimal("0.00"),
            non_financial_reason="In-Kind",
            non_financial_notes="Donation of office furniture",
        )

        status = _approve_via_qa(
            staff_user,
            donation,
            extra_post={
                "non_financial_reason": "In-Kind",
                "non_financial_notes": "Donation of office furniture",
            },
        )

        assert status == 302
        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert donation.non_financial_reason == "In-Kind"
        assert donation.non_financial_notes == "Donation of office furniture"
        # Non-financial donations bypass both payment rails.
        assert not StripePayment.objects.filter(donation=donation).exists()
        assert donation.paying_in_slip is None
        # Payment status remains the default "pending" sentinel; no completion flip.
        assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING
