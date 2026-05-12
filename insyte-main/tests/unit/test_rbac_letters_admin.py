"""RBAC tests for the letters admin surface (``/admin/letter-setup/*``).

Covers:
    - Anonymous requests redirect to the login page (302).
    - Authenticated client portal users are blocked from ``/admin/*`` by
      ``ClientPortalMiddleware`` (302 to the client portal dashboard).
    - Staff users with confirmed 2FA reach the views.
    - Path-traversal-shaped filenames on ``download_letter`` resolve to 404.
    - Cross-batch ``download_batch_file`` indexing is bounded by the batch's
      own ``output_files`` list.
    - ``letter_task_status`` accepts arbitrary task IDs but returns benign
      ``PENDING`` state (documented behavior — task IDs are UUIDs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import Client
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from clients.models import ClientPortalUser
from letters.models import LetterBatch, LetterTemplate
from tests.factories import CampaignFactory, ClientFactory, UserFactory

if TYPE_CHECKING:
    from core.models import User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _login_with_2fa(client: Client, user: User) -> None:
    """Force-login *user* and mark their session as 2FA-verified.

    ``ClientPortalMiddleware`` enforces mandatory 2FA on every authenticated
    request. Tests that hit ``/admin/*`` need a confirmed TOTP device plus the
    ``DEVICE_ID_SESSION_KEY`` set in the session.
    """
    device = TOTPDevice.objects.create(user=user, name="rbac-test", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()


def _make_staff_user() -> User:
    """Return a fresh staff user."""
    return UserFactory(is_staff=True)


def _make_portal_user() -> User:
    """Return a fresh non-staff user attached to a client portal profile."""
    user = UserFactory(is_staff=False)
    ClientPortalUser.objects.create(
        client=ClientFactory(),
        user=user,
        role="viewer",
        is_active=True,
    )
    return user


def _make_batch() -> tuple[LetterBatch, User]:
    """Create a ``LetterBatch`` plus its owning staff user."""
    user = UserFactory(is_staff=True)
    campaign = CampaignFactory(created_by=user)
    template = LetterTemplate.objects.create(
        name="Thanks",
        campaign=campaign,
        created_by=user,
    )
    template.file.save("template.docx", ContentFile(b"docx-bytes"), save=True)
    batch = LetterBatch.objects.create(
        campaign=campaign,
        template=template,
        batch_number=1,
        created_by=user,
    )
    return batch, user


# ---------------------------------------------------------------------------
# Endpoint catalog used by the GET-RBAC matrix
# ---------------------------------------------------------------------------


def _campaign_url(name: str, campaign_id: object) -> str:
    return reverse(f"custom_admin:{name}", kwargs={"campaign_id": campaign_id})


def _batch_url(name: str, batch_id: object) -> str:
    return reverse(f"custom_admin:{name}", kwargs={"batch_id": batch_id})


@pytest.mark.django_db()
class TestAnonymousAccess:
    """Anonymous requests to ``/admin/letter-setup/*`` redirect to login."""

    def test_letter_print_console_anon_redirects(self, client: Client) -> None:
        response = client.get(reverse("custom_admin:letter_print_console"))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_letter_setup_campaign_anon_redirects(self, client: Client) -> None:
        campaign = CampaignFactory()
        response = client.get(_campaign_url("letter_setup_campaign", campaign.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_add_letter_template_anon_redirects(self, client: Client) -> None:
        campaign = CampaignFactory()
        response = client.post(_campaign_url("add_letter_template", campaign.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_serve_reference_guide_anon_redirects(self, client: Client) -> None:
        response = client.get(reverse("custom_admin:letter_reference_guide"))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_preview_active_template_anon_redirects(self, client: Client) -> None:
        campaign = CampaignFactory()
        url = reverse(
            "custom_admin:preview_active_template",
            kwargs={
                "campaign_id": campaign.id,
                "template_type": LetterTemplate.TEMPLATE_TYPE_THANK_YOU,
            },
        )
        response = client.get(url)
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_generate_letter_anon_redirects(self, client: Client) -> None:
        campaign = CampaignFactory()
        response = client.post(_campaign_url("generate_letter", campaign.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_view_generated_letters_anon_redirects(self, client: Client) -> None:
        campaign = CampaignFactory()
        response = client.get(_campaign_url("view_generated_letters", campaign.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_download_letter_anon_redirects(self, client: Client) -> None:
        campaign = CampaignFactory()
        url = reverse(
            "custom_admin:download_letter",
            kwargs={"campaign_id": campaign.id, "filename": "missing.docx"},
        )
        response = client.get(url)
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_letter_task_status_anon_redirects(self, client: Client) -> None:
        url = reverse("custom_admin:letter_task_status", kwargs={"task_id": "abc-123"})
        response = client.get(url)
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_letter_batches_anon_redirects(self, client: Client) -> None:
        campaign = CampaignFactory()
        response = client.get(_campaign_url("letter_batches", campaign.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_letter_batch_detail_anon_redirects(self, client: Client) -> None:
        batch, _user = _make_batch()
        response = client.get(_batch_url("letter_batch_detail", batch.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_letter_batch_status_api_anon_redirects(self, client: Client) -> None:
        batch, _user = _make_batch()
        response = client.get(_batch_url("letter_batch_status_api", batch.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_cancel_batch_anon_redirects(self, client: Client) -> None:
        batch, _user = _make_batch()
        response = client.post(_batch_url("cancel_batch", batch.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_download_batch_file_anon_redirects(self, client: Client) -> None:
        batch, _user = _make_batch()
        url = reverse(
            "custom_admin:download_batch_file",
            kwargs={"batch_id": batch.id, "file_index": 0},
        )
        response = client.get(url)
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]

    def test_reset_failed_letters_anon_redirects(self, client: Client) -> None:
        campaign = CampaignFactory()
        response = client.post(_campaign_url("reset_failed_letters", campaign.id))
        assert response.status_code == 302
        assert "/auth/login" in response["Location"]


@pytest.mark.django_db()
class TestClientPortalUserBlocked:
    """A non-staff client-portal user must not reach ``/admin/letter-setup/*``."""

    def _portal_client(self) -> Client:
        client = Client()
        portal_user = _make_portal_user()
        _login_with_2fa(client, portal_user)
        return client

    def test_portal_user_blocked_from_letter_print_console(self) -> None:
        response = self._portal_client().get(
            reverse("custom_admin:letter_print_console")
        )
        assert response.status_code == 302
        assert "/admin/" not in response["Location"]

    def test_portal_user_blocked_from_letter_batches(self) -> None:
        campaign = CampaignFactory()
        response = self._portal_client().get(
            _campaign_url("letter_batches", campaign.id)
        )
        assert response.status_code == 302
        assert "/admin/" not in response["Location"]

    def test_portal_user_blocked_from_download_letter(self) -> None:
        campaign = CampaignFactory()
        url = reverse(
            "custom_admin:download_letter",
            kwargs={"campaign_id": campaign.id, "filename": "x.docx"},
        )
        response = self._portal_client().get(url)
        assert response.status_code == 302
        assert "/admin/" not in response["Location"]

    def test_portal_user_blocked_from_download_batch_file(self) -> None:
        batch, _user = _make_batch()
        url = reverse(
            "custom_admin:download_batch_file",
            kwargs={"batch_id": batch.id, "file_index": 0},
        )
        response = self._portal_client().get(url)
        assert response.status_code == 302
        assert "/admin/" not in response["Location"]

    def test_portal_user_blocked_from_letter_task_status(self) -> None:
        url = reverse(
            "custom_admin:letter_task_status", kwargs={"task_id": uuid4().hex}
        )
        response = self._portal_client().get(url)
        assert response.status_code == 302
        assert "/admin/" not in response["Location"]


@pytest.mark.django_db()
class TestStaffAccess:
    """Staff users with confirmed 2FA reach the letter admin views."""

    def _staff_client(self) -> tuple[Client, User]:
        client = Client()
        staff = _make_staff_user()
        _login_with_2fa(client, staff)
        return client, staff

    def test_staff_reaches_letter_print_console(self) -> None:
        client, _ = self._staff_client()
        response = client.get(reverse("custom_admin:letter_print_console"))
        assert response.status_code == 200

    def test_staff_reaches_letter_setup_campaign(self) -> None:
        client, _ = self._staff_client()
        campaign = CampaignFactory()
        response = client.get(_campaign_url("letter_setup_campaign", campaign.id))
        assert response.status_code == 200

    def test_staff_reaches_letter_batches(self) -> None:
        client, _ = self._staff_client()
        campaign = CampaignFactory()
        response = client.get(_campaign_url("letter_batches", campaign.id))
        assert response.status_code == 200

    def test_staff_reaches_letter_batch_detail(self) -> None:
        client, _ = self._staff_client()
        batch, _user = _make_batch()
        response = client.get(_batch_url("letter_batch_detail", batch.id))
        assert response.status_code == 200

    def test_staff_reaches_letter_batch_status_api(self) -> None:
        client, _ = self._staff_client()
        batch, _user = _make_batch()
        response = client.get(_batch_url("letter_batch_status_api", batch.id))
        assert response.status_code == 200

    def test_staff_reaches_letter_task_status(self) -> None:
        client, _ = self._staff_client()
        url = reverse(
            "custom_admin:letter_task_status", kwargs={"task_id": uuid4().hex}
        )
        response = client.get(url)
        assert response.status_code == 200

    def test_staff_reaches_view_generated_letters(self) -> None:
        client, _ = self._staff_client()
        campaign = CampaignFactory()
        response = client.get(_campaign_url("view_generated_letters", campaign.id))
        assert response.status_code == 200

    def test_staff_post_only_endpoints_redirect_on_get(self) -> None:
        """``cancel_batch`` / ``reset_failed_letters`` reject GET; staff still pass RBAC.

        These views check the request method *after* the staff decorator, so
        a GET should return 405 (not 302 to login).
        """
        client, _ = self._staff_client()
        batch, _user = _make_batch()

        cancel_response = client.get(_batch_url("cancel_batch", batch.id))
        assert cancel_response.status_code == 405

        reset_response = client.get(
            _campaign_url("reset_failed_letters", batch.campaign_id)
        )
        assert reset_response.status_code == 405

    def test_staff_get_on_generate_letter_redirects_to_setup(self) -> None:
        """``generate_letter`` redirects GET to the campaign workspace (not login)."""
        client, _ = self._staff_client()
        campaign = CampaignFactory()
        response = client.get(_campaign_url("generate_letter", campaign.id))
        assert response.status_code == 302
        assert f"/admin/letter-setup/campaign/{campaign.id}/" in response["Location"]

    def test_staff_get_on_add_letter_template_redirects_to_setup(self) -> None:
        client, _ = self._staff_client()
        campaign = CampaignFactory()
        response = client.get(_campaign_url("add_letter_template", campaign.id))
        assert response.status_code == 302
        assert f"/admin/letter-setup/campaign/{campaign.id}/" in response["Location"]


@pytest.mark.django_db()
class TestDownloadPathTraversal:
    """``download_letter`` must not escape its ``generated_letters/<campaign>/`` prefix.

    The view normalizes ``generated_letters/{campaign_id}/{filename}`` via
    ``normalize_media_storage_name``, which strips ``..`` segments. Any
    traversal-shaped filename should resolve to 404 — never serve a file
    under a different campaign's prefix.
    """

    def test_dotdot_filename_returns_404(self) -> None:
        client = Client()
        staff = _make_staff_user()
        _login_with_2fa(client, staff)

        own_campaign = CampaignFactory(created_by=staff)
        other_campaign = CampaignFactory()

        # Plant a real file under the *other* campaign's prefix.
        target_name = f"generated_letters/{other_campaign.id}/secret.docx"
        default_storage.save(target_name, ContentFile(b"top-secret"))

        try:
            traversal_filename = f"../{other_campaign.id}/secret.docx"
            url = reverse(
                "custom_admin:download_letter",
                kwargs={
                    "campaign_id": own_campaign.id,
                    "filename": traversal_filename,
                },
            )
            response = client.get(url)
            assert response.status_code == 404
        finally:
            default_storage.delete(target_name)

    def test_absolute_filename_returns_404(self) -> None:
        """An absolute path used as ``filename`` must not be served."""
        client = Client()
        staff = _make_staff_user()
        _login_with_2fa(client, staff)

        own_campaign = CampaignFactory(created_by=staff)
        url = reverse(
            "custom_admin:download_letter",
            kwargs={
                "campaign_id": own_campaign.id,
                "filename": "/etc/passwd",
            },
        )
        response = client.get(url)
        assert response.status_code == 404

    def test_missing_filename_returns_404(self) -> None:
        client = Client()
        staff = _make_staff_user()
        _login_with_2fa(client, staff)

        own_campaign = CampaignFactory(created_by=staff)
        url = reverse(
            "custom_admin:download_letter",
            kwargs={
                "campaign_id": own_campaign.id,
                "filename": "does-not-exist.docx",
            },
        )
        response = client.get(url)
        assert response.status_code == 404


@pytest.mark.django_db()
class TestDownloadBatchFileBounds:
    """``download_batch_file`` indexes the batch's own ``output_files`` list."""

    def test_out_of_range_file_index_returns_404(self) -> None:
        client = Client()
        staff = _make_staff_user()
        _login_with_2fa(client, staff)

        batch, _user = _make_batch()
        url = reverse(
            "custom_admin:download_batch_file",
            kwargs={"batch_id": batch.id, "file_index": 99},
        )
        response = client.get(url)
        assert response.status_code == 404

    def test_index_does_not_leak_other_batch_files(self) -> None:
        """Index 0 on batch A must not return batch B's file.

        ``output_files`` is server-controlled per-batch; ensure the view does
        not silently merge or fall back to another batch's storage.
        """
        client = Client()
        staff = _make_staff_user()
        _login_with_2fa(client, staff)

        batch_a, _ = _make_batch()
        batch_b, _ = _make_batch()

        b_storage = (
            f"generated_letters/{batch_b.campaign_id}/letters_b_THANKS_file1.docx"
        )
        default_storage.save(b_storage, ContentFile(b"batch-b-secret"))
        batch_b.output_files = [b_storage]
        batch_b.save(update_fields=["output_files"])

        try:
            # Batch A has *no* output files — request index 0 on A.
            url = reverse(
                "custom_admin:download_batch_file",
                kwargs={"batch_id": batch_a.id, "file_index": 0},
            )
            response = client.get(url)
            assert response.status_code == 404
        finally:
            default_storage.delete(b_storage)


@pytest.mark.django_db()
class TestLetterTaskStatusBenign:
    """``letter_task_status`` reflects ``AsyncResult`` state for any task ID.

    Task IDs are unguessable UUIDs so this is informational, not a Finding —
    the test pins behavior so future regressions stay visible.
    """

    def test_arbitrary_task_id_returns_pending(self) -> None:
        client = Client()
        staff = _make_staff_user()
        _login_with_2fa(client, staff)

        url = reverse(
            "custom_admin:letter_task_status",
            kwargs={"task_id": uuid4().hex},
        )
        response = client.get(url)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] in {"PENDING", "STARTED"}
