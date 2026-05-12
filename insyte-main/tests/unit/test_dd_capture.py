"""Unit tests for Direct Debit capture-only intake (Phase 3)."""

import json
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from donations.intake import (
    PHONE_INTAKE_DD_MANDATE_CONSENT_VERSION,
    create_phone_donation,
    create_phone_intake_batch,
)
from donations.models import Donation
from tests.factories import (
    CampaignFactory,
    SystemDonorFactory,
    UserFactory,
)


def _dd_payload(**overrides):
    base = {
        "amount": Decimal("20.00"),
        "currency": "GBP",
        "payment_method": Donation.PAYMENT_METHOD_DIRECT_DEBIT,
        "donation_date": date(2026, 5, 2),
        "donation_frequency": "monthly",
        "donor_source": "house_file",
        "card_holder_name": "Jane Doe",
        "sort_code": "60-83-71",
        "account_number": "12345678",
        "direct_debit_start_date": date(2026, 6, 1),
        "dd_mandate_consent": {"verbatim_read_aloud": True},
    }
    base.update(overrides)
    return base


@pytest.mark.django_db()
class TestDirectDebitIntake:
    """Service-layer tests for DD intake."""

    def _make(self):
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)
        return operator, campaign, donor, batch

    def test_happy_path_records_consent_and_normalised_bank_details(self) -> None:
        operator, campaign, donor, batch = self._make()

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_dd_payload(),
        )
        donation.refresh_from_db()

        # Sort code is digit-only; account number is left-padded to 8.
        assert donation.sort_code == "608371"
        assert donation.account_number == "12345678"
        assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING

        # field_data carries the consent block.
        consent = donation.field_data["dd_mandate_consent"]
        assert (
            consent["consent_text_version"] == PHONE_INTAKE_DD_MANDATE_CONSENT_VERSION
        )
        assert len(consent["consent_text_rendered_hash"]) == 64  # SHA-256 hex
        assert consent["verbatim_read_aloud"] is True
        assert consent["operator_username"] == operator.username

        # BACS validation ran (warning since modulus check not yet shipped).
        assert (
            donation.field_data["bacs_validation"]["reason"]
            == "format_only_check_passed"
        )
        assert donation.field_data["bacs_validation"]["overridden"] is False

    def test_dd_lands_at_flagged_until_modulus_check_ships(self) -> None:
        """Pre-modulus-table behaviour: every DD donation gets flagged."""
        operator, campaign, donor, batch = self._make()
        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_dd_payload(),
        )
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED

    def test_short_account_number_left_padded(self) -> None:
        operator, campaign, donor, batch = self._make()
        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_dd_payload(account_number="123456"),
        )
        donation.refresh_from_db()
        assert donation.account_number == "00123456"

    def test_invalid_sort_code_raises_validation_error(self) -> None:
        operator, campaign, donor, batch = self._make()
        with pytest.raises(ValidationError):
            create_phone_donation(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=donor,
                payload=_dd_payload(sort_code="123"),
            )

    def test_invalid_format_with_override_saves_flagged(self) -> None:
        """Operator can force-save a non-standard account; flagged for QA."""
        operator, campaign, donor, batch = self._make()
        # Override flag bypasses format-error rejection; it does NOT
        # bypass mandate consent.
        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_dd_payload(
                sort_code="123",  # invalid: only 3 digits
                bacs_validation_overridden=True,
            ),
        )
        assert donation.qa_status == Donation.QA_STATUS_FLAGGED
        assert donation.field_data["bacs_validation"]["overridden"] is True

    def test_missing_consent_raises(self) -> None:
        operator, campaign, donor, batch = self._make()
        with pytest.raises(ValidationError):
            create_phone_donation(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=donor,
                payload=_dd_payload(dd_mandate_consent={}),
            )

    def test_consent_verbatim_false_raises(self) -> None:
        operator, campaign, donor, batch = self._make()
        with pytest.raises(ValidationError):
            create_phone_donation(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=donor,
                payload=_dd_payload(
                    dd_mandate_consent={"verbatim_read_aloud": False},
                ),
            )

    def test_donation_model_declares_bank_fields_audit_exclude(self) -> None:
        """The audit-signal exclusion must list both bank fields.

        Asserting on ``Donation.audit_exclude_fields`` directly is more
        honest than scanning AuditLog rows post-hoc — the audit pre/post
        save signals are already covered in ``tests/unit/test_audit_signals*``;
        the contract this test guards is "the model promises bank fields
        won't land in audit changes".
        """
        assert "sort_code" in Donation.audit_exclude_fields
        assert "account_number" in Donation.audit_exclude_fields


@pytest.mark.django_db()
class TestDirectDebitIntakeViewLayer:
    """End-to-end through the JSON endpoint."""

    def _login(self):
        user = UserFactory(is_staff=True)
        client = Client()
        client.force_login(user)
        return client, user

    def test_post_creates_dd_donation(self) -> None:
        http, _ = self._login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        body = {
            "campaign_id": str(campaign.id),
            "amount": "20.00",
            "currency": "GBP",
            "payment_method": "direct_debit",
            "donation_frequency": "monthly",
            "donation_date": date(2026, 5, 2).isoformat(),
            "system_donor_id": str(donor.id),
            "donor_source": "house_file",
            "sort_code": "60-83-71",
            "account_number": "12345678",
            "direct_debit_start_date": "2026-06-01",
            "card_holder_name": "Jane Doe",
            "dd_mandate_consent": {"verbatim_read_aloud": True},
        }
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 201
        payload = resp.json()
        assert payload["payment_method"] == "direct_debit"
        assert payload["qa_status"] == "flagged"

    def test_post_without_consent_returns_400(self) -> None:
        http, _ = self._login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        body = {
            "campaign_id": str(campaign.id),
            "amount": "20.00",
            "payment_method": "direct_debit",
            "donation_frequency": "monthly",
            "system_donor_id": str(donor.id),
            "sort_code": "60-83-71",
            "account_number": "12345678",
            # No dd_mandate_consent
        }
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "consent" in resp.json()["error"].lower()

    def test_post_with_invalid_sort_code_returns_400(self) -> None:
        http, _ = self._login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        body = {
            "campaign_id": str(campaign.id),
            "amount": "20.00",
            "payment_method": "direct_debit",
            "system_donor_id": str(donor.id),
            "sort_code": "123",
            "account_number": "12345678",
            "dd_mandate_consent": {"verbatim_read_aloud": True},
        }
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400
