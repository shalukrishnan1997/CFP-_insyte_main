"""Audit-context middleware.

Stores the current request in a contextvar (via ``audit.signals``) so audit
signal handlers can attribute changes to the requesting user.
"""

from django.http import HttpRequest, HttpResponse
from django.utils.deprecation import MiddlewareMixin


class AuditRequestMiddleware(MiddlewareMixin):
    """Inject request context for audit logging.

    Performance impact: Negligible (<1ms per request).
    """

    def process_request(self, request: HttpRequest) -> None:  # type: ignore[override]
        from audit.signals import set_current_request

        set_current_request(request)
        return None

    def process_response(
        self, request: HttpRequest, response: HttpResponse
    ) -> HttpResponse:  # type: ignore[override]
        from audit.signals import clear_current_request

        clear_current_request()
        return response

    def process_exception(self, request: HttpRequest, exception: Exception) -> None:  # type: ignore[override]
        from audit.signals import clear_current_request

        clear_current_request()
        return None
