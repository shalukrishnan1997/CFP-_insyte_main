"""Client portal access-control middleware.

Redirects authenticated users to the dashboard appropriate for their role
(staff → admin, client → portal). Enforces mandatory 2FA and exempts the
authentication URLs from redirection logic.
"""

from django.conf import settings
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import resolve
from django.utils.deprecation import MiddlewareMixin
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from auth_app.models import EmailDevice
from responsehandling.permissions import user_has_access


def _has_confirmed_2fa_device(user: object) -> bool:
    """Return True when a user has at least one confirmed OTP-capable device."""
    has_totp = TOTPDevice.objects.devices_for_user(user, confirmed=True).exists()
    has_email = EmailDevice.objects.devices_for_user(user, confirmed=True).exists()
    has_backup = StaticDevice.objects.devices_for_user(user, confirmed=True).exists()
    return has_totp or has_email or has_backup


class ClientPortalMiddleware(MiddlewareMixin):
    """Middleware to redirect users based on their role.

    After login, users are redirected to the appropriate dashboard:
    - is_staff → Admin dashboard
    - has client_profile → Client portal
    - neither → Access denied

    Exempts authentication URLs from redirection logic.
    """

    EXEMPT_URLS = [
        "login",
        "logout",
        "setup_2fa",
        "manage_2fa",
        "show_backup_codes",
        "user_profile",
        "change_password",
        "password_reset",
        "password_reset_done",
        "password_reset_confirm",
        "password_reset_complete",
    ]

    def process_request(self, request: HttpRequest) -> HttpResponseRedirect | None:  # type: ignore[override]
        """Process incoming request and redirect based on user role.

        Args:
            request: HttpRequest object.

        Returns:
            None to continue processing or HttpResponseRedirect to redirect.
        """
        if not request.user.is_authenticated:
            return None

        try:
            current_url_name = resolve(request.path).url_name
        except Exception:
            return None

        if current_url_name in self.EXEMPT_URLS:
            return None

        # Enforce mandatory 2FA for all authenticated users
        has_confirmed_device = _has_confirmed_2fa_device(request.user)
        has_verified_2fa = has_confirmed_device and request.user.is_verified()
        if not has_verified_2fa:
            if not has_confirmed_device:
                return redirect("auth_app:setup_2fa")
            return redirect(
                f"{settings.LOGIN_URL}?{REDIRECT_FIELD_NAME}={request.path}"
            )

        user = request.user

        if user.is_staff:
            if request.path.startswith("/client/"):
                return redirect("custom_admin:admin_dashboard")
            return None

        if hasattr(user, "client_portal_profile") and user.client_portal_profile:
            if request.path.startswith("/admin/"):
                return redirect("client_portal:dashboard")
            return None

        # Admin URLs: allow the same users as @is_authenticated_and_is_staff
        # (staff or any group/permission), not only is_staff=True.
        if request.path.startswith("/admin/"):
            if user_has_access(user):
                return None
            return redirect(
                f"{settings.LOGIN_URL}?{REDIRECT_FIELD_NAME}={request.path}"
            )

        if request.path.startswith("/client/"):
            return redirect(
                f"{settings.LOGIN_URL}?{REDIRECT_FIELD_NAME}={request.path}"
            )

        return None
