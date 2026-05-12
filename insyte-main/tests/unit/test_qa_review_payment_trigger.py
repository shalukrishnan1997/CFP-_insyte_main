"""Unit tests for QA approval payment trigger behavior."""

from datetime import date

import pytest
from django.test import RequestFactory
from django.utils import timezone

from campaigns.models import CampaignDataFile, CampaignField
from custom_admin.views import qa_review
from donations.models import Donation
from donors.models import DataFileDonor
from tests.factories import (
    DonationFactory,
    PaymentGatewayConfigFactory,
    ScanPlaceholderFactory,
    UserFactory,
)


def _noop_message(_request: object, _message: object) -> None:
    """Ignore Django flash message calls during unit tests."""


def _required_qa_post_fields() -> dict[str, str]:
    """Return minimum required QA form fields for action submissions."""
    return {
        "donor_title": "Mr",
        "donor_first_name": "John",
        "donor_last_name": "Doe",
        "package_code": "PKG01",
        "donor_address_line1": "10 High Street",
        "donor_postcode": "SW1A 1AA",
    }


@pytest.mark.django_db()
class TestQaReviewPaymentTrigger:
    """Verify donation payment queueing from donation-level QA actions."""

    def test_approve_card_pending_processes_payment_synchronously(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Process payment synchronously when approving a card donation."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="card",
            payment_status="pending",
        )

        captured: dict[str, object] = {}

        def fake_process(
            target_donation: Donation,
            user: object,
            payment_method_id: str | None = None,
            require_qa_approved: bool = True,
        ) -> dict[str, object]:
            captured["donation_id"] = str(target_donation.id)
            captured["user_id"] = str(getattr(user, "id", ""))
            captured["payment_method_id"] = payment_method_id
            captured["require_qa_approved"] = require_qa_approved
            donation.payment_status = "completed"
            donation.save(update_fields=["payment_status"])
            return {"success": True, "payment_id": "pay-123"}

        monkeypatch.setattr(
            "payments.batch_payment.BatchPaymentService.process_donation_payment",
            fake_process,
        )
        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {
                "action": "approve",
                "stripe_payment_method_id": "pm_test_card_123",
                **_required_qa_post_fields(),
            },
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request,
            donation.batch,
            donation,
            next_id=None,
        )

        donation.refresh_from_db()

        assert response.status_code == 302
        assert donation.qa_status == donation.QA_STATUS_APPROVED
        assert donation.payment_status == "completed"
        assert captured == {
            "donation_id": str(donation.id),
            "user_id": str(staff_user.id),
            "payment_method_id": "pm_test_card_123",
            "require_qa_approved": False,
        }

    def test_approve_card_pending_does_not_approve_when_charge_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Leave donation unapproved when the synchronous charge fails."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="card",
            payment_status="pending",
        )

        def fake_process(
            _target_donation: Donation,
            _user: object,
            payment_method_id: str | None = None,
            require_qa_approved: bool = True,
        ) -> dict[str, object]:
            return {"success": False, "error": "Card declined"}

        monkeypatch.setattr(
            "payments.batch_payment.BatchPaymentService.process_donation_payment",
            fake_process,
        )
        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {
                "action": "approve",
                "stripe_payment_method_id": "pm_test_card_123",
                **_required_qa_post_fields(),
            },
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request,
            donation.batch,
            donation,
            next_id=None,
        )

        donation.refresh_from_db()

        assert response.status_code == 302
        assert donation.qa_status == donation.QA_STATUS_PENDING

    def test_approve_non_card_does_not_queue_payment_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Do not queue payment when approved donation is not card based."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="pending",
        )

        queued: dict[str, bool] = {"called": False}

        def fake_process(
            _target_donation: Donation,
            _user: object,
            payment_method_id: str | None = None,
            require_qa_approved: bool = True,
        ) -> dict[str, object]:
            queued["called"] = True
            return {"success": True}

        monkeypatch.setattr(
            "payments.batch_payment.BatchPaymentService.process_donation_payment",
            fake_process,
        )
        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", **_required_qa_post_fields()},
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request,
            donation.batch,
            donation,
            next_id=None,
        )

        donation.refresh_from_db()

        assert response.status_code == 302
        assert donation.qa_status == donation.QA_STATUS_APPROVED
        assert queued["called"] is False

    def test_approve_without_notes_clears_legacy_ocr_note(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Clear old OCR-generated note text on approval when QA leaves notes blank."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(
            payment_method="cheque",
            payment_status="pending",
            qa_notes=(
                "Auto-created from OCR scan processing (Warm record). "
                "OCR confidence: 61%. URN: URN100010."
            ),
        )

        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", "qa_notes": "", **_required_qa_post_fields()},
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request,
            donation.batch,
            donation,
            next_id=None,
        )

        donation.refresh_from_db()
        assert response.status_code == 302
        assert donation.qa_status == donation.QA_STATUS_APPROVED
        assert donation.qa_notes == ""

    def test_approve_requires_mandatory_qa_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Block review actions when mandatory QA fields are missing."""
        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(payment_method="cheque", payment_status="pending")

        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", "donor_first_name": "OnlyFirstName"},
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request,
            donation.batch,
            donation,
            next_id=None,
        )

        donation.refresh_from_db()
        assert response.status_code == 302
        assert donation.qa_status == donation.QA_STATUS_PENDING

    def test_approve_requires_completed_qa_redaction(
        self,
        monkeypatch: pytest.MonkeyPatch,
        redaction_required_all: object,
    ) -> None:
        """Approvals must stop until the QA reviewer saves a redacted scan copy."""
        del redaction_required_all

        staff_user = UserFactory(is_staff=True, is_superuser=True)
        donation = DonationFactory(payment_method="cheque", payment_status="pending")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status="pending",
            image_url="",
            image_path="ScanOutput/demo/qa_redaction_pending.png",
            page_keys=["ScanOutput/demo/qa_redaction_pending.png"],
        )

        monkeypatch.setattr(qa_review.messages, "success", _noop_message)
        monkeypatch.setattr(qa_review.messages, "info", _noop_message)
        monkeypatch.setattr(qa_review.messages, "error", _noop_message)

        request = RequestFactory().post(
            "/admin/qa/",
            {"action": "approve", **_required_qa_post_fields()},
        )
        request.user = staff_user
        qa_review._claim_reviewer_lock(donation.batch.id, staff_user)  # pyright: ignore[reportPrivateUsage]

        response = qa_review._handle_qa_action(  # pyright: ignore[reportPrivateUsage]
            request,
            donation.batch,
            donation,
            next_id=None,
        )

        donation.refresh_from_db()
        assert response.status_code == 302
        assert donation.qa_status == donation.QA_STATUS_PENDING

    def test_build_review_context_exposes_pending_redaction_state(
        self,
        redaction_required_all: object,
    ) -> None:
        """QA context should surface the scan redaction state and save endpoint."""
        del redaction_required_all
        donation = DonationFactory(payment_method="cheque")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status="pending",
            redaction_notes="Mask payment details",
        )

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["scan_redaction_required"] is True
        assert context["scan_redaction_pending"] is True
        assert context["scan_redaction_status"] == "pending"
        assert context["scan_redaction_notes"] == "Mask payment details"
        assert str(placeholder.id) == context["scan_placeholder_id"]
        assert "save-redaction" in str(context["scan_redaction_save_url"])

    def test_build_review_context_includes_publishable_key_for_card_payments(
        self,
    ) -> None:
        """Expose Stripe tokenization context for card donations awaiting payment."""
        donation = DonationFactory(payment_method="card", payment_status="pending")
        PaymentGatewayConfigFactory(
            client=donation.campaign.client,
            provider="stripe",
            is_active=True,
            publishable_key_encrypted="pk_test_qa_review",
        )

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["requires_card_tokenization"] is True
        assert context["stripe_publishable_key"] == "pk_test_qa_review"
        assert context["stripe_gateway_ready"] is True

    def test_build_review_context_logs_reason_when_stripe_unconfigured(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Locks the contract: when QA review can't resolve a publishable
        # key for a card donation, the actual StripePaymentError reason
        # is logged (donation id + client id + cause) instead of the
        # silent failure that previously left the UI banner unexplained.
        import logging

        # Test settings set ``disable_existing_loggers=True`` which flips
        # ``logger.disabled`` on every module that imported ``logging``
        # before Django's LOGGING config was applied. Re-enable so caplog
        # captures records in the full-suite test order.
        logging.getLogger("custom_admin.views.qa_review").disabled = False

        donation = DonationFactory(payment_method="card", payment_status="pending")

        with caplog.at_level("WARNING", logger="custom_admin.views.qa_review"):
            context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
                donation.batch,
                donation,
                Donation.objects.filter(pk=donation.pk),
                [str(donation.id)],
                {
                    "prev_id": None,
                    "next_id": None,
                    "prev_url": None,
                    "next_url": None,
                    "last_url": None,
                    "position": 1,
                    "total": 1,
                },
                position=0,
            )

        assert context["stripe_publishable_key"] == ""
        warnings = [
            r
            for r in caplog.records
            if r.name == "custom_admin.views.qa_review"
            and r.levelname == "WARNING"
            and "Stripe publishable key unavailable" in r.getMessage()
        ]
        assert warnings, "expected a StripePaymentError-cause warning to be logged"
        msg = warnings[0].getMessage()
        assert str(donation.id) in msg
        assert "Stripe is not configured or inactive" in msg

    def test_build_review_context_donation_date_initial_uses_stored_date(
        self,
    ) -> None:
        """Persisted donation_date is preserved so re-saves don't overwrite it."""
        stored = date(2024, 6, 15)
        donation = DonationFactory(donation_date=stored)

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["donation_date_initial"] == stored

    def test_build_review_context_donation_date_initial_defaults_to_today(
        self,
    ) -> None:
        """Missing donation_date falls back to today so reviewers don't get an empty input."""
        donation = DonationFactory(donation_date=None)

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["donation_date_initial"] == timezone.localdate()


