"""Per-payment-method gating around the new ``RedactionSettings`` singleton.

Replaces the deployment-wide ``REQUIRE_MANUAL_REDACTION`` env var with a row
in ``scans.RedactionSettings`` so admins can pick which payment methods need
manual redaction before QA approval. These tests pin the gate behaviour:

- card donations require redaction by default → approve blocked while pending
- card + completed redaction → approve allowed
- cheque donations don't require redaction by default → approve allowed even
  while the placeholder's redaction_status is still pending
- toggling the cheque flag flips the gate live
- visibility helper hides originals only when the method requires redaction
- the singleton enforces ``pk=1``
"""

from __future__ import annotations

from typing import cast

import pytest
from django.test import RequestFactory

from custom_admin.views import qa_review
from donations.models import Donation, DonationBatch
from scans.models import RedactionSettings, ScanPlaceholder
from scans.scan_redaction import (
    manual_redaction_required_for,
    placeholder_payment_method,
    scan_urls_hidden_for_user,
)
from tests.factories import (
    DonationBatchFactory,
    DonationFactory,
    ScanPlaceholderFactory,
    UserFactory,
)

_REQUIRED_QA_FIELDS = {
    "donor_title": "Mr",
    "donor_first_name": "Jane",
    "donor_last_name": "Doe",
    "package_code": "PKG01",
    "donor_address_line1": "1 Example Street",
    "donor_postcode": "AB1 2CD",
}


def _required_qa_post_fields() -> dict[str, str]:
    return dict(_REQUIRED_QA_FIELDS)


def _noop_message(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.mark.django_db
class TestRedactionSettingsModel:
    """Singleton + helper sanity checks."""

    def test_singleton_pk_is_forced_to_one(self) -> None:
        first = RedactionSettings.get_settings()
        # Trying to create a second row with pk=2 still ends up pointing at pk=1.
        rogue = RedactionSettings(pk=2, require_for_card=False)
        rogue.save()
        assert rogue.pk == 1
        assert RedactionSettings.objects.count() == 1
        first.refresh_from_db()
        assert first.require_for_card is False

    def test_default_seed_marks_card_and_direct_debit_required(self) -> None:
        """The migration seed should leave card + DD required, others not."""
        obj = RedactionSettings.get_settings()
        assert obj.require_for_card is True
        assert obj.require_for_direct_debit is True
        assert obj.require_for_cheque is False
        assert obj.require_for_cash is False

    def test_required_payment_methods_reflects_flags(self) -> None:
        obj = RedactionSettings.get_settings()
        obj.require_for_cheque = True
        obj.save()
        methods = obj.required_payment_methods()
        assert "card" in methods
        assert "direct_debit" in methods
        assert "cheque" in methods
        assert "cash" not in methods

    def test_unknown_payment_method_is_never_required(self) -> None:
        obj = RedactionSettings.get_settings()
        assert obj.requires_redaction("") is False
        assert obj.requires_redaction("totally_made_up_method") is False


@pytest.mark.django_db
class TestPendingRedactionPlaceholderGate:
    """``_pending_redaction_placeholder`` blocks per payment-method."""

    def test_card_with_pending_redaction_blocks_approval(self) -> None:
        donation = DonationFactory(payment_method="card", payment_status="pending")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )
        ph = qa_review._pending_redaction_placeholder(donation)  # pyright: ignore[reportPrivateUsage]
        assert ph is not None

    def test_card_with_completed_redaction_allows_approval(self) -> None:
        donation = DonationFactory(payment_method="card", payment_status="pending")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
        )
        ph = qa_review._pending_redaction_placeholder(donation)  # pyright: ignore[reportPrivateUsage]
        assert ph is None

    def test_cheque_with_pending_redaction_allows_approval_by_default(self) -> None:
        donation = DonationFactory(payment_method="cheque", payment_status="pending")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )
        ph = qa_review._pending_redaction_placeholder(donation)  # pyright: ignore[reportPrivateUsage]
        assert ph is None

    def test_toggling_cheque_flag_blocks_cheque_approval(self) -> None:
        obj = RedactionSettings.get_settings()
        obj.require_for_cheque = True
        obj.save()
        donation = DonationFactory(payment_method="cheque", payment_status="pending")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )
        ph = qa_review._pending_redaction_placeholder(donation)  # pyright: ignore[reportPrivateUsage]
        assert ph is not None


