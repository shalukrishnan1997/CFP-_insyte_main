"""Shared helpers for two-factor authentication views."""

from typing import Any

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django_otp.plugins.otp_totp.models import TOTPDevice

from auth_app.models import EmailDevice


def get_confirmed_2fa_devices(
    user: Any,
) -> tuple[list[TOTPDevice], list[EmailDevice], str | None]:
    """Return confirmed 2FA devices and the active method for a user."""
    totp_devices = list(TOTPDevice.objects.devices_for_user(user, confirmed=True))
    email_devices = list(EmailDevice.objects.devices_for_user(user, confirmed=True))

    active_method: str | None = None
    if totp_devices:
        active_method = "totp"
    elif email_devices:
        active_method = "email"

    return totp_devices, email_devices, active_method


def render_email_setup(
    request: HttpRequest,
    *,
    setup_step: str,
    email: str,
) -> HttpResponse:
    """Render the email OTP setup template."""
    return render(
        request,
        "auth/setup_2fa_email.html",
        {"setup_step": setup_step, "email": email},
    )


def render_totp_setup(
    request: HttpRequest,
    *,
    setup_step: str,
    qr_code: str | None = None,
    secret_key: str | None = None,
) -> HttpResponse:
    """Render the TOTP setup template."""
    context: dict[str, Any] = {"setup_step": setup_step, "method": "totp"}
    if qr_code is not None:
        context["qr_code"] = qr_code
    if secret_key is not None:
        context["secret_key"] = secret_key
    return render(request, "auth/setup_2fa.html", context)


def set_pending_device(
    request: HttpRequest,
    *,
    device_id: object,
    device_type: str,
) -> None:
    """Persist the pending 2FA device identifiers in the session."""
    request.session["pending_2fa_device_id"] = str(device_id)
    request.session["pending_2fa_device_type"] = device_type


def build_manage_2fa_context(user: Any) -> dict[str, Any]:
    """Build template context for the 2FA management page."""
    totp_devices, email_devices, active_method = get_confirmed_2fa_devices(user)
    return {
        "has_2fa": bool(totp_devices or email_devices),
        "devices": totp_devices,
        "email_devices": email_devices,
        "active_method": active_method,
    }
