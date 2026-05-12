"""HTTP-level tests for the phone-intake views.

These run through the URL → view → service-layer → DB stack so the JSON
parsing, decimal coercion, donor lookup tier dispatch, and 4xx error
paths are all covered end-to-end. The pure service-layer tests live in
``test_phone_intake_donation_creation.py``.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.test import Client
from django.urls import reverse

from donations.models import Donation, DonationBatch
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    SystemDonorFactory,
    UserFactory,
)


def _login() -> tuple[Client, object]:
    user = UserFactory(is_staff=True)
    client = Client()
    client.force_login(user)
    return client, user


@pytest.mark.django_db()
class TestPhoneIntakeHelp:
    """GET /admin/phone-intake/help/ — operator playbook."""

    def test_renders_help_page_for_staff(self) -> None:
        http, _ = _login()
        resp = http.get(reverse("custom_admin:phone_intake_help"))
        assert resp.status_code == 200
        body = resp.content.decode("utf-8")
        # Anchor a couple of section names so a future template rename
        # surfaces here rather than silently breaking the playbook.
        assert "Operator Playbook" in body
        assert "Direct debit" in body
        assert "Card (MOTO)" in body


@pytest.mark.django_db()
class TestPhoneIntakeConsole:
    """GET /admin/phone-intake/."""

    def test_renders_campaign_picker_when_no_campaign_id(self) -> None:
        http, _ = _login()
        resp = http.get(reverse("custom_admin:phone_intake_console"))
        assert resp.status_code == 200
        body = resp.content.decode("utf-8")
        # Two-step picker: client dropdown rendered server-side, campaigns
        # fetched via the JSON API once a client is chosen.
        assert "Pick the campaign this call is for" in body
        assert 'id="clientSelect"' in body
        assert 'id="campaignList"' in body
        assert reverse("custom_admin:campaigns_by_client_api") in body

    def test_picker_only_lists_clients_with_active_campaigns(self) -> None:
        # Operator-facing flow expects the dropdown to surface only clients
        # who currently have at least one active campaign — clients whose
        # campaigns are all draft/closed should be hidden so the operator
        # can't pick them by mistake.
        http, _ = _login()
        with_active = ClientFactory(name="Has Active")
        CampaignFactory(client=with_active, status="active")
        only_draft = ClientFactory(name="Only Draft")
        CampaignFactory(client=only_draft, status="draft")
        ClientFactory(name="No Campaigns")
        resp = http.get(reverse("custom_admin:phone_intake_console"))
        assert resp.status_code == 200
        body = resp.content.decode("utf-8")
        assert "Has Active" in body
        assert "Only Draft" not in body
        assert "No Campaigns" not in body

    def test_renders_console_for_valid_campaign(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        resp = http.get(
            reverse("custom_admin:phone_intake_console") + f"?campaign_id={campaign.id}"
        )
        assert resp.status_code == 200
        assert b"Phone Donation Intake" in resp.content
        assert campaign.name.encode() in resp.content

    def test_logs_reason_when_stripe_unconfigured(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Locks the contract: when StripePaymentService can't resolve a
        # publishable key, the actual reason ends up in the logs so an
        # operator hitting "Stripe is not configured" in the UI can
        # diagnose without reading the source.
        import logging

        # Test settings set ``disable_existing_loggers=True`` which flips
        # ``logger.disabled`` on every module that imported ``logging``
        # before Django's LOGGING config was applied. Order-dependent in
        # the full suite (single-test runs are immune because the module
        # imports after settings load). Re-enable so caplog sees records.
        logging.getLogger("donations.phone_intake_views").disabled = False

        http, _ = _login()
        campaign = CampaignFactory()
        with caplog.at_level("WARNING", logger="donations.phone_intake_views"):
            resp = http.get(
                reverse("custom_admin:phone_intake_console")
                + f"?campaign_id={campaign.id}"
            )
        assert resp.status_code == 200
        warnings = [
            r
            for r in caplog.records
            if r.name == "donations.phone_intake_views"
            and r.levelname == "WARNING"
            and "Stripe publishable key unavailable" in r.getMessage()
        ]
        assert warnings, "expected a StripePaymentError-cause warning to be logged"
        assert "Stripe is not configured or inactive" in warnings[0].getMessage()


@pytest.mark.django_db()
class TestPhoneIntakeCreateDonation:
    """POST /admin/phone-intake/donation/create/."""

    def test_happy_path_creates_donation_in_daily_batch(self) -> None:
        http, _user = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        body = {
            "campaign_id": str(campaign.id),
            "amount": "25.00",
            "currency": "GBP",
            "payment_method": "cheque",
            "donation_date": date.today().isoformat(),
            "gift_aid": True,
            "system_donor_id": str(donor.id),
            "donor_source": "house_file",
            "donor_match_status": "exact",
            "cheque_number": "123456",
            "cheque_date": date(2026, 4, 30).isoformat(),
        }
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 201
        payload = resp.json()
        assert payload["qa_status"] == "pending"
        assert payload["payment_method"] == "cheque"
        assert Decimal(payload["amount"]) == Decimal("25.00")
        assert payload["requires_charge"] is False
        assert payload["todays_batch"]["total_donations"] == 1
        assert Decimal(payload["todays_batch"]["total_amount"]) == Decimal("25.00")

        # And actually persisted:
        donation = Donation.objects.get(pk=payload["donation_id"])
        assert donation.system_donor_id == donor.id
        assert donation.field_data["intake_method"] == "phone"

    def test_running_totals_increase_across_two_donations(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        for amount in ("10.00", "15.00"):
            http.post(
                reverse("custom_admin:phone_intake_create_donation"),
                data=json.dumps(
                    {
                        "campaign_id": str(campaign.id),
                        "amount": amount,
                        "payment_method": "cash",
                        "donation_date": date.today().isoformat(),
                        "system_donor_id": str(donor.id),
                        "donor_source": "house_file",
                    }
                ),
                content_type="application/json",
            )

        # Hit the console; running totals should reflect both donations.
        resp = http.get(
            reverse("custom_admin:phone_intake_console") + f"?campaign_id={campaign.id}"
        )
        assert resp.status_code == 200
        # Right-rail shows £25.00 / 2 donations.
        assert b"25.00" in resp.content
        assert b"2 donations" in resp.content or b"2</span>" in resp.content

    def test_card_payment_flags_requires_charge(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "50.00",
                    "payment_method": "card",
                    "system_donor_id": str(donor.id),
                    "donor_source": "house_file",
                    "card_holder_name": "Jane Doe",
                    "card_last_four": "4242",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        assert resp.json()["requires_charge"] is True

    def test_invalid_json_returns_400(self) -> None:
        http, _ = _login()
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data="{not valid json",
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "Invalid JSON" in resp.json()["error"]

    def test_missing_campaign_id_returns_400(self) -> None:
        http, _ = _login()
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps({"amount": "10.00", "payment_method": "cash"}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_payment_method_returns_400(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "10.00",
                    "payment_method": "bitcoin",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "payment_method" in resp.json()["error"].lower()

    def test_missing_payment_method_returns_400(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps({"campaign_id": str(campaign.id), "amount": "10.00"}),
            content_type="application/json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db()
class TestPhoneIntakeCreateSystemDonor:
    """POST /admin/phone-intake/donor/create/."""

    def test_creates_pending_review_systemdonor(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        resp = http.post(
            reverse("custom_admin:phone_intake_create_system_donor"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "first_name": "Alice",
                    "last_name": "Walker",
                    "phone": "07700 900 123",
                    "postcode": "SW1A 1AA",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        payload = resp.json()
        assert payload["pending_review"] is True
        assert payload["source"] == "system_donor"

    def test_missing_first_or_last_name_returns_400(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        resp = http.post(
            reverse("custom_admin:phone_intake_create_system_donor"),
            data=json.dumps({"campaign_id": str(campaign.id), "first_name": "Alice"}),
            content_type="application/json",
        )
        assert resp.status_code == 400


@pytest.mark.django_db()
class TestPhoneIntakeCharge:
    """POST /admin/phone-intake/charge/."""

    def _make_card_donation(self, http: Client) -> str:
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "50.00",
                    "payment_method": "card",
                    "system_donor_id": str(donor.id),
                    "donor_source": "house_file",
                    "card_holder_name": "Jane Doe",
                    "card_last_four": "4242",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        return resp.json()["donation_id"]

    def test_missing_donation_id_returns_400(self) -> None:
        http, _ = _login()
        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps({"stripe_payment_method_id": "pm_x"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "donation_id" in resp.json()["error"]

    def test_missing_stripe_pm_returns_400(self) -> None:
        http, _ = _login()
        donation_id = self._make_card_donation(http)
        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps({"donation_id": donation_id}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "stripe_payment_method_id" in resp.json()["error"]

    def test_charge_on_non_card_donation_returns_400(self) -> None:
        from donations.models import Donation
        from tests.factories import DonationBatchFactory, DonationFactory

        http, _ = _login()
        batch = DonationBatchFactory()
        donation = DonationFactory(
            campaign=batch.campaign,
            batch=batch,
            payment_method=Donation.PAYMENT_METHOD_CHEQUE,
        )
        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps(
                {
                    "donation_id": str(donation.id),
                    "stripe_payment_method_id": "pm_x",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "card" in resp.json()["error"].lower()

    @patch("payments.batch_payment.BatchPaymentService.process_donation_payment")
    def test_happy_path_calls_process_payment_with_moto_true(
        self, mock_process: MagicMock
    ) -> None:
        http, _ = _login()
        donation_id = self._make_card_donation(http)
        mock_process.return_value = {
            "success": True,
            "requires_capture": True,
            "payment_id": "pmt_x",
            "message": "Card authorised.",
        }

        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps(
                {
                    "donation_id": donation_id,
                    "stripe_payment_method_id": "pm_test_visa",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["success"] is True

        # Verify the moto kwarg was forwarded. capture_immediately is True
        # because the SystemDonor under test is clean (not pending_review)
        # and the card+amount payload is auto-approve-eligible.
        kwargs = mock_process.call_args.kwargs
        assert kwargs["moto"] is True
        assert kwargs["payment_method_id"] == "pm_test_visa"
        assert kwargs["require_qa_approved"] is False
        assert kwargs["capture_immediately"] is True

    @patch("payments.batch_payment.BatchPaymentService.process_donation_payment")
    def test_charge_failure_returns_200_with_success_false(
        self, mock_process: MagicMock
    ) -> None:
        http, _ = _login()
        donation_id = self._make_card_donation(http)
        mock_process.return_value = {"success": False, "error": "Card declined"}

        resp = http.post(
            reverse("custom_admin:phone_intake_charge"),
            data=json.dumps(
                {
                    "donation_id": donation_id,
                    "stripe_payment_method_id": "pm_decline",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["error"] == "Card declined"


@pytest.mark.django_db()
def test_each_call_creates_a_new_batch() -> None:
    """One batch per phone call — two donations land in two distinct batches."""
    http, _ = _login()
    campaign = CampaignFactory()
    donor = SystemDonorFactory(client=campaign.client)

    batch_ids = set()
    for _ in range(2):
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "10.00",
                    "payment_method": "cash",
                    "system_donor_id": str(donor.id),
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        batch_ids.add(resp.json()["batch_id"])

    assert len(batch_ids) == 2
    assert DonationBatch.objects.filter(campaign=campaign).count() == 2


@pytest.mark.django_db()
class TestPhoneIntakeCreateDonationNonFinancial:
    """POST /admin/phone-intake/donation/create/ for non-financial donor updates."""

    def _base_body(self, *, campaign, donor, **overrides) -> dict[str, object]:
        body: dict[str, object] = {
            "campaign_id": str(campaign.id),
            "payment_method": "non_financial",
            "amount": "0.00",
            "system_donor_id": str(donor.id),
            "donor_source": "house_file",
            "non_financial_reason": "legacy",
            "non_financial_notes": "Family confirmed legacy.",
            "donor_first_name": "Updated",
            "donor_last_name": donor.last_name,
            "donor_urn": donor.external_urn or "",
            "donor_email": donor.email,
            "donor_phone": donor.phone,
        }
        body.update(overrides)
        return body

    def test_happy_path_mutates_donor_and_creates_skipped_donation(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, first_name="Original")

        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(self._base_body(campaign=campaign, donor=donor)),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.content

        donor.refresh_from_db()
        assert donor.first_name == "Updated"

        donation = Donation.objects.get(id=resp.json()["donation_id"])
        assert donation.payment_method == Donation.PAYMENT_METHOD_NON_FINANCIAL
        assert donation.amount == Decimal("0.00")
        assert donation.gift_aid is False
        assert donation.letter_status == "skipped"
        assert donation.non_financial_reason == "legacy"
        assert donation.field_data["intake_subtype"] == "donor_update"

    def test_missing_reason_returns_400_and_donor_unchanged(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, first_name="UnchangedName")

        body = self._base_body(campaign=campaign, donor=donor, non_financial_reason="")
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400
        donor.refresh_from_db()
        assert donor.first_name == "UnchangedName"
        assert (
            Donation.objects.filter(
                payment_method=Donation.PAYMENT_METHOD_NON_FINANCIAL
            ).count()
            == 0
        )

    def test_invalid_reason_returns_400(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        body = self._base_body(
            campaign=campaign, donor=donor, non_financial_reason="not-a-real-reason"
        )
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_missing_donor_returns_400(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()

        body = {
            "campaign_id": str(campaign.id),
            "payment_method": "non_financial",
            "amount": "0.00",
            "non_financial_reason": "legacy",
        }
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_invalid_contact_status_returns_400(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        body = self._base_body(
            campaign=campaign, donor=donor, donor_contact_status="not-a-status"
        )
        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(body),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_donor_deleted_between_select_and_submit_returns_410(self) -> None:
        """Race: donor exists at lookup time but is deleted before the
        orchestrator's row-locked re-fetch. Simulated by monkey-patching the
        orchestrator to raise ``DonorVanished``."""
        from donors.updates import DonorVanished

        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        body = self._base_body(campaign=campaign, donor=donor)

        with patch(
            "donations.phone_intake_views.process_phone_non_financial_intake",
            side_effect=DonorVanished("simulated"),
        ):
            resp = http.post(
                reverse("custom_admin:phone_intake_create_donation"),
                data=json.dumps(body),
                content_type="application/json",
            )
        assert resp.status_code == 410, resp.content

    def test_existing_cheque_path_unchanged(self) -> None:
        """Regression: financial path is untouched by the new branch."""
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "25.00",
                    "payment_method": "cheque",
                    "system_donor_id": str(donor.id),
                    "cheque_number": "999",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        donation = Donation.objects.get(id=resp.json()["donation_id"])
        assert donation.payment_method == Donation.PAYMENT_METHOD_CHEQUE
        assert donation.amount == Decimal("25.00")
        assert donation.letter_status == "pending"


@pytest.mark.django_db()
class TestPhoneIntakeCreateDonationDonorEditsFinancial:
    """Donor edits applied during a financial phone donation (PR #154).

    Until PR #154 the donor-edit panel was only wired up for non-financial
    intake. These tests cover the same surface against card / cheque /
    cash payment methods.
    """

    def test_cheque_donation_with_donor_updates_mutates_donor(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, first_name="Original")

        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "25.00",
                    "payment_method": "cheque",
                    "system_donor_id": str(donor.id),
                    "donor_source": "house_file",
                    "cheque_number": "999",
                    "donor_first_name": "Updated",
                    "donor_address_line1": "1 New Road",
                    "donor_postcode": "SW1A 1AA",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.content
        donor.refresh_from_db()
        assert donor.first_name == "Updated"
        assert donor.address_line1 == "1 New Road"
        assert donor.postcode == "SW1A 1AA"
        donation = Donation.objects.get(id=resp.json()["donation_id"])
        assert donation.payment_method == Donation.PAYMENT_METHOD_CHEQUE
        assert donation.amount == Decimal("25.00")

    def test_card_donation_with_donor_updates_mutates_donor(self) -> None:
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, email="old@example.com")

        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "10.00",
                    "payment_method": "card",
                    "system_donor_id": str(donor.id),
                    "donor_source": "house_file",
                    "card_holder_name": "J Doe",
                    "donor_email": "new@example.com",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201, resp.content
        donor.refresh_from_db()
        assert donor.email == "new@example.com"
        # Card donations still report requires_charge so the frontend
        # follows up with the Stripe charge endpoint — donor edits are
        # independent of charge success.
        body = resp.json()
        assert body["requires_charge"] is True

    def test_cheque_donation_no_donor_fields_leaves_donor_unchanged(self) -> None:
        """Operator never opens the donor-edit panel: no donor_* keys
        in the body, donor row is untouched."""
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, first_name="Untouched")

        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "5.00",
                    "payment_method": "cheque",
                    "system_donor_id": str(donor.id),
                    "donor_source": "house_file",
                    "cheque_number": "1",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 201
        donor.refresh_from_db()
        assert donor.first_name == "Untouched"

    def test_invalid_contact_status_rejected_on_financial_path(self) -> None:
        """The donor_contact_status choice-validity check now fires for
        every payment method, not just non-financial."""
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        resp = http.post(
            reverse("custom_admin:phone_intake_create_donation"),
            data=json.dumps(
                {
                    "campaign_id": str(campaign.id),
                    "amount": "5.00",
                    "payment_method": "cheque",
                    "system_donor_id": str(donor.id),
                    "donor_source": "house_file",
                    "cheque_number": "1",
                    "donor_contact_status": "not-a-real-status",
                }
            ),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "donor_contact_status" in resp.json()["error"]

    def test_donor_deleted_before_submit_returns_410_on_financial(self) -> None:
        """Donor row is deleted between page-load and submit while
        donor_updates is supplied → DonorVanished → HTTP 410."""
        http, _ = _login()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)

        from donors.updates import DonorVanished

        with patch(
            "donations.phone_intake_views.create_phone_donation",
            side_effect=DonorVanished("simulated"),
        ):
            resp = http.post(
                reverse("custom_admin:phone_intake_create_donation"),
                data=json.dumps(
                    {
                        "campaign_id": str(campaign.id),
                        "amount": "10.00",
                        "payment_method": "cheque",
                        "system_donor_id": str(donor.id),
                        "donor_source": "house_file",
                        "cheque_number": "1",
                        "donor_first_name": "Updated",
                    }
                ),
                content_type="application/json",
            )
        assert resp.status_code == 410, resp.content
