"""Playwright-specific fixtures for E2E tests.

These fixtures supplement the project-level ``conftest.py``
with browser-based helpers.
"""

import os
import uuid
from typing import TYPE_CHECKING

import pytest
from django.test import Client as DjangoTestClient
from playwright.sync_api import Page

from auth_app.models import EmailDevice
from clients.models import Client, ClientPortalUser
from core.models import User

if TYPE_CHECKING:
    from django.test.testcases import LiveServer


os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip browser E2E when Chromium is not installed (CI / fresh clones).

    Install with: ``uv run playwright install chromium``
    Force-skip with env: ``SKIP_PLAYWRIGHT_E2E=1``.
    """
    e2e_items = [
        item for item in items if "/tests/e2e/" in str(item.path).replace("\\", "/")
    ]
    if not e2e_items:
        return
    if os.environ.get("SKIP_PLAYWRIGHT_E2E", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        skip = pytest.mark.skip(reason="SKIP_PLAYWRIGHT_E2E is set")
        for item in e2e_items:
            item.add_marker(skip)
        return
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception:
        skip = pytest.mark.skip(
            reason="Playwright Chromium not available (uv run playwright install chromium)"
        )
        for item in e2e_items:
            item.add_marker(skip)


@pytest.fixture()
def live_base_url(live_server: LiveServer) -> str:
    """Base URL for the local development server.

    Override with ``--base-url`` CLI flag when running against
    a different host (e.g. staging).
    Scope is 'session' to satisfy pytest-playwright's pytest_base_url plugin
    which requests this fixture at session scope.
    """
    return live_server.url


@pytest.fixture()
def login_page(page: Page, live_base_url: str) -> Page:
    """Navigate to the login page and return the Page object.

    Args:
        page: Playwright page fixture.
        live_base_url: Development server URL.

    Returns:
        A ``Page`` already on the login screen.
    """
    page.goto(f"{live_base_url}/auth/login/")
    return page


def _authenticate_page_for_user(
    page: Page,
    live_base_url: str,
    user: User,
    otp_device: EmailDevice,
) -> None:
    """Attach an authenticated + OTP-verified session cookie to Playwright page.

    Args:
        page: Playwright browser page.
        live_base_url: Base URL for live Django test server.
        user: User to authenticate.
        otp_device: Confirmed OTP device for OTP verification session marker.
    """
    django_client = DjangoTestClient()
    django_client.force_login(user)
    session = django_client.session
    session["otp_device_id"] = otp_device.persistent_id
    session.save()

    session_cookie_value = django_client.cookies["sessionid"].value
    page.context.add_cookies(
        [
            {
                "name": "sessionid",
                "value": session_cookie_value,
                "url": live_base_url,
            }
        ]
    )


@pytest.fixture()
def staff_user_with_2fa(db: None) -> tuple[User, EmailDevice]:
    """Create a staff user with a confirmed email OTP device.

    Args:
        db: Pytest-django DB fixture.

    Returns:
        Tuple of created staff user and confirmed email OTP device.
    """
    staff_user = User.objects.create_user(  # type: ignore[attr-defined]
        username=f"staff-e2e-{uuid.uuid4().hex[:8]}",
        email=f"staff-e2e-{uuid.uuid4().hex[:8]}@example.com",
        password="e2e-pass-123!",
        is_staff=True,
    )
    otp_device = EmailDevice.objects.create(
        user=staff_user,
        name="e2e-staff-device",
        confirmed=True,
    )
    return staff_user, otp_device


@pytest.fixture()
def client_portal_user_with_2fa(db: None) -> tuple[User, EmailDevice]:
    """Create an active client portal user with confirmed email OTP device.

    Args:
        db: Pytest-django DB fixture.

    Returns:
        Tuple of created client portal user and confirmed email OTP device.
    """
    client = Client.objects.create(name=f"E2E Client {uuid.uuid4().hex[:8]}")
    portal_user = User.objects.create_user(  # type: ignore[attr-defined]
        username=f"portal-e2e-{uuid.uuid4().hex[:8]}",
        email=f"portal-e2e-{uuid.uuid4().hex[:8]}@example.com",
        password="e2e-pass-123!",
        is_staff=False,
    )
    ClientPortalUser.objects.create(
        client=client,
        user=portal_user,
        role="viewer",
        is_active=True,
    )
    otp_device = EmailDevice.objects.create(
        user=portal_user,
        name="e2e-portal-device",
        confirmed=True,
    )
    return portal_user, otp_device


@pytest.fixture()
def staff_authenticated_page(
    page: Page,
    live_base_url: str,
    staff_user_with_2fa: tuple[User, EmailDevice],
) -> Page:
    """Return a browser page authenticated as OTP-verified staff user.

    Args:
        page: Playwright page fixture.
        live_base_url: Base URL for live Django test server.
        staff_user_with_2fa: Tuple containing user and OTP device.

    Returns:
        Authenticated Playwright page.
    """
    staff_user, otp_device = staff_user_with_2fa
    _authenticate_page_for_user(page, live_base_url, staff_user, otp_device)
    return page


@pytest.fixture()
def client_authenticated_page(
    page: Page,
    live_base_url: str,
    client_portal_user_with_2fa: tuple[User, EmailDevice],
) -> Page:
    """Return a browser page authenticated as OTP-verified client portal user.

    Args:
        page: Playwright page fixture.
        live_base_url: Base URL for live Django test server.
        client_portal_user_with_2fa: Tuple containing user and OTP device.

    Returns:
        Authenticated Playwright page.
    """
    portal_user, otp_device = client_portal_user_with_2fa
    _authenticate_page_for_user(page, live_base_url, portal_user, otp_device)
    return page
