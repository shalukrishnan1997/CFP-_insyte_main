"""Unit tests for ``core.metrics`` — Prometheus counters + /metrics view."""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from core.metrics import (
    CONTENT_TYPE_LATEST,
    QA_DONATIONS_APPROVED_TOTAL,
    SCAN_OCR_LATENCY_SECONDS,
    observe_scan_ocr_latency,
    record_payment_attempt,
    record_qa_donation_approved,
)


@pytest.fixture()
def metrics_url() -> str:
    """Resolve the /metrics endpoint via its named URL."""
    return reverse("prometheus_metrics")


class TestMetricsEndpointAuth:
    """The /metrics endpoint must reject non-allowlisted IPs."""

    def test_default_allowlist_permits_localhost(
        self, client: Client, metrics_url: str
    ) -> None:
        response = client.get(metrics_url, REMOTE_ADDR="127.0.0.1")

        assert response.status_code == 200
        assert response["Content-Type"].startswith(CONTENT_TYPE_LATEST.split(";")[0])

    def test_disallowed_ip_returns_403(self, client: Client, metrics_url: str) -> None:
        response = client.get(metrics_url, REMOTE_ADDR="203.0.113.42")

        assert response.status_code == 403

    def test_setting_overrides_allowlist(
        self, client: Client, metrics_url: str, settings: pytest.FixtureRequest
    ) -> None:
        settings.METRICS_ALLOWED_IPS = ["10.20.30.40"]  # type: ignore[attr-defined]

        allowed = client.get(metrics_url, REMOTE_ADDR="10.20.30.40")
        denied = client.get(metrics_url, REMOTE_ADDR="127.0.0.1")

        assert allowed.status_code == 200
        assert denied.status_code == 403

    def test_x_forwarded_for_is_honoured(
        self, client: Client, metrics_url: str
    ) -> None:
        # When sat behind Traefik / Coolify the proxy forwards the real client
        # IP via X-Forwarded-For — the metrics view must inspect it so the
        # allowlist actually matches the upstream Prometheus host. The header
        # is only trusted when REMOTE_ADDR is in METRICS_TRUSTED_PROXIES,
        # which by default contains localhost.
        response = client.get(
            metrics_url,
            REMOTE_ADDR="127.0.0.1",  # trusted proxy
            HTTP_X_FORWARDED_FOR="127.0.0.1, 10.0.0.1",
        )

        assert response.status_code == 200

    def test_x_forwarded_for_ignored_when_remote_addr_untrusted(
        self, client: Client, metrics_url: str
    ) -> None:
        # An attacker on the public internet must not be able to spoof an
        # allowlisted IP via X-Forwarded-For. When REMOTE_ADDR is not in
        # METRICS_TRUSTED_PROXIES the header is ignored entirely, so the
        # request is judged by REMOTE_ADDR (8.8.8.8) and rejected.
        response = client.get(
            metrics_url,
            REMOTE_ADDR="8.8.8.8",
            HTTP_X_FORWARDED_FOR="127.0.0.1",
        )

        assert response.status_code == 403


class TestMetricsExpose:
    """The exposition payload must include our domain metrics."""

    def test_qa_counter_increments_and_appears_in_payload(
        self, client: Client, metrics_url: str
    ) -> None:
        before = QA_DONATIONS_APPROVED_TOTAL.labels(
            method="cash", client="acme-charity"
        )._value.get()  # type: ignore[attr-defined]

        record_qa_donation_approved(method="cash", client="acme-charity")

        after = QA_DONATIONS_APPROVED_TOTAL.labels(
            method="cash", client="acme-charity"
        )._value.get()  # type: ignore[attr-defined]
        assert after == before + 1

        response = client.get(metrics_url, REMOTE_ADDR="127.0.0.1")
        assert response.status_code == 200
        body = response.content.decode("utf-8")
        assert "qa_donations_approved_total" in body
        assert 'method="cash"' in body
        assert 'client="acme-charity"' in body

    def test_payment_counter_renders(self, client: Client, metrics_url: str) -> None:
        record_payment_attempt(
            status="success", method="checkout_session", client="acme"
        )

        response = client.get(metrics_url, REMOTE_ADDR="127.0.0.1")
        body = response.content.decode("utf-8")
        assert "payment_attempt_total" in body
        assert 'status="success"' in body
        assert 'method="checkout_session"' in body

    def test_scan_ocr_histogram_renders_buckets(
        self, client: Client, metrics_url: str
    ) -> None:
        observe_scan_ocr_latency(0.42, client="acme")

        response = client.get(metrics_url, REMOTE_ADDR="127.0.0.1")
        body = response.content.decode("utf-8")
        # Histograms expose _bucket / _count / _sum series.
        assert "scan_ocr_latency_seconds_bucket" in body
        assert "scan_ocr_latency_seconds_count" in body
        assert SCAN_OCR_LATENCY_SECONDS._name == "scan_ocr_latency_seconds"  # type: ignore[attr-defined]


class TestMetricLabelHardening:
    """Empty / missing labels must coerce to ``"unknown"`` so cardinality stays bounded."""

    def test_blank_method_becomes_unknown(
        self, client: Client, metrics_url: str
    ) -> None:
        record_qa_donation_approved(method="", client=None)
        response = client.get(metrics_url, REMOTE_ADDR="127.0.0.1")
        body = response.content.decode("utf-8")
        assert 'method="unknown"' in body
        assert 'client="unknown"' in body
