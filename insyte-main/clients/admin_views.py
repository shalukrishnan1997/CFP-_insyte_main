from typing import Any

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from clients.models import Client, ClientPortalUser
from core.pagination import paginate_queryset
from responsehandling.permissions import (
    has_permission_or_is_staff,
    is_authenticated_and_is_staff,
)


def _filter_portal_users(request: HttpRequest) -> tuple[Any, str, str, str, str]:
    """Apply search/filter params and return (qs, search, client, status, role)."""
    search = request.GET.get("search", "").strip()
    client_f = request.GET.get("client", "")
    status_f = request.GET.get("status", "")
    role_f = request.GET.get("role", "")

    qs = ClientPortalUser.objects.select_related(
        "user", "client", "invited_by"
    ).order_by("-is_primary", "client__name", "user__first_name")

    if search:
        qs = qs.filter(
            Q(user__username__icontains=search)
            | Q(user__email__icontains=search)
            | Q(user__first_name__icontains=search)
            | Q(user__last_name__icontains=search)
            | Q(client__name__icontains=search)
        )
    if client_f:
        qs = qs.filter(client_id=client_f)
    if status_f == "active":
        qs = qs.filter(is_active=True)
    elif status_f == "disabled":
        qs = qs.filter(is_active=False)
    if role_f:
        qs = qs.filter(role=role_f)

    return qs, search, client_f, status_f, role_f


@has_permission_or_is_staff("view_client")
def client_setup(request: HttpRequest) -> HttpResponse:
    """List all clients/charities and portal users with tab navigation."""
    view_mode = request.GET.get("view", "clients")
    clients_queryset = Client.objects.all().order_by("name")

    portal_users = None
    search_query = client_filter = status_filter = role_filter = ""

    if view_mode == "users":
        qs, search_query, client_filter, status_filter, role_filter = (
            _filter_portal_users(request)
        )
        portal_users = paginate_queryset(qs, request, per_page_param="per_page")
        clients = clients_queryset
    else:
        clients = paginate_queryset(
            clients_queryset, request, per_page_param="per_page"
        )

    total_users = ClientPortalUser.objects.count()
    active_users = ClientPortalUser.objects.filter(is_active=True).count()

    context = {
        "clients": clients,
        "charities": clients_queryset if view_mode == "users" else clients,
        "view_mode": view_mode,
        "portal_users": portal_users,
        "search_query": search_query,
        "client_filter": client_filter,
        "status_filter": status_filter,
        "role_filter": role_filter,
        "total_users": total_users,
        "active_users": active_users,
        "disabled_users": total_users - active_users,
        "primary_contacts": ClientPortalUser.objects.filter(is_primary=True).count(),
        "active": "client_setup",
        "breadcrumbs": [{"name": "Client Setup", "url": None}],
    }
    return render(request, "admin/client_setup.html", context)


@has_permission_or_is_staff("add_client")
def client_create(request: HttpRequest) -> HttpResponse:
    """Create new client/charity."""
    if request.method == "POST":
        try:
            client = Client.objects.create(
                name=request.POST.get("name", "").strip(),
                client_code=request.POST.get("client_code", "").strip().upper(),
                email=request.POST.get("email", "").strip(),
                phone=request.POST.get("phone", "").strip(),
                description=request.POST.get("description", "").strip(),
                website=request.POST.get("website", "").strip(),
                address_line1=request.POST.get("address_line1", "").strip(),
                address_line2=request.POST.get("address_line2", "").strip(),
                city=request.POST.get("city", "").strip(),
                postal_code=request.POST.get("postal_code", "").strip(),
                country=request.POST.get("country", "").strip(),
                is_active=request.POST.get("is_active") == "on",
            )
            if "logo" in request.FILES:
                client.logo = request.FILES["logo"]
                client.save()
            messages.success(request, f"Client '{client.name}' created successfully!")
            return redirect("custom_admin:client_setup")
        except Exception as e:
            messages.error(request, f"Error creating client: {e!s}")

    context = {
        "active": "client_setup",
        "breadcrumbs": [
            {"name": "Client Setup", "url": reverse("custom_admin:client_setup")},
            {"name": "Create Client", "url": None},
        ],
    }
    return render(request, "admin/client_form.html", context)