@pytest.mark.django_db()
class TestQaReviewContextCustomFields:
    """Verify QA context only exposes campaign-defined custom fields."""

    def test_build_review_context_excludes_ocr_metadata_from_custom_fields(
        self,
    ) -> None:
        """Show real custom fields while keeping OCR metadata out of the panel."""
        donation = DonationFactory()
        custom_field = CampaignField.objects.create(
            campaign=donation.campaign,
            label="Reference code",
            field_type=CampaignField.FIELD_TEXT,
            order=1,
        )
        donation.field_data = {
            str(custom_field.id): "ABC-123",
            "ocr_extracted": True,
            "ocr_confidence": 0.6592,
            "scan_placeholder_id": "904ebd25-1519-48f7-bff9-df830ffc03f0",
            "record_type": "cold",
            "scan_purpose": "donation",
            "confidence": {"amount": 0.52},
        }
        donation.save(update_fields=["field_data"])

        donations_qs = Donation.objects.filter(pk=donation.pk)
        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            donations_qs,
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["field_data_items"] == [("Reference code", "ABC-123")]
        assert context["is_ocr_extracted"] is True
        assert context["record_type"] == "cold"
        assert context["identifier_source"] == ""
        assert context["donor_match_status"] == ""
        assert context["exception_reason"] == ""
        assert context["confidence_scores"] == {"amount": 0.52}

    def test_build_review_context_prefers_explicit_scan_review_metadata(self) -> None:
        """Expose explicit scan review metadata without match-status shortcuts."""
        donation = DonationFactory()
        donation.field_data = {
            "ocr_extracted": True,
            "campaign_temperature": "warm",
            "identifier_source": "qr",
            "donor_match_status": "manual_review",
            "exception_reason": "warm_source_miss_rescan_under_cold_campaign",
            "confidence": {"urn": 1.0},
        }
        donation.save(update_fields=["field_data"])

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["record_type"] == "warm"
        assert context["identifier_source"] == "qr"
        assert context["donor_match_status"] == "manual_review"
        assert (
            context["exception_reason"] == "warm_source_miss_rescan_under_cold_campaign"
        )

    def test_build_review_context_hides_legacy_ocr_generated_notes(self) -> None:
        """Do not prefill QA notes textarea with legacy OCR-generated note text."""
        donation = DonationFactory(
            qa_notes=(
                "Auto-created from OCR scan processing (Warm record). "
                "OCR confidence: 61%. URN: URN100010."
            )
        )

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["qa_notes_prefill"] == ""

    def test_build_review_context_hides_donor_confidence_for_matched_house_file(
        self,
    ) -> None:
        """Hide donor badges when values come from a matched source donor."""
        donation = DonationFactory()
        donation.field_data = {
            "ocr_extracted": True,
            "donor_match_status": "matched",
            "confidence": {
                "donor_name": 0.51,
                "email": 0.64,
                "address_line1": 0.74,
                "amount": 0.83,
                "gift_aid": 0.92,
            },
        }
        donation.save(update_fields=["field_data"])

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["confidence_scores"] == {"amount": 0.83, "gift_aid": 0.92}

    def test_build_review_context_hides_donor_confidence_for_matched_data_file(
        self,
    ) -> None:
        """Hide donor badges when active donor values come from data file."""
        user = UserFactory()
        donation = DonationFactory(donor=None, data_file_donor=None)
        data_file = CampaignDataFile.objects.create(
            campaign=donation.campaign,
            created_by=user,
        )
        data_file_donor = DataFileDonor.objects.create(
            data_file=data_file,
            client=donation.campaign.client,
            urn="DF-1001",
            first_name="Data",
            last_name="Donor",
        )
        donation.data_file_donor = data_file_donor
        donation.field_data = {
            "ocr_extracted": True,
            "donor_match_status": "matched",
            "confidence": {"phone": 0.61, "postcode": 0.58, "amount": 0.77},
        }
        donation.save(update_fields=["data_file_donor", "field_data"])

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["confidence_scores"] == {"amount": 0.77}

    def test_build_review_context_keeps_donor_confidence_for_ocr_new_donor(
        self,
    ) -> None:
        """Keep donor badges when donor was created from OCR (not matched source)."""
        donation = DonationFactory()
        donation.field_data = {
            "ocr_extracted": True,
            "donor_match_status": "new_donor_created",
            "confidence": {"donor_name": 0.52, "phone": 0.66, "amount": 0.79},
        }
        donation.save(update_fields=["field_data"])

        context = qa_review._build_review_context(  # pyright: ignore[reportPrivateUsage]
            donation.batch,
            donation,
            Donation.objects.filter(pk=donation.pk),
            [str(donation.id)],
            {
                "prev_id": None,
                "next_id": None,
                "prev_url": None,
                "next_url": None,
                "last_url": None,
                "position": 1,
                "total": 1,
            },
            position=0,
        )

        assert context["confidence_scores"] == {
            "donor_name": 0.52,
            "phone": 0.66,
            "amount": 0.79,
        }
