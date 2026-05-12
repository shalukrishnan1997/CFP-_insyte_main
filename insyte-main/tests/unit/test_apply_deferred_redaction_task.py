"""Unit tests for ``apply_deferred_redaction_task``.

The post-charge task applies the PAN/expiry/signature blackout layer.
It also safety-nets the CVV layer: if the CVV pass never ran (e.g.
``apply_cvv_redaction_task`` was lost), this task applies CVV first
inside the same row lock before applying the post-charge layer.
"""

from __future__ import annotations

from typing import Any

import pytest

from scans.models import ScanPlaceholder
from scans.tasks import apply_deferred_redaction_task
from tests.factories import (
    DonationFactory,
    ScanPlaceholderFactory,
)


@pytest.mark.django_db()
class TestApplyDeferredRedactionTask:
    """Coverage for the post-charge redaction Celery task."""

    def test_no_op_when_status_completed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Already-redacted placeholders short-circuit without calling R2."""
        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
            redaction_coords_post_charge=[
                [{"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}]
            ],
        )

        called: list[Any] = []
        monkeypatch.setattr(
            "scans.scan_redaction.replace_placeholder_with_server_redacted_coords",
            lambda *a, **k: called.append((a, k)),
        )

        result = apply_deferred_redaction_task.apply(args=(str(placeholder.id),)).get()

        assert result == {"status": "noop", "placeholder_id": str(placeholder.id)}
        assert called == []

    def test_applies_post_charge_only_when_already_deferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DEFERRED → only the post-charge layer fires (CVV already ran)."""
        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_DEFERRED,
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
            return ["ScanOutput/redacted_key.png"]

        monkeypatch.setattr(
            "scans.scan_redaction.replace_placeholder_with_server_redacted_coords",
            fake_replace,
        )

        result = apply_deferred_redaction_task.apply(args=(str(placeholder.id),)).get()

        assert result == {"status": "applied", "placeholder_id": str(placeholder.id)}
        # Renderer called exactly once with the post-charge rectangles.
        assert captured == [[[{"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.05}]]]

    def test_safety_net_applies_cvv_first_when_status_cvv_pending(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CVV_PENDING → CVV layer applied first, then post-charge layer."""
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
            return ["ScanOutput/redacted_key.png"]

        monkeypatch.setattr(
            "scans.scan_redaction.replace_placeholder_with_server_redacted_coords",
            fake_replace,
        )

        result = apply_deferred_redaction_task.apply(args=(str(placeholder.id),)).get()

        assert result["status"] == "applied"
        # Renderer was called twice: CVV first, then post-charge.
        assert len(captured) == 2
        assert captured[0] == [[{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]]
        assert captured[1] == [[{"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.05}]]
        placeholder.refresh_from_db()
        assert placeholder.cvv_redacted_at is not None

    def test_returns_not_found_when_placeholder_missing(self) -> None:
        """A stale placeholder id returns a not_found result, no exception."""
        result = apply_deferred_redaction_task.apply(
            args=("00000000-0000-0000-0000-000000000000",)
        ).get()
        assert result == {
            "status": "not_found",
            "placeholder_id": "00000000-0000-0000-0000-000000000000",
        }

    def test_value_error_is_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ValueError from the renderer is reported as non-retryable error."""
        donation = DonationFactory(payment_method="card")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_DEFERRED,
            redaction_coords_post_charge=[
                [{"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}]
            ],
        )

        def fake_replace(*_a: Any, **_k: Any) -> list[str]:
            raise ValueError("bad coords")

        monkeypatch.setattr(
            "scans.scan_redaction.replace_placeholder_with_server_redacted_coords",
            fake_replace,
        )

        result = apply_deferred_redaction_task.apply(args=(str(placeholder.id),)).get()

        assert result["status"] == "error"
        assert result["retryable"] is False
        assert "bad coords" in result["message"]
