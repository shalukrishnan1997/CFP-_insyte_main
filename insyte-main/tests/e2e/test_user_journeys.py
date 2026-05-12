"""End-to-end user journey tests using Playwright.

These tests validate high-value user flows across authentication guards,
staff admin access, client portal access, and role-based route boundaries.
"""

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.django_db(transaction=True)
def test_unauthenticated_user_redirected_to_login_for_admin_route(
    page: Page,
    live_base_url: str,
) -> None:
    """Verify unauthenticated users cannot access admin dashboard directly.

    Args:
        page: Playwright page fixture.
        live_base_url: Base URL for live Django test server.
    """
    page.goto(f"{live_base_url}/admin/")

    expect(page).to_have_url(f"{live_base_url}/auth/login/")
    expect(page.locator("#id_username")).to_be_visible()
    expect(page.get_by_role("button", name="Sign In")).to_be_visible()


@pytest.mark.django_db(transaction=True)
def test_staff_user_admin_journey_core_pages(
    staff_authenticated_page: Page,
    live_base_url: str,
) -> None:
    """Validate key staff user journey across core admin pages.

    Args:
        staff_authenticated_page: Authenticated/verified staff page fixture.
        live_base_url: Base URL for live Django test server.
    """
    staff_authenticated_page.goto(f"{live_base_url}/admin/")
    expect(staff_authenticated_page).to_have_url(f"{live_base_url}/admin/")
    expect(
        staff_authenticated_page.get_by_role("heading", name="Admin Dashboard")
    ).to_be_visible()

    staff_authenticated_page.goto(f"{live_base_url}/admin/campaigns/")
    expect(staff_authenticated_page).to_have_url(
        re.compile(rf"{re.escape(live_base_url)}/admin/campaigns/.*")
    )

    staff_authenticated_page.goto(f"{live_base_url}/admin/reports/")
    expect(staff_authenticated_page).to_have_url(f"{live_base_url}/admin/reports/")

    staff_authenticated_page.goto(f"{live_base_url}/admin/settings/")
    expect(staff_authenticated_page).to_have_url(f"{live_base_url}/admin/settings/")


@pytest.mark.django_db(transaction=True)
def test_client_portal_user_journey_core_pages(
    client_authenticated_page: Page,
    live_base_url: str,
) -> None:
    """Validate key client portal user journey pages.

    Args:
        client_authenticated_page: Authenticated/verified client page fixture.
        live_base_url: Base URL for live Django test server.
    """
    client_authenticated_page.goto(f"{live_base_url}/client/")
    expect(client_authenticated_page).to_have_url(f"{live_base_url}/client/")
    expect(
        client_authenticated_page.get_by_role("heading", name="Dashboard")
    ).to_be_visible()

    client_authenticated_page.goto(f"{live_base_url}/client/reports/")
    expect(client_authenticated_page).to_have_url(f"{live_base_url}/client/reports/")
    expect(
        client_authenticated_page.get_by_role("heading", name="Reports & Analytics")
    ).to_be_visible()


@pytest.mark.django_db(transaction=True)
def test_role_based_route_boundaries(
    client_authenticated_page: Page,
    live_base_url: str,
) -> None:
    """Ensure staff/client users are redirected from unauthorized route namespaces.

    Args:
        client_authenticated_page: Authenticated/verified client page fixture.
        live_base_url: Base URL for live Django test server.
    """
    client_authenticated_page.goto(f"{live_base_url}/admin/")
    expect(client_authenticated_page).to_have_url(f"{live_base_url}/client/")
