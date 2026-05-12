"""RBAC tests for scan admin/API endpoints and signed scan-view tokens.

Covers two security surfaces:

1. Standard RBAC matrix (anonymous / client-portal / staff) on a representative
   slice of ``/admin/scan-processing/*`` and ``/admin/api/scan-processing/*``
   routes wrapped in ``@is_authenticated_and_is_staff``.
2. The signed-token authorisation logic in
   ``scans.api_views._request_can_view_scan`` used by ``scan_placeholder_view``,
   ``scan_placeholder_pdf`` and ``scan_image_serve``.

For tokens we exhaustively cover the threat model on ``scan_image_serve``
(wrong salt, expired, wrong-resource binding, tampered payload, staff bypass,
client-portal blocked by middleware) and smoke-test the same checks on the
two sibling endpoints to ensure they call ``_request_can_view_scan``.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core import signing
from django.core.signing import TimestampSigner, b62_encode
from django.test import Client, RequestFactory
from django.urls import reverse

from clients.models import ClientPortalUser
from scans import api_views as scan_api_views
from tests.factories import (
    ClientFactory,
    ScanBatchFactory,
    ScanPlaceholderFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from core.models import User
    from scans.models import ScanPlaceholder

_TEST_SALT = "custom_admin.scan_view"
_TOKEN_MAX_AGE = 60 * 60


def _build_expired_token(resource_id: str, *, age_seconds: int = 7200) -> str:
    """Return a token whose embedded timestamp is older than ``max_age``.

    Reproduces ``django.core.signing.dumps`` byte-for-byte but with a back-dated
    timestamp, then re-signs with the current key so only the *age* check fails.

    Args:
        resource_id: Placeholder UUID embedded in the payload.
        age_seconds: Seconds in the past for the embedded timestamp. Default
            7200 (2 hours) — comfortably above ``_SCAN_VIEW_TOKEN_MAX_AGE_SECS``.

    Returns:
        Signed token string that ``signing.loads`` will reject as expired.
    """
    signer = TimestampSigner(salt=_TEST_SALT)
    payload = json.dumps({"id": resource_id}, separators=(",", ":")).encode()
    encoded = signing.b64_encode(payload).decode()
    old_ts = b62_encode(int(time.time()) - age_seconds)
    prefix = f"{encoded}{signer.sep}{old_ts}"
    sig = signer.signature(prefix)
    return f"{prefix}{signer.sep}{sig}"


def _scan_image_url(*, key: str, placeholder_id: str, token: str | None) -> str:
    """Return the relative ``scan_image_serve`` URL with the given parameters."""
    base = reverse("custom_admin:scan_image_serve")
    parts = [f"key={key}", f"id={placeholder_id}"]
    if token is not None:
        parts.append(f"token={token}")
    return f"{base}?{'&'.join(parts)}"


@pytest.fixture()
def placeholder() -> ScanPlaceholder:
    """Return a saved ``ScanPlaceholder`` for token tests."""
    batch = ScanBatchFactory()
    return ScanPlaceholderFactory(batch=batch)


@pytest.fixture()
def client_portal_user(db: None) -> User:
    """Return an authenticated client-portal (non-staff, no perms) user."""
    del db
    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(
        client=ClientFactory(),
        user=user,
        role="viewer",
        is_active=True,
    )
    return user


# ───────────────────────── Standard RBAC matrix ──────────────────────────


_STAFF_ONLY_ROUTES: list[tuple[str, str]] = [
    # (url_name, http_method)
    ("custom_admin:scan_processing_dashboard", "GET"),
    ("custom_admin:scan_new_batch", "GET"),
    ("custom_admin:scan_batch_list_api", "GET"),
    ("custom_admin:scan_placeholder_list", "GET"),
    ("custom_admin:scan_batch_status", "GET"),
]


@pytest.mark.django_db()
class TestScanAdminRbacMatrix:
    """RBAC sweep over a representative slice of staff-only scan routes."""

    @pytest.mark.parametrize(("url_name", "method"), _STAFF_ONLY_ROUTES)
    def test_anonymous_blocked(self, url_name: str, method: str) -> None:
        """Anonymous users must not reach staff-only scan endpoints."""
        client = Client()
        url = reverse(url_name)
        response = client.generic(method, url)

        assert response.status_code in (302, 403), (
            f"{url_name} ({method}) returned {response.status_code} for anonymous"
        )
        if response.status_code == 302:
            assert "/auth/login" in response["Location"]

    @pytest.mark.parametrize(("url_name", "method"), _STAFF_ONLY_ROUTES)
    def test_client_portal_user_blocked(
        self, url_name: str, method: str, client_portal_user: User
    ) -> None:
        """A client-portal user (no perms, no group) must not reach admin scans."""
        client = Client()
        client.force_login(client_portal_user)
        url = reverse(url_name)
        response = client.generic(method, url)

        assert response.status_code in (302, 403), (
            f"{url_name} ({method}) returned {response.status_code} for client user"
        )

    @pytest.mark.parametrize(("url_name", "method"), _STAFF_ONLY_ROUTES)
    def test_staff_user_reaches_endpoint(self, url_name: str, method: str) -> None:
        """Staff must reach the endpoint (200 or a controlled non-auth error)."""
        staff = UserFactory(is_staff=True)
        client = Client()
        client.force_login(staff)
        url = reverse(url_name)
        response = client.generic(method, url)

        assert response.status_code != 403, (
            f"{url_name} ({method}) wrongly forbade staff: {response.status_code}"
        )
        if response.status_code == 302:
            assert "/auth/login" not in response["Location"]


# ───────────────────────── Token endpoints — anonymous baseline ──────────


_TOKEN_ENDPOINTS: list[str] = [
    "custom_admin:scan_image_serve",
    "custom_admin:scan_placeholder_view",
    "custom_admin:scan_placeholder_pdf",
]


@pytest.mark.django_db()
class TestTokenEndpointAnonymousAccess:
    """Token endpoints must reject anonymous requests with 403, not redirect."""

    @pytest.mark.parametrize("url_name", _TOKEN_ENDPOINTS)
    def test_anonymous_without_token_returns_403(
        self, url_name: str, placeholder: ScanPlaceholder
    ) -> None:
        """Anonymous + no token must produce a 403 JSON response."""
        url = reverse(url_name)
        params = {"id": str(placeholder.id)}
        if url_name == "custom_admin:scan_image_serve":
            params["key"] = placeholder.image_path
        response = Client().get(url, params)

        assert response.status_code == 403
        assert response.json() == {"error": "Authentication required"}


# ───────────────────────── Signed-token threat model ─────────────────────


@pytest.mark.django_db()
class TestScanViewTokenSecurity:
    """Threat model coverage for ``_request_can_view_scan`` on scan_image_serve.

    Each test constructs a fresh anonymous request and exercises the view
    function directly so that the view's own ``return JsonResponse(403)``
    branch is observed (rather than redirect behaviour from middleware).
    """

    def test_valid_token_grants_access(
        self, placeholder: ScanPlaceholder, rf: RequestFactory
    ) -> None:
        """Sanity: a freshly minted token must be accepted."""
        token = scan_api_views.build_scan_view_token(str(placeholder.id))
        request = rf.get(
            _scan_image_url(
                key=placeholder.image_path,
                placeholder_id=str(placeholder.id),
                token=token,
            )
        )
        request.user = AnonymousUser()  # type: ignore[assignment]

        assert (
            scan_api_views._request_can_view_scan(request, str(placeholder.id)) is True
        )

    def test_token_signed_with_wrong_salt_rejected(
        self, placeholder: ScanPlaceholder, rf: RequestFactory
    ) -> None:
        """A token signed under a different salt must not unlock the resource."""
        bad_token = signing.dumps({"id": str(placeholder.id)}, salt="wrong.salt")
        request = rf.get(
            _scan_image_url(
                key=placeholder.image_path,
                placeholder_id=str(placeholder.id),
                token=bad_token,
            )
        )
        request.user = AnonymousUser()  # type: ignore[assignment]

        assert (
            scan_api_views._request_can_view_scan(request, str(placeholder.id)) is False
        )

    def test_expired_token_rejected(
        self, placeholder: ScanPlaceholder, rf: RequestFactory
    ) -> None:
        """A token whose embedded timestamp is older than 1h must be rejected."""
        expired = _build_expired_token(
            str(placeholder.id), age_seconds=_TOKEN_MAX_AGE + 60
        )
        request = rf.get(
            _scan_image_url(
                key=placeholder.image_path,
                placeholder_id=str(placeholder.id),
                token=expired,
            )
        )
        request.user = AnonymousUser()  # type: ignore[assignment]

        assert (
            scan_api_views._request_can_view_scan(request, str(placeholder.id)) is False
        )

    def test_token_for_different_resource_rejected(self, rf: RequestFactory) -> None:
        """A token bound to placeholder A must not unlock placeholder B."""
        placeholder_a = ScanPlaceholderFactory()
        placeholder_b = ScanPlaceholderFactory()

        token_a = scan_api_views.build_scan_view_token(str(placeholder_a.id))
        request = rf.get(
            _scan_image_url(
                key=placeholder_b.image_path,
                placeholder_id=str(placeholder_b.id),
                token=token_a,
            )
        )
        request.user = AnonymousUser()  # type: ignore[assignment]

        assert (
            scan_api_views._request_can_view_scan(request, str(placeholder_b.id))
            is False
        )
        # Token IS valid for its own resource — proves the rejection above is
        # binding-related, not a generic signature failure.
        assert (
            scan_api_views._request_can_view_scan(request, str(placeholder_a.id))
            is True
        )

    def test_tampered_payload_rejected(
        self, placeholder: ScanPlaceholder, rf: RequestFactory
    ) -> None:
        """Mutating the payload segment must invalidate the signature."""
        good = scan_api_views.build_scan_view_token(str(placeholder.id))
        head, sep, tail = good.partition(":")
        # Flip the first character of the base64 payload to corrupt it without
        # changing the segment count.
        flipped = ("Z" if head[:1] != "Z" else "Y") + head[1:]
        tampered = f"{flipped}{sep}{tail}"

        request = rf.get(
            _scan_image_url(
                key=placeholder.image_path,
                placeholder_id=str(placeholder.id),
                token=tampered,
            )
        )
        request.user = AnonymousUser()  # type: ignore[assignment]

        assert (
            scan_api_views._request_can_view_scan(request, str(placeholder.id)) is False
        )

    def test_staff_user_bypasses_token_requirement(
        self, placeholder: ScanPlaceholder, rf: RequestFactory
    ) -> None:
        """An authenticated staff user reaches the resource without a token."""
        staff = UserFactory(is_staff=True)
        request = rf.get(
            _scan_image_url(
                key=placeholder.image_path,
                placeholder_id=str(placeholder.id),
                token=None,
            )
        )
        request.user = staff  # type: ignore[assignment]

        assert (
            scan_api_views._request_can_view_scan(request, str(placeholder.id)) is True
        )

    def test_random_garbage_token_rejected(
        self, placeholder: ScanPlaceholder, rf: RequestFactory
    ) -> None:
        """A non-signed string must be rejected without raising."""
        request = rf.get(
            _scan_image_url(
                key=placeholder.image_path,
                placeholder_id=str(placeholder.id),
                token="not-a-valid-token",
            )
        )
        request.user = AnonymousUser()  # type: ignore[assignment]

        assert (
            scan_api_views._request_can_view_scan(request, str(placeholder.id)) is False
        )


# ───────────────────────── Sibling-endpoint smoke tests ──────────────────


@pytest.mark.django_db()
class TestTokenEnforcementOnSiblingEndpoints:
    """Confirm scan_placeholder_view and scan_placeholder_pdf use the same gate."""

    def test_placeholder_view_rejects_wrong_salt_token(
        self, placeholder: ScanPlaceholder
    ) -> None:
        """``scan_placeholder_view`` must call ``_request_can_view_scan``."""
        bad_token = signing.dumps({"id": str(placeholder.id)}, salt="wrong.salt")
        url = reverse("custom_admin:scan_placeholder_view")
        response = Client().get(url, {"id": str(placeholder.id), "token": bad_token})

        assert response.status_code == 403

    def test_placeholder_view_rejects_expired_token(
        self, placeholder: ScanPlaceholder
    ) -> None:
        """``scan_placeholder_view`` must reject tokens past their max age."""
        expired = _build_expired_token(
            str(placeholder.id), age_seconds=_TOKEN_MAX_AGE + 60
        )
        url = reverse("custom_admin:scan_placeholder_view")
        response = Client().get(url, {"id": str(placeholder.id), "token": expired})

        assert response.status_code == 403

    def test_placeholder_view_rejects_token_for_other_resource(self) -> None:
        """``scan_placeholder_view`` must enforce resource-id binding."""
        placeholder_a = ScanPlaceholderFactory()
        placeholder_b = ScanPlaceholderFactory()
        token_a = scan_api_views.build_scan_view_token(str(placeholder_a.id))

        url = reverse("custom_admin:scan_placeholder_view")
        response = Client().get(url, {"id": str(placeholder_b.id), "token": token_a})

        assert response.status_code == 403

    def test_placeholder_pdf_rejects_wrong_salt_token(
        self, placeholder: ScanPlaceholder
    ) -> None:
        """``scan_placeholder_pdf`` must call ``_request_can_view_scan``."""
        bad_token = signing.dumps({"id": str(placeholder.id)}, salt="wrong.salt")
        url = reverse("custom_admin:scan_placeholder_pdf")
        response = Client().get(url, {"id": str(placeholder.id), "token": bad_token})

        assert response.status_code == 403

    def test_placeholder_pdf_rejects_token_for_other_resource(self) -> None:
        """``scan_placeholder_pdf`` must enforce resource-id binding."""
        placeholder_a = ScanPlaceholderFactory()
        placeholder_b = ScanPlaceholderFactory()
        token_a = scan_api_views.build_scan_view_token(str(placeholder_a.id))

        url = reverse("custom_admin:scan_placeholder_pdf")
        response = Client().get(url, {"id": str(placeholder_b.id), "token": token_a})

        assert response.status_code == 403


# ───────────────────────── Middleware does not honour tokens ─────────────


@pytest.mark.django_db()
class TestClientPortalMiddlewareIgnoresScanToken:
    """``ClientPortalMiddleware`` must redirect client-portal users off /admin/*.

    Tokens are checked inside the view; a valid token must not let an
    authenticated client-portal user bypass the role-routing middleware.
    """

    def test_client_user_redirected_off_admin_even_with_valid_token(
        self,
        rf: RequestFactory,
        placeholder: ScanPlaceholder,
        client_portal_user: User,
    ) -> None:
        """Middleware redirects the client user before the view runs."""
        from unittest.mock import MagicMock, patch

        from client_portal.middleware import ClientPortalMiddleware

        token = scan_api_views.build_scan_view_token(str(placeholder.id))
        request = rf.get(
            _scan_image_url(
                key=placeholder.image_path,
                placeholder_id=str(placeholder.id),
                token=token,
            )
        )
        request.user = client_portal_user  # type: ignore[assignment]
        # Simulate OTP middleware having marked the user verified so we exercise
        # the role-routing branch, not the 2FA enforcement branch.
        request.user.is_verified = lambda: True  # type: ignore[method-assign]

        middleware = ClientPortalMiddleware(lambda req: None)  # type: ignore[arg-type,return-value]
        with (
            patch("client_portal.middleware.resolve") as mock_resolve,
            patch(
                "client_portal.middleware._has_confirmed_2fa_device",
                return_value=True,
            ),
        ):
            mock_url = MagicMock()
            mock_url.url_name = "scan_image_serve"
            mock_resolve.return_value = mock_url
            result = middleware.process_request(request)

        assert result is not None
        assert result.status_code == 302
        assert "/client/" in result["Location"]


# ───────────────────────── Resource isolation sanity ─────────────────────


@pytest.mark.django_db()
def test_unknown_placeholder_id_via_token_does_not_leak(
    rf: RequestFactory,
) -> None:
    """A token with a bogus ID must not authorise access to a real resource."""
    real = ScanPlaceholderFactory()
    bogus_id = str(uuid.uuid4())
    bogus_token = scan_api_views.build_scan_view_token(bogus_id)

    request = rf.get(
        _scan_image_url(
            key=real.image_path,
            placeholder_id=str(real.id),
            token=bogus_token,
        )
    )
    request.user = AnonymousUser()  # type: ignore[assignment]

    assert scan_api_views._request_can_view_scan(request, str(real.id)) is False
