import json
from collections import defaultdict
from typing import Any

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from clients.models import ClientPortalUser
from core.pagination import paginate_queryset
from custom_admin.forms.user_forms import UserCreateForm
from responsehandling.permissions import is_authenticated_and_is_staff

_REDIRECT_URL = "custom_admin:admin_user_management"

_EXCLUDED_MODELS = [
    "logentry",
    "token",
    "tokenproxy",
    "contenttype",
    "approvallog",
    "segment",
    "exportlog",
    "session",
]


# ── POST action handlers ─────────────────────────────────────────────


def _post_str(request: HttpRequest, field: str) -> str:
    """Return a stripped string from POST data, defaulting to ``''``."""
    return (request.POST.get(field) or "").strip()


def _assign_groups(user: Any, group_ids: list[str]) -> None:
    """Set or clear a user's groups from a list of group PKs."""
    if group_ids:
        user.groups.set(Group.objects.filter(pk__in=group_ids))
    else:
        user.groups.clear()


def _handle_toggle_active(request: HttpRequest, user_id: str | None) -> HttpResponse:
    """Toggle a user's active status."""
    User = get_user_model()
    if not user_id:
        messages.error(request, "Missing user id.")
        return redirect(_REDIRECT_URL)
    target = get_object_or_404(User, pk=user_id)
    if str(target.pk) == str(request.user.pk):
        messages.warning(request, "You cannot change your own active status here.")
        return redirect(_REDIRECT_URL)
    target.is_active = not bool(target.is_active)
    target.save()
    state = "activated" if target.is_active else "deactivated"
    messages.success(request, f"User {target.get_username()} {state} successfully.")
    return redirect(_REDIRECT_URL)


def _validate_required(request: HttpRequest, fields: list[tuple[str, str]]) -> bool:
    """Validate that all required fields are non-empty.

    Returns False and adds an error message on the first missing field.
    """
    for value, label in fields:
        if not value:
            messages.error(request, f"{label} is required.")
            return False
    return True


def _apply_user_password(
    request: HttpRequest, target: Any, password: str, confirm: str
) -> bool:
    """Set password on target if provided.  Returns False on mismatch."""
    if not password:
        return True
    if password != confirm:
        messages.error(request, "Passwords do not match.")
        return False
    target.set_password(password)
    return True


def _handle_user_edit(request: HttpRequest, user_id: str | None) -> HttpResponse:
    """Edit an existing user's details and permissions."""
    User = get_user_model()
    if not user_id:
        messages.error(request, "Missing user id.")
        return redirect(_REDIRECT_URL)
    target = get_object_or_404(User, pk=user_id)

    email = _post_str(request, "email")
    first_name = _post_str(request, "first_name")

    if not _validate_required(request, [(email, "Email"), (first_name, "First name")]):
        return redirect(_REDIRECT_URL)
    if not _apply_user_password(
        request,
        target,
        _post_str(request, "password"),
        _post_str(request, "password_confirm"),
    ):
        return redirect(_REDIRECT_URL)

    new_is_staff = request.POST.get("is_staff") == "on"
    group_ids = request.POST.getlist("groups")
    has_client_profile = ClientPortalUser.objects.filter(user=target).exists()
    if not new_is_staff and not group_ids and not has_client_profile:
        messages.error(
            request,
            "Cannot save: the user would have no access. Keep staff access, "
            "assign at least one group, or link them to a client portal profile.",
        )
        return redirect(_REDIRECT_URL)

    target.email = email
    target.first_name = first_name
    target.is_active = request.POST.get("is_active") == "on"
    target.is_staff = new_is_staff
    _assign_groups(target, group_ids)
    target.save()
    messages.success(request, f"User {target.get_username()} updated successfully.")
    return redirect(_REDIRECT_URL)


def _handle_user_create(
    request: HttpRequest, _user_id: str | None = None
) -> HttpResponse:
    """Create a new user with group assignments.

    New users are always created as active staff members — admins can demote or
    disable them via the edit form. They're also flagged ``must_change_password``
    so the password the admin set is treated as a one-time credential.
    """
    User = get_user_model()
    form = UserCreateForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, str(error))
        return redirect(_REDIRECT_URL)

    data = form.cleaned_data
    new_user = User(
        username=data["username"],
        email=data["email"],
        first_name=data["first_name"],
        last_name=data["last_name"],
        is_active=True,
        is_staff=True,
        must_change_password=True,
        created_by=request.user if request.user.is_authenticated else None,
    )
    new_user.set_password(data["password1"])
    new_user.save()

    groups = data.get("groups")
    if groups:
        new_user.groups.set(groups)

    messages.success(request, f"User {new_user.get_username()} created.")
    return redirect(_REDIRECT_URL)


def _handle_group_edit(
    request: HttpRequest, _user_id: str | None = None
) -> HttpResponse:
    """Create or update a permission group."""
    group_id = request.POST.get("group_id")
    name = (request.POST.get("name") or "").strip()
    perms = request.POST.getlist("permissions")

    if not name:
        messages.error(request, "Group name is required.")
        return redirect(_REDIRECT_URL)

    if group_id:
        grp = get_object_or_404(Group, pk=group_id)
        grp.name = name
        grp.save()
        if perms:
            grp.permissions.set(Permission.objects.filter(pk__in=perms))
        else:
            grp.permissions.clear()
        messages.success(request, f"Group '{grp.name}' updated.")
    else:
        grp = Group.objects.create(name=name)
        if perms:
            grp.permissions.set(Permission.objects.filter(pk__in=perms))
        messages.success(request, f"Group '{grp.name}' created.")

    return redirect(_REDIRECT_URL)


