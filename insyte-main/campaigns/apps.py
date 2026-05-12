from django.apps import AppConfig


class CampaignsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "campaigns"

    def ready(self) -> None:
        """Connect campaign signal handlers."""
        from campaigns.signals import connect_campaign_signals

        connect_campaign_signals()
