"""Authentication views — login, OTP verification, logout.

Handles the two-step login flow (credentials → OTP) and logout
with audit logging.
"""

from typing import cast

from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods
from django_otp.plugins.otp_static.models import StaticDevice
from django_otp.plugins.otp_totp.models import TOTPDevice

from audit.utils import get_client_ip, log_action
from auth_app.models import EmailDevice, OTPThrottledError
from core.agent_debug import agent_debug_log
from responsehandling.permissions import user_has_access

User = get_user_model()
OTP_MAX_FAILURES_SESSION = "pre_2fa_failed_attempts"
OTP_MAX_FAILURES = 5


def _has_confirmed_2fa_device(user: object) -> bool:
    """Return True when a user has at least one confirmed OTP-capable device."""
    has_totp = TOTPDevice.objects.devices_for_user(user, confirmed=True).exists()
    has_email = EmailDevice.objects.devices_for_user(user, confirmed=True).exists()
    has_backup = StaticDevice.objects.devices_for_user(user, confirmed=True).exists()
    return has_totp or has_email or has_backup


def _has_client_portal_profile(user: User) -> bool:
    """True when the user has a linked client portal profile (safe for missing relation)."""
    try:
        return bool(user.client_portal_profile)
    except ObjectDoesNotExist:
        return False


def user_login(request: HttpRequest) -> HttpResponse:
    """Handle user login with two-factor authentication.

    Two-step process:
        1. Username / password authentication.
        2. OTP verification (if 2FA is enabled).

    Staff users redirect to admin dashboard, regular users to user dashboard.

    Args:
        request: HTTP request.

    Returns:
        Redirect on success, rendered login page otherwise.
    """
    if request.user.is_authenticated:
        current_user = cast(User, request.user)
        is_verified = (
            _has_confirmed_2fa_device(current_user) and current_user.is_verified()
        )

        # #region agent log
        agent_debug_log(
            "auth.py:user_login",
            "authenticated user hit login",
            "H-A",
            {
                "is_verified": is_verified,
                "is_staff": current_user.is_staff,
                "has_client_profile": _has_client_portal_profile(current_user),
                "has_groups": current_user.groups.exists(),
                "has_perms": current_user.user_permissions.exists(),
            },
        )
        # #endregion

        if is_verified:
            if current_user.is_staff:
                return redirect("custom_admin:admin_dashboard")
            if _has_client_portal_profile(current_user):
                return redirect("client_portal:dashboard")
            if user_has_access(current_user):
                return redirect("core:user_dashboard")
            messages.error(
                request,
                "Your account has no staff flag, groups, or client portal "
                "profile assigned. Contact your administrator.",
            )
            logout(request)
            return redirect("auth_app:login")

        # If not verified, always enforce 2FA setup or re-login for verification
        if not _has_confirmed_2fa_device(current_user):
            messages.warning(
                request,
                "Two-factor authentication is required before continuing.",
            )
            return redirect("auth_app:setup_2fa")

        messages.info(request, "Please sign in again to complete verification.")
        logout(request)
        return redirect("auth_app:login")

    if request.method == "POST":
        # OTP verification step
        if "otp_token" in request.POST:
            return _handle_otp_verification(request)

        # Username / password authentication
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        remember_me = request.POST.get("remember_me")

        if not username or not password:
            messages.error(request, "Please provide both username/email and password.")
            return render(request, "auth/login.html")

        user = authenticate(request, username=username, password=password)

        # If auth failed and input looks like an email, try email lookup
        if user is None and "@" in username:
            try:
                user_obj = User.objects.only("username").filter(email=username).first()
                if user_obj:
                    user = authenticate(
                        request, username=user_obj.username, password=password
                    )
            except Exception:
                pass

        if user is None:
            log_action(
                user=None,
                action="LOGIN_FAILED",
                model_name="User",
                object_repr=username,
                ip_address=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                summary=f"Failed login attempt for '{username}'",
            )
            messages.error(request, "Invalid username/email or password.")
            return render(request, "auth/login.html")

        if not user.is_active:
            messages.error(request, "Your account has been deactivated.")
            return render(request, "auth/login.html")

        # No bypass, always enforce 2FA

        # 2FA check
        if _has_confirmed_2fa_device(user):
            request.session["pre_2fa_user_id"] = str(user.pk)
            request.session["pre_2fa_remember_me"] = remember_me
            request.session["pre_2fa_backend"] = user.backend

            # Determine which 2FA method the user has
            totp_devices = list(
                TOTPDevice.objects.devices_for_user(user, confirmed=True)
            )
            email_devices = list(
                EmailDevice.objects.devices_for_user(user, confirmed=True)
            )

            method = "totp"
            if email_devices and not totp_devices:
                method = "email"
                device = email_devices[0]
                try:
                    device.generate_challenge()
                except OTPThrottledError:
                    # Generic copy — never disclose remaining cool-down time
                    # (would let an attacker enumerate the throttle window).
                    messages.info(
                        request,
                        "A code was just sent. Please check your inbox before "
                        "requesting another.",
                    )

            return render(
                request,
                "auth/login.html",
                {
                    "show_otp": True,
                    "username": username,
                    "otp_method": method,
                    "user_email": user.email,
                },
            )

        return _start_2fa_setup_flow(request, user, remember_me)

    return render(request, "auth/login.html")


