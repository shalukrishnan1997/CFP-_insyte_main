import logging
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

logger = logging.getLogger("security")

# List of sensitive paths commonly targeted by bot scans
SENSITIVE_PATHS = [
    ".env",
    ".git",
    "wp-config.php",
    "phpinfo.php",
    "info.php",
    ".aws/credentials",
    "config.json",
    ".env.local",
    ".env.production",
    ".env.development",
]


class ScanDetectionMiddleware:
    """
    Middleware to detect and log (or block) frequent 404 scans for sensitive paths.

    This helps in identifying automated security scans probing for sensitive
    configuration files or known vulnerabilities.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        path = request.path.lower()

        # Check if the requested path contains any sensitive patterns
        if any(sp in path for sp in SENSITIVE_PATHS):
            client_ip = self._get_client_ip(request)
            user_agent = request.META.get("HTTP_USER_AGENT", "Unknown")

            logger.warning(
                f"SECURITY SCAN DETECTED: IP={client_ip} | path={path} | UA={user_agent}"
            )

            # Optional: We could return 403 Forbidden here to signal awareness,
            # but 404 is often safer as it confirms the file "doesn't exist".
            # For now, we just log and continue to allow local/legitimate 404 handling.

        return self.get_response(request)

    def _get_client_ip(self, request: HttpRequest) -> str:
        """Helper to extract client IP including proxy headers."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "unknown")
