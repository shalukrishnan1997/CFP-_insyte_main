# Generated manually for PCI manual-redaction workflow

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_redaction_fields(apps, schema_editor):
    ScanPlaceholder = apps.get_model("core", "ScanPlaceholder")
    for ph in ScanPlaceholder.objects.iterator(chunk_size=500):
        keys = list(ph.page_keys) if ph.page_keys else []
        if not keys and ph.image_path:
            keys = [ph.image_path]
        ph.original_page_keys = keys
        ph.redaction_status = "completed"
        ph.save(update_fields=["original_page_keys", "redaction_status"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0057_remove_scanbatch_scanbatch_pay_method_form_type_compat_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="scanplaceholder",
            name="original_page_keys",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "Snapshot of R2 keys at ingest (before manual redaction). "
                    "Used for redaction workflow and audit."
                ),
            ),
        ),
        migrations.AddField(
            model_name="scanplaceholder",
            name="redaction_completed_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When manual redaction was marked complete",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="scanplaceholder",
            name="redaction_notes",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Optional notes from the staff member who completed redaction"
                ),
            ),
        ),
        migrations.AddField(
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
                    "Manual redaction state when REQUIRE_MANUAL_REDACTION is enabled"
                ),
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="scanplaceholder",
            name="redacted_by",
            field=models.ForeignKey(
                blank=True,
                help_text="Staff user who uploaded redacted pages",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="redacted_scan_placeholders",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="scanplaceholder",
            index=models.Index(
                fields=["redaction_status", "-updated_at"],
                name="scan_redaction_stat_upd_idx",
            ),
        ),
        migrations.AlterModelOptions(
            name="scanplaceholder",
            options={
                "ordering": ["created_at"],
                "permissions": [
                    (
                        "view_unredacted_scan",
                        "Can view scan images before manual redaction is complete",
                    ),
                ],
                "verbose_name": "Scan Placeholder",
                "verbose_name_plural": "Scan Placeholders",
            },
        ),
        migrations.RunPython(backfill_redaction_fields, migrations.RunPython.noop),
    ]
