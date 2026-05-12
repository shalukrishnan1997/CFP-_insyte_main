"""Comprehensive RBAC sweep across donor / donation / campaign / client / banking admin views.

Three layers of guarantees are exercised:

1. Anonymous and client-portal users cannot reach domain admin views.
2. Cross-client identifiers in scoped URLs (e.g. ``/admin/donations/<client>/<campaign>/...``)
   404 instead of leaking the foreign client's data.
3. Bare-codename permissions on banking views resolve via the suffix-match path
   in :func:`responsehandling.permissions.has_permission_or_is_staff`, so a
   group that owns ``donations.view_donation`` can hit ``daily_banking_dashboard``.

The new ``ClientPortalMiddleware`` redirect step is invoked directly because
``responsehandling.settings.test`` removes that middleware from the request stack.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.http import HttpResponse
from django.test import Client as DjangoClient
from django.test import RequestFactory
from django.urls import reverse

from banking.models import PayingInSlip
from campaigns.models import Campaign
from client_portal import middleware as middleware_module
from client_portal.middleware import ClientPortalMiddleware
from clients.models import ClientPortalUser
from donations.models import Donation
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    DonorFactory,
    PayingInSlipFactory,
    SystemDonorFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _staff_client() -> tuple[DjangoClient, Any]:
    """Return a Django test client logged in as an active staff user."""
    user = UserFactory(is_staff=True, is_superuser=False)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    client = DjangoClient()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


def _bypass_2fa(monkeypatch: pytest.MonkeyPatch, user: Any) -> None:
    """Make :class:`ClientPortalMiddleware` treat the user as 2FA-verified."""
    monkeypatch.setattr(
        middleware_module,
        "_has_confirmed_2fa_device",
        lambda _u: True,
    )
    monkeypatch.setattr(user, "is_verified", lambda: True, raising=False)


def _grant_donation_permission(user: Any, codename: str) -> Any:
    """Attach a Donation-model permission to ``user`` and reload its cache."""
    from core.models import User

    content_type = ContentType.objects.get_for_model(Donation)
    permission = Permission.objects.get(content_type=content_type, codename=codename)
    group = Group.objects.create(name=f"banking-{codename}-{uuid.uuid4().hex[:8]}")
    group.permissions.add(permission)
    user.groups.add(group)
    return User.objects.get(pk=user.pk)


def _logged_in_user_with_donation_permission(
    codename: str,
) -> tuple[DjangoClient, Any]:
    """Create a non-staff user with a single ``Donation`` permission and log them in."""
    user = UserFactory(is_staff=False, is_superuser=False)
    user.set_password("testpass123!")
    user.save(update_fields=["password"])
    user = _grant_donation_permission(user, codename)

    client = DjangoClient()
    assert client.login(username=user.username, password="testpass123!")
    return client, user


def _seed_client(name: str) -> dict[str, Any]:
    """Create a self-contained client with a campaign, batch, donor and donation."""
    client_org = ClientFactory(name=name, is_active=True)
    campaign = CampaignFactory(client=client_org, status=Campaign.STATUS_ACTIVE)
    batch = DonationBatchFactory(campaign=campaign)
    donor = DonorFactory(client=client_org)
    SystemDonorFactory(
        client=client_org, external_urn=donor.urn or f"URN-{uuid.uuid4().hex[:8]}"
    )
    donation = DonationFactory(
        campaign=campaign,
        batch=batch,
        donor=donor,
        payment_method="cheque",
        qa_status="approved",
        amount=Decimal("50.00"),
    )
    slip = PayingInSlip.objects.create(
        slip_number=f"SLIP-{uuid.uuid4().hex[:6]}",
        client=client_org,
        payment_type="cheque",
        banking_date=date(2026, 3, 1),
        created_by=UserFactory(),
        status="draft",
    )
    return {
        "client": client_org,
        "campaign": campaign,
        "batch": batch,
        "donor": donor,
        "donation": donation,
        "slip": slip,
    }


# ---------------------------------------------------------------------------
# 1. Access matrix: anonymous / client-portal / staff
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestAnonymousCannotReachAdminViews:
    """Every URL in scope redirects an unauthenticated user to login."""

    @pytest.fixture()
    def seeded(self) -> dict[str, Any]:
        return _seed_client("Anon Probe Charity")

    def _assert_redirects(self, url: str) -> None:
        response = DjangoClient().get(url)
        assert response.status_code in (301, 302), (
            f"{url} should redirect anonymous users, got {response.status_code}"
        )

    def test_anonymous_blocked_from_donation_urls(self, seeded: dict[str, Any]) -> None:
        org = seeded
        urls = [
            reverse(
                "custom_admin:donation_batch_list",
                kwargs={
                    "client_id": org["client"].id,
                    "campaign_id": org["campaign"].id,
                },
            ),
            reverse(
                "custom_admin:donation_batch_detail",
                kwargs={
                    "client_id": org["client"].id,
                    "campaign_id": org["campaign"].id,
                    "batch_id": org["batch"].id,
                },
            ),
            reverse(
                "custom_admin:donation_edit",
                kwargs={"donation_id": org["donation"].id},
            ),
        ]
        for url in urls:
            self._assert_redirects(url)

    def test_anonymous_blocked_from_donor_and_supporter_urls(self) -> None:
        urls = [
            reverse("custom_admin:donor_imports"),
            reverse("custom_admin:supporter_list"),
        ]
        for url in urls:
            self._assert_redirects(url)

    def test_anonymous_blocked_from_campaign_urls(self, seeded: dict[str, Any]) -> None:
        urls = [
            reverse("custom_admin:admin_campaigns"),
            reverse("custom_admin:campaign_create"),
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": seeded["campaign"].id},
            ),
        ]
        for url in urls:
            self._assert_redirects(url)

    def test_anonymous_blocked_from_client_urls(self, seeded: dict[str, Any]) -> None:
        urls = [
            reverse("custom_admin:client_setup"),
            reverse("custom_admin:client_create"),
            reverse(
                "custom_admin:client_edit",
                kwargs={"client_id": seeded["client"].id},
            ),
            reverse(
                "custom_admin:client_payment_config",
                kwargs={"client_id": seeded["client"].id},
            ),
        ]
        for url in urls:
            self._assert_redirects(url)

    def test_anonymous_blocked_from_banking_urls(self, seeded: dict[str, Any]) -> None:
        urls = [
            reverse("custom_admin:daily_banking_dashboard"),
            reverse(
                "custom_admin:daily_banking_batch_detail",
                kwargs={"batch_id": seeded["batch"].id},
            ),
            reverse("custom_admin:slip_list"),
            reverse("custom_admin:slip_detail", kwargs={"slip_id": seeded["slip"].id}),
            reverse("custom_admin:slip_edit", kwargs={"slip_id": seeded["slip"].id}),
            reverse("custom_admin:slip_print", kwargs={"slip_id": seeded["slip"].id}),
            reverse("custom_admin:htmx_banking_batch_queue"),
        ]
        for url in urls:
            self._assert_redirects(url)


@pytest.mark.django_db()
class TestClientPortalUserBlockedFromAdmin:
    """A user with a ``ClientPortalUser`` profile is bounced off ``/admin/*``."""

    def test_middleware_redirects_portal_user_away_from_admin_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_org = ClientFactory()
        portal_user = UserFactory(is_staff=False, is_superuser=False)
        ClientPortalUser.objects.create(
            client=client_org,
            user=portal_user,
            role="viewer",
            is_active=True,
        )
        _bypass_2fa(monkeypatch, portal_user)

        admin_paths = [
            reverse("custom_admin:admin_dashboard"),
            reverse("custom_admin:admin_campaigns"),
            reverse("custom_admin:client_setup"),
            reverse("custom_admin:supporter_list"),
            reverse("custom_admin:donor_imports"),
            reverse("custom_admin:daily_banking_dashboard"),
        ]

        for path in admin_paths:
            request = RequestFactory().get(path)
            request.user = portal_user
            response = ClientPortalMiddleware(
                lambda _r: HttpResponse()
            ).process_request(request)

            assert response is not None, f"{path} should redirect, got None"
            assert response.status_code == 302
            assert "/client/" in response.url


@pytest.mark.django_db()
class TestStaffCanReachAdminViews:
    """Authenticated staff users see real responses on every URL in scope."""

    @pytest.fixture()
    def seeded(self) -> dict[str, Any]:
        return _seed_client("Staff Reach Charity")

    def test_staff_get_2xx_on_listing_endpoints(self, seeded: dict[str, Any]) -> None:
        client, _ = _staff_client()
        org = seeded

        listing_urls = [
            reverse("custom_admin:admin_campaigns"),
            reverse("custom_admin:campaign_create"),
            reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": org["campaign"].id},
            ),
            reverse("custom_admin:client_setup"),
            reverse("custom_admin:client_create"),
            reverse(
                "custom_admin:client_edit",
                kwargs={"client_id": org["client"].id},
            ),
            reverse(
                "custom_admin:client_payment_config",
                kwargs={"client_id": org["client"].id},
            ),
            reverse("custom_admin:donor_imports"),
            reverse("custom_admin:supporter_list"),
            reverse(
                "custom_admin:donation_batch_list",
                kwargs={
                    "client_id": org["client"].id,
                    "campaign_id": org["campaign"].id,
                },
            ),
            reverse(
                "custom_admin:donation_batch_detail",
                kwargs={
                    "client_id": org["client"].id,
                    "campaign_id": org["campaign"].id,
                    "batch_id": org["batch"].id,
                },
            ),
            reverse(
                "custom_admin:donation_edit",
                kwargs={"donation_id": org["donation"].id},
            ),
            reverse("custom_admin:daily_banking_dashboard"),
            reverse(
                "custom_admin:daily_banking_batch_detail",
                kwargs={"batch_id": org["batch"].id},
            ),
            reverse("custom_admin:slip_list"),
            reverse("custom_admin:slip_detail", kwargs={"slip_id": org["slip"].id}),
            reverse("custom_admin:slip_edit", kwargs={"slip_id": org["slip"].id}),
            reverse("custom_admin:slip_print", kwargs={"slip_id": org["slip"].id}),
            reverse("custom_admin:htmx_banking_batch_queue"),
        ]
        for url in listing_urls:
            response = client.get(url, follow=True)
            assert response.status_code == 200, (
                f"GET {url} expected 200, got {response.status_code}"
            )


# ---------------------------------------------------------------------------
# 2. Cross-client IDOR: A-scoped URL with B-resource id must 404
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestCrossClientIDORReturns404:
    """Mixing client/campaign/batch/donor IDs across tenants returns 404."""

    @pytest.fixture()
    def two_clients(self) -> tuple[dict[str, Any], dict[str, Any]]:
        a = _seed_client("Tenant A")
        b = _seed_client("Tenant B")
        return a, b

    def test_donation_batch_list_rejects_other_clients_campaign(
        self, two_clients: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        client, _ = _staff_client()
        a, b = two_clients
        url = reverse(
            "custom_admin:donation_batch_list",
            kwargs={
                "client_id": a["client"].id,
                "campaign_id": b["campaign"].id,
            },
        )
        assert client.get(url).status_code == 404

    def test_donation_batch_detail_rejects_other_clients_batch(
        self, two_clients: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        client, _ = _staff_client()
        a, b = two_clients
        url = reverse(
            "custom_admin:donation_batch_detail",
            kwargs={
                "client_id": a["client"].id,
                "campaign_id": a["campaign"].id,
                "batch_id": b["batch"].id,
            },
        )
        assert client.get(url).status_code == 404

    def test_donation_batch_detail_rejects_inactive_client_pair(
        self, two_clients: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        a, _ = two_clients
        a["client"].is_active = False
        a["client"].save(update_fields=["is_active"])
        client, _ = _staff_client()
        url = reverse(
            "custom_admin:donation_batch_list",
            kwargs={
                "client_id": a["client"].id,
                "campaign_id": a["campaign"].id,
            },
        )
        assert client.get(url).status_code == 404

    def test_donation_batch_detail_rejects_other_clients_campaign_with_own_batch(
        self, two_clients: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Even when a batch_id exists, mismatched campaign in URL 404s."""
        client, _ = _staff_client()
        a, b = two_clients
        url = reverse(
            "custom_admin:donation_batch_detail",
            kwargs={
                "client_id": a["client"].id,
                "campaign_id": b["campaign"].id,
                "batch_id": a["batch"].id,
            },
        )
        assert client.get(url).status_code == 404

    def test_random_uuid_in_donation_batch_detail_returns_404(
        self, two_clients: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        client, _ = _staff_client()
        a, _ = two_clients
        url = reverse(
            "custom_admin:donation_batch_list",
            kwargs={
                "client_id": uuid.uuid4(),
                "campaign_id": a["campaign"].id,
            },
        )
        assert client.get(url).status_code == 404

    def test_campaign_edit_returns_200_for_any_existing_campaign(
        self, two_clients: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """Documents current behaviour: ``campaign_edit`` is not client-scoped.

        The view itself takes only a ``campaign_id`` and never compares it to
        the user's tenant, which is intentional: every staff user is a
        bureau operator with cross-client visibility. This test guards the
        invariant explicitly so a future regression that *does* introduce
        per-staff-tenant scoping is caught.
        """
        client, _ = _staff_client()
        _, b = two_clients
        url = reverse(
            "custom_admin:campaign_edit",
            kwargs={"campaign_id": b["campaign"].id},
        )
        assert client.get(url).status_code == 200

    def test_campaign_edit_404s_for_unknown_campaign(self) -> None:
        client, _ = _staff_client()
        url = reverse(
            "custom_admin:campaign_edit",
            kwargs={"campaign_id": uuid.uuid4()},
        )
        assert client.get(url).status_code == 404

    def test_client_edit_404s_for_unknown_client(self) -> None:
        client, _ = _staff_client()
        url = reverse(
            "custom_admin:client_edit",
            kwargs={"client_id": uuid.uuid4()},
        )
        assert client.get(url).status_code == 404

    def test_client_payment_config_404s_for_unknown_client(self) -> None:
        client, _ = _staff_client()
        url = reverse(
            "custom_admin:client_payment_config",
            kwargs={"client_id": uuid.uuid4()},
        )
        assert client.get(url).status_code == 404

    def test_supporter_detail_404s_for_unknown_donor(self) -> None:
        client, _ = _staff_client()
        url = reverse(
            "custom_admin:supporter_detail",
            kwargs={"pk": uuid.uuid4()},
        )
        assert client.get(url).status_code == 404

    def test_slip_detail_404s_for_unknown_slip(self) -> None:
        client, _ = _staff_client()
        url = reverse("custom_admin:slip_detail", kwargs={"slip_id": 9_999_999})
        assert client.get(url).status_code == 404

    def test_slip_edit_404s_for_unknown_slip(self) -> None:
        client, _ = _staff_client()
        url = reverse("custom_admin:slip_edit", kwargs={"slip_id": 9_999_999})
        assert client.get(url).status_code == 404

    def test_slip_delete_returns_404_for_unknown_slip(self) -> None:
        client, _ = _staff_client()
        url = reverse("custom_admin:slip_delete", kwargs={"slip_id": 9_999_999})
        assert client.post(url).status_code == 404

    def test_slip_add_donations_blocks_cross_client_donation(
        self, two_clients: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        """A draft slip on tenant A cannot ingest a tenant B donation."""
        client, _ = _staff_client()
        a, b = two_clients
        url = reverse(
            "custom_admin:slip_add_donations",
            kwargs={"slip_id": a["slip"].id},
        )
        response = client.post(
            url,
            {"donation_ids": json.dumps([str(b["donation"].id)])},
        )
        assert response.status_code == 400
        body = json.loads(response.content)
        assert body["success"] is False
        assert "different client" in body["error"]

    def test_create_paying_in_slip_rejects_cross_client_donation_mix(
        self, two_clients: tuple[dict[str, Any], dict[str, Any]]
    ) -> None:
        client, _ = _staff_client()
        a, b = two_clients
        response = client.post(
            reverse("custom_admin:create_paying_in_slip"),
            {
                "slip_number": f"SLIP-MIX-{uuid.uuid4().hex[:6]}",
                "banking_date": "2026-03-15",
                "batch_ids": "[]",
                "donation_ids": json.dumps(
                    [str(a["donation"].id), str(b["donation"].id)]
                ),
            },
        )
        assert response.status_code == 400
        body = json.loads(response.content)
        assert body["success"] is False
        assert "exactly one client" in body["error"]


# ---------------------------------------------------------------------------
# 3. Banking permission resolution via bare codenames
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestBankingBareCodenamePermissionResolution:
    """``has_permission_or_is_staff`` resolves bare codenames against any app.

    Banking views are decorated with bare codenames (e.g. ``view_donation``);
    the decorator has to resolve those against ``user.get_all_permissions()``
    via suffix-match so model relocation between apps doesn't silently deny
    long-lived groups. These tests assert the suffix-match path works for the
    exact codenames the banking views ship with.
    """

    def test_view_donation_grants_dashboard_access(self) -> None:
        client, _ = _logged_in_user_with_donation_permission("view_donation")
        response = client.get(reverse("custom_admin:daily_banking_dashboard"))
        assert response.status_code == 200

    def test_view_donation_grants_slip_list_and_detail_access(self) -> None:
        slip = PayingInSlipFactory()
        client, _ = _logged_in_user_with_donation_permission("view_donation")
        assert client.get(reverse("custom_admin:slip_list")).status_code == 200
        assert (
            client.get(
                reverse("custom_admin:slip_detail", kwargs={"slip_id": slip.id})
            ).status_code
            == 200
        )

    def test_change_donation_grants_slip_status_endpoint(self) -> None:
        slip = PayingInSlipFactory(status="draft")
        client, _ = _logged_in_user_with_donation_permission("change_donation")
        response = client.post(
            reverse(
                "custom_admin:slip_update_status",
                kwargs={"slip_id": slip.id},
            ),
            {"status": "ready"},
        )
        # Permission cleared the gate; payload may be 200 (valid) or 400
        # (validation), but never 302 (redirect = denied).
        assert response.status_code in (200, 400)

    def test_delete_donation_grants_slip_delete_endpoint(self) -> None:
        slip = PayingInSlipFactory(status="draft")
        client, _ = _logged_in_user_with_donation_permission("delete_donation")
        response = client.post(
            reverse("custom_admin:slip_delete", kwargs={"slip_id": slip.id})
        )
        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["success"] is True

    def test_user_without_permission_is_redirected_from_dashboard(self) -> None:
        user = UserFactory(is_staff=False, is_superuser=False)
        # Grant an unrelated permission so the user clears
        # ``user_has_access`` (groups exist) but still lacks ``view_donation``.
        unrelated_group = Group.objects.create(name="empty-group")
        user.groups.add(unrelated_group)
        user.set_password("testpass123!")
        user.save(update_fields=["password"])

        client = DjangoClient()
        assert client.login(username=user.username, password="testpass123!")
        response = client.get(reverse("custom_admin:daily_banking_dashboard"))
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# 4. Cross-client cohabitation guards on the donation_quick_edit endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestDonationQuickEditAuth:
    """``donation_quick_edit`` requires POST and ``change_donation``."""

    def test_get_returns_405(self) -> None:
        client, _ = _staff_client()
        donation = DonationFactory()
        response = client.get(
            reverse(
                "custom_admin:donation_quick_edit",
                kwargs={"donation_id": donation.id},
            )
        )
        assert response.status_code == 405

    def test_anonymous_post_redirects(self) -> None:
        donation = DonationFactory()
        response = DjangoClient().post(
            reverse(
                "custom_admin:donation_quick_edit",
                kwargs={"donation_id": donation.id},
            ),
            {"amount": "10.00", "currency": "GBP", "payment_method": "card"},
        )
        assert response.status_code in (301, 302)

    def test_staff_post_succeeds(self) -> None:
        client, _ = _staff_client()
        donation = DonationFactory()
        response = client.post(
            reverse(
                "custom_admin:donation_quick_edit",
                kwargs={"donation_id": donation.id},
            ),
            {
                "amount": "12.50",
                "currency": "GBP",
                "payment_method": "card",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        assert response.status_code == 200
        body = json.loads(response.content)
        assert body["success"] is True
