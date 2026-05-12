"""Move scan_form_type from Campaign to ScanBatch.

Batch layout is now chosen at ingest time rather than stored on the campaign.
"""

from django.db import migrations, models


def copy_campaign_scan_form_type_to_scan_batches(apps, schema_editor) -> None:
    """Backfill existing scan batches from their parent campaign layout."""
    Campaign = apps.get_model("core", "Campaign")
    ScanBatch = apps.get_model("core", "ScanBatch")

    campaign_layouts = dict(Campaign.objects.values_list("id", "scan_form_type"))
    batches_to_update = []

    for batch in ScanBatch.objects.all().iterator():
        layout = campaign_layouts.get(batch.campaign_id, "simplex")
        if batch.scan_form_type != layout:
            batch.scan_form_type = layout
            batches_to_update.append(batch)

    if batches_to_update:
        ScanBatch.objects.bulk_update(batches_to_update, ["scan_form_type"])


def noop_reverse(apps, schema_editor) -> None:
    """No-op reverse migration for forward-only dev refactor."""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0035_scanplaceholder_page_keys"),
    ]

    operations = [
        migrations.AddField(
            model_name="scanbatch",
            name="scan_form_type",
            field=models.CharField(
                choices=[
                    ("simplex", "Simplex"),
                    ("duplex", "Duplex"),
                    ("simplex_with_payment", "Simplex + Payment Doc"),
                    ("duplex_with_payment", "Duplex + Payment Doc"),
                    ("mixed_mail", "Legacy Mixed Mail / Patch T"),
                ],
                db_index=True,
                default="simplex",
                help_text=(
                    "Physical layout of donor documents in this batch. "
                    "Simplex/Duplex are valid for card, direct debit, cash, and "
                    "non-financial batches. Payment-doc layouts are valid for cheque, "
                    "voucher, and postal order batches."
                ),
                max_length=30,
            ),
        ),
        migrations.RunPython(
            copy_campaign_scan_form_type_to_scan_batches,
            reverse_code=noop_reverse,
        ),
        migrations.RemoveField(
            model_name="campaign",
            name="scan_form_type",
        ),
    ]