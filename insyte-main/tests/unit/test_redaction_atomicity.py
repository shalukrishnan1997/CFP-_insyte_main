"""Tests for the atomic temp-key swap that backs the redaction upload flow.

The atomic-swap pattern guarantees that a mid-upload R2 failure never leaves
a partial blob at the placeholder's final key. This module covers:

* a mid-upload PUT failure rolls back cleanly: the placeholder stays at its
  previous (unredacted) state, ``redaction_status`` flips to ``blocked``
  with ``redaction_error`` populated, and ``page_keys`` is *not* updated
  to point at any partial blob.
* a copy-stage failure cleans up the temp blob and likewise marks the
  placeholder blocked.
* a fully-successful swap clears any prior blocked/error state on the
  placeholder.
"""

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile

from scans.scan_redaction import replace_placeholder_with_redacted_uploads
from tests.factories import ScanPlaceholderFactory


def _two_page_uploads() -> list[UploadedFile]:
    return [
        SimpleUploadedFile("page1.tif", b"redacted-1", content_type="image/tiff"),
        SimpleUploadedFile("page2.tif", b"redacted-2", content_type="image/tiff"),
    ]


@pytest.mark.django_db
def test_put_failure_marks_blocked_and_preserves_page_keys() -> None:
    """A failed temp PUT must not advance the placeholder past the previous state."""
    original_keys = ["client/a/page1.tif", "client/a/page2.tif"]
    placeholder = ScanPlaceholderFactory(
        image_url="https://cdn.example.com/client/a/page1.tif",
        image_path="client/a/page1.tif",
        page_keys=list(original_keys),
        original_page_keys=list(original_keys),
        redaction_status="pending",
    )

    def boom_put(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated R2 PUT mid-upload failure")

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.r2_put_object", side_effect=boom_put),
        patch("core.storage_backends.r2_copy_object") as copy_mock,
        patch("core.storage_backends.r2_delete_object") as delete_mock,
        patch(
            "core.storage_backends.r2_public_url",
            side_effect=lambda key: f"https://cdn.example.com/{key}",
        ),
        pytest.raises(RuntimeError, match="simulated R2 PUT"),
    ):
        replace_placeholder_with_redacted_uploads(placeholder, _two_page_uploads())

    # No copy can happen if the temp PUT itself failed; no delete either,
    # because the temp blob was never created.
    assert copy_mock.call_count == 0
    assert delete_mock.call_count == 0

    placeholder.refresh_from_db()
    # Critical PII guarantee: page_keys must NOT have advanced.
    assert placeholder.page_keys == original_keys
    assert placeholder.image_path == "client/a/page1.tif"
    assert placeholder.redaction_status == placeholder.REDACTION_BLOCKED
    assert "RuntimeError" in placeholder.redaction_error
    assert "simulated R2 PUT" in placeholder.redaction_error
    # The audit-snapshot is preserved for retry.
    assert placeholder.original_page_keys == original_keys


@pytest.mark.django_db
def test_copy_failure_cleans_temp_and_marks_blocked() -> None:
    """A copy-stage failure must clean its temp blob and block the placeholder."""
    original_keys = ["client/a/page1.tif"]
    placeholder = ScanPlaceholderFactory(
        image_url="https://cdn.example.com/client/a/page1.tif",
        image_path="client/a/page1.tif",
        page_keys=list(original_keys),
        original_page_keys=list(original_keys),
        redaction_status="pending",
    )
    uploads = [
        SimpleUploadedFile("page1.tif", b"redacted-1", content_type="image/tiff"),
    ]

    def boom_copy(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated copy_object failure")

    deleted: list[str] = []

    def record_delete(key: str) -> None:
        deleted.append(key)

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.r2_put_object"),
        patch("core.storage_backends.r2_copy_object", side_effect=boom_copy),
        patch("core.storage_backends.r2_delete_object", side_effect=record_delete),
        patch(
            "core.storage_backends.r2_public_url",
            side_effect=lambda key: f"https://cdn.example.com/{key}",
        ),
        pytest.raises(RuntimeError, match="simulated copy_object"),
    ):
        replace_placeholder_with_redacted_uploads(placeholder, uploads)

    placeholder.refresh_from_db()
    assert placeholder.page_keys == original_keys
    assert placeholder.redaction_status == placeholder.REDACTION_BLOCKED
    assert "copy_object" in placeholder.redaction_error

    # The temp blob (under redacted-tmp/) must have been cleaned best-effort.
    assert any(key.startswith("redacted-tmp/") for key in deleted), (
        f"expected a temp-blob delete, got {deleted}"
    )


@pytest.mark.django_db
def test_successful_swap_clears_prior_blocked_state() -> None:
    """A retry that succeeds must clear redaction_error and flip status back."""
    original_keys = ["client/a/page1.tif"]
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="client/a/page1.tif",
        page_keys=list(original_keys),
        original_page_keys=list(original_keys),
        redaction_status="blocked",
        redaction_error="previous attempt failed: ConnectionError",
    )
    uploads = [
        SimpleUploadedFile("page1.tif", b"redacted-1", content_type="image/tiff"),
    ]

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.r2_put_object"),
        patch("core.storage_backends.r2_copy_object"),
        patch("core.storage_backends.r2_delete_object"),
        patch(
            "core.storage_backends.r2_public_url",
            side_effect=lambda key: f"https://cdn.example.com/{key}",
        ),
    ):
        replace_placeholder_with_redacted_uploads(placeholder, uploads)

    placeholder.refresh_from_db()
    assert placeholder.redaction_status == placeholder.REDACTION_COMPLETED
    assert placeholder.redaction_error == ""