def _client_activate(
    request: HttpRequest, client: Any, client_id: int, activate: bool
) -> HttpResponse:
    """Activate or deactivate a client."""
    client.is_active = activate
    client.save()
    verb = "activated" if activate else "deactivated"
    extra = (
        " and is now available for campaigns."
        if activate
        else ". They will not appear in campaign creation or other processes."
    )
    messages.success(request, f"Client '{client.name}' has been {verb}{extra}")
    return redirect("custom_admin:client_edit", client_id=client_id)


def _validate_portal_user_fields(
    request: HttpRequest, username: str, email: str, password: str
) -> bool:
    """Validate portal user required fields and uniqueness.  Returns True if valid."""
    User = get_user_model()
    if not username or not email or not password:
        messages.error(request, "Username, email, and password are required.")
        return False
    if User.objects.filter(username=username).exists():
        messages.error(request, "Username already exists.")
        return False
    if User.objects.filter(email=email).exists():
        messages.error(request, "Email already exists.")
        return False
    return True


def _create_portal_user_for_client(
    request: HttpRequest, client: Any, client_id: int
) -> HttpResponse | None:
    """Create a portal user attached to *client*.  Returns redirect or None."""
    User = get_user_model()
    username = request.POST.get("username", "").strip()
    email = request.POST.get("email", "").strip()
    password = request.POST.get("password", "").strip()

    if not _validate_portal_user_fields(request, username, email, password):
        return None

    user = User.objects.create_user(
        username=username,
        email=email,
        password=password,
        first_name=request.POST.get("first_name", "").strip(),
        last_name=request.POST.get("last_name", "").strip(),
        is_staff=False,
        is_active=True,
    )
    is_primary = request.POST.get("is_primary") == "on"
    if is_primary:
        ClientPortalUser.objects.filter(client=client, is_primary=True).update(
            is_primary=False
        )
    ClientPortalUser.objects.create(
        client=client,
        user=user,
        role=request.POST.get("role", "viewer"),
        is_primary=is_primary,
        invited_by=request.user,
    )
    messages.success(
        request, f"Portal user '{username}' created successfully for {client.name}"
    )
    return redirect("custom_admin:client_edit", client_id=client_id)


def _get_portal_user(request: HttpRequest, client: Any) -> ClientPortalUser | None:
    """Look up a portal user from POST data; show error on failure."""
    portal_user_id = request.POST.get("portal_user_id", "").strip()
    if not portal_user_id:
        messages.error(request, "Invalid request.")
        return None
    try:
        return ClientPortalUser.objects.get(id=portal_user_id, client=client)
    except ClientPortalUser.DoesNotExist:
        messages.error(request, "Portal user not found.")
        return None


def _handle_reset_password(
    request: HttpRequest, client: Any, client_id: int
) -> HttpResponse | None:
    """Reset a portal user's password."""
    new_password = request.POST.get("new_password", "").strip()
    if not new_password:
        messages.error(request, "Password is required.")
        return None
    pu = _get_portal_user(request, client)
    if not pu:
        return None
    pu.user.set_password(new_password)
    pu.user.save()
    messages.success(request, f"Password reset successfully for {pu.user.username}.")
    return redirect("custom_admin:client_edit", client_id=client_id)


def _handle_toggle_access(
    request: HttpRequest, client: Any, client_id: int
) -> HttpResponse | None:
    """Toggle portal user access."""
    pu = _get_portal_user(request, client)
    if not pu:
        return None
    pu.is_active = not pu.is_active
    pu.save()
    pu.user.is_active = pu.is_active
    pu.user.save()
    status = "enabled" if pu.is_active else "disabled"
    messages.success(request, f"Portal access {status} for {pu.user.username}.")
    return redirect("custom_admin:client_edit", client_id=client_id)


def _handle_set_primary(
    request: HttpRequest, client: Any, client_id: int
) -> HttpResponse | None:
    """Make a portal user the primary contact."""
    pu = _get_portal_user(request, client)
    if not pu:
        return None
    ClientPortalUser.objects.filter(client=client, is_primary=True).update(
        is_primary=False
    )
    pu.is_primary = True
    pu.save()
    messages.success(request, f"{pu.user.username} set as primary contact.")
    return redirect("custom_admin:client_edit", client_id=client_id)


