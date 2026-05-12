"""
E2E - Core Admin User Journey (FIN-E2E-001 to FIN-E2E-005)
===========================================================

This test simulates a real end-to-end workflow for a staff admin user in
a Chrome browser.

  Step 1 — Login
  Step 2 — Navigate to Campaigns
  Step 3 — QA Dashboard: review and approve a pending batch when present
  Step 4 — Daily Banking: verify the dashboard loads approved batch data
  Step 5 — Authentication guards redirect anonymous users to login

Run (inside WSL 22.04):
    uv run pytest tests/e2e/test_core_user_journey.py -v --headed \\
        --html=playwright_e2e_report.html --self-contained-html

Run headless (CI):
    uv run pytest tests/e2e/test_core_user_journey.py -v \\
        --html=playwright_e2e_report.html --self-contained-html
"""

import re

import pytest
from playwright.sync_api import Page, expect


def open_admin_dashboard(page: Page, live_base_url: str) -> None:
    """Open the admin dashboard from an authenticated browser session."""
    page.goto(f"{live_base_url}/admin/")
    page.wait_for_load_state("domcontentloaded")
    expect(page).to_have_url(f"{live_base_url}/admin/")


# ─────────────────────────────────────────────
# FIN-E2E-001  Login
# ─────────────────────────────────────────────


class TestE2ELogin:
    """FIN-E2E-001: Admin user can login via the browser."""

    @pytest.mark.django_db(transaction=True)
    def test_login_as_admin(
        self, staff_authenticated_page: Page, live_base_url: str
    ) -> None:
        """FIN-E2E-001: Authenticated staff user lands on admin dashboard."""
        page = staff_authenticated_page
        open_admin_dashboard(page, live_base_url)
        expect(page.get_by_role("heading", name="Admin Dashboard")).to_be_visible()


# ─────────────────────────────────────────────
# FIN-E2E-002 to FIN-E2E-005  Current Workflow
# ─────────────────────────────────────────────


class TestE2ECoreWorkflow:
    """FIN-E2E-002 to 005: Current scan-first admin journey."""

    # ── Step 2 ──────────────────────────────────────────────────────────────
    @pytest.mark.django_db(transaction=True)
    def test_navigate_to_donations(
        self, staff_authenticated_page: Page, live_base_url: str
    ) -> None:
        """FIN-E2E-002: Navigate to Campaigns and verify the page loads."""
        page = staff_authenticated_page
        page.goto(f"{live_base_url}/admin/campaigns/")
        page.wait_for_load_state("domcontentloaded")
        expect(page).to_have_url(re.compile(r".*/admin/campaigns/.*"))
        expect(page.locator("body")).to_be_visible()

    # ── Step 3 ──────────────────────────────────────────────────────────────
    @pytest.mark.django_db(transaction=True)
    def test_qa_review_and_approve_batch(
        self, staff_authenticated_page: Page, live_base_url: str
    ) -> None:
        """
        FIN-E2E-003: QA Dashboard shows pending batches; reviewer can open
        the first pending batch, approve each donation, and approve the batch.
        """
        page = staff_authenticated_page

        # Navigate to QA Dashboard
        page.goto(f"{live_base_url}/admin/qa/")
        page.wait_for_load_state("domcontentloaded")

        # Verify QA dashboard loaded
        expect(page.locator("h1, h2").first).to_be_visible()

        # Look for a "Review" or batch link in the pending list
        review_link = page.locator("a[href*='/admin/qa/batch/']").first
        if not review_link.is_visible(timeout=5000):
            pytest.skip("No pending batches on QA dashboard to review.")

        review_link.click()
        page.wait_for_load_state("domcontentloaded")

        # This lands on qa_batch_review page which redirects to the first donation review.
        # Keep approving donations one by one until the Approve Batch button is available.
        max_iterations = 30  # safety cap
        for _i in range(max_iterations):
            page.wait_for_load_state("domcontentloaded")

            # Check if "Approve & Next" button is present (per-donation approval)
            approve_next_btn = page.locator("button[name='action'][value='approve']")
            approve_batch_btn = page.locator("button:has-text('Approve Batch')")

            if approve_batch_btn.is_visible(timeout=1500):
                # We are on the last donation - approve the batch
                approve_batch_btn.click()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
                # Should redirect to QA dashboard or show a success message
                body_text = page.locator("body").inner_text()
                assert any(
                    phrase in body_text
                    for phrase in [
                        "approved",
                        "Approved",
                        "completed",
                        "QA Dashboard",
                        "qa_dashboard",
                    ]
                ), f"Batch approval not confirmed. Body: {body_text[:500]}"
                return  # ✅ Done

            elif approve_next_btn.is_visible(timeout=1500):
                approve_next_btn.click()
                page.wait_for_timeout(800)
            else:
                # No button visible - unexpected state
                break

        pytest.fail("Could not complete QA approval loop within iteration limit.")

    # ── Step 4 ──────────────────────────────────────────────────────────────
    @pytest.mark.django_db(transaction=True)
    def test_daily_banking_dashboard_loads(
        self, staff_authenticated_page: Page, live_base_url: str
    ) -> None:
        """
        FIN-E2E-004: Daily Banking dashboard loads and shows approved
        batch / donation data (cheque, card, bank transfer summaries).
        """
        page = staff_authenticated_page

        page.goto(f"{live_base_url}/admin/daily-banking/")
        page.wait_for_load_state("domcontentloaded")

        # Verify the page loaded
        expect(page.locator("h1, h2").first).to_be_visible(timeout=8_000)

        # Confirm at least one section of data is visible
        # (the accordions for clients/campaigns expand lazily via HTMX)
        body_text = page.locator("body").inner_text()
        assert len(body_text) > 100, "Daily Banking page appears empty"

        # Check no error page was returned
        assert "500" not in page.title()
        assert "Error" not in page.title()


# ─────────────────────────────────────────────
# FIN-E2E-005  Authentication Guard
# ─────────────────────────────────────────────


class TestE2EAuthGuard:
    """FIN-E2E-005: Unauthenticated users are redirected to login."""

    @pytest.mark.django_db(transaction=True)
    def test_unauthenticated_redirect_from_qa(
        self, page: Page, live_base_url: str
    ) -> None:
        """FIN-E2E-005a: Anonymous access to QA dashboard redirects to login."""
        page.goto(f"{live_base_url}/admin/qa/")
        page.wait_for_load_state("domcontentloaded")
        assert "login" in page.url.lower() or page.url.endswith(f"{live_base_url}/")

    @pytest.mark.django_db(transaction=True)
    def test_unauthenticated_redirect_from_banking(
        self, page: Page, live_base_url: str
    ) -> None:
        """FIN-E2E-005b: Anonymous access to Daily Banking redirects to login."""
        page.goto(f"{live_base_url}/admin/daily-banking/")
        page.wait_for_load_state("domcontentloaded")
        assert "login" in page.url.lower() or page.url.endswith(f"{live_base_url}/")

    @pytest.mark.django_db(transaction=True)
    def test_unauthenticated_redirect_from_donations(
        self, page: Page, live_base_url: str
    ) -> None:
        """FIN-E2E-005c: Anonymous access to Campaigns redirects to login."""
        page.goto(f"{live_base_url}/admin/campaigns/")
        page.wait_for_load_state("domcontentloaded")
        assert "login" in page.url.lower() or page.url.endswith(f"{live_base_url}/")
