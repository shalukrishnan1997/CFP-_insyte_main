"""Two-factor authentication setup and management views.

Supports TOTP (authenticator app) and Email OTP methods, plus backup
code generation and regeneration.
"""

import base64
from io import BytesIO

import qrcode
import qrcode.image.svg  # pyright: ignore[reportUnusedImport]
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from auth_app.models import EmailDevice, OTPThrottledError
from auth_app.views.two_factor_helpers import (
    build_manage_2fa_context,
    get_confirmed_2fa_devices,
    render_email_setup,
    render_totp_setup,
    set_pending_device,
)

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


@login_required
def setup_2fa(request: HttpRequest) -> HttpResponse:
    """Set up two-factor authentication for the current user.

    Supports both TOTP (authenticator app) and Email OTP. The user
    chooses the method first, then follows method-specific steps.

    Args:
        request: HTTP request.

    Returns:
        Rendered setup page or redirect on completion.
    """
    # Already has 2FA?
    totp_devices, email_devices, _active_method = get_confirmed_2fa_devices(
        request.user
    )
    if totp_devices or email_devices:
        messages.info(
            request, "Two-factor authentication is already enabled for your account."
        )
        return redirect("auth_app:manage_2fa")

    if not request.user.email:
        messages.error(
            request,
            "You must have an email address to set up two-factor authentication.",
        )
        return redirect("auth_app:user_profile")

    if request.method == "POST":
        action = request.POST.get("action")
        method = request.POST.get("method")  # "totp" or "email"

        if action == "select_method":
            request.session["2fa_method"] = method
            return redirect("auth_app:setup_2fa")

        selected_method = request.session.get("2fa_method")

        if selected_method == "email":
            return _handle_email_otp_setup(request, action)

        # Default: TOTP
        return _handle_totp_setup(request, action)

    # GET — show method selection or method-specific start page
    selected_method = request.session.get("2fa_method")
    if selected_method == "email":
        return render_email_setup(
            request,
            setup_step="start",
            email=request.user.email,
        )
    if selected_method:
        return render_totp_setup(request, setup_step="start")

    return render(request, "auth/select_2fa_method.html")


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------


@login_required
def show_backup_codes(request: HttpRequest) -> HttpResponse:
    """Display backup codes after 2FA setup (one-time view).

    Args:
        request: HTTP request.

    Returns:
        Rendered backup-codes page.
    """
    backup_codes = request.session.get("backup_codes", [])

    if not backup_codes:
        messages.error(request, "No backup codes found. Please regenerate them.")
        return redirect("auth_app:manage_2fa")

    # Clear from session after displaying (one-time view)
    request.session.pop("backup_codes", None)

    return render(request, "auth/2fa_backup_codes.html", {"backup_codes": backup_codes})


@login_required
def manage_2fa(request: HttpRequest) -> HttpResponse:
    """Manage existing 2FA settings — disable or regenerate backup codes.

    Args:
        request: HTTP request.

    Returns:
        Rendered management page.
    """
    context = build_manage_2fa_context(request.user)
    has_2fa = bool(context["has_2fa"])

    if request.method == "POST":
        action = request.POST.get("action")

        if action == "disable" and has_2fa:
            password = request.POST.get("password", "")
            if not request.user.check_password(password):
                messages.error(request, "Incorrect password. Cannot disable 2FA.")
                return redirect("auth_app:manage_2fa")

            TOTPDevice.objects.devices_for_user(request.user).delete()
            EmailDevice.objects.devices_for_user(request.user).delete()
            StaticDevice.objects.filter(user=request.user).delete()

            messages.success(request, "Two-factor authentication has been disabled.")
            return redirect("auth_app:user_profile")

        if action == "switch_method" and has_2fa:
            password = request.POST.get("password", "")
            if not request.user.check_password(password):
                messages.error(request, "Incorrect password. Cannot switch 2FA method.")
                return redirect("auth_app:manage_2fa")

            TOTPDevice.objects.devices_for_user(request.user).delete()
            EmailDevice.objects.devices_for_user(request.user).delete()
            StaticDevice.objects.filter(user=request.user).delete()
            for key in (
                "pending_2fa_device_id",
                "pending_2fa_device_type",
                "2fa_method",
            ):
                request.session.pop(key, None)

            messages.info(
                request,
                "Choose a new two-factor authentication method to continue.",
            )
            return redirect("auth_app:setup_2fa")

        if action == "regenerate_backup_codes":
            backup_codes = _generate_backup_codes(request.user)
            messages.success(
                request, "New backup codes have been generated. Save them securely!"
            )
            return render(
                request, "auth/2fa_backup_codes.html", {"backup_codes": backup_codes}
            )

    return render(request, "auth/manage_2fa.html", context)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _generate_backup_codes(user: object) -> list[str]:
    """Create (or recreate) 10 static backup codes for *user*.

    Args:
        user: Django user instance.

    Returns:
        List of 10 plain-text backup codes.
    """
    backup_device, _created = StaticDevice.objects.get_or_create(
        user=user, name="Backup Codes"
    )
    backup_device.token_set.all().delete()

    codes: list[str] = []
    for _ in range(10):
        token = StaticToken.random_token()
        codes.append(token)
        StaticToken.objects.create(device=backup_device, token=token)
    return codes