def _handle_remove_user(
    request: HttpRequest, client: Any, client_id: int
) -> HttpResponse | None:
    """Remove a portal user and their auth user."""
    pu = _get_portal_user(request, client)
    if not pu:
        return None
    username = pu.user.username
    user = pu.user
    pu.delete()
    user.delete()
    messages.success(request, f"Portal user '{username}' removed successfully.")
    return redirect("custom_admin:client_edit", client_id=client_id)


_CLIENT_TEXT_FIELDS = (
    "name",
    "client_code",
    "email",
    "phone",
    "description",
    "website",
    "address_line1",
    "address_line2",
    "city",
    "postal_code",
    "country",
)


def _handle_update_client(request: HttpRequest, client: Any) -> HttpResponse | None:
    """Apply general client field updates from POST data."""
    for field in _CLIENT_TEXT_FIELDS:
        value = request.POST.get(field, "").strip()
        if field == "client_code":
            value = value.upper()
        setattr(client, field, value)
    if "is_active" in request.POST:
        client.is_active = request.POST.get("is_active") == "on"
    if "logo" in request.FILES:
        client.logo = request.FILES["logo"]
    client.save()
    messages.success(request, f"Client '{client.name}' updated successfully!")
    return redirect("custom_admin:client_setup")


def _build_client_edit_context(client: Client) -> dict[str, Any]:
    """Build template context for the client edit view.

    Args:
        client: Client being edited.

    Returns:
        Context dict.
    """
    portal_users = list(
        ClientPortalUser.objects.filter(client=client)
        .select_related("user", "invited_by")
        .order_by("-is_primary", "user__date_joined")
    )
    # The template still renders a single primary user; surface the primary
    # (or the first user as a fallback) so the "Portal User Active" panel
    # populates after creation. Without this, the template falls through to
    # the "No Portal User" state because `has_portal_user` was never set.
    primary_portal_user = next(
        (pu for pu in portal_users if pu.is_primary),
        portal_users[0] if portal_users else None,
    )
    return {
        "client": client,
        "charity": client,
        "active": "client_setup",
        "portal_users": portal_users,
        "portal_user_count": len(portal_users),
        "has_portal_user": bool(portal_users),
        "portal_user": primary_portal_user,
        "breadcrumbs": [
            {"name": "Client Setup", "url": reverse("custom_admin:client_setup")},
            {"name": f"Edit {client.name}", "url": None},
        ],
    }


def _dispatch_client_edit_post(
    request: HttpRequest, client: Any, client_id: int
) -> HttpResponse | None:
    """Route client_edit POST actions to the correct handler."""
    action = request.POST.get("action", "")

    if action == "activate_client":
        return _client_activate(request, client, client_id, activate=True)
    if action == "deactivate_client":
        return _client_activate(request, client, client_id, activate=False)
    _action_map: dict[str, Any] = {
        "create_portal_user": _create_portal_user_for_client,
        "reset_password": _handle_reset_password,
        "toggle_access": _handle_toggle_access,
        "set_primary": _handle_set_primary,
        "remove_user": _handle_remove_user,
    }

    handler = _action_map.get(action)
    if handler:
        try:
            result = handler(request, client, client_id)
            return result if result else None
        except Exception as e:
            messages.error(request, f"Error: {e!s}")
            return None

    # Default: update client fields
    try:
        return _handle_update_client(request, client)
    except Exception as e:
        messages.error(request, f"Error updating client: {e!s}")
        return None


@has_permission_or_is_staff("change_client")
def client_edit(request: HttpRequest, client_id: int) -> HttpResponse:
    """Edit existing client/charity and manage multiple portal users."""
    client = get_object_or_404(Client, pk=client_id)

    if request.method == "POST":
        result = _dispatch_client_edit_post(request, client, client_id)
        if result:
            return result
        return redirect("custom_admin:client_edit", client_id=client_id)

    return render(request, "admin/client_edit.html", _build_client_edit_context(client))


