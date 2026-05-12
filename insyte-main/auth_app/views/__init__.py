"""Auth views package — login, profile, and two-factor authentication.

Re-exports all public view functions so URL config continues to work
with ``from auth_app import views`` / ``views.user_login`` etc.
"""

from .auth import (
    _complete_login,
    _handle_otp_verification,
    user_login,
    user_logout,
)
from .profile import change_password, user_profile
from .two_factor import manage_2fa, setup_2fa, show_backup_codes

__all__ = [
    "_complete_login",
    "_handle_otp_verification",
    "change_password",
    "manage_2fa",
    "setup_2fa",
    "show_backup_codes",
    "user_login",
    "user_logout",
    "user_profile",
]
