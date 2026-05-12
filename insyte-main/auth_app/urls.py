from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views

app_name = "auth_app"

urlpatterns = [
    # Login & Logout
    path("login/", views.user_login, name="login"),
    path("logout/", views.user_logout, name="logout"),
    # Two-Factor Authentication
    path("setup-2fa/", views.setup_2fa, name="setup_2fa"),
    path("manage-2fa/", views.manage_2fa, name="manage_2fa"),
    path("backup-codes/", views.show_backup_codes, name="show_backup_codes"),
    # Profile Management
    path("profile/", views.user_profile, name="user_profile"),
    path("change-password/", views.change_password, name="change_password"),
    # Password Reset Flow
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="auth/password_reset.html",
            email_template_name="auth/password_reset_email.html",
            subject_template_name="auth/password_reset_subject.txt",
            success_url=reverse_lazy("auth_app:password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(
            template_name="auth/password_reset_done.html"
        ),
        name="password_reset_done",
    ),
    path(
        "password-reset-confirm/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="auth/password_reset_confirm.html",
            success_url=reverse_lazy("auth_app:password_reset_complete"),
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset-complete/",
        auth_views.PasswordResetCompleteView.as_view(
            template_name="auth/password_reset_complete.html"
        ),
        name="password_reset_complete",
    ),
]
