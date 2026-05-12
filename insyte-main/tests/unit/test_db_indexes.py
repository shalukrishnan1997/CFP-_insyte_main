"""Verify hot-path database indexes exist after migration.

These tests guard the indexes added in
``donations/migrations/0005_donationbatch_batch_camp_stat_created_idx_and_more.py``
and ``scans/migrations/0007_scanuploadprogress_scanprogress_camp_stat_idx_and_more.py``.

The indexes back the hottest query paths in the QA review pipeline and the
scanner-upload webhook flow, so a regression that drops or renames them would
silently degrade dashboard latency at scale (100 QA reviewers + 5 scanners).

Index introspection requires Django's ``connection.introspection.get_constraints``,
which exposes index column ordering on PostgreSQL. SQLite (the test backend)
also reports indexes here, so the tests run on both backends without skips.
"""

from __future__ import annotations

import pytest
from django.db import connection


def _index_columns(table_name: str, index_name: str) -> list[str] | None:
    """Return the ordered column list for ``index_name`` on ``table_name``.

    Args:
        table_name: Physical database table name.
        index_name: Index identifier as declared in ``Meta.indexes``.

    Returns:
        Ordered list of column names backing the index, or ``None`` when
        the index is absent from the live schema.
    """
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, table_name)
    constraint = constraints.get(index_name)
    if constraint is None:
        return None
    columns = constraint.get("columns")
    if not columns:
        return None
    return list(columns)


@pytest.mark.django_db()
def test_donationbatch_camp_stat_created_idx_exists() -> None:
    """QA dashboard list query relies on (campaign, status, created_at)."""
    columns = _index_columns("donations_donationbatch", "batch_camp_stat_created_idx")
    assert columns == ["campaign_id", "status", "created_at"]


@pytest.mark.django_db()
def test_donationbatch_camp_paystat_idx_exists() -> None:
    """Payment retry queries rely on (campaign, payment_status)."""
    columns = _index_columns("donations_donationbatch", "batch_camp_paystat_idx")
    assert columns == ["campaign_id", "payment_status"]


@pytest.mark.django_db()
def test_scanuploadprogress_camp_stat_idx_exists() -> None:
    """Scanner sweep tasks rely on (campaign, status)."""
    columns = _index_columns("scans_scanuploadprogress", "scanprogress_camp_stat_idx")
    assert columns == ["campaign_id", "status"]


@pytest.mark.django_db()
def test_scanuploadprogress_latest_urn_idx_exists() -> None:
    """Donor URN lookups rely on a single-column index on ``latest_urn``."""
    columns = _index_columns("scans_scanuploadprogress", "scanprogress_latest_urn_idx")
    assert columns == ["latest_urn"]
