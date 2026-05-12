from django.apps import AppConfig


class CustomAdminConfig(AppConfig):
    """Configuration for the custom_admin application."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "custom_admin"
    verbose_name = "Custom Admin"
