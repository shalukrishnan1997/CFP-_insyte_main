from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self) -> None:
        """Import core domain signals (audit signals live in audit.signals).

        ``core.signals`` is imported purely for its module-level @receiver
        registrations; the import looks unused but removing it silently
        disconnects every domain signal handler.
        """
        import core.signals  # noqa: F401  # pyright: ignore[reportUnusedImport]
