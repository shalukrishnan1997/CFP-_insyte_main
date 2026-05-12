"""Tests for core/views.py — dashboard and HTMX endpoints."""

import uuid

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from tests.factories import CampaignFactory, UserFactory


@pytest.mark.django_db()
class TestUserDashboardView:
    """Tests for user_dashboard view."""

    def test_unauthenticated_redirects(self) -> None:
        client = Client()
        response = client.get("/dashboard/")
        assert response.status_code == 302

    def test_authenticated_staff_renders_dashboard(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/dashboard/")
        assert response.status_code == 200

    def test_authenticated_non_staff_with_permission_renders_dashboard(
        self,
    ) -> None:
        user = UserFactory(is_staff=False)
        perm = Permission.objects.filter(codename="view_campaign").first()
        assert perm is not None
        user.user_permissions.add(perm)

        client = Client()
        client.force_login(user)

        response = client.get("/dashboard/")
        assert response.status_code == 200

    def test_dashboard_context_has_currency(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/dashboard/")
        assert response.status_code == 200
        assert "currency_symbol" in response.context
        assert "currency_code" in response.context

    def test_dashboard_context_has_campaign_count(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/dashboard/")
        assert response.status_code == 200
        assert "my_campaigns" in response.context

    def test_dashboard_context_has_donation_count(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/dashboard/")
        assert response.status_code == 200
        assert "my_donations" in response.context


@pytest.mark.django_db()
class TestHtmxCampaignSearchView:
    """Tests for htmx_campaign_search view."""

    def test_unauthenticated_redirects(self) -> None:
        client = Client()
        response = client.get("/htmx/campaigns/search/")
        assert response.status_code == 302

    def test_authenticated_returns_json(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/htmx/campaigns/search/?q=test")
        assert response.status_code == 200
        data = response.json()
        assert "campaigns" in data

    def test_search_empty_query_returns_all(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/htmx/campaigns/search/")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["campaigns"], list)


@pytest.mark.django_db()
class TestHtmxDonationStatsView:
    """Tests for htmx_donation_stats view."""

    def test_unauthenticated_redirects(self) -> None:
        client = Client()
        fake_id = uuid.uuid4()
        response = client.get(f"/htmx/campaigns/{fake_id}/stats/")
        assert response.status_code == 302

    def test_invalid_campaign_returns_404(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        fake_id = uuid.uuid4()
        response = authenticated_client.get(f"/htmx/campaigns/{fake_id}/stats/")
        assert response.status_code == 404

    def test_valid_campaign_returns_stats(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        campaign = CampaignFactory(
            name="Test Campaign",
            created_by=staff_user,  # type: ignore[arg-type]
        )
        response = authenticated_client.get(f"/htmx/campaigns/{campaign.id}/stats/")
        assert response.status_code == 200
        data = response.json()
        assert "total_donations" in data
        assert "total_amount" in data
        assert "currency_symbol" in data


@pytest.mark.django_db()
class TestHtmxValidateEmailView:
    """Tests for htmx_validate_email view."""

    def test_unauthenticated_redirects(self) -> None:
        client = Client()
        response = client.get("/htmx/validate-email/")
        assert response.status_code == 302

    def test_empty_email_returns_invalid(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/htmx/validate-email/")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False

    def test_malformed_email_returns_invalid(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/htmx/validate-email/?email=notanemail")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False

    def test_existing_email_returns_invalid(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        user = UserFactory(email="taken@example.com")
        response = authenticated_client.get(f"/htmx/validate-email/?email={user.email}")
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is False

    def test_available_email_returns_valid(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get(
            "/htmx/validate-email/?email=available_unique@example.com"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["valid"] is True
