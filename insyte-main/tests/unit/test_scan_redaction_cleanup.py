"""Tests covering original-key cleanup after a successful redaction upload.

The redaction flow must purge the unredacted originals from R2 once the
redacted copies are written and the placeholder is updated. The on-model
``original_page_keys`` snapshot is preserved as an audit trail.
"""

from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile, UploadedFile

from scans.scan_redaction import replace_placeholder_with_redacted_uploads
from tests.factories import ScanPlaceholderFactory


def _build_uploads() -> list[UploadedFile]:
    return [
        SimpleUploadedFile("page1.tif", b"redacted-bytes-1", content_type="image/tiff"),
        SimpleUploadedFile("page2.tif", b"redacted-bytes-2", content_type="image/tiff"),
    ]


@pytest.mark.django_db
def test_replace_placeholder_deletes_original_r2_keys() -> None:
    """Each original key in original_page_keys is deleted after save succeeds.

    The atomic-swap pattern fires two r2_delete_object calls per page (one
    for the temp blob after the swap completes) plus one per original; this
    test focuses on the original-cleanup invariant and inspects the delete
    args rather than the raw call_count.
    """
    original_keys = ["client/a/page1.tif", "client/a/page2.tif"]
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="client/a/page1.tif",
        page_keys=list(original_keys),
        original_page_keys=list(original_keys),
        redaction_status="pending",
    )

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.r2_put_object") as put_mock,
        patch("core.storage_backends.r2_copy_object") as copy_mock,
        patch("core.storage_backends.r2_delete_object") as delete_mock,
        patch(
            "core.storage_backends.r2_public_url",
            side_effect=lambda key: f"https://cdn.example.com/{key}",
        ),
    ):
        new_keys = replace_placeholder_with_redacted_uploads(
            placeholder, _build_uploads()
        )

    assert len(new_keys) == 2
    # Temp put per page (atomic swap stages bytes outside the final key).
    assert put_mock.call_count == 2
    # Server-side copy per page.
    assert copy_mock.call_count == 2
    # Cross-check the original-delete invariant by filtering the delete list.
    deleted = [call.args[0] for call in delete_mock.call_args_list]
    for original_key in original_keys:
        assert original_key in deleted, (
            f"original key {original_key} should have been deleted; got {deleted}"
        )

    placeholder.refresh_from_db()
    assert placeholder.original_page_keys == original_keys
    assert placeholder.redaction_status == placeholder.REDACTION_COMPLETED
    assert placeholder.page_keys == new_keys


@pytest.mark.django_db
def test_replace_placeholder_skips_delete_when_new_key_collides() -> None:
    """Defensive: never delete a key we just wrote in the same call."""
    original_keys = ["client/a/page1.tif", "client/a/page2.tif"]
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="client/a/page1.tif",
        page_keys=list(original_keys),
        original_page_keys=list(original_keys),
        redaction_status="pending",
    )

    def fake_redacted_key(
        original_key: str, _placeholder_id: str, index: int, _filename: str
    ) -> str:
        if index == 0:
            return original_key
        return f"client/a/page{index + 1}_redacted.tif"

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.r2_put_object"),
        patch("core.storage_backends.r2_copy_object"),
        patch("core.storage_backends.r2_delete_object") as delete_mock,
        patch(
            "core.storage_backends.r2_public_url",
            side_effect=lambda key: f"https://cdn.example.com/{key}",
        ),
        patch(
            "scans.scan_redaction._redacted_r2_storage_key",
            side_effect=fake_redacted_key,
        ),
    ):
        replace_placeholder_with_redacted_uploads(placeholder, _build_uploads())

    deleted_keys = {call.args[0] for call in delete_mock.call_args_list}
    # The original-cleanup loop only emits one delete (page 2) since page 1
    # was overwritten in place. Temp-blob deletes target redacted-tmp/* keys,
    # never legacy originals — this filter isolates the final-key cleanup.
    original_deletes = {k for k in deleted_keys if not k.startswith("redacted-tmp/")}
    assert original_deletes == {"client/a/page2.tif"}


@pytest.mark.django_db
def test_replace_placeholder_swallows_delete_failures() -> None:
    """An original-delete failure must not propagate — redaction succeeded."""
    original_keys = ["client/a/page1.tif", "client/a/page2.tif"]
    placeholder = ScanPlaceholderFactory(
        image_url="",
        image_path="client/a/page1.tif",
        page_keys=list(original_keys),
        original_page_keys=list(original_keys),
        redaction_status="pending",
    )

    def flaky_delete(key: str) -> None:
        # Only fail on the original-cleanup step (sibling-key layout); let
        # the temp-blob deletes inside the atomic swap succeed.
        if not key.startswith("redacted-tmp/") and key == original_keys[0]:
            raise RuntimeError("boom")

    with (
        patch("core.storage_backends.r2_enabled", return_value=True),
        patch("core.storage_backends.r2_put_object"),
        patch("core.storage_backends.r2_copy_object"),
        patch("core.storage_backends.r2_delete_object", side_effect=flaky_delete),
        patch(
            "core.storage_backends.r2_public_url",
            side_effect=lambda key: f"https://cdn.example.com/{key}",
        ),
    ):
        new_keys = replace_placeholder_with_redacted_uploads(
            placeholder, _build_uploads()
        )

    assert len(new_keys) == 2

    placeholder.refresh_from_db()
    assert placeholder.original_page_keys == original_keys
    assert placeholder.redaction_status == placeholder.REDACTION_COMPLETED
