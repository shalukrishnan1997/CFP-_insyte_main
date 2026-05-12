"""RBAC tests for QA review endpoints and the ``qa_access_required`` decorator.

Covers:

- Direct decorator behaviour for staff / admin-group / QA-group / permission /
  no-access users (mirroring ``has_qa_access``'s contract).
- HTTP-level role coverage on every QA URL: anonymous, client portal user,
  non-QA non-staff user, QA-group user, staff. Verifies the
  ``ClientPortalMiddleware`` + decorator stack rejects unauthorised callers
  without rendering a 500 / template error.
- State-changing endpoints (``qa_approve_batch``, ``qa_reject_batch``,
  ``qa_update_batch_status``, ``qa_batch_resubmit``) reject direct POST from
  callers without the matching ``change_donationbatch`` permission, even when
  they hold weaker QA-only access (e.g. ``view_donationbatch``).
- ``setup_qa_permissions`` management command attaches the documented
  permission set to the ``QA`` group.

Outer ``is_authenticated_and_is_staff`` redirects unauthenticated and
no-access users (302). The inner ``has_qa_access`` raises ``PermissionDenied``
(403) for users who pass the outer check but do not satisfy QA access (e.g.
authenticated user with a permission unrelated to QA). Tests assert the
correct status per path rather than collapsing both into one.
"""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING, Any

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse

from clients.models import ClientPortalUser
from custom_admin.views import qa_review
from custom_admin.views.qa_utils import has_qa_access, qa_access_required
from donations.models import Donation, DonationBatch
from tests.factories import (
    ClientFactory,
    DonationBatchFactory,
    DonationFactory,
    UserFactory,
)

if TYPE_CHECKING:
    from core.models import User