def _finish_2fa_setup(request: HttpRequest, success_msg: str) -> HttpResponse:
    """Common finalisation after successful 2FA device confirmation.

    Generates backup codes and either shows them or redirects to login.

    Args:
        request: HTTP request.
        success_msg: Flash message for the user.

    Returns:
        Redirect to backup-codes page or login.
    """
    # Clean up session keys
    for key in ("pending_2fa_device_id", "pending_2fa_device_type", "2fa_method"):
        request.session.pop(key, None)

    backup_device, created = StaticDevice.objects.get_or_create(
        user=request.user, name="Backup Codes"
    )

    if created or not backup_device.token_set.exists():
        codes = _generate_backup_codes(request.user)
        messages.success(request, success_msg)
        request.session["backup_codes"] = codes
        return redirect("auth_app:show_backup_codes")

    messages.success(request, success_msg.replace(" successfully", ""))
    logout(request)
    messages.info(
        request, "Please login again with your new two-factor authentication."
    )
    return redirect("auth_app:login")


def _handle_email_otp_setup(request: HttpRequest, action: str | None) -> HttpResponse:
    """Process Email OTP setup actions (generate / verify / resend).

    Args:
        request: HTTP POST request.
        action: The ``action`` form field value.

    Returns:
        Rendered page or redirect.
    """
    if action == "generate":
        device, created = EmailDevice.objects.get_or_create(
            user=request.user,
            name=f"{request.user.username}'s email OTP",
            defaults={"confirmed": False},
        )
        if not created:
            device.confirmed = False
            device.save()

        try:
            device.generate_challenge()
        except OTPThrottledError:
            messages.info(
                request,
                "A code was just sent. Please check your inbox before "
                "requesting another.",
            )
            return render_email_setup(
                request,
                setup_step="verify",
                email=request.user.email,
            )
        set_pending_device(request, device_id=device.id, device_type="email")

        messages.success(
            request, f"A 6-digit code has been sent to {request.user.email}"
        )
        return render_email_setup(
            request,
            setup_step="verify",
            email=request.user.email,
        )

    if action == "verify":
        device = _get_pending_device(request, EmailDevice)
        if device is None:
            return redirect("auth_app:setup_2fa")

        otp_token = request.POST.get("otp_token", "").strip()
        if device.verify_token(otp_token):
            device.confirmed = True
            device.save()
            return _finish_2fa_setup(
                request,
                "Email-based two-factor authentication has been enabled successfully!",
            )

        messages.error(
            request, "Invalid or expired verification code. Please try again."
        )
        return render_email_setup(
            request,
            setup_step="verify",
            email=request.user.email,
        )

    if action == "resend":
        device_id = request.session.get("pending_2fa_device_id")
        if device_id:
            try:
                device = EmailDevice.objects.get(id=device_id, user=request.user)
            except EmailDevice.DoesNotExist:
                messages.error(request, "Session expired. Please start setup again.")
                return redirect("auth_app:setup_2fa")

            try:
                device.generate_challenge()
                messages.success(
                    request, f"A new code has been sent to {request.user.email}"
                )
            except OTPThrottledError:
                messages.info(
                    request,
                    "A code was just sent. Please check your inbox before "
                    "requesting another.",
                )

        return render_email_setup(
            request,
            setup_step="verify",
            email=request.user.email,
        )

    return redirect("auth_app:setup_2fa")


def _handle_totp_setup(request: HttpRequest, action: str | None) -> HttpResponse:
    """Process TOTP setup actions (generate / verify).

    Args:
        request: HTTP POST request.
        action: The ``action`` form field value.

    Returns:
        Rendered page or redirect.
    """
    if action == "generate":
        device = TOTPDevice.objects.create(
            user=request.user,
            name=f"{request.user.username}'s device",
            confirmed=False,
        )
        qr_code_base64 = _generate_qr_code(device.config_url)

        set_pending_device(request, device_id=device.id, device_type="totp")

        return render_totp_setup(
            request,
            setup_step="verify",
            qr_code=qr_code_base64,
            secret_key=device.key,
        )

    if action == "verify":
        device = _get_pending_device(request, TOTPDevice, confirmed=False)
        if device is None:
            return redirect("auth_app:setup_2fa")

        otp_token = request.POST.get("otp_token", "").strip()
        if device.verify_token(otp_token):
            device.confirmed = True
            device.save()
            return _finish_2fa_setup(
                request,
                "Two-factor authentication has been enabled successfully!",
            )

        messages.error(request, "Invalid verification code. Please try again.")
        qr_code_base64 = _generate_qr_code(device.config_url)
        return render_totp_setup(
            request,
            setup_step="verify",
            qr_code=qr_code_base64,
            secret_key=device.key,
        )

    return redirect("auth_app:setup_2fa")


def _get_pending_device(
    request: HttpRequest, model_class: type, **extra_filters: object
) -> object | None:
    """Retrieve the pending 2FA device from the session.

    Args:
        request: HTTP request.
        model_class: OTP device model (``TOTPDevice`` or ``EmailDevice``).
        **extra_filters: Additional queryset filters (e.g. ``confirmed=False``).

    Returns:
        Device instance or ``None`` (with error message set).
    """
    device_id = request.session.get("pending_2fa_device_id")
    if not device_id:
        messages.error(request, "Session expired. Please start setup again.")
        return None
    try:
        return model_class.objects.get(id=device_id, user=request.user, **extra_filters)
    except model_class.DoesNotExist:
        messages.error(request, "Invalid device. Please start setup again.")
        return None


def _generate_qr_code(provisioning_uri: str) -> str:
    """Return a base64-encoded PNG QR code for the given URI.

    Args:
        provisioning_uri: The ``otpauth://`` URI.

    Returns:
        Base64 string suitable for ``<img src="data:image/png;base64,...">``.
    """
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(provisioning_uri)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")  # pyright: ignore[reportCallIssue]
    return base64.b64encode(buffer.getvalue()).decode()