def _handle_otp_verification(request: HttpRequest) -> HttpResponse:
    """Verify OTP token from a partially authenticated session.

    Args:
        request: HTTP POST request with ``otp_token``.

    Returns:
        Redirect on success or re-rendered login page on failure.
    """
    user_id = request.session.get("pre_2fa_user_id")
    remember_me = request.session.get("pre_2fa_remember_me")
    backend = request.session.get("pre_2fa_backend")
    failed_attempts = int(request.session.get(OTP_MAX_FAILURES_SESSION, 0))

    if not user_id:
        messages.error(request, "Session expired. Please login again.")
        return redirect("auth_app:login")

    if failed_attempts >= OTP_MAX_FAILURES:
        for key in (
            "pre_2fa_user_id",
            "pre_2fa_remember_me",
            "pre_2fa_backend",
            OTP_MAX_FAILURES_SESSION,
        ):
            request.session.pop(key, None)
        messages.error(
            request,
            "Too many invalid verification codes. Please sign in again.",
        )
        return redirect("auth_app:login")

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        messages.error(request, "User not found. Please try again.")
        return redirect("auth_app:login")

    otp_token = request.POST.get("otp_token", "").strip()
    if not otp_token:
        messages.error(request, "Please enter your verification code.")
        return render(request, "auth/login.html", {"show_otp": True})

    otp_device = None

    # Try TOTP devices first
    for device in TOTPDevice.objects.devices_for_user(user, confirmed=True):
        if device.verify_token(otp_token):
            otp_device = device
            break

    # Try email devices
    if otp_device is None:
        for device in EmailDevice.objects.devices_for_user(user, confirmed=True):
            if device.verify_token(otp_token):
                otp_device = device
                break

    # Try static (backup) codes
    if otp_device is None:
        from django_otp.plugins.otp_static.models import StaticDevice

        for device in StaticDevice.objects.devices_for_user(user, confirmed=True):
            if device.verify_token(otp_token):
                otp_device = device
                messages.warning(
                    request,
                    "You used a backup code. Consider generating new backup codes.",
                )
                break

    if otp_device is None:
        request.session[OTP_MAX_FAILURES_SESSION] = failed_attempts + 1
        log_action(
            user=user,
            action="2FA_FAILED",
            model_name="User",
            object_id=str(user.id),
            object_repr=user.username,
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            summary=f"Failed 2FA verification for user {user.username}",
        )
        if request.session[OTP_MAX_FAILURES_SESSION] >= OTP_MAX_FAILURES:
            for key in (
                "pre_2fa_user_id",
                "pre_2fa_remember_me",
                "pre_2fa_backend",
                OTP_MAX_FAILURES_SESSION,
            ):
                request.session.pop(key, None)
            messages.error(
                request,
                "Too many invalid verification codes. Please sign in again.",
            )
            return redirect("auth_app:login")

        messages.error(request, "Invalid verification code. Please try again.")
        return render(request, "auth/login.html", {"show_otp": True})

    if backend:
        user.backend = backend

    # Clear pre-2FA session data
    for key in (
        "pre_2fa_user_id",
        "pre_2fa_remember_me",
        "pre_2fa_backend",
        OTP_MAX_FAILURES_SESSION,
    ):
        request.session.pop(key, None)

    return _complete_login(request, user, remember_me, otp_device)