def _handle_reset_password(
    request: HttpRequest, user_id: str | None = None
) -> HttpResponse:
    """Reset a user's password (admin action).

    Sets ``must_change_password=True`` so the admin-chosen password is treated
    as a one-time credential the user must replace at next sign-in.
    """
    User = get_user_model()
    if not user_id:
        messages.error(request, "Missing user id.")
        return redirect(_REDIRECT_URL)
    target = get_object_or_404(User, pk=user_id)

    password1 = _post_str(request, "new_password1")
    password2 = _post_str(request, "new_password2")

    if not password1:
        messages.error(request, "New password is required.")
        return redirect(_REDIRECT_URL)
    if password1 != password2:
        messages.error(request, "Passwords do not match.")
        return redirect(_REDIRECT_URL)

    target.set_password(password1)
    target.must_change_password = True
    target.save()
    messages.success(
        request, f"Password for {target.get_username()} has been reset successfully."
    )
    return redirect(_REDIRECT_URL)


_ACTION_DISPATCH: dict[str, Any] = {
    "toggle_active": _handle_toggle_active,
    "edit": _handle_user_edit,
    "create": _handle_user_create,
    "group_edit": _handle_group_edit,
    "reset_password": _handle_reset_password,
}


def _dispatch_user_post(request: HttpRequest) -> HttpResponse:
    """Route POST actions to the correct handler."""
    action = request.POST.get("action")
    user_id = request.POST.get("user_id")

    if not action:
        messages.error(request, "Invalid request.")
        return redirect(_REDIRECT_URL)

    handler = _ACTION_DISPATCH.get(action)
    if not handler:
        messages.error(request, "Unknown action requested.")
        return redirect(_REDIRECT_URL)

    try:
        return handler(request, user_id)
    except Exception as e:
        messages.error(request, f"Action failed: {e}")
        return redirect(_REDIRECT_URL)


# ── Context / GET helpers ─────────────────────────────────────────────


def _build_permissions_data(
    all_permissions: Any,
) -> tuple[str, str]:
    """Build JSON-serialised permissions structures for the template.

    Returns:
        Tuple of (permissions_list_json, permissions_grouped_json).
    """
    permissions_by_model: dict[str, list[dict]] = defaultdict(list)
    permissions_list: list[dict] = []

    for perm in all_permissions:
        entry = {
            "id": perm.id,
            "name": str(perm.name),
            "codename": str(perm.codename),
            "app": str(perm.content_type.app_label),
            "model": str(perm.content_type.model),
        }
        permissions_list.append(entry)
        permissions_by_model[perm.content_type.model].append(entry)

    permissions_grouped = [
        {
            "model": model_name,
            "model_display": model_name.replace("_", " ").title(),
            "permissions": perms,
        }
        for model_name, perms in sorted(permissions_by_model.items())
    ]

    return (
        json.dumps(permissions_list, ensure_ascii=False),
        json.dumps(permissions_grouped, ensure_ascii=False),
    )


def _build_users_json(users: Any) -> str:
    """Serialise paginated user objects to JSON for the template."""
    return json.dumps(
        [
            {
                "id": str(u.id),
                "username": str(u.username),
                "email": str(u.email),
                "first_name": str(u.first_name),
                "last_name": str(u.last_name),
                "is_active": bool(u.is_active),
                "is_staff": bool(u.is_staff),
                "groups": [int(g.id) for g in u.groups.all()],
            }
            for u in users
        ],
        ensure_ascii=False,
    )


@is_authenticated_and_is_staff
def admin_user_management(request: HttpRequest) -> HttpResponse:
    """Manage users, groups, and permissions.

    Handles CRUD operations for users and groups including:
    - Toggle user active status
    - Edit existing user details and permissions
    - Create new users with group assignments
    - Delete users (except superusers and self)
    - Create/update groups with permissions

    Args:
        request: HTTP request with optional POST data for user/group actions.

    Returns:
        Rendered user management page with users, groups, and permissions.
    """
    if request.method == "POST":
        return _dispatch_user_post(request)

    User = get_user_model()
    portal_user_ids = ClientPortalUser.objects.values_list("user_id", flat=True)
    users_qs = (
        User.objects.prefetch_related("groups")
        .exclude(id__in=portal_user_ids)
        .order_by("-date_joined")
    )
    users = paginate_queryset(users_qs, request, per_page_param="per_page")

    all_permissions = (
        Permission.objects.select_related("content_type")
        .exclude(content_type__model__in=_EXCLUDED_MODELS)
        .order_by("content_type__app_label", "codename")
    )
    perms_json, perms_grouped_json = _build_permissions_data(all_permissions)

    context = {
        "users": users,
        "all_groups": Group.objects.all().order_by("name"),
        "all_permissions": perms_json,
        "permissions_grouped": perms_grouped_json,
        "users_json": _build_users_json(users),
        "active": "user_management",
    }
    return render(request, "admin/user_management.html", context)
