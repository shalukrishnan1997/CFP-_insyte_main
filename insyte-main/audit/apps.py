from django.apps import AppConfig


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "audit"

    def ready(self) -> None:
        """Connect per-model audit signal handlers."""
        from audit.signals import connect_audit_signals

        connect_audit_signals()