@pytest.mark.django_db
class TestVisibilityGate:
    """``scan_urls_hidden_for_user`` should respect per-method policy."""

    def test_card_pending_hides_from_non_qa_user(self) -> None:
        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )
        random_user = UserFactory(is_staff=False)
        assert scan_urls_hidden_for_user(placeholder, random_user) is True

    def test_cheque_pending_visible_when_method_not_required(self) -> None:
        donation = DonationFactory(payment_method="cheque")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )
        random_user = UserFactory(is_staff=False)
        assert scan_urls_hidden_for_user(placeholder, random_user) is False

    def test_completed_redaction_always_visible(self) -> None:
        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
        )
        random_user = UserFactory(is_staff=False)
        assert scan_urls_hidden_for_user(placeholder, random_user) is False


@pytest.mark.django_db
class TestPlaceholderPaymentMethodFallback:
    """``placeholder_payment_method`` should resolve from donation or extracted_data."""

    def test_reads_from_linked_donation_when_present(self) -> None:
        donation = DonationFactory(payment_method="direct_debit")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            extracted_data={"payment_method": "card"},  # ignored when donation is set
        )
        assert placeholder_payment_method(placeholder) == "direct_debit"

    def test_falls_back_to_extracted_data_before_capture(self) -> None:
        placeholder = ScanPlaceholderFactory(
            donation=None,
            extracted_data={"payment_method": "cheque"},
        )
        assert placeholder_payment_method(placeholder) == "cheque"

    def test_returns_empty_when_method_unknown(self) -> None:
        placeholder = ScanPlaceholderFactory(
            donation=None,
            extracted_data={},
        )
        assert placeholder_payment_method(placeholder) == ""


@pytest.mark.django_db
class TestHandleQaActionApproveBlocksWhenRedactionRequired:
    """End-to-end guard inside ``_handle_qa_action`` for the approve path."""

    def test_approve_card_with_pending_redaction_returns_redirect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(payment_method="card", payment_status="pending")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )
        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", **_required_qa_post_fields()},
        )
        request.user = staff
        qa_review._claim_reviewer_lock(donation.batch.id, staff)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request,
            donation.batch,
            donation,
            next_id=None,
        )

        donation.refresh_from_db()
        # Redirect to the same donation; status untouched.
        assert response.status_code == 302
        assert donation.qa_status == Donation.QA_STATUS_PENDING

    def test_approve_cheque_with_pending_redaction_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(payment_method="cheque", payment_status="pending")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )
        # Cheque must be in PENDING_QA so the QA flow accepts the action.
        donation.batch.status = DonationBatch.STATUS_PENDING_QA
        donation.batch.save(update_fields=["status", "updated_at"])

        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", **_required_qa_post_fields()},
        )
        request.user = staff
        qa_review._claim_reviewer_lock(donation.batch.id, staff)  # pyright: ignore[reportPrivateUsage]

        qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request,
            donation.batch,
            donation,
            next_id=None,
        )

        donation.refresh_from_db()
        assert donation.qa_status == Donation.QA_STATUS_APPROVED


@pytest.mark.django_db
class TestHelperHelpers:
    """Smoke-test the public ``manual_redaction_required_for`` helper."""

    def test_helper_returns_true_for_card_by_default(self) -> None:
        assert manual_redaction_required_for("card") is True

    def test_helper_returns_false_for_cheque_by_default(self) -> None:
        assert manual_redaction_required_for("cheque") is False

    def test_helper_reflects_admin_change(self) -> None:
        obj = RedactionSettings.get_settings()
        obj.require_for_cheque = True
        obj.save()
        assert manual_redaction_required_for("cheque") is True


# Used by the approve-path test to silence ScanPlaceholder field-validation
# wiring while keeping mypy/pyright happy with the cast.
_ = cast(DonationBatchFactory, DonationBatchFactory)
