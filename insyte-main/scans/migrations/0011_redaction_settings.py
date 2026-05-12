"""Create RedactionSettings singleton + relax ScanPlaceholder.redaction_status help text.

Defaults (`require_for_card=True`, `require_for_direct_debit=True`, all others
`False`) match the policy the user picked when this refactor was planned.
"""

from typing import Any

from django.conf import settings
from django.db import migrations, models


def create_default_settings(apps: Any, schema_editor: Any) -> None:
    """Insert the singleton row with card + direct debit pre-required."""
    del schema_editor
    RedactionSettings = apps.get_model("scans", "RedactionSettings")
    RedactionSettings.objects.update_or_create(
        pk=1,
        defaults={
            "require_for_card": True,
            "require_for_direct_debit": True,
            "require_for_cash": False,
            "require_for_caf": False,
            "require_for_cheque": False,
            "require_for_postal_order": False,
            "require_for_non_financial": False,
        },
    )


def remove_default_settings(apps: Any, schema_editor: Any) -> None:
    """Delete the singleton row when the migration is reversed."""
    del schema_editor
    RedactionSettings = apps.get_model("scans", "RedactionSettings")
    RedactionSettings.objects.filter(pk=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("scans", "0010_scanplaceholder_donor_match_candidates"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="RedactionSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "require_for_card",
                    models.BooleanField(
                        default=True,
                        help_text="QA must redact the scan before approving Card donations.",
                    ),
                ),
                (
                    "require_for_direct_debit",
                    models.BooleanField(
                        default=True,
                        help_text="QA must redact the scan before approving Direct Debit donations.",
                    ),
                ),
                (
                    "require_for_cash",
                    models.BooleanField(
                        default=False,
                        help_text="QA must redact the scan before approving Cash donations.",
                    ),
                ),
                (
                    "require_for_caf",
                    models.BooleanField(
                        default=False,
                        help_text="QA must redact the scan before approving CAF Voucher donations.",
                    ),
                ),
                (
                    "require_for_cheque",
                    models.BooleanField(
                        default=False,
                        help_text="QA must redact the scan before approving Cheque donations.",
                    ),
                ),
                (
                    "require_for_postal_order",
                    models.BooleanField(
                        default=False,
                        help_text="QA must redact the scan before approving Postal Order donations.",
                    ),
                ),
                (
                    "require_for_non_financial",
                    models.BooleanField(
                        default=False,
                        help_text="QA must redact the scan before approving Non-Financial donations.",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=models.deletion.SET_NULL,
                        related_name="redaction_settings_updates",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Redaction Settings",
                "verbose_name_plural": "Redaction Settings",
            },
        ),
        migrations.AlterField(
            model_name="scanplaceholder",
            name="redaction_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending redaction"),
                    ("in_progress", "Redaction in progress"),
                    ("completed", "Redaction completed"),
                    ("blocked", "Blocked"),
                ],
                db_index=True,
                default="completed",
                help_text=(
                    "Manual redaction state. RedactionSettings determines which "
                    "payment methods require redaction before QA approval."
                ),
                max_length=20,
            ),
        ),
        migrations.RunPython(create_default_settings, remove_default_settings),
    ]
