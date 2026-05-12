"""Unit tests for ``core.middleware.RequestIDMiddleware`` and its log filter."""

from __future__ import annotations

import logging
import re
import uuid

from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory

from core.middleware import (
    REQUEST_ID_HEADER,
    RequestIDLogFilter,
    RequestIDMiddleware,
    get_current_request_id,
)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _ok_view(request: HttpRequest) -> HttpResponse:
    """Trivial view returning 200 OK; used as the wrapped middleware target."""
    return HttpResponse("ok")


class TestRequestIDMiddlewareGenerates:
    """Behaviour when no incoming X-Request-ID header is supplied."""

    def test_generates_uuid_when_header_absent(self, rf: RequestFactory) -> None:
        middleware = RequestIDMiddleware(_ok_view)
        request = rf.get("/whatever/")

        response = middleware(request)

        assert response.status_code == 200
        request_id = response[REQUEST_ID_HEADER]
        assert _UUID_RE.match(request_id), f"expected UUID, got {request_id!r}"

    def test_attaches_request_id_to_request_object(self, rf: RequestFactory) -> None:
        captured: dict[str, str] = {}

        def view(request: HttpRequest) -> HttpResponse:
            captured["id"] = request.request_id  # type: ignore[attr-defined]
            return HttpResponse("ok")

        middleware = RequestIDMiddleware(view)
        request = rf.get("/whatever/")
        response = middleware(request)

        assert captured["id"] == response[REQUEST_ID_HEADER]


class TestRequestIDMiddlewarePropagates:
    """Behaviour when the upstream proxy/load balancer supplied a header."""

    def test_reuses_incoming_header(self, rf: RequestFactory) -> None:
        upstream_id = str(uuid.uuid4())
        middleware = RequestIDMiddleware(_ok_view)
        request = rf.get("/whatever/", HTTP_X_REQUEST_ID=upstream_id)

        response = middleware(request)

        assert response[REQUEST_ID_HEADER] == upstream_id

    def test_contextvar_clears_after_response(self, rf: RequestFactory) -> None:
        middleware = RequestIDMiddleware(_ok_view)
        request = rf.get("/whatever/")
        middleware(request)

        # Outside the request lifetime the contextvar must not leak the ID.
        assert get_current_request_id() == ""


class TestRequestIDLogFilter:
    """Pairs with the JSON formatter to surface the ID in production logs."""

    def test_filter_injects_request_id_attribute(self, rf: RequestFactory) -> None:
        captured: dict[str, str] = {}
        log_filter = RequestIDLogFilter()

        def view(request: HttpRequest) -> HttpResponse:
            record = logging.LogRecord(
                name="insyte.test",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg="hello",
                args=None,
                exc_info=None,
            )
            log_filter.filter(record)
            captured["id"] = record.request_id  # type: ignore[attr-defined]
            return HttpResponse("ok")

        middleware = RequestIDMiddleware(view)
        response = middleware(rf.get("/whatever/"))

        assert captured["id"] == response[REQUEST_ID_HEADER]
        assert _UUID_RE.match(captured["id"])

    def test_filter_falls_back_to_dash_outside_request(self) -> None:
        log_filter = RequestIDLogFilter()
        record = logging.LogRecord(
            name="insyte.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=None,
            exc_info=None,
        )

        log_filter.filter(record)

        # Default sentinel keeps log lines well-formed when emitted from
        # Celery / management commands that have no active request.
        assert record.request_id == "-"  # type: ignore[attr-defined]