@is_authenticated_and_is_staff
def portal_user_create(request: HttpRequest) -> HttpResponse:
    """Create a new portal user for a client."""
    if request.method == "POST":
        client_id = request.POST.get("client_id", "").strip()
        if not client_id:
            messages.error(request, "Client is required.")
        else:
            client = get_object_or_404(Client, pk=client_id)
            try:
                result = _create_portal_user_for_client(request, client, int(client_id))
                if result:
                    return result
            except Exception as e:
                messages.error(request, f"Error creating user: {e!s}")

    client_id_param = request.GET.get("client_id") or request.POST.get("client_id", "")
    if client_id_param:
        return redirect("custom_admin:client_edit", client_id=client_id_param)
    return redirect("custom_admin:client_setup")


@has_permission_or_is_staff("change_client")
def client_payment_config(request: HttpRequest, client_id: str) -> HttpResponse:
    """Manage payment gateway configuration for a client."""
    client = get_object_or_404(Client, pk=client_id)

    from payments.models import PaymentGatewayConfig

    if request.method == "POST":
        provider = request.POST.get("provider", "").strip().lower()
        is_active = request.POST.get("is_active") == "on"

        if provider != "stripe":
            messages.error(
                request,
                "Only Stripe configuration is available right now.",
            )
            return redirect("custom_admin:client_payment_config", client_id=client_id)

        new_secret = request.POST.get("stripe_secret_key", "").strip()
        new_publishable = request.POST.get("stripe_publishable_key", "").strip()
        new_webhook = request.POST.get("stripe_webhook_secret", "").strip()

        # Reject obvious slot-swaps before they reach the database — Stripe.js
        # silently fails to mount the card form if the publishable slot holds
        # a secret key, leaving QA reviewers unable to enter card details.
        from payments.services import validate_stripe_key_prefixes

        prefix_error = validate_stripe_key_prefixes(
            publishable_key=new_publishable,
            secret_key=new_secret,
            webhook_secret=new_webhook,
        )
        if prefix_error:
            messages.error(request, prefix_error)
            return redirect("custom_admin:client_payment_config", client_id=client_id)

        prior = PaymentGatewayConfig.objects.filter(
            client=client, provider=provider
        ).first()

        config, created = PaymentGatewayConfig.objects.get_or_create(
            client=client,
            provider=provider,
            defaults={"is_active": is_active},
        )
        config.is_active = is_active
        config.secret_key_encrypted = new_secret or (
            prior.get_secret_key() if prior else ""
        )
        config.publishable_key_encrypted = new_publishable or (
            prior.get_publishable_key() if prior else ""
        )
        config.webhook_secret_encrypted = new_webhook or (
            prior.get_webhook_secret() if prior else ""
        )
        config.save()

        status_msg = "created" if created else "updated"
        messages.success(
            request,
            f"Payment configuration for {(provider or '').title()} {status_msg} successfully.",
        )
        return redirect("custom_admin:client_payment_config", client_id=client_id)

    # Get existing configs
    configs = PaymentGatewayConfig.objects.filter(client=client)
    config_dict = {cfg.provider: cfg for cfg in configs}
    stripe_gateway = config_dict.get("stripe")

    # Never echo the secret values back to the page — only the publishable
    # key (which is intentionally non-secret).
    stripe_secret_configured = bool(stripe_gateway and stripe_gateway.get_secret_key())
    stripe_webhook_configured = bool(
        stripe_gateway and stripe_gateway.get_webhook_secret()
    )
    stripe_publishable_value = (
        stripe_gateway.get_publishable_key() if stripe_gateway else ""
    )

    context = {
        "client": client,
        "charity": client,
        "configs": config_dict,
        "stripe_gateway": stripe_gateway,
        "stripe_secret_configured": stripe_secret_configured,
        "stripe_webhook_configured": stripe_webhook_configured,
        "stripe_publishable_value": stripe_publishable_value,
        "active": "client_setup",
        "breadcrumbs": [
            {"name": "Client Setup", "url": reverse("custom_admin:client_setup")},
            {"name": f"Payment Config: {client.name}", "url": None},
        ],
    }

    return render(request, "admin/client_payment_config.html", context)
