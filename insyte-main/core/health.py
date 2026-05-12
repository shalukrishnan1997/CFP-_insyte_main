"""Health check endpoint for production monitoring.

Verifies connectivity to critical infrastructure:
- Database (PostgreSQL / SQLite)
- Cache (Redis / LocMem)
- Celery worker availability
- Document AI configuration (presence check)
- Resend email backend configuration (presence check)

Returns HTTP 200 if all checks pass, HTTP 503 if any fail.
"""

import logging

from django.conf import settings
from django.db import connection
from django.http import HttpRequest, JsonResponse

logger = logging.getLogger(__name__)


def _result(status: str, healthy: bool, warning: bool = False) -> dict[str, str | bool]:
    """Build a normalized health check payload."""
    payload: dict[str, str | bool] = {"status": status, "healthy": healthy}
    if warning:
        payload["warning"] = True
    return payload


def health_live(request: HttpRequest) -> JsonResponse:
    """Minimal liveness probe for reverse proxies and container orchestration.

    Only verifies that the app process can reach the database. Use this for
    Docker/Kubernetes/Coolify health checks so a temporary Celery or Redis
    outage does not drain all backends (which surfaces as "no available server"
    from the load balancer).

    Full dependency checks remain on :func:`health_check` (``/health/``).

    Args:
        request: HTTP request.

    Returns:
        JSON ``{"alive": true}`` with status 200, or 503 if the DB is unreachable.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as e:
        logger.error("Health live: database failed — %s", e)
        return JsonResponse(
            {"alive": False, "error": "database_unavailable"}, status=503
        )
    return JsonResponse({"alive": True}, status=200)


def health_check(request: HttpRequest) -> JsonResponse:
    """Health check endpoint for load balancers and monitoring.

    Checks database, cache, and Celery connectivity.
    No authentication required — returns minimal info.

    Args:
        request: HTTP request.

    Returns:
        JSON response with status of each component.
    """
    checks: dict[str, dict[str, str | bool]] = {}
    healthy = True

    # Database check
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = _result("ok", True)
    except Exception as e:
        logger.error("Health check: database failed — %s", e)
        checks["database"] = _result("unavailable", False)
        healthy = False

    # Cache check
    try:
        from django.core.cache import cache

        cache.set("_health_check", "ok", 10)
        value = cache.get("_health_check")
        if value == "ok":
            checks["cache"] = _result("ok", True)
        else:
            checks["cache"] = _result("mismatch", False)
            healthy = False
    except Exception as e:
        logger.error("Health check: cache failed — %s", e)
        checks["cache"] = _result("unavailable", False)
        healthy = False

    healthy_ref = [healthy]
    _check_celery(checks, healthy_ref)
    healthy = healthy_ref[0]

    # Document AI configuration check
    _check_document_ai(checks, healthy_ref)
    healthy = healthy_ref[0]

    # Resend email backend configuration check
    _check_resend(checks, healthy_ref)
    healthy = healthy_ref[0]

    status_code = 200 if healthy else 503
    return JsonResponse(
        {"healthy": healthy, "checks": checks},
        status=status_code,
    )


def _check_document_ai(checks: dict, healthy_ref: list[bool]) -> None:
    """Add a Document AI configuration check to the health checks dict.

    This is a lightweight presence check — it does not make a live API call.
    It verifies that GOOGLE_CLOUD_PROJECT_ID is set and at least one active
    Client has a Document_ai_processor_id configured.

    Args:
        checks: Mutable checks dict to update.
        healthy_ref: Single-element list whose first item is the overall
            health boolean (mutated in place).
    """
    import os

    from django.conf import settings

    project_id = getattr(settings, "GOOGLE_CLOUD_PROJECT_ID", "")
    has_credentials = bool(
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", "")
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON_B64", "")
    )

    if not project_id:
        checks["document_ai"] = _result("not_configured", True, warning=True)
        return

    if not has_credentials:
        checks["document_ai"] = _result("not_configured", True, warning=True)
        return

    try:
        from clients.models import Client

        configured_count = (
            Client.objects.filter(
                is_active=True,
            )
            .exclude(document_ai_processor_id="")
            .count()
        )

        checks["document_ai"] = _result(
            "configured" if configured_count else "not_configured",
            True,
            warning=configured_count == 0,
        )
    except Exception as exc:  # pragma: no cover
        logger.error("Health check: document_ai failed — %s", exc)
        checks["document_ai"] = _result("unavailable", False)
        healthy_ref[0] = False


def _check_resend(
    checks: dict[str, dict[str, str | bool]], healthy_ref: list[bool]
) -> None:
    """Add a Resend email backend configuration check.

    Presence check only — no live API call (Resend has rate limits, and a
    readiness probe is the wrong place to spend quota). Only relevant when
    EMAIL_BACKEND is the Resend backend; SMTP/console/locmem deployments
    report ``not_applicable``. A missing key is reported as a warning rather
    than a hard failure, matching the Document AI check.

    ``healthy_ref`` is accepted for parity with other ``_check_*`` helpers but
    is never flipped here — a misconfigured key is a warning, not a failure.
    """
    del healthy_ref  # unused; kept for signature parity
    backend = str(getattr(settings, "EMAIL_BACKEND", ""))
    if backend != "core.mail_backends.ResendBackend":
        checks["resend"] = _result("not_applicable", True, warning=True)
        return

    api_key = getattr(settings, "RESEND_API_KEY", "") or ""
    if not api_key:
        checks["resend"] = _result("not_configured", True, warning=True)
        return
    checks["resend"] = _result("configured", True)


def _check_celery(
    checks: dict[str, dict[str, str | bool]], healthy_ref: list[bool]
) -> None:
    """Add the Celery readiness check using the configured verification mode.

    ``worker`` mode requires a live worker responding to Celery inspect and is
    the production-safe default. ``broker`` mode verifies broker connectivity
    only, which is useful for local SQLite-backed development where remote
    inspect is not supported reliably.
    """
    from responsehandling.celery import app as celery_app

    mode = str(getattr(settings, "HEALTH_CHECK_CELERY_MODE", "worker")).lower()

    try:
        if mode == "broker":
            connection_obj = celery_app.connection_for_read()
            try:
                connection_obj.ensure_connection(max_retries=0)
            finally:
                connection_obj.release()
            checks["celery"] = _result("broker_ok", True, warning=True)
            return

        inspect = celery_app.control.inspect(timeout=2.0)
        stats = inspect.stats()
        if stats:
            checks["celery"] = _result("ok", True)
            return

        checks["celery"] = _result("unavailable", False)
        healthy_ref[0] = False
    except Exception as exc:
        logger.error("Health check: celery failed — %s", exc)
        checks["celery"] = _result("unavailable", False)
        healthy_ref[0] = False
