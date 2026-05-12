"""User profile and password management views."""

from typing import cast

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from core.models import User


@login_required
def user_profile(request: HttpRequest) -> HttpResponse:
    """View and edit the current user's own profile.

    Args:
        request: HTTP request.

    Returns:
        Rendered profile page.
    """
    user = cast(User, request.user)
    if request.method == "POST":
        # The profile page submits TWO kinds of POST: a photo-only one (the
        # auto-submit `<input type="file">` for the avatar, or the "Remove
        # photo" button), and a full profile-edit one. The photo-only form
        # only sends `profile_image` / `remove_profile_image`, so the email
        # field is absent — don't apply email-required validation in that
        # case (otherwise photo upload always fails with "Email is required").
        photo_only = "email" not in request.POST and (
            "profile_image" in request.FILES
            or request.POST.get("remove_profile_image") == "1"
        )

        if request.POST.get("remove_profile_image") == "1" and user.profile_image:
            user.profile_image.delete(save=False)
            user.profile_image = None
        uploaded_image = request.FILES.get("profile_image")
        if uploaded_image is not None:
            user.profile_image = uploaded_image

        if not photo_only:
            user.first_name = request.POST.get("first_name", "").strip()
            user.last_name = request.POST.get("last_name", "").strip()
            user.email = request.POST.get("email", "").strip()

            if not user.email:
                messages.error(request, "Email is required.")
                return redirect("auth_app:user_profile")

        try:
            user.save()
            messages.success(
                request,
                "Profile photo updated."
                if photo_only
                else "Profile updated successfully!",
            )
        except Exception as exc:
            messages.error(request, f"Error updating profile: {exc!s}")

    user_data = {
        "full_name": user.get_full_name(),
        "Username": user.username,
        "Last Name": user.last_name,
        "Email": user.email,
        "Is Staff": user.is_staff,
        "Is Active": user.is_active,
        "last_login": user.last_login,
        "date_joined": user.date_joined,
    }

    return render(request, "auth/profile.html", {"user": user, "user_data": user_data})


@login_required
def change_password(request: HttpRequest) -> HttpResponse:
    """Allow the current user to change their password.

    Args:
        request: HTTP request.

    Returns:
        Rendered change-password form or redirect on success.
    """
    # @login_required guarantees request.user is authenticated
    user = cast(User, request.user)
    if request.method == "POST":
        current_password = request.POST.get("current_password") or ""
        new_password = request.POST.get("new_password")
        confirm_password = request.POST.get("confirm_password")

        if not user.check_password(current_password):
            messages.error(request, "Current password is incorrect.")
            return redirect("auth_app:change_password")

        if not new_password:
            messages.error(request, "New password cannot be empty.")
            return redirect("auth_app:change_password")

        if new_password != confirm_password:
            messages.error(request, "New passwords do not match.")
            return redirect("auth_app:change_password")

        if len(new_password) < 8:
            messages.error(request, "Password must be at least 8 characters long.")
            return redirect("auth_app:change_password")

        try:
            validate_password(new_password, user=user)
        except ValidationError as e:
            for error in e.messages:
                messages.error(request, error)
            return redirect("auth_app:change_password")

        user.set_password(new_password)
        if user.must_change_password:
            user.must_change_password = False
        user.save()

        # Keep the user logged in after password change
        update_session_auth_hash(request, user)
        messages.success(request, "Password changed successfully!")
        return redirect("auth_app:user_profile")

    return render(request, "auth/change_password.html")
