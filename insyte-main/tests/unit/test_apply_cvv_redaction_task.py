"""Unit tests for ``apply_cvv_redaction_task``.

The CVV task fires on every Stripe authorization attempt (success or
failure) and applies *only* the CVV-box rectangles. PAN, expiry, and
signature stay readable until the post-charge task runs separately.
"""

from __future__ import annotations

from typing import Any

import pytest

from scans.models import ScanPlaceholder
from scans.tasks import apply_cvv_redaction_task
from tests.factories import (
    DonationFactory,
    ScanPlaceholderFactory,
)


@pytest.mark.django_db()
class TestApplyCvvRedactionTask:
    """Coverage for the auth-time CVV redaction Celery task."""

    def test_applies_cvv_when_status_cvv_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saved CVV coords get applied to R2; status flips to DEFERRED."""
        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
            redaction_coords_cvv=[
                [{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]
            ],
            redaction_coords_post_charge=[
                [{"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.05}]
            ],
        )

        captured: list[list[list[dict[str, float]]]] = []

        def fake_replace(
            ph: ScanPlaceholder,
            page_rects: list[list[dict[str, float]]],
            *,
            user: Any,
            redaction_notes: str,
        ) -> list[str]:
            captured.append(page_rects)
            return ["ScanOutput/cvv_redacted.png"]

        monkeypatch.setattr(
            "scans.scan_redaction.replace_placeholder_with_server_redacted_coords",
            fake_replace,
        )

        result = apply_cvv_redaction_task.apply(args=(str(placeholder.id),)).get()

        assert result == {"status": "applied", "placeholder_id": str(placeholder.id)}
        # Renderer called exactly once with the CVV rectangles only.
        assert captured == [[[{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]]]
        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_DEFERRED
        assert placeholder.cvv_redacted_at is not None
        # Post-charge layer is still pending — completed_at not set.
        assert placeholder.redaction_completed_at is None

    def test_no_op_when_no_cvv_coords(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-card method with empty cvv_pages — promote to DEFERRED, skip R2."""
        donation = DonationFactory(payment_method="cheque")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
            redaction_coords_cvv=[[]],
            redaction_coords_post_charge=[
                [{"x": 0.1, "y": 0.5, "width": 0.4, "height": 0.05}]
            ],
        )

        called: list[Any] = []
        monkeypatch.setattr(
            "scans.scan_redaction.replace_placeholder_with_server_redacted_coords",
            lambda *a, **k: called.append((a, k)),
        )

        result = apply_cvv_redaction_task.apply(args=(str(placeholder.id),)).get()

        assert result == {"status": "noop", "placeholder_id": str(placeholder.id)}
        assert called == []
        placeholder.refresh_from_db()
        # Status promoted to DEFERRED so post-charge runs cleanly.
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_DEFERRED
        assert placeholder.cvv_redacted_at is not None

    def test_no_op_when_already_deferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A second auth attempt fires the task again — must be a no-op."""
        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_DEFERRED,
            redaction_coords_cvv=[
                [{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]
            ],
        )

        called: list[Any] = []
        monkeypatch.setattr(
            "scans.scan_redaction.replace_placeholder_with_server_redacted_coords",
            lambda *a, **k: called.append((a, k)),
        )

        result = apply_cvv_redaction_task.apply(args=(str(placeholder.id),)).get()

        assert result == {"status": "noop", "placeholder_id": str(placeholder.id)}
        assert called == []

    def test_no_op_when_already_completed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fully redacted placeholders short-circuit."""
        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
            redaction_coords_cvv=[
                [{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]
            ],
        )

        called: list[Any] = []
        monkeypatch.setattr(
            "scans.scan_redaction.replace_placeholder_with_server_redacted_coords",
            lambda *a, **k: called.append((a, k)),
        )

        result = apply_cvv_redaction_task.apply(args=(str(placeholder.id),)).get()

        assert result == {"status": "noop", "placeholder_id": str(placeholder.id)}
        assert called == []

    def test_returns_not_found_when_placeholder_missing(self) -> None:
        """A stale placeholder id returns a not_found result, no exception."""
        result = apply_cvv_redaction_task.apply(
            args=("00000000-0000-0000-0000-000000000000",)
        ).get()
        assert result == {
            "status": "not_found",
            "placeholder_id": "00000000-0000-0000-0000-000000000000",
        }