def _complete_login(
    request: HttpRequest,
    user: User,
    remember_me: str | None,
    otp_device: object | None = None,
) -> HttpResponse:
    """Finalise login: set session, audit, redirect.

    Args:
        request: HTTP request.
        user: Authenticated user instance.
        remember_me: Truthy string if "Remember Me" was checked.
        otp_device: The verified OTP device, if any.

    Returns:
        Redirect to the appropriate dashboard.
    """
    login(request, user)

    if otp_device:
        request.session["otp_device_id"] = otp_device.persistent_id

    if not remember_me:
        request.session.set_expiry(0)

    # #region agent log
    agent_debug_log(
        "auth.py:_complete_login",
        "complete login redirect decision",
        "H-A",
        {
            "is_staff": user.is_staff,
            "has_client_profile": _has_client_portal_profile(user),
            "has_groups": user.groups.exists(),
            "has_perms": user.user_permissions.exists(),
            "username": user.username,
        },
    )
    # #endregion

    has_any_access = _has_client_portal_profile(user) or user_has_access(user)
    if not has_any_access:
        log_action(
            user=user,
            action="LOGIN_DENIED_NO_ACCESS",
            model_name="User",
            object_id=str(user.id),
            object_repr=user.get_full_name() or user.username,
            ip_address=get_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            summary=(
                f"Login completed but user {user.username} has no portal or staff access"
            ),
        )
        messages.error(
            request,
            "Your account has no staff flag, groups, or client portal "
            "profile assigned. Contact your administrator.",
        )
        logout(request)
        return redirect("auth_app:login")

    log_action(
        user=user,
        action="LOGIN",
        model_name="User",
        object_id=str(user.id),
        object_repr=user.get_full_name() or user.username,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        summary=f"User {user.username} logged in from {get_client_ip(request)}",
    )
    messages.success(
        request,
        f"Welcome back, {user.get_full_name() or user.username}!",
    )

    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts=None):
        return redirect(next_url)

    if user.is_staff:
        return redirect("custom_admin:admin_dashboard")
    if _has_client_portal_profile(user):
        return redirect("client_portal:dashboard")
    return redirect("core:user_dashboard")


def _start_2fa_setup_flow(
    request: HttpRequest,
    user: User,
    remember_me: str | None,
) -> HttpResponse:
    """Log in a user without 2FA devices and force setup flow.

    Args:
        request: HTTP request.
        user: Authenticated user instance.
        remember_me: Truthy string if "Remember Me" was checked.

    Returns:
        Redirect to mandatory 2FA setup page.
    """
    login(request, user)

    if not remember_me:
        request.session.set_expiry(0)

    log_action(
        user=user,
        action="2FA_SETUP_REQUIRED",
        model_name="User",
        object_id=str(user.id),
        object_repr=user.get_full_name() or user.username,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        summary=f"User {user.username} must complete 2FA setup",
    )
    messages.warning(
        request,
        "Two-factor authentication is required. Please complete setup now.",
    )
    return redirect("auth_app:setup_2fa")


@login_required
@require_http_methods(["POST"])
def user_logout(request: HttpRequest) -> HttpResponse:
    """Handle user logout with audit logging.

    Args:
        request: HTTP POST request.

    Returns:
        Redirect to login page.
    """
    user_name = request.user.get_full_name() or request.user.username
    user_id = str(request.user.id)
    username = request.user.username

    log_action(
        user=request.user,
        action="LOGOUT",
        model_name="User",
        object_id=user_id,
        object_repr=user_name,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        summary=f"User {username} logged out from {get_client_ip(request)}",
    )

    logout(request)
    messages.info(
        request, f"Goodbye {user_name}! You have been logged out successfully."
    )
    return redirect("auth_app:login")
