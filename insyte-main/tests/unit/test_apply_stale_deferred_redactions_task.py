"""Unit tests for the retention-TTL sweeper task.

``apply_stale_deferred_redactions_task`` runs hourly via Celery beat. It
force-applies any placeholder stuck in ``REDACTION_CVV_PENDING`` or
``REDACTION_DEFERRED`` past ``INSYTE_REDACTION_RETENTION_DAYS`` so a
donation that never converges (SCA abandoned, operator forgot, queue
dispatch lost) doesn't leak PII indefinitely.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from scans.models import ScanPlaceholder
from scans.tasks import apply_stale_deferred_redactions_task
from tests.factories import DonationFactory, ScanPlaceholderFactory


@pytest.fixture(autouse=True)
def _seven_day_ttl(settings: Any) -> None:
    """Pin the retention TTL to 7 days for every test in this module."""
    settings.INSYTE_REDACTION_RETENTION_DAYS = 7


def _backdate(placeholder: ScanPlaceholder, days_ago: int) -> None:
    """Force ``updated_at`` to *days_ago* days in the past via raw UPDATE.

    ``auto_now`` overrides any value set on a normal save, so we have to
    bypass the model layer to predate the placeholder.
    """
    cutoff = timezone.now() - timedelta(days=days_ago)
    ScanPlaceholder.objects.filter(pk=placeholder.pk).update(updated_at=cutoff)


def _patch_post_charge_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Replace ``apply_deferred_redaction_task.delay`` with a recorder."""
    calls: list[tuple[str, ...]] = []

    def fake_delay(*args: str, **_kwargs: Any) -> None:
        calls.append(args)

    from scans import tasks

    monkeypatch.setattr(tasks.apply_deferred_redaction_task, "delay", fake_delay)
    return calls


@pytest.mark.django_db()
class TestApplyStaleDeferredRedactionsTask:
    """Coverage for the periodic retention TTL sweeper."""

    def test_sweeps_cvv_pending_past_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A CVV_PENDING placeholder older than the TTL gets enqueued."""
        donation = DonationFactory(payment_method="card", payment_status="failed")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
        )
        _backdate(placeholder, days_ago=8)
        calls = _patch_post_charge_delay(monkeypatch)

        result = apply_stale_deferred_redactions_task()

        assert result["status"] == "ok"
        assert result["swept"] == 1
        assert calls == [(str(placeholder.id),)]

    def test_sweeps_deferred_past_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A DEFERRED placeholder past the TTL also gets enqueued."""
        donation = DonationFactory(payment_method="card", payment_status="completed")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_DEFERRED,
        )
        _backdate(placeholder, days_ago=10)
        calls = _patch_post_charge_delay(monkeypatch)

        result = apply_stale_deferred_redactions_task()

        assert result["swept"] == 1
        assert calls == [(str(placeholder.id),)]

    def test_skips_within_ttl_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Recent placeholders are left alone."""
        donation = DonationFactory(payment_method="card", payment_status="failed")
        ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_CVV_PENDING,
        )
        # No backdating — updated_at = now.
        calls = _patch_post_charge_delay(monkeypatch)

        result = apply_stale_deferred_redactions_task()

        assert result["swept"] == 0
        assert calls == []

    def test_skips_completed_placeholders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Already-redacted placeholders are not re-swept regardless of age."""
        donation = DonationFactory(payment_method="card", payment_status="completed")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
        )
        _backdate(placeholder, days_ago=30)
        calls = _patch_post_charge_delay(monkeypatch)

        result = apply_stale_deferred_redactions_task()

        assert result["swept"] == 0
        assert calls == []

    def test_skips_pending_placeholders_without_coords(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PENDING (no coords saved yet) is not swept — operator hasn't engaged."""
        donation = DonationFactory(payment_method="card", payment_status="pending")
        placeholder = ScanPlaceholderFactory(
            donation=donation,
            redaction_status=ScanPlaceholder.REDACTION_PENDING,
        )
        _backdate(placeholder, days_ago=30)
        calls = _patch_post_charge_delay(monkeypatch)

        result = apply_stale_deferred_redactions_task()

        # Nothing to apply — the operator hasn't drawn coords. The
        # PENDING-and-stale case is a separate alert surface, not a
        # forced-redact case.
        assert result["swept"] == 0
        assert calls == []
