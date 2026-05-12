"""Tests for auth_app/views/profile.py — user profile and password management."""

import pytest
from django.test import Client


@pytest.mark.django_db()
class TestUserProfileView:
    """Tests for the user_profile view."""

    def test_unauthenticated_redirects(self) -> None:
        client = Client()
        response = client.get("/auth/profile/")
        assert response.status_code == 302
        assert "next=/auth/profile/" in response["Location"]

    def test_get_renders_profile(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/auth/profile/")
        assert response.status_code == 200

    def test_post_update_profile_success(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.post(
            "/auth/profile/",
            {
                "first_name": "Alice",
                "last_name": "Smith",
                "email": "alice@example.com",
            },
        )
        # Profile view renders (200) on success (no separate redirect)
        assert response.status_code == 200

    def test_post_missing_email_shows_error(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.post(
            "/auth/profile/",
            {"first_name": "Alice", "last_name": "Smith", "email": ""},
        )
        assert response.status_code == 302

    def test_post_updates_name_fields(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        from django.contrib.auth import get_user_model

        User = get_user_model()
        authenticated_client.post(
            "/auth/profile/",
            {
                "first_name": "Bob",
                "last_name": "Jones",
                "email": "bob@example.com",
            },
        )
        # after post: user should be updated
        from django.contrib.auth import get_user_model

        user = User.objects.get(email="bob@example.com")
        assert user.first_name == "Bob"
        assert user.last_name == "Jones"

    def test_profile_context_has_user_data(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/auth/profile/")
        assert response.status_code == 200
        assert "user_data" in response.context

    def test_photo_only_post_does_not_require_email(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        """Posting only `profile_image` (the auto-submit photo flow) must
        not trigger the 'Email is required' validation. Regression test
        for QA bug 28-04-2026 / 01-05-2026."""
        from io import BytesIO

        from django.contrib.auth import get_user_model
        from django.core.files.uploadedfile import SimpleUploadedFile

        User = get_user_model()
        # Pin known values so we can assert they survive the photo-only POST.
        user_before = User.objects.get(pk=staff_user.pk)  # type: ignore[attr-defined]
        user_before.email = "before@example.com"
        user_before.first_name = "Original"
        user_before.last_name = "Name"
        user_before.save()
        original_email = user_before.email

        # Minimal valid PNG (1x1 transparent)
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\x00\x01"
            b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        upload = SimpleUploadedFile(
            "avatar.png", BytesIO(png_bytes).getvalue(), content_type="image/png"
        )
        response = authenticated_client.post(
            "/auth/profile/",
            {"profile_image": upload},
        )
        # Photo-only POST renders the page (200) — does NOT redirect with an
        # "Email is required" error (which would be a 302).
        assert response.status_code == 200

        user_after = User.objects.get(pk=staff_user.pk)  # type: ignore[attr-defined]
        assert user_after.email == original_email
        assert user_after.first_name == "Original"
        assert user_after.last_name == "Name"
        assert bool(user_after.profile_image)

    def test_remove_photo_only_does_not_require_email(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        """The 'Remove photo' submit (also no email field) is treated as a
        photo-only POST."""
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user_before = User.objects.get(pk=staff_user.pk)  # type: ignore[attr-defined]
        user_before.email = "preserve@example.com"
        user_before.save()
        original_email = user_before.email

        response = authenticated_client.post(
            "/auth/profile/", {"remove_profile_image": "1"}
        )
        assert response.status_code == 200

        user_after = User.objects.get(pk=staff_user.pk)  # type: ignore[attr-defined]
        assert user_after.email == original_email


@pytest.mark.django_db()
class TestChangePasswordView:
    """Tests for the change_password view."""

    def test_unauthenticated_redirects(self) -> None:
        client = Client()
        response = client.get("/auth/change-password/")
        assert response.status_code == 302
        assert "next=/auth/change-password/" in response["Location"]

    def test_get_renders_form(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.get("/auth/change-password/")
        assert response.status_code == 200

    def test_post_wrong_current_password_redirects(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.post(
            "/auth/change-password/",
            {
                "current_password": "wrong_password",
                "new_password": "newPass123!",
                "confirm_password": "newPass123!",
            },
        )
        assert response.status_code == 302

    def test_post_empty_new_password_redirects(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.post(
            "/auth/change-password/",
            {
                "current_password": "testpass123!",
                "new_password": "",
                "confirm_password": "",
            },
        )
        assert response.status_code == 302

    def test_post_mismatched_passwords_redirects(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.post(
            "/auth/change-password/",
            {
                "current_password": "testpass123!",
                "new_password": "newPass123!",
                "confirm_password": "differentPass123!",
            },
        )
        assert response.status_code == 302

    def test_post_short_password_redirects(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.post(
            "/auth/change-password/",
            {
                "current_password": "testpass123!",
                "new_password": "short",
                "confirm_password": "short",
            },
        )
        assert response.status_code == 302

    def test_post_valid_change_redirects_to_profile(
        self, authenticated_client: Client, staff_user: object
    ) -> None:
        response = authenticated_client.post(
            "/auth/change-password/",
            {
                "current_password": "testpass123!",
                "new_password": "NewSecurePass99!",
                "confirm_password": "NewSecurePass99!",
            },
        )
        # Should redirect to profile on success
        assert response.status_code == 302
        assert "/auth/profile/" in response["Location"]
