"""Integration tests for QA Review views.

FIN-QA-INT-* test cases covering QA dashboard, batch review,
and approval/rejection endpoints.
"""

from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from campaigns.models import CampaignDataFile
from custom_admin.views import qa_review as qa_review_module
from donors.models import DataFileDonor
from tests.factories import (
    DonationBatchFactory,
    DonationFactory,
    SystemDonorFactory,
    UserFactory,
)


@pytest.fixture()
def staff_client() -> tuple[Client, Any]:
    """Return authenticated client and staff user."""
    user = UserFactory(is_staff=True, is_superuser=True)
    admin_group, _ = Group.objects.get_or_create(name="admin")
    user.groups.add(admin_group)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = Client()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


# ═══════════════════════════════════════════════════════════════
# QA Review — Page Loads
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestQAViewsLoad:
    """FIN-QA-INT-001 to 002: QA pages load correctly."""

    def test_qa_dashboard_loads(self, staff_client: tuple[Client, Any]) -> None:
        """FIN-QA-INT-001: QA dashboard returns 200."""
        client, _ = staff_client
        response = client.get(reverse("custom_admin:qa_dashboard"))
        assert response.status_code == 200

    def test_qa_dashboard_shows_batches(self, staff_client: tuple[Client, Any]) -> None:
        """FIN-QA-INT-002: QA dashboard lists pending batches."""
        client, _ = staff_client
        DonationBatchFactory(
            batch_name="QA-REVIEW-001",
            status="pending_qa",
        )
        response = client.get(reverse("custom_admin:qa_dashboard"))
        assert response.status_code == 200


