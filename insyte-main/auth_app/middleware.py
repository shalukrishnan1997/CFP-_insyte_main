"""Auth-related middleware.

Currently hosts ``ForcePasswordChangeMiddleware``, which redirects authenticated
users whose ``must_change_password`` flag is set to the change-password screen
until they replace the admin-chosen password with one of their own.
"""

from django.contrib import messages
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import resolve
from django.utils.deprecation import MiddlewareMixin


class ForcePasswordChangeMiddleware(MiddlewareMixin):
    """Gate every page behind ``change_password`` when the flag is set.

    Runs after :class:`client_portal.middleware.ClientPortalMiddleware` so 2FA
    enrolment still takes priority. The password-change screen itself, logout,
    and the forgot-password reset flow are exempt so the user is never trapped.
    """

    EXEMPT_URL_NAMES: frozenset[str] = frozenset(
        {
            "change_password",
            "logout",
            "password_reset",
            "password_reset_done",
            "password_reset_confirm",
            "password_reset_complete",
        }
    )

    _INFO_MESSAGE = "Please set a new password before continuing."

    def process_request(self, request: HttpRequest) -> HttpResponseRedirect | None:  # type: ignore[override]
        """Redirect to ``change_password`` when the current user must change it.

        Skips anonymous users, static/media, and the exempt URL names. Also
        skips users who have not yet completed 2FA verification — letting
        ``ClientPortalMiddleware`` funnel them into setup first.
        """
        if not request.user.is_authenticated:
            return None

        if not getattr(request.user, "must_change_password", False):
            return None

        try:
            current_url_name = resolve(request.path).url_name
        except Exception:
            return None

        if current_url_name in self.EXEMPT_URL_NAMES:
            return None

        if not request.user.is_verified():
            return None

        messages.info(request, self._INFO_MESSAGE)
        return redirect("auth_app:change_password")
