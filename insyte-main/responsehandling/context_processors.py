"""Context processors to add user permissions and role information to all templates."""

from typing import Any

from django.http import HttpRequest

from core.constants import CURRENCY_CODE, CURRENCY_SYMBOL


def currency_constants(request: HttpRequest) -> dict[str, str]:
    """Add currency symbol and code to all template contexts.

    Eliminates the need for views to manually include these in every context dict.
    """
    return {
        "currency_symbol": CURRENCY_SYMBOL,
        "currency_code": CURRENCY_CODE,
    }


def user_permissions(request: HttpRequest) -> dict[str, Any]:
    """Add user role and permission information to template context.

    Uses prefetch_related/select_related to avoid N+1 queries on
    groups -> permissions -> content_type lookups.
    """
    user = request.user
    context = {
        "is_admin_user": False,
        "is_regular_user": False,
        "user_permissions": [],
        "base_url_prefix": "/admin",
        "url_namespace": "custom_admin",
    }

    if user.is_authenticated:
        context["is_admin_user"] = user.is_staff

        has_groups = user.groups.exists()
        has_perms = user.user_permissions.exists()
        context["is_regular_user"] = (has_groups or has_perms) and not user.is_staff

        if context["is_regular_user"]:
            context["base_url_prefix"] = "/user"
            # Regular users share the staff-facing URLconf and template shell.
            # There is no registered "user" namespace in this project.
            context["url_namespace"] = "custom_admin"

        if user.is_staff:
            context["user_permissions"] = ["all"]
        else:
            perms = set()
            # Single prefetched query: groups -> permissions -> content_type
            groups = user.groups.prefetch_related("permissions__content_type").all()
            for group in groups:
                for perm in group.permissions.all():
                    perms.add(f"{perm.content_type.app_label}.{perm.codename}")
                    perms.add(perm.codename)
            # User-level permissions in a single query
            for perm in user.user_permissions.select_related("content_type").all():
                perms.add(f"{perm.content_type.app_label}.{perm.codename}")
                perms.add(perm.codename)
            context["user_permissions"] = list(perms)

    return context
