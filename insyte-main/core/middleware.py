"""Generic request-scoped middleware for the core app.

Currently provides :class:`RequestIDMiddleware`, which assigns each incoming
request a stable correlation ID for log aggregation and trace correlation
across the QA, scan, and payment pipelines.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from collections.abc import Callable
from typing import Final

from django.http import HttpRequest, HttpResponse

REQUEST_ID_HEADER: Final = "X-Request-ID"
REQUEST_ID_META_KEY: Final = "HTTP_X_REQUEST_ID"

# Context variable that propagates the active request_id to anything that
# runs inside the request lifecycle — including logging filters, signal
# handlers, and any nested helper code that wants to record the correlation
# ID without taking the HttpRequest as an argument.
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "insyte_request_id",
    default="",
)


def get_current_request_id() -> str:
    """Return the request ID for the active request, or empty string.

    Safe to call outside a request (e.g. from Celery tasks); returns ``""``
    when no request is in flight.
    """
    return _request_id_var.get()


def set_current_request_id(request_id: str) -> contextvars.Token[str]:
    """Set the active request ID and return the reset token.

    Exposed primarily so background workers (Celery tasks, management
    commands) can stamp their own correlation ID and tear it down again.
    """
    return _request_id_var.set(request_id)


def reset_current_request_id(token: contextvars.Token[str]) -> None:
    """Reset the request-id contextvar using a token from :func:`set_current_request_id`."""
    _request_id_var.reset(token)


def _generate_request_id() -> str:
    """Return a fresh UUIDv4 string for use as a request ID."""
    return str(uuid.uuid4())


class RequestIDMiddleware:
    """Attach a stable request ID to every request and response.

    Behaviour:
        * If the incoming request carries an ``X-Request-ID`` header it is
          reused (so upstream proxies / load balancers can propagate a trace
          ID end-to-end).
        * Otherwise a fresh UUIDv4 is generated.
        * The ID is stored on ``request.request_id`` for in-process callers,
          published on a :mod:`contextvars` ContextVar so log filters can
          read it without a request handle, and echoed back on the response
          via the ``X-Request-ID`` header.

    Should run very early in the middleware stack — placed straight after
    :class:`core.security_middleware.ScanDetectionMiddleware` so subsequent
    middleware (auth, audit, client portal) and any logging that happens
    inside them all share the same correlation ID.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.META.get(REQUEST_ID_META_KEY, "").strip()
        request_id = incoming or _generate_request_id()

        # Stash on the request so view code can include it in domain logs
        # without needing to import this module's contextvar helpers.
        request.request_id = request_id  # type: ignore[attr-defined]

        token = set_current_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            reset_current_request_id(token)

        response[REQUEST_ID_HEADER] = request_id
        return response


class RequestIDLogFilter(logging.Filter):
    """Logging filter that injects ``request_id`` into every log record.

    Pairs with the ``json`` formatter in
    :mod:`responsehandling.settings.production` so each emitted JSON line
    carries the correlation ID without every call site having to pass it
    through ``extra=``.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        record.request_id = get_current_request_id() or "-"
        return True
