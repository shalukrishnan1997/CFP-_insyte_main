"""Tests for the scan-batch dashboard's status filter + pagination.

Covers ``scans.admin_views._apply_dashboard_status_filter`` and
``_get_filtered_scan_batches`` — the helpers that back the
``/admin/scan-processing/`` dashboard.

The previous behaviour was ``[:50]`` and "show everything"; the new
behaviour hides completed and 100%-scanned batches by default
("active"), exposes a ``?status=`` toggle, and paginates via Django
``Paginator``.
"""

from typing import Any

import pytest

from scans.admin_views import (
    DEFAULT_DASHBOARD_PER_PAGE,
    _apply_dashboard_status_filter,
    _get_filtered_scan_batches,
    _scan_batch_queryset,
)
from scans.models import ScanBatch
from tests.factories import ScanBatchFactory


@pytest.mark.django_db()
class TestApplyDashboardStatusFilter:
    """Unit-level coverage for the queryset filter."""

    def test_active_default_hides_completed_status(self) -> None:
        active = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)
        completed = ScanBatchFactory(status=ScanBatch.STATUS_COMPLETED)

        qs = _apply_dashboard_status_filter(_scan_batch_queryset(), "active")
        ids = set(qs.values_list("id", flat=True))

        assert active.id in ids
        assert completed.id not in ids

    def test_active_default_hides_full_progress_batches(self) -> None:
        full = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=10,
            processed_scans=10,
        )
        partial = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=10,
            processed_scans=4,
        )
        empty = ScanBatchFactory(
            status=ScanBatch.STATUS_PENDING,
            total_scans=0,
            processed_scans=0,
        )

        qs = _apply_dashboard_status_filter(_scan_batch_queryset(), "active")
        ids = set(qs.values_list("id", flat=True))

        assert partial.id in ids
        assert empty.id in ids
        assert full.id not in ids

    def test_completed_filter_returns_finished_or_full_progress(self) -> None:
        completed = ScanBatchFactory(status=ScanBatch.STATUS_COMPLETED)
        full = ScanBatchFactory(
            status=ScanBatch.STATUS_PROCESSING,
            total_scans=5,
            processed_scans=5,
        )
        active = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        qs = _apply_dashboard_status_filter(_scan_batch_queryset(), "completed")
        ids = set(qs.values_list("id", flat=True))

        assert completed.id in ids
        assert full.id in ids
        assert active.id not in ids

    def test_all_filter_returns_everything(self) -> None:
        completed = ScanBatchFactory(status=ScanBatch.STATUS_COMPLETED)
        active = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        qs = _apply_dashboard_status_filter(_scan_batch_queryset(), "all")
        ids = set(qs.values_list("id", flat=True))

        assert completed.id in ids
        assert active.id in ids


@pytest.mark.django_db()
class TestGetFilteredScanBatchesPagination:
    """The ``[:50]`` slice was a silent data-loss bug; verify Paginator."""

    def test_pagination_uses_per_page(self) -> None:
        for _ in range(7):
            ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        batches, page_obj = _get_filtered_scan_batches(
            campaign_id="",
            attention_filter="",
            status_filter="active",
            page=1,
            per_page=3,
        )

        assert len(batches) == 3
        assert page_obj.paginator.count == 7
        assert page_obj.paginator.num_pages == 3

    def test_pagination_returns_requested_page(self) -> None:
        for _ in range(5):
            ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        _, page_obj = _get_filtered_scan_batches(
            campaign_id="",
            attention_filter="",
            status_filter="active",
            page=2,
            per_page=2,
        )

        assert page_obj.number == 2
        assert page_obj.has_previous() is True
        assert page_obj.has_next() is True

    def test_out_of_range_page_clamps_to_last(self) -> None:
        for _ in range(3):
            ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        _, page_obj = _get_filtered_scan_batches(
            campaign_id="",
            attention_filter="",
            status_filter="active",
            page=99,
            per_page=2,
        )

        # ``Paginator.get_page`` clamps an out-of-range page to the
        # last page rather than raising — protects against
        # bookmarked deep links going stale.
        assert page_obj.number == page_obj.paginator.num_pages

    def test_default_filter_drops_completed_batches_above_50(self) -> None:
        # Reproduce the original to-do scenario: dozens of completed
        # batches drowning out a single active one. Active should
        # surface even when the queryset is large.
        for _ in range(60):
            ScanBatchFactory(status=ScanBatch.STATUS_COMPLETED)
        active = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        batches, page_obj = _get_filtered_scan_batches(
            campaign_id="",
            attention_filter="",
            status_filter="active",
            page=1,
            per_page=DEFAULT_DASHBOARD_PER_PAGE,
        )

        ids = [b.id for b in batches]
        assert active.id in ids
        # Only the active batch survived the default filter.
        assert page_obj.paginator.count == 1


@pytest.mark.django_db()
class TestScanProcessingDashboardView:
    """End-to-end view coverage for the ``status`` query param."""

    def test_default_request_uses_active_filter(
        self, authenticated_client: Any, staff_user: object
    ) -> None:
        completed = ScanBatchFactory(status=ScanBatch.STATUS_COMPLETED)
        active = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        response = authenticated_client.get("/admin/scan-processing/")

        assert response.status_code == 200
        ids = [b.id for b in response.context["scan_batches"]]
        assert active.id in ids
        assert completed.id not in ids
        assert response.context["selected_status_filter"] == "active"
        assert response.context["total_batch_count"] == 1

    def test_status_all_includes_completed(
        self, authenticated_client: Any, staff_user: object
    ) -> None:
        completed = ScanBatchFactory(status=ScanBatch.STATUS_COMPLETED)
        active = ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        response = authenticated_client.get("/admin/scan-processing/?status=all")

        assert response.status_code == 200
        ids = [b.id for b in response.context["scan_batches"]]
        assert active.id in ids
        assert completed.id in ids

    def test_invalid_status_param_falls_back_to_active(
        self, authenticated_client: Any, staff_user: object
    ) -> None:
        response = authenticated_client.get("/admin/scan-processing/?status=garbage")

        assert response.status_code == 200
        assert response.context["selected_status_filter"] == "active"

    def test_invalid_page_param_falls_back_to_first_page(
        self, authenticated_client: Any, staff_user: object
    ) -> None:
        ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        response = authenticated_client.get(
            "/admin/scan-processing/?page=abc&per_page=xyz"
        )

        assert response.status_code == 200
        assert response.context["page_obj"].number == 1

    def test_per_page_is_clamped(
        self, authenticated_client: Any, staff_user: object
    ) -> None:
        for _ in range(3):
            ScanBatchFactory(status=ScanBatch.STATUS_PROCESSING)

        response = authenticated_client.get("/admin/scan-processing/?per_page=999999")

        assert response.status_code == 200
        # Clamp at 200; with only 3 active batches paginator stays at 1 page.
        assert response.context["paginator"].per_page <= 200
