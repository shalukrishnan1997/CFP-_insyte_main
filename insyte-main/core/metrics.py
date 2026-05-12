"""Prometheus metrics for the INSYTE observability stack.

Exposes domain-level counters and histograms covering the QA review queue,
the Stripe payment pipeline, and the OCR scan pipeline. Metric *definitions*
live here so every instrumentation site imports the same instance and the
:class:`prometheus_client.CollectorRegistry` does not see duplicate metrics
across reloads.

The :func:`metrics_view` Django view serves the Prometheus exposition
format for any IP in ``settings.METRICS_ALLOWED_IPS`` — typically the
internal monitoring host(s). Public exposure is blocked with HTTP 403.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Final

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

QA_DONATIONS_APPROVED_TOTAL: Final[Counter] = Counter(
    "qa_donations_approved_total",
    "Total donations whose QA status flipped to approved.",
    labelnames=("method", "client"),
)

PAYMENT_ATTEMPT_TOTAL: Final[Counter] = Counter(
    "payment_attempt_total",
    "Total Stripe API payment attempts, labelled by outcome.",
    labelnames=("status", "method", "client"),
)

SCAN_OCR_LATENCY_SECONDS: Final[Histogram] = Histogram(
    "scan_ocr_latency_seconds",
    "Wall-clock latency of a single OCR call against Google Document AI.",
    labelnames=("client",),
    # Buckets tuned for typical Document AI latency (sub-second to ~30s
    # for slow PDF rasterisation / batched pages).
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0),
)


# ---------------------------------------------------------------------------
# Instrumentation helpers (import-friendly wrappers)
# ---------------------------------------------------------------------------


def _safe_label(value: str | None) -> str:
    """Return a Prometheus-friendly label, falling back to ``"unknown"``.

    Empty strings are intentionally normalised because Prometheus treats
    ``label=""`` and ``label`` as different time series, which would
    bloat cardinality on missing data.
    """
    if not value:
        return "unknown"
    return str(value)


def record_qa_donation_approved(method: str | None, client: str | None) -> None:
    """Increment :data:`QA_DONATIONS_APPROVED_TOTAL` once per QA approval."""
    QA_DONATIONS_APPROVED_TOTAL.labels(
        method=_safe_label(method),
        client=_safe_label(client),
    ).inc()


def record_payment_attempt(status: str, method: str | None, client: str | None) -> None:
    """Increment :data:`PAYMENT_ATTEMPT_TOTAL` for one Stripe API call."""
    PAYMENT_ATTEMPT_TOTAL.labels(
        status=_safe_label(status),
        method=_safe_label(method),
        client=_safe_label(client),
    ).inc()


def observe_scan_ocr_latency(seconds: float, client: str | None) -> None:
    """Observe one OCR call's wall-clock duration in seconds."""
    SCAN_OCR_LATENCY_SECONDS.labels(client=_safe_label(client)).observe(seconds)


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------


_DEFAULT_ALLOWED_IPS: Final[tuple[str, ...]] = ("127.0.0.1", "::1")
_DEFAULT_TRUSTED_PROXIES: Final[tuple[str, ...]] = ("127.0.0.1", "::1")


def _trusted_proxies() -> Iterable[str]:
    """Return the configured trusted-proxy list, defaulting to localhost only."""
    return getattr(settings, "METRICS_TRUSTED_PROXIES", _DEFAULT_TRUSTED_PROXIES)


def _client_ip(request: HttpRequest) -> str:
    """Best-effort client IP.

    ``X-Forwarded-For`` is only honoured when ``REMOTE_ADDR`` is itself in
    ``settings.METRICS_TRUSTED_PROXIES`` — otherwise an unauthenticated
    attacker could spoof the header from the public internet and bypass the
    metrics IP allowlist. When the connecting peer is not a trusted proxy
    we ignore the header entirely and return ``REMOTE_ADDR`` directly.
    """
    remote_addr = request.META.get("REMOTE_ADDR", "")
    if remote_addr in set(_trusted_proxies()):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            # First entry is the original client; trim whitespace defensively.
            return forwarded.split(",")[0].strip()
    return remote_addr


def _allowed_ips() -> Iterable[str]:
    """Return the configured allowlist, defaulting to localhost only."""
    return getattr(settings, "METRICS_ALLOWED_IPS", _DEFAULT_ALLOWED_IPS)


def _is_allowed(request: HttpRequest) -> bool:
    """Whether the request's source IP is in the metrics allowlist."""
    return _client_ip(request) in set(_allowed_ips())


@csrf_exempt
@require_GET
def metrics_view(request: HttpRequest) -> HttpResponse:
    """Expose Prometheus metrics over HTTP for internal scrapers.

    Args:
        request: HTTP GET request from the Prometheus scraper.

    Returns:
        HTTP 200 with the latest exposition payload for allowed IPs;
        HTTP 403 for everyone else.
    """
    if not _is_allowed(request):
        client_ip = _client_ip(request)
        logger.warning("Metrics scrape denied: ip=%s path=%s", client_ip, request.path)
        return HttpResponseForbidden(
            "Metrics endpoint is restricted to internal monitoring hosts."
        )

    registry: CollectorRegistry = REGISTRY
    payload = generate_latest(registry)
    return HttpResponse(payload, content_type=CONTENT_TYPE_LATEST)
