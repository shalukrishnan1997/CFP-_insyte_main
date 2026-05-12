"""Tests for the QA reject-reason picklist and donor contact-status updates."""

from __future__ import annotations

import pytest
from django.test import RequestFactory

from custom_admin.forms.qa_forms import DonorContactStatusForm, QAActionForm
from custom_admin.views import qa_review
from donations.models import Donation
from donors.models import Donor
from letters.tasks import get_donor_context
from tests.factories import DonationFactory, UserFactory


def _noop_message(_request: object, _message: object) -> None:
    """Swallow Django flash messages in unit tests."""


def _required_qa_post_fields() -> dict[str, str]:
    """Minimum required QA fields for action submissions."""
    return {
        "donor_title": "Mr",
        "donor_first_name": "John",
        "donor_last_name": "Doe",
        "package_code": "PKG01",
        "donor_address_line1": "10 High Street",
        "donor_postcode": "SW1A 1AA",
    }


def _silence_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa_review.messages, "success", _noop_message)
    monkeypatch.setattr(qa_review.messages, "info", _noop_message)
    monkeypatch.setattr(qa_review.messages, "error", _noop_message)


# ---------------------------------------------------------------------------
# QAActionForm validation
# ---------------------------------------------------------------------------


class TestQAActionFormValidation:
    """Form-level validation for the reject-reason picklist rules."""

    def test_approve_does_not_require_reason(self) -> None:
        form = QAActionForm(data={"action": "approve"})
        assert form.is_valid(), form.errors

    def test_reject_without_reason_is_invalid(self) -> None:
        form = QAActionForm(data={"action": "reject"})
        assert not form.is_valid()
        assert "qa_reject_reason" in form.errors

    def test_reject_with_standard_reason_allows_empty_notes(self) -> None:
        form = QAActionForm(
            data={
                "action": "reject",
                "qa_reject_reason": Donation.QA_REJECT_REASON_ILLEGIBLE,
            }
        )
        assert form.is_valid(), form.errors

    def test_reject_with_other_requires_notes(self) -> None:
        form = QAActionForm(
            data={
                "action": "reject",
                "qa_reject_reason": Donation.QA_REJECT_REASON_OTHER,
            }
        )
        assert not form.is_valid()
        assert "qa_notes" in form.errors

    def test_reject_with_other_and_notes_is_valid(self) -> None:
        form = QAActionForm(
            data={
                "action": "reject",
                "qa_reject_reason": Donation.QA_REJECT_REASON_OTHER,
                "qa_notes": "Duplicate of donation #1234",
            }
        )
        assert form.is_valid(), form.errors


# ---------------------------------------------------------------------------
# _handle_qa_action behaviour
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestHandleQaActionRejectReason:
    """End-to-end persistence of qa_reject_reason via the view handler."""

    def test_reject_without_reason_keeps_status_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(payment_method="cheque", payment_status="pending")
        _silence_messages(monkeypatch)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "reject", **_required_qa_post_fields()},
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request, donation.batch, donation, next_id=None
        )
        donation.refresh_from_db()

        assert response.status_code == 302
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.qa_reject_reason == ""

    def test_reject_other_without_notes_keeps_status_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(payment_method="cheque", payment_status="pending")
        _silence_messages(monkeypatch)

        request = RequestFactory().post(
            "/admin/qa/",
            {
                "action": "reject",
                "qa_reject_reason": Donation.QA_REJECT_REASON_OTHER,
                "qa_notes": "   ",
                **_required_qa_post_fields(),
            },
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request, donation.batch, donation, next_id=None
        )
        donation.refresh_from_db()

        assert response.status_code == 302
        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.qa_reject_reason == ""

    def test_reject_with_standard_reason_saves_without_notes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(payment_method="cheque", payment_status="pending")
        _silence_messages(monkeypatch)

        request = RequestFactory().post(
            "/admin/qa/",
            {
                "action": "reject",
                "qa_reject_reason": Donation.QA_REJECT_REASON_ILLEGIBLE,
                **_required_qa_post_fields(),
            },
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request, donation.batch, donation, next_id=None
        )
        donation.refresh_from_db()

        assert response.status_code == 302
        assert donation.qa_status == Donation.QA_STATUS_REJECTED
        assert donation.qa_reject_reason == Donation.QA_REJECT_REASON_ILLEGIBLE

    def test_approve_clears_previous_reject_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="pending",
            qa_status=Donation.QA_STATUS_REJECTED,
            qa_reject_reason=Donation.QA_REJECT_REASON_ILLEGIBLE,
        )
        _silence_messages(monkeypatch)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", **_required_qa_post_fields()},
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request, donation.batch, donation, next_id=None
        )
        donation.refresh_from_db()

        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert donation.qa_reject_reason == ""


