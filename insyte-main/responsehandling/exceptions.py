"""Custom exception handlers for REST API and shared JSON response helpers."""

from typing import Any

from django.http import JsonResponse
from rest_framework.response import Response
from rest_framework.views import exception_handler


def custom_exception_handler(
    exc: Exception, context: dict[str, Any]
) -> Response | None:
    """Custom exception handler that provides consistent error responses.

    Args:
        exc: The exception that was raised.
        context: Dictionary with view, args, kwargs, and request.

    Returns:
        DRF Response with standardised error format, or None.
    """
    # Call REST framework's default exception handler first
    response = exception_handler(exc, context)

    if response is not None:
        # Standardize error response format
        custom_response_data = {
            "success": False,
            "error": {
                "message": str(exc),
                "detail": response.data,
                "status_code": response.status_code,
            },
        }
        response.data = custom_response_data

    return response


def json_error(msg: str, status: int = 400) -> JsonResponse:
    """Return a standardised ``{"success": false, "error": "..."}`` response."""
    return JsonResponse({"success": False, "error": msg}, status=status)


def json_ok(data: dict[str, Any] | None = None) -> JsonResponse:
    """Return a standardised ``{"success": true, ...}`` response."""
    return JsonResponse({"success": True, **(data or {})})
