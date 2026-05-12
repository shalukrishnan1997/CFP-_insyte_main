"""RBAC integration tests for ``custom_admin`` DRF + JSON API endpoints.

This module enumerates every URL exposed via ``custom_admin/api_urls.py`` and
every function-based JSON view in ``custom_admin/api_views.py`` (plus
``service_items_api``). For each endpoint we assert the same matrix:

    1. Anonymous            -> 302/401/403 (never reaches handler)
    2. Client portal user   -> blocked by ``ClientPortalMiddleware``
                                 from ``/admin/*`` paths
    3. Staff                -> 200
    4. Group-backed user    -> 200 (no ``is_staff`` flag, but ``user_has_access``)

We also enforce three security floors:

    * REST framework ``DEFAULT_PERMISSION_CLASSES`` is ``IsAuthenticated``.
    * No re-exported ViewSet drops to ``AllowAny`` accidentally.
    * Object-level cross-tenant isolation: ``donor_search``,
      ``package_codes_api`` and ``scanned_form_lookup`` must not leak rows
      across clients.

Note on the test environment:
    ``ClientPortalMiddleware`` is *disabled* in ``responsehandling.settings.test``
    so that ``force_login`` works without 2FA setup. The middleware-routing
    assertions therefore call ``ClientPortalMiddleware.process_request``
    directly, mirroring the pattern in ``tests/unit/test_middleware_extended.py``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse
from rest_framework.test import APIClient

from clients.models import ClientPortalUser
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationFactory,
    DonorFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    UserFactory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_group_user() -> Any:
    """Return a non-staff user with a group (passes ``user_has_access``)."""
    user = UserFactory(is_staff=False, is_superuser=False)
    user.groups.add(Group.objects.create(name=f"rbac-grp-{user.username}"))
    return user


def _make_portal_user(client: Any) -> Any:
    """Return a non-staff user wired up as a client-portal profile."""
    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(
        client=client,
        user=user,
        role="viewer",
        is_active=True,
    )
    return user


def _api(user: Any | None = None) -> APIClient:
    """Return a DRF ``APIClient`` optionally pre-authenticated."""
    api = APIClient()
    if user is not None:
        api.force_authenticate(user=user)
    return api


# Endpoints exposed via the DRF router (``custom_admin/api_urls.py``).
# These are list endpoints; collection-level GET is enough to verify the
# ``permission_classes`` floor on each viewset.
ROUTER_LIST_URLS: list[str] = [
    "/admin/api/campaign-data-files/",
    "/admin/api/data-file-uploads/",
    "/admin/api/letter-batches/",
]


# ---------------------------------------------------------------------------
# Floor: settings-level invariants
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestDrfDefaultsFloor:
    """Verify the REST framework defaults provide a safe floor."""

    def test_default_permission_class_is_isauthenticated(self) -> None:
        """``DEFAULT_PERMISSION_CLASSES`` must include ``IsAuthenticated``."""
        rest = getattr(settings, "REST_FRAMEWORK", {})
        defaults = rest.get("DEFAULT_PERMISSION_CLASSES", [])
        assert "rest_framework.permissions.IsAuthenticated" in defaults, (
            "DRF default permissions must require authentication (no AllowAny floor)."
        )

    def test_default_authentication_is_session(self) -> None:
        """Session auth must be configured (CSRF + cookie protected)."""
        rest = getattr(settings, "REST_FRAMEWORK", {})
        auth = rest.get("DEFAULT_AUTHENTICATION_CLASSES", [])
        assert "rest_framework.authentication.SessionAuthentication" in auth

    def test_router_viewsets_have_explicit_permission_classes(self) -> None:
        """Each registered viewset must opt in to a permission class explicitly.

        Relying solely on ``DEFAULT_PERMISSION_CLASSES`` is brittle — a future
        ``REST_FRAMEWORK`` override could downgrade access. Each viewset in
        ``custom_admin/api_urls.py`` must carry its own ``permission_classes``.
        """
        from rest_framework.permissions import AllowAny

        from custom_admin.api_views import (
            CampaignDataFileViewSet,
            DataFileUploadViewSet,
            LetterBatchViewSet,
        )

        for viewset in (
            CampaignDataFileViewSet,
            DataFileUploadViewSet,
            LetterBatchViewSet,
        ):
            perms = getattr(viewset, "permission_classes", None)
            assert perms, (
                f"{viewset.__name__} has no explicit permission_classes; "
                "must not rely on framework defaults alone."
            )
            assert AllowAny not in perms, (
                f"{viewset.__name__} unexpectedly grants AllowAny."
            )


# ---------------------------------------------------------------------------
# Anonymous probes — no endpoint may serve an unauthenticated caller
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestAnonymousAccessIsBlocked:
    """Every endpoint must reject unauthenticated callers."""

    @pytest.mark.parametrize("url", ROUTER_LIST_URLS)
    def test_router_endpoint_blocks_anonymous(self, url: str) -> None:
        """DRF viewsets must return 401/403 for anonymous callers."""
        response = _api().get(url)
        assert response.status_code in (401, 403), (
            f"Anonymous reached {url} (status={response.status_code})"
        )

    def test_donor_search_blocks_anonymous(self) -> None:
        """``donor_search`` must redirect anonymous callers to login."""
        response = _api().get(reverse("custom_admin:donor_search") + "?q=ab")
        # Decorator redirects unauthenticated users to LOGIN_URL.
        assert response.status_code in (302, 401, 403)

    def test_address_lookup_blocks_anonymous(self) -> None:
        """``address_lookup_proxy`` must reject anonymous callers."""
        response = _api().get(
            reverse("custom_admin:address_lookup_proxy") + "?postcode=SW1A1AA"
        )
        assert response.status_code in (302, 401, 403)

    def test_campaigns_by_client_api_blocks_anonymous(self) -> None:
        """``campaigns_by_client_api`` must reject anonymous callers."""
        response = _api().get(
            reverse("custom_admin:campaigns_by_client_api")
            + "?client_id=00000000-0000-0000-0000-000000000000"
        )
        assert response.status_code in (302, 401, 403)

    def test_package_codes_api_blocks_anonymous(self) -> None:
        """``package_codes_api`` must reject anonymous callers."""
        campaign = CampaignFactory()
        url = reverse(
            "custom_admin:package_codes_api",
            kwargs={"campaign_id": str(campaign.id)},
        )
        response = _api().get(url)
        assert response.status_code in (302, 401, 403)

    def test_scanned_form_lookup_blocks_anonymous(self) -> None:
        """``scanned_form_lookup`` must reject anonymous callers."""
        response = _api().get(
            reverse("custom_admin:scanned_form_lookup")
            + "?campaign_id=00000000-0000-0000-0000-000000000000&urn=URN0"
        )
        assert response.status_code in (302, 401, 403)

    def test_service_items_api_blocks_anonymous(self) -> None:
        """``service_items_api`` must reject anonymous callers."""
        response = _api().get(reverse("custom_admin:service_items_api"))
        assert response.status_code in (302, 401, 403)


# ---------------------------------------------------------------------------
# Staff and group-backed users reach all endpoints
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestStaffAccess:
    """Staff users must reach every endpoint."""

    @pytest.mark.parametrize("url", ROUTER_LIST_URLS)
    def test_staff_can_list_router_endpoints(self, url: str) -> None:
        """Staff get 200 on every DRF list endpoint."""
        user = UserFactory(is_staff=True, is_superuser=True)
        response = _api(user).get(url)
        assert response.status_code == 200

    def test_staff_can_call_donor_search(self) -> None:
        """Staff get 200 on ``donor_search`` (returns empty list with short query)."""
        user = UserFactory(is_staff=True, is_superuser=True)
        api = _api()
        api.force_login(user)
        response = api.get(reverse("custom_admin:donor_search") + "?q=ab")
        assert response.status_code == 200
        assert response.json() == {"donors": []}

    def test_staff_can_call_address_lookup(self) -> None:
        """Staff reach ``address_lookup_proxy`` and get a structured JSON reply."""
        user = UserFactory(is_staff=True, is_superuser=True)
        api = _api()
        api.force_login(user)
        with patch("custom_admin.api_views._lookup_postcodes_io") as mock_io:
            from django.http import JsonResponse

            mock_io.return_value = JsonResponse({"success": True, "addresses": []})
            response = api.get(
                reverse("custom_admin:address_lookup_proxy") + "?postcode=SW1A1AA"
            )
        assert response.status_code == 200

    def test_staff_can_call_campaigns_by_client_api(self) -> None:
        """Staff reach ``campaigns_by_client_api``."""
        user = UserFactory(is_staff=True, is_superuser=True)
        client_obj = ClientFactory()
        CampaignFactory(client=client_obj, name="Visible")
        api = _api()
        api.force_login(user)
        response = api.get(
            reverse("custom_admin:campaigns_by_client_api")
            + f"?client_id={client_obj.id}"
        )
        assert response.status_code == 200
        assert any(c["name"] == "Visible" for c in response.json()["campaigns"])

    def test_campaigns_by_client_api_status_filter(self) -> None:
        """``?status=active`` restricts results; omitting it keeps the
        existing all-statuses behaviour for the two existing callers
        (``donor_imports.html``, invoice ``create_elite``)."""
        user = UserFactory(is_staff=True, is_superuser=True)
        client_obj = ClientFactory()
        CampaignFactory(client=client_obj, name="Live one", status="active")
        CampaignFactory(client=client_obj, name="Old one", status="closed")
        CampaignFactory(client=client_obj, name="Draft one", status="draft")
        api = _api()
        api.force_login(user)

        filtered = api.get(
            reverse("custom_admin:campaigns_by_client_api")
            + f"?client_id={client_obj.id}&status=active"
        )
        assert filtered.status_code == 200
        names = {c["name"] for c in filtered.json()["campaigns"]}
        assert names == {"Live one"}

        unfiltered = api.get(
            reverse("custom_admin:campaigns_by_client_api")
            + f"?client_id={client_obj.id}"
        )
        assert unfiltered.status_code == 200
        all_names = {c["name"] for c in unfiltered.json()["campaigns"]}
        assert all_names == {"Live one", "Old one", "Draft one"}

    def test_campaigns_by_client_api_empty_when_no_active_campaigns(self) -> None:
        # Phone-intake picker fetches `?status=active` per-client; locks the
        # contract that an empty result is a successful 200 with [] (so the
        # JS shows an empty-state, not a network error).
        user = UserFactory(is_staff=True, is_superuser=True)
        client_obj = ClientFactory()
        CampaignFactory(client=client_obj, status="closed")
        api = _api()
        api.force_login(user)

        response = api.get(
            reverse("custom_admin:campaigns_by_client_api")
            + f"?client_id={client_obj.id}&status=active"
        )
        assert response.status_code == 200
        assert response.json()["campaigns"] == []

    def test_campaigns_by_client_api_unknown_client_returns_404(self) -> None:
        # Was a 500 before — bad UUID for a non-existent Client raised
        # ``Client.DoesNotExist`` which Django rendered as an unhandled
        # exception. The handler now returns a clean 404.
        import uuid as _uuid

        user = UserFactory(is_staff=True, is_superuser=True)
        api = _api()
        api.force_login(user)
        response = api.get(
            reverse("custom_admin:campaigns_by_client_api")
            + f"?client_id={_uuid.uuid4()}"
        )
        assert response.status_code == 404
        assert response.json() == {"error": "Client not found"}

    def test_campaigns_by_client_api_invalid_status_returns_400(self) -> None:
        # Reject typos (``?status=acitve``) instead of silently returning []
        # — surfaces caller bugs rather than masking them as empty data.
        user = UserFactory(is_staff=True, is_superuser=True)
        client_obj = ClientFactory()
        api = _api()
        api.force_login(user)
        response = api.get(
            reverse("custom_admin:campaigns_by_client_api")
            + f"?client_id={client_obj.id}&status=acitve"
        )
        assert response.status_code == 400
        assert response.json() == {"error": "Invalid status"}

    def test_staff_can_call_package_codes_api(self) -> None:
        """Staff reach ``package_codes_api``."""
        user = UserFactory(is_staff=True, is_superuser=True)
        campaign = CampaignFactory()
        api = _api()
        api.force_login(user)
        response = api.get(
            reverse(
                "custom_admin:package_codes_api",
                kwargs={"campaign_id": str(campaign.id)},
            )
        )
        assert response.status_code == 200
        assert response.json()["status"] == "success"

    def test_staff_can_call_scanned_form_lookup(self) -> None:
        """Staff reach ``scanned_form_lookup``."""
        user = UserFactory(is_staff=True, is_superuser=True)
        campaign = CampaignFactory()
        api = _api()
        api.force_login(user)
        response = api.get(
            reverse("custom_admin:scanned_form_lookup")
            + f"?campaign_id={campaign.id}&urn=NOPE"
        )
        assert response.status_code == 200

    def test_staff_can_call_service_items_api(self) -> None:
        """Staff reach ``service_items_api``."""
        user = UserFactory(is_staff=True, is_superuser=True)
        api = _api()
        api.force_login(user)
        response = api.get(reverse("custom_admin:service_items_api"))
        assert response.status_code == 200


@pytest.mark.django_db()
class TestGroupBackedNonStaffAccess:
    """Non-staff users with any group should reach the same endpoints (mirrors UI rules)."""

    @pytest.mark.parametrize("url", ROUTER_LIST_URLS)
    def test_group_user_can_list_router_endpoints(self, url: str) -> None:
        """Group-backed non-staff users get 200 on every DRF list endpoint."""
        response = _api(_make_group_user()).get(url)
        assert response.status_code == 200, (
            f"Group-backed user blocked from {url}: {response.status_code}"
        )

    def test_group_user_can_call_function_views(self) -> None:
        """Group-backed users reach function-based JSON views."""
        user = _make_group_user()
        campaign = CampaignFactory()
        api = _api()
        api.force_login(user)

        for url in (
            reverse("custom_admin:donor_search") + "?q=ab",
            reverse(
                "custom_admin:package_codes_api",
                kwargs={"campaign_id": str(campaign.id)},
            ),
            reverse("custom_admin:service_items_api"),
        ):
            response = api.get(url)
            assert response.status_code == 200, (
                f"Group-backed user blocked from {url}: {response.status_code}"
            )


# ---------------------------------------------------------------------------
# Client portal users must not slip past ``ClientPortalMiddleware`` into /admin/*
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestClientPortalUserBlockedFromAdmin:
    """A client portal user hitting any ``/admin/*`` URL must be redirected."""

    def _process_admin_path(self, user: Any, path: str) -> Any:
        """Invoke ``ClientPortalMiddleware.process_request`` directly.

        Args:
            user: The authenticated user.
            path: Admin-mounted path under test.

        Returns:
            The middleware response (``HttpResponseRedirect`` if blocked,
            ``None`` if it would let the request through).
        """
        from client_portal.middleware import ClientPortalMiddleware

        # 2FA enforcement is exercised elsewhere; isolate routing here.
        user.is_verified = lambda: True  # type: ignore[method-assign]
        request = RequestFactory().get(path)
        request.user = user

        middleware = ClientPortalMiddleware(
            get_response=lambda _request: HttpResponse()
        )

        with (
            patch("client_portal.middleware.resolve") as mock_resolve,
            patch(
                "client_portal.middleware._has_confirmed_2fa_device",
                return_value=True,
            ),
        ):
            url = MagicMock()
            url.url_name = "rbac-test"
            mock_resolve.return_value = url
            return middleware.process_request(request)

    @pytest.mark.parametrize(
        "path",
        [
            "/admin/api/campaign-data-files/",
            "/admin/api/data-file-uploads/",
            "/admin/api/letter-batches/",
            "/admin/api/donors/search/",
            "/admin/api/address-lookup/",
            "/admin/api/campaigns/",
            "/admin/api/scanned-form/lookup/",
            "/admin/api/service-items/",
        ],
    )
    def test_portal_user_redirected_off_admin_endpoints(self, path: str) -> None:
        """Every API path under ``/admin/`` redirects portal users to ``/client/``."""
        portal_client = ClientFactory()
        portal_user = _make_portal_user(portal_client)

        response = self._process_admin_path(portal_user, path)

        assert response is not None, f"Portal user reached {path}"
        assert response.status_code == 302
        assert "/client/" in response["Location"], (
            f"Portal user not routed to client portal for {path}"
        )


# ---------------------------------------------------------------------------
# Object-level cross-client isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestCrossClientIsolation:
    """Endpoints that scope by client must not leak rows across tenants."""

    def test_donor_search_does_not_leak_across_clients(self) -> None:
        """``donor_search?campaign_id=X`` must return only client X's donors."""
        from custom_admin.api_views import donor_search

        client_a = ClientFactory(name="Tenant A")
        client_b = ClientFactory(name="Tenant B")
        shared_urn = "URN-SHARED-9001"

        DonorFactory(
            client=client_a,
            urn=shared_urn,
            first_name="Alice",
            last_name="Allowed",
        )
        DonorFactory(
            client=client_b,
            urn=shared_urn,
            first_name="Bob",
            last_name="Forbidden",
        )

        campaign_a = CampaignFactory(client=client_a)

        request = RequestFactory().get(
            reverse("custom_admin:donor_search"),
            {"q": shared_urn, "campaign_id": str(campaign_a.id)},
        )
        request.user = UserFactory(is_staff=True, is_superuser=True)

        response = donor_search(request)

        assert response.status_code == 200
        donors = json.loads(response.content)["donors"]
        names = {f"{d['first_name']} {d['last_name']}" for d in donors}
        assert "Alice Allowed" in names
        assert "Bob Forbidden" not in names, (
            "donor_search leaked rows from a different client (IDOR)."
        )

    def test_donor_search_explicit_client_id_does_not_leak(self) -> None:
        """``donor_search?client_id=X`` must restrict to client X."""
        from custom_admin.api_views import donor_search

        client_a = ClientFactory()
        client_b = ClientFactory()

        DonorFactory(client=client_a, urn="URN-A-1", first_name="Anna", last_name="A")
        DonorFactory(client=client_b, urn="URN-B-1", first_name="Anna", last_name="B")

        request = RequestFactory().get(
            reverse("custom_admin:donor_search"),
            {"q": "Anna", "client_id": str(client_a.id)},
        )
        request.user = UserFactory(is_staff=True, is_superuser=True)
        response = donor_search(request)

        assert response.status_code == 200
        urns = {d["urn"] for d in json.loads(response.content)["donors"]}
        assert "URN-A-1" in urns
        assert "URN-B-1" not in urns

    def test_package_codes_api_returns_only_codes_for_target_campaign(self) -> None:
        """Codes attached to a *different* campaign must not surface."""
        from campaigns.models import PackageCode
        from custom_admin.api_views import package_codes_api

        campaign_one = CampaignFactory()
        campaign_two = CampaignFactory()

        code_one = PackageCode.objects.create(code="PKG-VISIBLE", is_active=True)
        code_other = PackageCode.objects.create(code="PKG-HIDDEN", is_active=True)
        campaign_one.package_codes.add(code_one)
        campaign_two.package_codes.add(code_other)

        request = RequestFactory().get(
            reverse(
                "custom_admin:package_codes_api",
                kwargs={"campaign_id": str(campaign_one.id)},
            )
        )
        request.user = UserFactory(is_staff=True, is_superuser=True)

        response = package_codes_api(request, campaign_id=str(campaign_one.id))

        assert response.status_code == 200
        codes = {row["code"] for row in json.loads(response.content)["package_codes"]}
        assert "PKG-VISIBLE" in codes
        assert "PKG-HIDDEN" not in codes

    def test_scanned_form_lookup_does_not_leak_across_campaigns(self) -> None:
        """Looking up by URN must scope to the requested campaign only.

        A donation+scan exists in ``campaign_other``. Asking for the same URN
        in ``campaign_target`` (different campaign — different tenant) must
        return ``has_image: False``.
        """
        from custom_admin.api_views import scanned_form_lookup

        client_a = ClientFactory()
        client_b = ClientFactory()
        campaign_target = CampaignFactory(client=client_a)
        campaign_other = CampaignFactory(client=client_b)

        shared_urn = "URN-CROSS-LEAK-001"
        donor_other = DonorFactory(client=client_b, urn=shared_urn)
        donation_other = DonationFactory(campaign=campaign_other, donor=donor_other)
        ScanPlaceholderFactory(
            donation=donation_other,
            urn=shared_urn,
            batch=ScanBatchFactory(campaign=campaign_other),
        )

        request = RequestFactory().get(
            reverse("custom_admin:scanned_form_lookup"),
            {"campaign_id": str(campaign_target.id), "urn": shared_urn},
        )
        request.user = UserFactory(is_staff=True, is_superuser=True)

        response = scanned_form_lookup(request)
        payload = json.loads(response.content)

        assert response.status_code == 200
        assert payload["has_image"] is False, (
            "scanned_form_lookup served a scan from a different campaign/client."
        )
        assert payload["page_urls"] == []


# ---------------------------------------------------------------------------
# Sanity: the function-based decorators behave as documented for an explicit
# anonymous request.
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestDecoratorSanity:
    """Direct calls to the decorated view with ``AnonymousUser`` redirect to login."""

    def test_donor_search_redirects_anon_via_decorator(self) -> None:
        """Anonymous request to the decorated view yields a 302 redirect."""
        from custom_admin.api_views import donor_search

        request = RequestFactory().get(reverse("custom_admin:donor_search"))
        request.user = AnonymousUser()
        # Add session/messages middleware shims so messages.info doesn't crash.
        request.session = {}  # type: ignore[attr-defined]
        request._messages = MagicMock()  # type: ignore[attr-defined]

        response = donor_search(request)
        assert response.status_code == 302
        assert settings.LOGIN_URL in response["Location"]