# Test settings exclude ClientPortalMiddleware. Re-add it for tests that
# specifically exercise the role-routing redirect logic.
_MIDDLEWARE_WITH_PORTAL = [
    *settings.MIDDLEWARE,
    "client_portal.middleware.ClientPortalMiddleware",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(
    rf: RequestFactory,
    user: Any,
    *,
    method: str = "get",
    path: str = "/admin/qa/",
) -> HttpRequest:
    """Build a request with messages/session storage attached.

    Args:
        rf: ``RequestFactory`` instance.
        user: User to attach (anonymous or real).
        method: HTTP method (``get`` / ``post``).
        path: URL path.

    Returns:
        HttpRequest ready for direct view invocation.
    """
    request = getattr(rf, method)(path)
    request.user = user
    request.session = {}  # type: ignore[assignment]
    request._messages = FallbackStorage(request)  # type: ignore[attr-defined]
    return request


def _dummy_view(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
    """Trivial view that signals it was reached."""
    del request, args, kwargs
    return HttpResponse("OK")


def _bypass_portal_2fa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``_has_confirmed_2fa_device`` true and ``user.is_verified`` true.

    Only required when ``ClientPortalMiddleware`` is re-enabled via
    ``@override_settings``; the default test settings exclude that middleware.
    """
    monkeypatch.setattr(
        "client_portal.middleware._has_confirmed_2fa_device",
        lambda _user: True,
    )

    # Replace ``OTPMiddleware.__call__`` with a stub that attaches a verified
    # ``is_verified`` to the request user. The real middleware does this via
    # ``functools.partial`` after consulting the OTP device tables — building
    # real devices in test setup is heavier than the contract under test
    # warrants.
    def _stub_otp_call(self: Any, request: HttpRequest) -> Any:
        if hasattr(request, "user") and request.user.is_authenticated:
            request.user.is_verified = lambda: True  # type: ignore[method-assign]
        return self.get_response(request)

    monkeypatch.setattr("django_otp.middleware.OTPMiddleware.__call__", _stub_otp_call)


def _qa_group(user: User) -> None:
    """Add *user* to the ``QA`` group, creating it if needed."""
    group, _ = Group.objects.get_or_create(name="QA")
    user.groups.add(group)


def _grant_view_donationbatch(user: User) -> None:
    """Attach the post-split ``donations.view_donationbatch`` permission."""
    ct = ContentType.objects.get_for_model(DonationBatch)
    perm = Permission.objects.get(codename="view_donationbatch", content_type=ct)
    user.user_permissions.add(perm)


def _grant_change_donationbatch(user: User) -> None:
    """Attach the ``donations.change_donationbatch`` permission."""
    ct = ContentType.objects.get_for_model(DonationBatch)
    perm = Permission.objects.get(codename="change_donationbatch", content_type=ct)
    user.user_permissions.add(perm)


def _make_client_portal_user(client_obj: object) -> User:
    """Return a non-staff user with a ``client_portal_profile`` attached.

    Args:
        client_obj: Client instance (avoids name shadowing the test client).
    """
    user = UserFactory(is_staff=False, is_superuser=False)
    ClientPortalUser.objects.create(client=client_obj, user=user, role="viewer")
    return user


# ---------------------------------------------------------------------------
# Decorator-level tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestQaAccessRequiredDecorator:
    """Cover ``qa_access_required`` directly with ``RequestFactory``."""

    def test_anonymous_user_redirects_to_login(self, rf: RequestFactory) -> None:
        """Outer ``is_authenticated_and_is_staff`` must redirect anonymous users."""
        view = qa_access_required(_dummy_view)
        request = _make_request(rf, AnonymousUser())

        response = view(request)

        assert response.status_code == 302

    def test_staff_user_passes_through(self, rf: RequestFactory) -> None:
        """Staff users always reach the wrapped view."""
        user = UserFactory(is_staff=True)
        view = qa_access_required(_dummy_view)
        request = _make_request(rf, user)

        response = view(request)

        assert response.status_code == 200
        assert response.content == b"OK"

    def test_qa_group_user_passes_through(self, rf: RequestFactory) -> None:
        """QA-group membership grants access without ``is_staff``."""
        user = UserFactory(is_staff=False)
        _qa_group(user)
        view = qa_access_required(_dummy_view)
        request = _make_request(rf, user)

        response = view(request)

        assert response.status_code == 200

    def test_admin_group_user_passes_through(self, rf: RequestFactory) -> None:
        """Lowercase ``admin`` group grants access (matched case-insensitively)."""
        user = UserFactory(is_staff=False)
        admin_group, _ = Group.objects.get_or_create(name="admin")
        user.groups.add(admin_group)
        view = qa_access_required(_dummy_view)
        request = _make_request(rf, user)

        response = view(request)

        assert response.status_code == 200

    def test_view_donationbatch_perm_passes_through(self, rf: RequestFactory) -> None:
        """``view_donationbatch`` (any app label) grants QA access."""
        user = UserFactory(is_staff=False)
        _grant_view_donationbatch(user)
        view = qa_access_required(_dummy_view)
        request = _make_request(rf, user)

        response = view(request)

        assert response.status_code == 200

    def test_unrelated_perm_user_denied_with_403(self, rf: RequestFactory) -> None:
        """A user with a non-QA permission passes the outer check but the
        inner ``has_qa_access`` raises ``PermissionDenied`` → 403, not 302.
        """
        user = UserFactory(is_staff=False)
        # Grant any permission unrelated to DonationBatch / Donation so the
        # outer ``is_authenticated_and_is_staff`` check (which only inspects
        # *existence* of perms) succeeds.
        ct = ContentType.objects.get_for_model(Group)
        perm = Permission.objects.get(codename="view_group", content_type=ct)
        user.user_permissions.add(perm)
        view = qa_access_required(_dummy_view)
        request = _make_request(rf, user)

        with pytest.raises(PermissionDenied):
            view(request)

    def test_authenticated_no_access_redirects(self, rf: RequestFactory) -> None:
        """Authenticated user with no groups/perms → outer redirects (302)."""
        user = UserFactory(is_staff=False)
        view = qa_access_required(_dummy_view)
        request = _make_request(rf, user)

        response = view(request)

        assert response.status_code == 302


# ---------------------------------------------------------------------------
# HTTP-level role coverage on every QA URL
# ---------------------------------------------------------------------------


def _qa_url_specs() -> list[tuple[str, str, list[str]]]:
    """Return ``(url_name, method, arg_keys)`` for every QA URL.

    ``arg_keys`` names the path-arg slots resolved against the fixtures at
    test time (``"batch_id"`` / ``"donation_id"``).
    """
    return [
        ("custom_admin:qa_dashboard", "get", []),
        ("custom_admin:qa_batch_review", "get", ["batch_id"]),
        (
            "custom_admin:qa_single_donation_review",
            "get",
            ["batch_id", "donation_id"],
        ),
        (
            "custom_admin:qa_save_scan_redaction",
            "post",
            ["batch_id", "donation_id"],
        ),
        (
            "custom_admin:qa_resolve_pending_donor",
            "post",
            ["batch_id", "donation_id"],
        ),
        (
            "custom_admin:qa_send_authentication_link",
            "post",
            ["batch_id", "donation_id"],
        ),
        ("custom_admin:qa_update_batch_status", "post", ["batch_id"]),
        ("custom_admin:qa_approve_batch", "post", ["batch_id"]),
        ("custom_admin:qa_reject_batch", "post", ["batch_id"]),
        ("custom_admin:qa_batch_resubmit", "post", ["batch_id"]),
        ("custom_admin:qa_batch_stats_api", "get", []),
        ("custom_admin:htmx_assign_donor_to_donation", "post", ["donation_id"]),
        ("custom_admin:download_gift_aid_report", "get", ["batch_id"]),
    ]


@pytest.mark.django_db()
class TestQaUrlRoleCoverage:
    """Every QA URL hit by every role: anon / portal / non-QA / QA / staff."""

    @pytest.fixture()
    def batch(self) -> DonationBatch:
        return DonationBatchFactory()

    @pytest.fixture()
    def donation(self, batch: DonationBatch) -> Donation:
        return DonationFactory(batch=batch, campaign=batch.campaign)

    def _resolve(
        self,
        name: str,
        arg_keys: list[str],
        batch: DonationBatch,
        donation: Donation,
    ) -> str:
        kwargs: dict[str, Any] = {}
        for key in arg_keys:
            if key == "batch_id":
                kwargs["batch_id"] = batch.id
            elif key == "donation_id":
                kwargs["donation_id"] = donation.id
        return reverse(name, kwargs=kwargs)

    def test_anonymous_user_blocked_from_every_qa_url(
        self,
        client: Client,
        batch: DonationBatch,
        donation: Donation,
    ) -> None:
        """Anonymous users must never reach a QA endpoint and must not 5xx."""
        for name, method, arg_keys in _qa_url_specs():
            url = self._resolve(name, arg_keys, batch, donation)
            response = getattr(client, method)(url)

            # 302 redirect to login is the expected outcome. 405 (wrong method)
            # would mean the endpoint executed before auth; not allowed.
            assert response.status_code in (302, 301), (
                f"{name} returned {response.status_code} for anonymous"
            )
            location = response["Location"]
            assert "/login" in location or "next=" in location, (
                f"{name} redirected anonymous user to non-login URL: {location}"
            )

    def test_client_portal_user_redirected_off_admin(
        self,
        batch: DonationBatch,
        donation: Donation,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A user with ``client_portal_profile`` must be bounced from /admin/*.

        Re-enables ``ClientPortalMiddleware`` (the test settings strip it) so
        the role-routing redirect actually fires; bypasses 2FA via patches.
        """
        with override_settings(MIDDLEWARE=_MIDDLEWARE_WITH_PORTAL):
            _bypass_portal_2fa(monkeypatch)
            isolated_client = Client()
            client_obj = ClientFactory()
            portal_user = _make_client_portal_user(client_obj)
            isolated_client.force_login(portal_user)

            for name, method, arg_keys in _qa_url_specs():
                url = self._resolve(name, arg_keys, batch, donation)
                response = getattr(isolated_client, method)(url)

                assert response.status_code == 302, (
                    f"{name} returned {response.status_code} for portal user"
                )
                assert "/client/" in response["Location"], (
                    f"{name} did not redirect portal user to /client/; "
                    f"got {response['Location']}"
                )

    def test_non_qa_authenticated_user_denied(
        self,
        client: Client,
        batch: DonationBatch,
        donation: Donation,
    ) -> None:
        """An authenticated user with no QA access never reaches a QA view."""
        user = UserFactory(is_staff=False)
        # No groups, no permissions → outer decorator fires.
        client.force_login(user)

        for name, method, arg_keys in _qa_url_specs():
            url = self._resolve(name, arg_keys, batch, donation)
            response = getattr(client, method)(url)

            assert response.status_code in (302, 403), (
                f"{name} returned {response.status_code} for non-QA user"
            )

    def test_qa_group_user_reaches_read_endpoints(
        self,
        client: Client,
        batch: DonationBatch,
    ) -> None:
        """QA-group user reaches read endpoints (no 403/302 from auth layer).

        The set of URLs gated by ``qa_access_required`` alone (no extra
        ``has_permission_or_is_staff`` decorator) must accept QA-group users.
        Other endpoints carry an additional permission requirement, which is
        intentional and exercised in :class:`TestQaStateChangeAuthorization`.
        """
        del batch  # fixture present so the dashboard sees ≥1 batch
        user = UserFactory(is_staff=False)
        _qa_group(user)
        # The QA group as configured by ``setup_qa_permissions`` carries the
        # full permission set. Replicate that so we read the dashboard end-to-
        # end here rather than retesting the command.
        for codename, model in (
            ("view_donationbatch", DonationBatch),
            ("change_donationbatch", DonationBatch),
            ("view_donation", Donation),
            ("change_donation", Donation),
        ):
            ct = ContentType.objects.get_for_model(model)
            perm = Permission.objects.get(codename=codename, content_type=ct)
            user.user_permissions.add(perm)
        client.force_login(user)

        # Dashboard is the canonical sanity check that the auth + middleware
        # stack lets QA users in. We don't try every URL here — many of the
        # POST endpoints have business-logic preconditions that are out of
        # scope for an RBAC test.
        response = client.get(reverse("custom_admin:qa_dashboard"))
        assert response.status_code == 200

        response = client.get(reverse("custom_admin:qa_batch_stats_api"))
        assert response.status_code == 200

    def test_staff_user_reaches_read_endpoints(
        self,
        client: Client,
        batch: DonationBatch,
    ) -> None:
        """Staff users reach the QA dashboard and stats API."""
        del batch  # ensure the fixture creates a batch in the DB
        staff = UserFactory(is_staff=True)
        client.force_login(staff)

        response = client.get(reverse("custom_admin:qa_dashboard"))
        assert response.status_code == 200

        response = client.get(reverse("custom_admin:qa_batch_stats_api"))
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# State-changing endpoints: the QA-only access is not enough — they require
# the matching ``change_*`` permission (or staff).
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestQaStateChangeAuthorization:
    """A user with QA *read* access alone must NOT be able to mutate state."""

    @pytest.fixture()
    def batch(self) -> DonationBatch:
        return DonationBatchFactory()

    def test_view_only_user_cannot_approve_batch(
        self, client: Client, batch: DonationBatch
    ) -> None:
        """``view_donationbatch`` lets you read the dashboard, not approve."""
        user = UserFactory(is_staff=False)
        _qa_group(user)
        _grant_view_donationbatch(user)
        # Deliberately omit ``change_donationbatch``.
        client.force_login(user)

        url = reverse("custom_admin:qa_approve_batch", args=[batch.id])
        response = client.post(url)

        # ``has_permission_or_is_staff('change_donationbatch')`` redirects to /
        # for users without the perm. State must NOT change.
        assert response.status_code == 302
        assert response["Location"] == "/", (
            f"Expected redirect to '/'; got {response['Location']}"
        )
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_view_only_user_cannot_reject_batch(
        self, client: Client, batch: DonationBatch
    ) -> None:
        """``view_donationbatch`` cannot reject."""
        user = UserFactory(is_staff=False)
        _qa_group(user)
        _grant_view_donationbatch(user)
        client.force_login(user)

        url = reverse("custom_admin:qa_reject_batch", args=[batch.id])
        response = client.post(url, data={"rejection_reason": "x"})

        assert response.status_code == 302
        assert response["Location"] == "/"
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_view_only_user_cannot_update_batch_status(
        self, client: Client, batch: DonationBatch
    ) -> None:
        """``view_donationbatch`` cannot update batch status directly."""
        user = UserFactory(is_staff=False)
        _qa_group(user)
        _grant_view_donationbatch(user)
        client.force_login(user)

        url = reverse("custom_admin:qa_update_batch_status", args=[batch.id])
        response = client.post(url, data={"status": DonationBatch.STATUS_APPROVED})

        assert response.status_code == 302
        assert response["Location"] == "/"
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_view_only_user_cannot_resubmit_batch(
        self, client: Client, batch: DonationBatch
    ) -> None:
        """``view_donationbatch`` cannot resubmit a batch."""
        user = UserFactory(is_staff=False)
        _qa_group(user)
        _grant_view_donationbatch(user)
        client.force_login(user)

        url = reverse("custom_admin:qa_batch_resubmit", args=[batch.id])
        response = client.post(url)

        assert response.status_code == 302
        assert response["Location"] == "/"
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_change_perm_user_authorized_for_state_change(
        self, client: Client, batch: DonationBatch
    ) -> None:
        """``change_donationbatch`` does authorize approve.

        Doesn't assert the batch transitions to APPROVED — that depends on
        donation-level state and is out of scope. The decisive RBAC question
        is that the request is *not* auth-rejected: a non-302-to-/ outcome
        proves the decorator stack let the request reach the view.
        """
        user = UserFactory(is_staff=False)
        _qa_group(user)
        _grant_view_donationbatch(user)
        _grant_change_donationbatch(user)
        client.force_login(user)

        url = reverse("custom_admin:qa_approve_batch", args=[batch.id])
        response = client.post(url)

        # Either 200 (renders blocking-context page) or 302 to qa_dashboard
        # is fine — the request reached the view. We assert the redirect is
        # *not* the bare "/" produced by has_permission_or_is_staff failure.
        assert response.status_code in (200, 302)
        if response.status_code == 302:
            assert response["Location"] != "/"


# ---------------------------------------------------------------------------
# ``has_qa_access`` direct unit checks specific to states ``test_qa_access.py``
# does not cover (legacy admin group case-folding, low-perm denial path).
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestHasQaAccessEdgeCases:
    """Additional ``has_qa_access`` cases not covered by ``test_qa_access.py``."""

    def test_unrelated_permission_does_not_grant_access(self) -> None:
        """A non-DonationBatch permission must not satisfy QA access."""
        user = UserFactory(is_staff=False)
        ct = ContentType.objects.get_for_model(Group)
        perm = Permission.objects.get(codename="view_group", content_type=ct)
        user.user_permissions.add(perm)

        assert has_qa_access(user) is False

    def test_admin_group_match_is_case_insensitive(self) -> None:
        """``ADMIN`` (uppercase) is treated identically to ``admin``."""
        user = UserFactory(is_staff=False)
        group, _ = Group.objects.get_or_create(name="ADMIN")
        user.groups.add(group)

        assert has_qa_access(user) is True

    def test_qa_group_match_is_case_insensitive(self) -> None:
        """Lowercase ``qa`` group works as well as ``QA``."""
        user = UserFactory(is_staff=False)
        group, _ = Group.objects.get_or_create(name="qa")
        user.groups.add(group)

        assert has_qa_access(user) is True


# ---------------------------------------------------------------------------
# ``setup_qa_permissions`` management command
# ---------------------------------------------------------------------------


@pytest.mark.django_db()
class TestSetupQaPermissionsCommand:
    """Verify the QA group ends up with the documented permission set."""

    REQUIRED_CODENAMES = {
        "view_donation",
        "add_donation",
        "change_donation",
        "view_donationbatch",
        "change_donationbatch",
    }

    def _run(self) -> None:
        out = StringIO()
        call_command("setup_qa_permissions", stdout=out)

    def test_creates_qa_group_with_required_permissions(self) -> None:
        """First run must create the QA group with the documented codenames."""
        assert not Group.objects.filter(name="QA").exists()

        self._run()

        qa_group = Group.objects.get(name="QA")
        codenames = {p.codename for p in qa_group.permissions.all()}
        assert codenames == self.REQUIRED_CODENAMES

    def test_grants_only_donation_and_donationbatch_perms(self) -> None:
        """The command must not over-grant — every perm must target Donation
        or DonationBatch.
        """
        self._run()
        qa_group = Group.objects.get(name="QA")
        donation_ct = ContentType.objects.get_for_model(Donation)
        batch_ct = ContentType.objects.get_for_model(DonationBatch)
        valid_cts = {donation_ct.id, batch_ct.id}

        for perm in qa_group.permissions.all():
            assert perm.content_type_id in valid_cts, (
                f"Unexpected permission target: {perm.content_type}"
            )

    def test_idempotent_on_rerun(self) -> None:
        """Re-running the command must converge on the same permission set,
        not duplicate or accumulate stale perms.
        """
        self._run()
        # Add an unrelated permission that should be cleared by re-run.
        qa_group = Group.objects.get(name="QA")
        ct = ContentType.objects.get_for_model(Group)
        stray = Permission.objects.get(codename="view_group", content_type=ct)
        qa_group.permissions.add(stray)

        self._run()

        qa_group.refresh_from_db()
        codenames = {p.codename for p in qa_group.permissions.all()}
        assert codenames == self.REQUIRED_CODENAMES
        assert "view_group" not in codenames

    def test_qa_group_membership_grants_qa_access(self) -> None:
        """End-to-end: a user added to the freshly-created QA group is
        accepted by ``has_qa_access`` (no perm copying required).
        """
        self._run()
        user = UserFactory(is_staff=False)
        qa_group = Group.objects.get(name="QA")
        user.groups.add(qa_group)

        assert has_qa_access(user) is True


# ---------------------------------------------------------------------------
# Direct POST coverage on state-changing endpoints — RequestFactory variant
# This mirrors ``TestQaStateChangeAuthorization`` but reaches into the view
# functions without middleware, which is the path a malicious caller using
# session hijacking + CSRF bypass would take.
# ---------------------------------------------------------------------------


def _noop_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence Django flash messages in direct view calls."""
    for level in ("success", "info", "error", "warning"):
        monkeypatch.setattr(qa_review.messages, level, lambda *_a, **_kw: None)


@pytest.mark.django_db()
class TestQaStateChangeDirectPost:
    """Directly invoke the state-changing views to confirm decorator wins."""

    def test_non_qa_user_direct_approve_post_blocked(
        self, rf: RequestFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``qa_approve_batch`` must auth-reject a non-QA user even on direct POST."""
        _noop_messages(monkeypatch)
        batch = DonationBatchFactory()
        user = UserFactory(is_staff=False)
        # Give them an unrelated permission so the outer
        # ``is_authenticated_and_is_staff`` passes — this proves the inner
        # ``has_permission_or_is_staff('change_donationbatch')`` is doing
        # the heavy lifting.
        ct = ContentType.objects.get_for_model(Group)
        user.user_permissions.add(
            Permission.objects.get(codename="view_group", content_type=ct)
        )
        request = _make_request(
            rf,
            user,
            method="post",
            path=reverse("custom_admin:qa_approve_batch", args=[batch.id]),
        )

        response = qa_review.qa_approve_batch(request, batch.id)

        assert response.status_code == 302
        assert response["Location"] == "/"
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_non_qa_user_direct_reject_post_blocked(
        self, rf: RequestFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``qa_reject_batch`` must auth-reject a non-QA user even on direct POST."""
        _noop_messages(monkeypatch)
        batch = DonationBatchFactory()
        user = UserFactory(is_staff=False)
        ct = ContentType.objects.get_for_model(Group)
        user.user_permissions.add(
            Permission.objects.get(codename="view_group", content_type=ct)
        )
        request = _make_request(
            rf,
            user,
            method="post",
            path=reverse("custom_admin:qa_reject_batch", args=[batch.id]),
        )

        response = qa_review.qa_reject_batch(request, batch.id)

        assert response.status_code == 302
        assert response["Location"] == "/"
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA

    def test_non_qa_user_direct_update_status_blocked(
        self, rf: RequestFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``qa_update_batch_status`` must auth-reject a non-QA user."""
        _noop_messages(monkeypatch)
        batch = DonationBatchFactory()
        user = UserFactory(is_staff=False)
        ct = ContentType.objects.get_for_model(Group)
        user.user_permissions.add(
            Permission.objects.get(codename="view_group", content_type=ct)
        )
        request = _make_request(
            rf,
            user,
            method="post",
            path=reverse("custom_admin:qa_update_batch_status", args=[batch.id]),
        )
        request.POST = {"status": DonationBatch.STATUS_APPROVED}  # type: ignore[assignment]

        response = qa_review.qa_update_batch_status(request, batch.id)

        assert response.status_code == 302
        assert response["Location"] == "/"
        batch.refresh_from_db()
        assert batch.status == DonationBatch.STATUS_PENDING_QA
