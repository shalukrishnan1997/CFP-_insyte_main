import time
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponseBase
from django.shortcuts import redirect
from django.urls import reverse
from rest_framework.permissions import BasePermission

from core.agent_debug import agent_debug_log


def is_authenticated_and_is_staff(
    view_func: Callable[..., HttpResponseBase] | None = None,
    login_url: str = "",
    non_staff_redirect: str | None = None,
    redirect_field_name: str = "next",
) -> Callable[..., Any]:
    """Combined decorator that checks authentication and staff/permission status.

    Shows a friendly message and redirects to the login page if the user is
    not authenticated.  Allows access if user is staff OR has groups/permissions.

    Args:
        view_func: The view function to decorate.
        login_url: URL to redirect unauthenticated users to.
        non_staff_redirect: URL for users without system access; default is login page.
        redirect_field_name: Query parameter name for the redirect URL.

    Returns:
        Decorated view function or decorator.
    """
    login_url = login_url or settings.LOGIN_URL

    def decorator(
        view: Callable[..., HttpResponseBase],
    ) -> Callable[..., HttpResponseBase]:
        @wraps(view)
        def _wrapped_view(
            request: HttpRequest, *args: Any, **kwargs: Any
        ) -> HttpResponseBase:
            start = time.perf_counter()
            user = getattr(request, "user", None)

            # #region agent log
            if request.path == "/dashboard/":
                agent_debug_log(
                    "permissions.py:is_authenticated_and_is_staff",
                    "dashboard decorator entry",
                    "H1",
                    {
                        "path": request.path,
                        "has_user": bool(user),
                        "is_authenticated": bool(
                            getattr(user, "is_authenticated", False)
                        ),
                    },
                )
            # #endregion

            # Not authenticated -> send to login with a helpful message
            if not (user and user.is_authenticated):
                messages.info(request, "Please sign in to continue.")
                return redirect(login_url)

            # Re-check is_active so a session that was active when an admin
            # deactivated the account stops being honored on the next request.
            if not getattr(user, "is_active", True):
                messages.info(request, "Please sign in to continue.")
                return redirect(login_url)

            # Check if user has system access (staff OR has groups/permissions)
            has_access = (
                getattr(user, "is_staff", False)
                or user.groups.exists()
                or user.user_permissions.exists()
            )

            # #region agent log
            if request.path == "/dashboard/":
                agent_debug_log(
                    "permissions.py:is_authenticated_and_is_staff",
                    "dashboard decorator access evaluated",
                    "H1",
                    {
                        "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
                        "username": getattr(user, "username", "?"),
                        "is_staff": getattr(user, "is_staff", False),
                        "has_access": has_access,
                    },
                )
            # #endregion

            if not has_access:
                # #region agent log
                agent_debug_log(
                    "permissions.py:is_authenticated_and_is_staff",
                    "no-access redirect",
                    "H-A/H-C",
                    {
                        "is_staff": getattr(user, "is_staff", False),
                        "has_groups": user.groups.exists(),
                        "has_perms": user.user_permissions.exists(),
                        "non_staff_redirect": non_staff_redirect,
                        "path": request.path,
                        "username": getattr(user, "username", "?"),
                    },
                )
                # #endregion
                messages.warning(
                    request,
                    "You don't have access to this system. Contact your administrator if you need access.",
                )
                fallback = reverse("auth_app:login")
                return redirect(non_staff_redirect or fallback)

            # Allowed
            return view(request, *args, **kwargs)

        return _wrapped_view

    if view_func:
        return decorator(view_func)
    return decorator


def has_permission_or_is_staff(permission_codename: str) -> Callable[..., Any]:
    """Decorator that checks if user is staff OR has specific permission.

    Args:
        permission_codename: Permission string, e.g. 'view_campaign' or 'core.view_campaign'.

    Returns:
        Decorator function.
    """

    def decorator(
        view_func: Callable[..., HttpResponseBase],
    ) -> Callable[..., HttpResponseBase]:
        @wraps(view_func)
        def _wrapped_view(
            request: HttpRequest, *args: Any, **kwargs: Any
        ) -> HttpResponseBase:
            user = getattr(request, "user", None)

            # Not authenticated
            if not (user and user.is_authenticated):
                messages.info(request, "Please sign in to continue.")
                return redirect(settings.LOGIN_URL)

            # Re-check is_active so a session that was active when an admin
            # deactivated the account stops being honored on the next request.
            if not getattr(user, "is_active", True):
                messages.info(request, "Please sign in to continue.")
                return redirect(settings.LOGIN_URL)

            # Staff always has access
            if getattr(user, "is_staff", False):
                return view_func(request, *args, **kwargs)

            # Check permission (supports both 'app.permission' and 'permission' format).
            # Bare codenames are matched against any app — required so the decorator
            # keeps working as models migrate between apps.
            if "." in permission_codename:
                has_perm = user.has_perm(permission_codename)
            else:
                suffix = f".{permission_codename}"
                has_perm = any(
                    perm.endswith(suffix) for perm in user.get_all_permissions()
                )

            if has_perm:
                return view_func(request, *args, **kwargs)

            # No access
            messages.warning(
                request,
                "You don't have permission to access this page. Contact your administrator if you need access.",
            )
            return redirect("/")

        return _wrapped_view

    return decorator


def user_has_access(user: Any) -> bool:
    """Check if user has access to the system.

    Args:
        user: User instance (or AnonymousUser).

    Returns:
        True if user is authenticated, active, and is staff OR has any
        groups/permissions.
    """
    if not user or not user.is_authenticated:
        return False

    if not getattr(user, "is_active", True):
        return False

    if user.is_staff:
        return True

    # Check if user has any groups or permissions
    return user.groups.exists() or user.user_permissions.exists()


class HasSystemAccessPermission(BasePermission):
    """DRF permission mirroring template admin access checks."""

    def has_permission(self, request: HttpRequest, view: Any) -> bool:
        return user_has_access(getattr(request, "user", None))