# ═══════════════════════════════════════════════════════════════
# QA Review — Batch Detail & Donations
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestQABatchDetail:
    """FIN-QA-INT-003 to 004: Batch review detail views."""

    def test_qa_batch_detail_loads(self, staff_client: tuple[Client, Any]) -> None:
        """FIN-QA-INT-003: QA batch detail/review page loads."""
        client, _ = staff_client
        batch = DonationBatchFactory(status="pending_qa")
        DonationFactory(batch=batch, campaign=batch.campaign)
        response = client.get(
            reverse("custom_admin:qa_batch_review", kwargs={"batch_id": batch.pk})
        )
        assert response.status_code == 200

    def test_qa_batch_detail_shows_donations(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """FIN-QA-INT-004: Batch detail lists donations."""
        client, _ = staff_client
        batch = DonationBatchFactory(status="pending_qa")
        DonationFactory(batch=batch, campaign=batch.campaign, amount="50.00")
        DonationFactory(batch=batch, campaign=batch.campaign, amount="75.00")
        response = client.get(
            reverse("custom_admin:qa_batch_review", kwargs={"batch_id": batch.pk})
        )
        assert response.status_code == 200

    def test_qa_batch_review_shows_rejected_records_and_reason(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Rejected records panel shows rejected donations with QA reason."""
        client, _ = staff_client
        batch = DonationBatchFactory(status="rejected")
        DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            qa_status="rejected",
            qa_notes="Address mismatch with scanned form",
        )
        DonationFactory(batch=batch, campaign=batch.campaign, qa_status="approved")

        response = client.get(
            reverse("custom_admin:qa_batch_review", kwargs={"batch_id": batch.pk})
        )

        assert response.status_code == 200
        assert b"Rejected Records" in response.content
        assert b"Address mismatch with scanned form" in response.content

    def test_qa_single_donation_review_shows_package_code_field(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """QA single-donation review shows the package code input when present."""
        client, _ = staff_client
        batch = DonationBatchFactory(status="pending_qa")
        donation = DonationFactory(batch=batch, campaign=batch.campaign)
        donation.field_data = {"package_code": "PKG-QA-01"}
        donation.save(update_fields=["field_data"])

        response = client.get(
            reverse(
                "custom_admin:qa_single_donation_review",
                kwargs={"batch_id": batch.pk, "donation_id": donation.pk},
            )
        )

        assert response.status_code == 200
        assert b'name="package_code"' in response.content
        assert b'value="PKG-QA-01"' in response.content

    def test_qa_single_donation_review_preserves_initial_urn_for_search_widget(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """The donor-search widget should retain the server-rendered URN value."""
        client, user = staff_client
        batch = DonationBatchFactory(status="pending_qa")
        data_file = CampaignDataFile.objects.create(
            campaign=batch.campaign,
            created_by=user,
        )
        data_file_donor = DataFileDonor.objects.create(
            data_file=data_file,
            client=batch.campaign.client,
            urn="URN-QA-WIDGET-01",
            first_name="Daisy",
            last_name="Edwards",
        )
        system_donor = SystemDonorFactory(
            client=batch.campaign.client,
            external_urn="",
            first_name="Daisy",
            last_name="Edwards",
        )
        donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            donor=None,
            donor_source="data_file",
            data_file_donor=data_file_donor,
            system_donor=system_donor,
        )

        response = client.get(
            reverse(
                "custom_admin:qa_single_donation_review",
                kwargs={"batch_id": batch.pk, "donation_id": donation.pk},
            ),
            follow=True,
        )

        assert response.status_code == 200
        assert b'value="URN-QA-WIDGET-01"' in response.content
        assert (
            b"window.donorAssignWidget = function donorAssignWidget(searchUrl, assignUrl, initialQuery = '')"
            in response.content
        )
        assert b"query: initialQuery || ''" in response.content


# ═══════════════════════════════════════════════════════════════
# QA Review — Authentication
# ═══════════════════════════════════════════════════════════════


@pytest.mark.django_db()
class TestQAAuth:
    """FIN-QA-INT-005: QA views require authentication."""

    def test_unauthenticated_redirect(self) -> None:
        """FIN-QA-INT-005: Anonymous user redirected from QA."""
        client = Client()
        response = client.get(reverse("custom_admin:qa_dashboard"))
        assert response.status_code in [302, 301]


@pytest.mark.django_db()
class TestQAApproveAtomic:
    """Approve action persists POSTed field edits in one request."""

    def test_approve_applies_amount_from_same_post(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        client, user = staff_client
        batch = DonationBatchFactory(status="pending_qa", default_payment_method="cash")
        donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            amount=Decimal("25.00"),
            payment_method="cash",
            qa_status="pending",
        )
        donor = donation.donor
        assert donor is not None
        qa_review_module._claim_reviewer_lock(batch.id, user)  # pyright: ignore[reportPrivateUsage]
        url = reverse(
            "custom_admin:qa_single_donation_review",
            kwargs={"batch_id": batch.pk, "donation_id": donation.pk},
        )
        data = {
            "amount": "100.00",
            "package_code": "PKG-QA-ATOMIC",
            "donor_first_name": donor.first_name,
            "donor_last_name": donor.last_name,
            "donor_title": donor.title or "",
            "donor_email": donor.email or "",
            "donor_phone": donor.phone or "",
            "donor_address_line1": donor.address_line1 or "",
            "donor_address_line2": getattr(donor, "address_line2", "") or "",
            "donor_city": donor.city or "",
            "donor_county": getattr(donor, "county", "") or "",
            "donor_postcode": donor.postcode or "",
            "donor_urn": donor.urn or "",
            "action": "approve",
        }
        response = client.post(url, data, follow=True)
        assert response.status_code == 200
        donation.refresh_from_db()
        assert donation.amount == Decimal("100.00")
        assert donation.qa_status == "approved"

    def test_approve_persists_posted_donation_date(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        client, user = staff_client
        batch = DonationBatchFactory(status="pending_qa", default_payment_method="cash")
        donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            amount=Decimal("25.00"),
            payment_method="cash",
            qa_status="pending",
        )
        donor = donation.donor
        assert donor is not None
        qa_review_module._claim_reviewer_lock(batch.id, user)  # pyright: ignore[reportPrivateUsage]
        url = reverse(
            "custom_admin:qa_single_donation_review",
            kwargs={"batch_id": batch.pk, "donation_id": donation.pk},
        )
        data = {
            "amount": "25.00",
            "donation_date": "2024-05-06",
            "package_code": "PKG-QA-DATE",
            "donor_first_name": donor.first_name,
            "donor_last_name": donor.last_name,
            "donor_title": donor.title or "",
            "donor_email": donor.email or "",
            "donor_phone": donor.phone or "",
            "donor_address_line1": donor.address_line1 or "",
            "donor_address_line2": getattr(donor, "address_line2", "") or "",
            "donor_city": donor.city or "",
            "donor_county": getattr(donor, "county", "") or "",
            "donor_postcode": donor.postcode or "",
            "donor_urn": donor.urn or "",
            "action": "approve",
        }
        response = client.post(url, data, follow=True)
        assert response.status_code == 200
        donation.refresh_from_db()
        assert str(donation.donation_date) == "2024-05-06"
        assert donation.qa_status == "approved"

    def test_reject_requires_notes(self, staff_client: tuple[Client, Any]) -> None:
        client, user = staff_client
        batch = DonationBatchFactory(status="pending_qa", default_payment_method="cash")
        donation = DonationFactory(
            batch=batch,
            campaign=batch.campaign,
            payment_method="cash",
            qa_status="pending",
        )
        donor = donation.donor
        assert donor is not None
        qa_review_module._claim_reviewer_lock(batch.id, user)  # pyright: ignore[reportPrivateUsage]
        url = reverse(
            "custom_admin:qa_single_donation_review",
            kwargs={"batch_id": batch.pk, "donation_id": donation.pk},
        )
        data = {
            "amount": str(donation.amount),
            "package_code": "PKG-QA-REJECT",
            "donor_first_name": donor.first_name,
            "donor_last_name": donor.last_name,
            "donor_title": donor.title or "",
            "donor_email": donor.email or "",
            "donor_phone": donor.phone or "",
            "donor_address_line1": donor.address_line1 or "",
            "donor_address_line2": getattr(donor, "address_line2", "") or "",
            "donor_city": donor.city or "",
            "donor_county": getattr(donor, "county", "") or "",
            "donor_postcode": donor.postcode or "",
            "donor_urn": donor.urn or "",
            "action": "reject",
        }
        response = client.post(url, data, follow=True)
        assert response.status_code == 200
        donation.refresh_from_db()
        assert donation.qa_status == "pending"

    def test_approve_succeeds_when_source_donor_urn_would_conflict(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Approving should not fail when a linked source donor already has a different URN."""
        client, user = staff_client
        batch = DonationBatchFactory(status="pending_qa", default_payment_method="cash")
        campaign = batch.campaign
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        DataFileDonor.objects.create(
            data_file=data_file,
            client=campaign.client,
            urn="URN-CONFLICT",
            first_name="Existing",
            last_name="Donor",
        )
        source_donor = DataFileDonor.objects.create(
            data_file=data_file,
            client=campaign.client,
            urn="URN-SOURCE",
            first_name="Source",
            last_name="Donor",
            country="United Kingdom",
        )
        system_donor = SystemDonorFactory(
            client=campaign.client,
            external_urn="URN-CONFLICT",
        )
        donation = DonationFactory(
            batch=batch,
            campaign=campaign,
            donor=None,
            donor_source="data_file",
            data_file_donor=source_donor,
            system_donor=system_donor,
            payment_method="cash",
            qa_status="pending",
        )

        qa_review_module._claim_reviewer_lock(batch.id, user)  # pyright: ignore[reportPrivateUsage]
        url = reverse(
            "custom_admin:qa_single_donation_review",
            kwargs={"batch_id": batch.pk, "donation_id": donation.pk},
        )
        data = {
            "amount": str(donation.amount),
            "package_code": "PKG-QA-CONFLICT",
            "donor_first_name": "Arthur",
            "donor_last_name": "Thomas",
            "donor_title": "Dr",
            "donor_email": "",
            "donor_phone": "",
            "donor_address_line1": "2 Elm Crescent",
            "donor_address_line2": "",
            "donor_city": "Manchester",
            "donor_county": "",
            "donor_postcode": "M28 2DD",
            "donor_country": "United Kingdom",
            "donor_urn": "URN-CONFLICT",
            "action": "approve",
        }

        response = client.post(url, data, follow=True)

        assert response.status_code == 200
        donation.refresh_from_db()
        source_donor.refresh_from_db()
        assert donation.qa_status == "approved"
        assert source_donor.urn == "URN-SOURCE"

    def test_approve_preserves_existing_data_file_urn_when_posted_urn_is_blank(
        self, staff_client: tuple[Client, Any]
    ) -> None:
        """Blank QA URN input must not null out the imported data-file donor URN."""
        client, user = staff_client
        batch = DonationBatchFactory(status="pending_qa", default_payment_method="cash")
        campaign = batch.campaign
        data_file = CampaignDataFile.objects.create(campaign=campaign, created_by=user)
        source_donor = DataFileDonor.objects.create(
            data_file=data_file,
            client=campaign.client,
            urn="URN100006",
            first_name="Arthur",
            last_name="Thomas",
            country="United Kingdom",
        )
        system_donor = SystemDonorFactory(
            client=campaign.client,
            external_urn="",
            first_name="Arthur",
            last_name="Thomas",
        )
        donation = DonationFactory(
            batch=batch,
            campaign=campaign,
            donor=None,
            donor_source="data_file",
            data_file_donor=source_donor,
            system_donor=system_donor,
            payment_method="cash",
            qa_status="pending",
        )

        qa_review_module._claim_reviewer_lock(batch.id, user)  # pyright: ignore[reportPrivateUsage]
        url = reverse(
            "custom_admin:qa_single_donation_review",
            kwargs={"batch_id": batch.pk, "donation_id": donation.pk},
        )
        data = {
            "amount": str(donation.amount),
            "package_code": "Spring2026/06",
            "donor_first_name": "Arthur",
            "donor_last_name": "Thomas",
            "donor_title": "Dr",
            "donor_email": "aclark@gmail.com",
            "donor_phone": "",
            "donor_address_line1": "2 Elm Crescent",
            "donor_address_line2": "Worsley",
            "donor_city": "Manchester",
            "donor_county": "",
            "donor_postcode": "M28 2DD",
            "donor_country": "United Kingdom",
            "donor_urn": "",
            "action": "approve",
        }

        response = client.post(url, data, follow=True)

        assert response.status_code == 200
        donation.refresh_from_db()
        source_donor.refresh_from_db()
        assert donation.qa_status == "approved"
        assert source_donor.urn == "URN100006"
