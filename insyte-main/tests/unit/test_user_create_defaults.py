"""Tests for custom-admin user create / edit defaults and stranding guard."""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from tests.factories import UserFactory


@pytest.mark.django_db()
class TestHandleUserCreate:
    """Tests for ``_handle_user_create`` — the custom-admin create flow."""

    def test_new_user_is_always_active_and_staff(self) -> None:
        admin = UserFactory(is_staff=True, is_superuser=True, is_active=True)
        admin.is_verified = lambda: True  # type: ignore[method-assign]
        client = Client()
        client.force_login(admin)

        response = client.post(
            reverse("custom_admin:admin_user_management"),
            {
                "action": "create",
                "username": "newly-created",
                "email": "newly-created@test.insyte.local",
                "first_name": "Newly",
                "last_name": "Created",
                "password1": "Sup3rSecurePass!xyz",
                "password2": "Sup3rSecurePass!xyz",
            },
        )
        assert response.status_code == 302

        User = get_user_model()
        created = User.objects.get(username="newly-created")
        assert created.is_active is True
        assert created.is_staff is True

    def test_new_user_is_flagged_must_change_password(self) -> None:
        admin = UserFactory(is_staff=True, is_superuser=True, is_active=True)
        admin.is_verified = lambda: True  # type: ignore[method-assign]
        client = Client()
        client.force_login(admin)

        client.post(
            reverse("custom_admin:admin_user_management"),
            {
                "action": "create",
                "username": "must-change",
                "email": "must-change@test.insyte.local",
                "first_name": "Must",
                "last_name": "Change",
                "password1": "Sup3rSecurePass!xyz",
                "password2": "Sup3rSecurePass!xyz",
            },
        )

        User = get_user_model()
        created = User.objects.get(username="must-change")
        assert created.must_change_password is True

    def test_new_user_created_by_is_populated(self) -> None:
        admin = UserFactory(is_staff=True, is_superuser=True, is_active=True)
        admin.is_verified = lambda: True  # type: ignore[method-assign]
        client = Client()
        client.force_login(admin)

        client.post(
            reverse("custom_admin:admin_user_management"),
            {
                "action": "create",
                "username": "tracked-creator",
                "email": "tracked-creator@test.insyte.local",
                "first_name": "Tracked",
                "last_name": "Creator",
                "password1": "Sup3rSecurePass!xyz",
                "password2": "Sup3rSecurePass!xyz",
            },
        )

        User = get_user_model()
        created = User.objects.get(username="tracked-creator")
        assert created.created_by_id == admin.id

    def test_mismatched_passwords_rejected(self) -> None:
        admin = UserFactory(is_staff=True, is_superuser=True, is_active=True)
        admin.is_verified = lambda: True  # type: ignore[method-assign]
        client = Client()
        client.force_login(admin)

        client.post(
            reverse("custom_admin:admin_user_management"),
            {
                "action": "create",
                "username": "bad-pwd",
                "email": "bad-pwd@test.insyte.local",
                "first_name": "Bad",
                "last_name": "Pwd",
                "password1": "Sup3rSecurePass!xyz",
                "password2": "Different!xyz",
            },
        )

        User = get_user_model()
        assert not User.objects.filter(username="bad-pwd").exists()


@pytest.mark.django_db()
class TestHandleUserEditStrandingGuard:
    """Tests for the stranding guard in ``_handle_user_edit``."""

    def test_cannot_demote_last_access_source(self) -> None:
        admin = UserFactory(is_staff=True, is_superuser=True, is_active=True)
        admin.is_verified = lambda: True  # type: ignore[method-assign]
        target = UserFactory(is_staff=True, is_active=True)
        client = Client()
        client.force_login(admin)

        response = client.post(
            reverse("custom_admin:admin_user_management"),
            {
                "action": "edit",
                "user_id": str(target.id),
                "email": target.email,
                "first_name": target.first_name,
                # is_staff checkbox intentionally omitted, no groups, no portal
            },
        )
        assert response.status_code == 302

        target.refresh_from_db()
        assert target.is_staff is True
