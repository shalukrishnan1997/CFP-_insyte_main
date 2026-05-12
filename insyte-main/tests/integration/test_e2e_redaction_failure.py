"""Integration tests for the R2 redaction failure rollback path.

Covers the end-to-end behaviour of ``save_uploaded_redacted_pages`` when
the second R2 ``PutObject`` call raises mid-flight:

* the placeholder ends with no partial ``page_keys`` written (rollback),
* the placeholder's ``redaction_status`` flips to ``REDACTION_BLOCKED``
  with ``redaction_error`` populated,
* the original (unredacted) ``page_keys`` snapshot is preserved so the
  operator can retry without losing the source document.

Uses the shared ``000015.pdf`` fixture as a stand-in for staff-uploaded
redacted page bytes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from scans.models import ScanPlaceholder
from scans.scan_redaction import save_uploaded_redacted_pages
from tests.factories import ScanPlaceholderFactory, UserFactory

_FIXTURE_PDF = Path(__file__).resolve().parent.parent / "fixtures" / "000015.pdf"


def _load_fixture_bytes() -> bytes:
    """Return the raw bytes of the shared ``000015.pdf`` fixture."""
    return _FIXTURE_PDF.read_bytes()


@pytest.mark.django_db()
class TestRedactionR2FailureRollback:
    """E2E: a mid-redaction R2 failure must roll back ``page_keys``."""

    def test_second_put_failure_rolls_back_page_keys(self) -> None:
        """First R2 PUT succeeds, second raises — placeholder must roll back.

        After the failure the placeholder has no partially-written
        ``page_keys`` (still pointing at the original keys), the
        ``redaction_status`` is ``REDACTION_BLOCKED``, and
        ``redaction_error`` records the failure class.
        """
        user = UserFactory(is_staff=True)
        original_keys = [
            "ScanOutput/client-a/page1.pdf",
            "ScanOutput/client-a/page2.pdf",
        ]
        placeholder = ScanPlaceholderFactory(
            image_url="https://cdn.example.com/ScanOutput/client-a/page1.pdf",
            image_path=original_keys[0],
            page_keys=list(original_keys),
            original_page_keys=list(original_keys),
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )

        pdf_bytes = _load_fixture_bytes()
        files = [
            SimpleUploadedFile("page1.pdf", pdf_bytes, content_type="application/pdf"),
            SimpleUploadedFile("page2.pdf", pdf_bytes, content_type="application/pdf"),
        ]

        # First call (sentinel) succeeds; second call (per-page upload)
        # raises mid-flight to simulate an R2 outage.
        call_count = {"n": 0}

        def fake_put(*_args: object, **_kwargs: object) -> None:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise RuntimeError("R2 PutObject 503: second page upload failed")

        with (
            patch("core.storage_backends.r2_enabled", return_value=True),
            patch("core.storage_backends.r2_put_object", side_effect=fake_put),
            patch("core.storage_backends.r2_copy_object"),
            patch("core.storage_backends.r2_delete_object"),
            patch(
                "core.storage_backends.r2_public_url",
                side_effect=lambda key: f"https://cdn.example.com/{key}",
            ),
            pytest.raises(RuntimeError, match="R2 PutObject 503"),
        ):
            save_uploaded_redacted_pages(
                placeholder=placeholder,
                user=user,
                files=files,
                notes="e2e-rollback-test",
            )

        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_BLOCKED
        # Critical PII guarantee: page_keys must remain at the original
        # snapshot — no partial redacted blob is referenced.
        assert placeholder.page_keys == original_keys
        assert placeholder.original_page_keys == original_keys
        assert placeholder.image_path == original_keys[0]
        assert "RuntimeError" in placeholder.redaction_error
        assert "R2 PutObject 503" in placeholder.redaction_error

    def test_failure_cleans_up_already_uploaded_destination_keys(self) -> None:
        """Pages uploaded before the failure must be deleted from R2.

        ``_atomic_upload_redacted_page`` swaps bytes out of
        ``redacted-tmp/`` straight onto the *final* destination key, so a
        mid-batch failure leaves orphaned blobs that the periodic
        ``redacted-tmp/`` cleanup task will never sweep. The rollback path
        must therefore explicitly call ``r2_delete_object`` on every key
        already uploaded before the failure fired — otherwise R2 storage
        grows unboundedly on every retry.
        """
        user = UserFactory(is_staff=True)
        # Three pages: pages 1 & 2 succeed, page 3 fails. The cleanup
        # path must delete the two pages already swapped into their
        # final destination keys.
        original_keys = [
            "ScanOutput/client-c/page1.pdf",
            "ScanOutput/client-c/page2.pdf",
            "ScanOutput/client-c/page3.pdf",
        ]
        placeholder = ScanPlaceholderFactory(
            image_url="https://cdn.example.com/ScanOutput/client-c/page1.pdf",
            image_path=original_keys[0],
            page_keys=list(original_keys),
            original_page_keys=list(original_keys),
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )

        pdf_bytes = _load_fixture_bytes()
        files = [
            SimpleUploadedFile("page1.pdf", pdf_bytes, content_type="application/pdf"),
            SimpleUploadedFile("page2.pdf", pdf_bytes, content_type="application/pdf"),
            SimpleUploadedFile("page3.pdf", pdf_bytes, content_type="application/pdf"),
        ]

        # Sentinel write + 2 successful page writes (each is put-tmp then
        # delete-tmp = 1 put + 1 delete) = 5 puts before the third page's
        # put raises. Track only puts to keep the trigger logic readable.
        put_count = {"n": 0}

        def fake_put(*_args: object, **_kwargs: object) -> None:
            put_count["n"] += 1
            # Sentinel(1) + page1 temp(2) + page2 temp(3) succeed; page3
            # temp(4) raises mid-flight.
            if put_count["n"] >= 4:
                raise RuntimeError("R2 PutObject 503: third page failed")

        with (
            patch("core.storage_backends.r2_enabled", return_value=True),
            patch("core.storage_backends.r2_put_object", side_effect=fake_put),
            patch("core.storage_backends.r2_copy_object"),
            patch("core.storage_backends.r2_delete_object") as mock_delete,
            patch(
                "core.storage_backends.r2_public_url",
                side_effect=lambda key: f"https://cdn.example.com/{key}",
            ),
            pytest.raises(RuntimeError, match="R2 PutObject 503"),
        ):
            save_uploaded_redacted_pages(
                placeholder=placeholder,
                user=user,
                files=files,
                notes="orphan-cleanup-test",
            )

        # Of the three pages, two were swapped into final destination
        # keys before page 3 raised. The cleanup path must call
        # ``r2_delete_object`` for each — extract just those calls (the
        # mock also receives temp-key deletes from the atomic-swap
        # success path, sentinel cleanup, etc.).
        deleted_keys = [call.args[0] for call in mock_delete.call_args_list]
        # Each successfully-uploaded page lives under a key matching
        # ``_redacted_r2_storage_key``: parent path + ``_redacted_<short>_<i>.<ext>``.
        rollback_deletes = [
            k
            for k in deleted_keys
            if "_redacted_" in k and not k.startswith("redacted-tmp/")
        ]
        assert len(rollback_deletes) == 2, (
            "expected exactly two orphan deletes (one per uploaded page) "
            f"but got: {rollback_deletes}"
        )

        # Placeholder still rolls back as before.
        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_BLOCKED
        assert placeholder.page_keys == original_keys

    def test_sentinel_failure_marks_blocked_and_preserves_page_keys(self) -> None:
        """If the very first R2 write fails, the placeholder must still roll back.

        The sentinel write that ``save_uploaded_redacted_pages`` performs
        before any per-page upload is the earliest failure point. A
        connection refusal there must still leave ``page_keys`` untouched
        and flip ``redaction_status`` to ``REDACTION_BLOCKED``.
        """
        user = UserFactory(is_staff=True)
        original_keys = ["ScanOutput/client-b/page1.pdf"]
        placeholder = ScanPlaceholderFactory(
            image_url="https://cdn.example.com/ScanOutput/client-b/page1.pdf",
            image_path=original_keys[0],
            page_keys=list(original_keys),
            original_page_keys=list(original_keys),
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )

        pdf_bytes = _load_fixture_bytes()
        files = [
            SimpleUploadedFile("page1.pdf", pdf_bytes, content_type="application/pdf"),
        ]

        with (
            patch("core.storage_backends.r2_enabled", return_value=True),
            patch(
                "core.storage_backends.r2_put_object",
                side_effect=ConnectionError("R2 unreachable"),
            ),
            patch("core.storage_backends.r2_copy_object"),
            patch("core.storage_backends.r2_delete_object"),
            pytest.raises(ConnectionError, match="R2 unreachable"),
        ):
            save_uploaded_redacted_pages(
                placeholder=placeholder,
                user=user,
                files=files,
                notes="",
            )

        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_BLOCKED
        assert placeholder.page_keys == original_keys
        assert "ConnectionError" in placeholder.redaction_error
