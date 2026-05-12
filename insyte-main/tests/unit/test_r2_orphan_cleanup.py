"""Tests for the periodic ``cleanup_r2_orphans_task``.

The task walks the redaction-related R2 prefixes and deletes any object that
is not referenced by a ``ScanPlaceholder`` *and* is older than the 24-hour
grace window. The grace window matters: a fresh temp blob from an in-flight
atomic redaction swap must never be deleted before the upload completes.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone as dj_timezone

from core.storage_backends import R2ObjectInfo
from scans.tasks import cleanup_r2_orphans_task
from tests.factories import ScanPlaceholderFactory


def _info(key: str, last_modified: datetime, size: int = 1024) -> R2ObjectInfo:
    return R2ObjectInfo(key=key, size=size, last_modified=last_modified)


@pytest.mark.django_db
def test_only_old_unreferenced_keys_are_deleted() -> None:
    """5 listed keys: 3 referenced + 2 orphans (1 fresh, 1 stale) → only the stale one dies."""
    referenced_key_pages = "redacted/client/a/page1.tif"
    referenced_key_image = "redacted/client/a/image_path.tif"
    referenced_key_original = "originals/client/a/source.tif"
    fresh_orphan = "redacted-tmp/abc123/client/a/in_flight.tif"
    stale_orphan = "redacted-tmp/zzz999/client/a/abandoned.tif"

    # Reference the first three keys via different placeholder fields so the
    # cross-reference set actually exercises every field the task scans.
    ScanPlaceholderFactory(
        page_keys=[referenced_key_pages],
        original_page_keys=[referenced_key_original],
        image_path=referenced_key_image,
        image_url="",
    )

    now = dj_timezone.now()
    fresh_dt = now - timedelta(hours=1)  # well under the 24h grace window
    stale_dt = now - timedelta(hours=48)  # comfortably older than the cutoff
    referenced_stale_dt = now - timedelta(hours=72)  # old, but referenced

    listings_by_prefix: dict[str, list[R2ObjectInfo]] = {
        "redacted-tmp/": [
            _info(fresh_orphan, fresh_dt),
            _info(stale_orphan, stale_dt, size=4096),
        ],
        "redacted/": [
            _info(referenced_key_pages, referenced_stale_dt),
            _info(referenced_key_image, referenced_stale_dt),
        ],
        "originals/": [
            _info(referenced_key_original, referenced_stale_dt),
        ],
    }

    deleted_keys: list[str] = []

    def fake_list(prefix: str, max_keys: int = 1000) -> list[R2ObjectInfo]:
        return list(listings_by_prefix.get(prefix, []))

    def fake_delete(key: str) -> None:
        deleted_keys.append(key)

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch(
            "core.storage_backends.r2_list_prefix_with_metadata",
            side_effect=fake_list,
        ),
        patch("core.storage_backends.r2_delete_object", side_effect=fake_delete),
    ):
        result = cleanup_r2_orphans_task()

    assert result["status"] == "ok"
    assert deleted_keys == [stale_orphan]
    assert result["deleted"] == 1
    assert result["bytes_freed"] == 4096
    assert fresh_orphan not in deleted_keys
    for ref_key in (
        referenced_key_pages,
        referenced_key_image,
        referenced_key_original,
    ):
        assert ref_key not in deleted_keys


@pytest.mark.django_db
def test_returns_skipped_when_r2_not_configured() -> None:
    """No-op cleanly when R2 is disabled in the environment."""
    with (
        patch("core.storage_backends.r2_enabled", return_value=False),
        patch("core.storage_backends.r2_list_prefix_with_metadata") as list_mock,
    ):
        result = cleanup_r2_orphans_task()

    assert result["status"] == "skipped"
    assert result["deleted"] == 0
    assert list_mock.call_count == 0


@pytest.mark.django_db
def test_naive_last_modified_is_treated_as_utc() -> None:
    """Defensive: naive LastModified must not crash the cutoff comparison."""
    # Strip tzinfo to mimic an unexpected naive timestamp from a future SDK
    # regression — the task should still treat it as UTC and not crash.
    naive_stale = (dj_timezone.now() - timedelta(hours=48)).replace(tzinfo=None)
    listings_by_prefix: dict[str, list[R2ObjectInfo]] = {
        "redacted-tmp/": [_info("redacted-tmp/x/orphan.bin", naive_stale, size=10)],
        "redacted/": [],
        "originals/": [],
    }

    deleted: list[str] = []

    def fake_list(prefix: str, max_keys: int = 1000) -> list[R2ObjectInfo]:
        return list(listings_by_prefix.get(prefix, []))

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch(
            "core.storage_backends.r2_list_prefix_with_metadata",
            side_effect=fake_list,
        ),
        patch(
            "core.storage_backends.r2_delete_object",
            side_effect=lambda key: deleted.append(key),
        ),
    ):
        result = cleanup_r2_orphans_task()

    assert deleted == ["redacted-tmp/x/orphan.bin"]
    assert result["bytes_freed"] == 10


@pytest.mark.django_db
def test_transient_list_failure_is_logged_and_skipped() -> None:
    """A list error on one prefix must not break cleanup of the others."""
    fresh = dj_timezone.now() - timedelta(hours=1)  # within grace, safe orphan
    stale = dj_timezone.now() - timedelta(hours=72)

    def fake_list(prefix: str, max_keys: int = 1000) -> list[R2ObjectInfo]:
        if prefix == "redacted/":
            raise RuntimeError("simulated transient R2 listing failure")
        if prefix == "redacted-tmp/":
            return [_info("redacted-tmp/x/orphan.bin", stale, size=200)]
        return [_info("originals/recent.bin", fresh, size=1)]

    deleted: list[str] = []

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch(
            "core.storage_backends.r2_list_prefix_with_metadata",
            side_effect=fake_list,
        ),
        patch(
            "core.storage_backends.r2_delete_object",
            side_effect=lambda key: deleted.append(key),
        ),
    ):
        result = cleanup_r2_orphans_task()

    assert deleted == ["redacted-tmp/x/orphan.bin"]
    assert result["status"] == "ok"
    assert result["deleted"] == 1
    assert result["per_prefix"]["redacted/"]["errored"] == 1