# ---------------------------------------------------------------------------
# Donor contact-status handler
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestHandleDonorStatusUpdate:
    """Verify donor contact_status updates are independent of QA actions."""

    def test_status_change_leaves_qa_status_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="pending",
            qa_status=Donation.QA_STATUS_APPROVED,
        )
        _silence_messages(monkeypatch)

        request = RequestFactory().post(
            "/admin/qa/",
            {
                "save_donor_status": "1",
                "contact_status": Donor.CONTACT_STATUS_DECEASED,
                "contact_status_reason": "Family informed us",
            },
        )
        request.user = staff_user

        response = qa_review._handle_donor_status_update(  # pyright: ignore[reportPrivateUsage]
            request, donation.batch, donation
        )
        donation.refresh_from_db()
        assert donation.donor is not None
        donation.donor.refresh_from_db()

        assert response.status_code == 302
        assert donation.qa_status == Donation.QA_STATUS_APPROVED
        assert donation.donor.contact_status == Donor.CONTACT_STATUS_DECEASED
        assert donation.donor.contact_status_reason == "Family informed us"
        assert donation.donor.contact_status_changed_at is not None
        # System donor is also updated so future campaigns honour the status.
        assert donation.system_donor is not None
        donation.system_donor.refresh_from_db()
        assert donation.system_donor.contact_status == Donor.CONTACT_STATUS_DECEASED

    def test_invalid_status_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(payment_method="cheque", payment_status="pending")
        _silence_messages(monkeypatch)

        request = RequestFactory().post(
            "/admin/qa/",
            {"save_donor_status": "1", "contact_status": "not_a_valid_status"},
        )
        request.user = staff_user

        qa_review._handle_donor_status_update(  # pyright: ignore[reportPrivateUsage]
            request, donation.batch, donation
        )
        assert donation.donor is not None
        donation.donor.refresh_from_db()
        assert donation.donor.contact_status == Donor.CONTACT_STATUS_NORMAL

    def test_form_accepts_valid_status(self) -> None:
        form = DonorContactStatusForm(
            data={
                "contact_status": Donor.CONTACT_STATUS_GONE_AWAY,
                "contact_status_reason": "Returned mail",
            }
        )
        assert form.is_valid(), form.errors


# ---------------------------------------------------------------------------
# Letter context
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestLetterContextRejectReason:
    """Verify reject_reason / reject_notes are in the letter template context."""

    def test_context_includes_reject_reason_display_label(self) -> None:
        donation = DonationFactory(
            qa_status=Donation.QA_STATUS_REJECTED,
            qa_reject_reason=Donation.QA_REJECT_REASON_ILLEGIBLE,
            qa_notes="couldn't read the form",
        )

        ctx = get_donor_context(donation)

        assert ctx["reject_reason"] == "Illegible / cannot read form"
        assert ctx["reject_notes"] == "couldn't read the form"

    def test_context_reject_reason_empty_when_not_rejected(self) -> None:
        donation = DonationFactory(qa_status=Donation.QA_STATUS_APPROVED)

        ctx = get_donor_context(donation)

        assert ctx["reject_reason"] == ""
