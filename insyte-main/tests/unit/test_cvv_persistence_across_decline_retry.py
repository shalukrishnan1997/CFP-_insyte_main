"""Integration test: CVV stays redacted across a decline → retry → succeed cycle.

Covers the load-bearing composition between ``apply_cvv_redaction`` and
``apply_deferred_redaction``:

* After the CVV pass, the original (pre-CVV) blobs are deleted from R2.
* ``original_page_keys`` is promoted to point at the CVV-redacted blobs
  so the post-charge pass operates on the *current* bytes, not on the
  deleted originals.
* Running the post-charge pass after the CVV pass succeeds end-to-end
  without "blob not found" errors.

If anyone removes the ``original_page_keys`` promotion in
``apply_cvv_redaction``, this test fails on the second pass.
"""

from __future__ import annotations

import pytest

from scans.models import ScanPlaceholder
from scans.scan_redaction import apply_cvv_redaction, apply_deferred_redaction
from tests.factories import DonationFactory, ScanPlaceholderFactory


class _R2Store:
    """In-memory R2 stub: dict-of-bytes with read-after-delete enforcement."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise AssertionError(
                f"R2 stub: get_object('{key}') called for a key that was "
                f"never written or has already been deleted. Keys present: "
                f"{sorted(self.objects)}"
            )
        return self.objects[key]

    def put(self, key: str, body: bytes) -> None:
        self.objects[key] = body

    def copy(self, src: str, dst: str) -> None:
        if src not in self.objects:
            raise AssertionError(f"R2 stub: copy from missing key '{src}'")
        self.objects[dst] = self.objects[src]

    def delete(self, key: str) -> None:
        # Idempotent — Cloudflare R2 silently succeeds on missing keys.
        self.objects.pop(key, None)


def _install_r2_stub(monkeypatch: pytest.MonkeyPatch) -> _R2Store:
    """Monkeypatch the storage_backends module to use an in-memory R2 store."""
    from core import storage_backends

    store = _R2Store()

    def get(key: str) -> bytes:
        return store.get(key)

    def put(key: str, body: bytes, content_type: str = "") -> None:
        del content_type
        store.put(key, body)

    def copy(src: str, dst: str) -> None:
        store.copy(src, dst)

    def delete(key: str) -> None:
        store.delete(key)

    monkeypatch.setattr(storage_backends, "r2_get_object", get)
    monkeypatch.setattr(storage_backends, "r2_put_object", put)
    monkeypatch.setattr(storage_backends, "r2_copy_object", copy)
    monkeypatch.setattr(storage_backends, "r2_delete_object", delete)
    monkeypatch.setattr(storage_backends, "r2_enabled", lambda: True)
    monkeypatch.setattr(
        storage_backends, "r2_public_url", lambda key: f"https://cdn.test/{key}"
    )
    return store


def _stub_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> list[list[list[dict[str, float]]]]:
    """Replace ``apply_redactions`` with a no-op that records the rect sets.

    The real Pillow renderer is exhaustively covered by
    ``test_scan_redaction_renderer.py``. Here we just need to know that
    the renderer was called with the right inputs at the right time.
    """
    invocations: list[list[list[dict[str, float]]]] = []

    def fake_apply(
        source_bytes: bytes, content_type: str, rects: list[dict[str, float]]
    ) -> tuple[bytes, str]:
        del content_type
        invocations.append([rects])
        return source_bytes + b"|redacted", "image/png"

    monkeypatch.setattr("scans.scan_redaction_renderer.apply_redactions", fake_apply)
    return invocations


@pytest.mark.django_db()
class TestCvvPersistenceAcrossDeclineRetry:
    """End-to-end integration: CVV pass → post-charge pass over an in-memory R2."""

    def test_two_pass_composition_does_not_lose_track_of_source_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The post-charge pass must read CVV-redacted bytes, not deleted originals."""
        store = _install_r2_stub(monkeypatch)
        rect_invocations = _stub_renderer(monkeypatch)

        # Seed the original scan bytes — what the operator drew CVV + PAN
        # rectangles onto.
        original_key = "ScanOutput/demo/page_0.png"
        store.put(original_key, b"original-page-bytes")

        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
            image_url="",
            image_path=original_key,
            page_keys=[original_key],
            original_page_keys=[original_key],
            redaction_coords_cvv=[
                [{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]
            ],
            redaction_coords_post_charge=[
                [{"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.05}]
            ],
        )

        # ── Pass 1: CVV pass fires immediately on the auth attempt ──────
        applied = apply_cvv_redaction(placeholder)
        assert applied is True

        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_DEFERRED
        assert placeholder.cvv_redacted_at is not None
        # The original blob is GONE from R2 — proves the CVV pass deleted it.
        assert original_key not in store.objects, (
            "CVV pass should have deleted the original key after swap"
        )
        # ``original_page_keys`` was promoted to the CVV-redacted blob so
        # the post-charge pass reads the *current* bytes.
        assert placeholder.original_page_keys == placeholder.page_keys
        # The new key exists and looks like a redacted-PNG sibling.
        cvv_key = placeholder.page_keys[0]
        assert cvv_key in store.objects
        assert cvv_key != original_key
        # Renderer was called once with only the CVV rectangle.
        assert rect_invocations == [
            [[{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]]
        ]

        # ── Pass 2: post-charge pass fires after the charge succeeds ────
        applied = apply_deferred_redaction(placeholder)
        assert applied is True

        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_COMPLETED
        assert placeholder.redaction_completed_at is not None

        # The CVV-only intermediate blob is gone too — only the fully
        # redacted final blob remains.
        assert cvv_key not in store.objects
        final_key = placeholder.page_keys[0]
        assert final_key in store.objects
        assert final_key != cvv_key

        # Renderer was called a second time, with only the post-charge
        # rectangles. The CVV-only set must not appear in the second call.
        assert rect_invocations == [
            [[{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]],
            [[{"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.05}]],
        ]
