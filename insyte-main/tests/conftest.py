"""Shared pytest fixtures for the INSYTE DMS test suite.

Fixtures defined here are automatically available to all tests
under the ``tests/`` directory without explicit import.

Usage:
    uv run pytest tests/ -v
"""

import uuid
from typing import TYPE_CHECKING

import django
import pytest
from django.test import Client, RequestFactory

if TYPE_CHECKING:
    from core.models import User

# Ensure Django settings are loaded before any test runs.
django.setup()


@pytest.fixture()
def rf() -> RequestFactory:
    """Provide a Django ``RequestFactory`` instance."""
    return RequestFactory()


@pytest.fixture()
def client() -> Client:
    """Provide a Django test ``Client`` instance."""
    return Client()


@pytest.fixture()
def staff_user(db: None) -> User:
    """Create and return a staff user for testing.

    Args:
        db: Pytest-django database access fixture.

    Returns:
        A ``User`` instance with ``is_staff=True``.
    """
    from core.models import User

    return User.objects.create_user(  # type: ignore[attr-defined]
        username=f"staff-{uuid.uuid4().hex[:8]}",
        email="staff@test.insyte.local",
        password="testpass123!",
        is_staff=True,
    )


@pytest.fixture()
def authenticated_client(client: Client, staff_user: User) -> Client:
    """Return a Django test client logged in as a staff user.

    Args:
        client: Django test client fixture.
        staff_user: Staff user fixture.

    Returns:
        An authenticated ``Client`` instance.
    """
    client.force_login(staff_user)
    return client


@pytest.fixture()
def redaction_required_all(db: None) -> RedactionSettings:
    """Set the singleton so every payment method requires redaction.

    Replaces the prior ``@override_settings(REQUIRE_MANUAL_REDACTION=True)``
    decorator. Returns the saved singleton so tests can assert on it.
    """
    del db
    from scans.models import PAYMENT_METHOD_REDACTION_FIELD_MAP, RedactionSettings

    obj = RedactionSettings.get_settings()
    for field in PAYMENT_METHOD_REDACTION_FIELD_MAP.values():
        setattr(obj, field, True)
    obj.save()
    return obj


@pytest.fixture()
def redaction_required_none(db: None) -> RedactionSettings:
    """Set the singleton so no payment method requires redaction."""
    del db
    from scans.models import PAYMENT_METHOD_REDACTION_FIELD_MAP, RedactionSettings

    obj = RedactionSettings.get_settings()
    for field in PAYMENT_METHOD_REDACTION_FIELD_MAP.values():
        setattr(obj, field, False)
    obj.save()
    return obj


if TYPE_CHECKING:
    from scans.models import RedactionSettings
