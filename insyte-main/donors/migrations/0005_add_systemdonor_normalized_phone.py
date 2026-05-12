"""Add ``SystemDonor.normalized_phone`` and backfill existing rows.

Without the backfill, donors imported before this migration would not be
findable via phone search until a future ``save()`` recomputes the field —
which may never happen for inactive donors. The data migration runs the
same :func:`core.services.phone.normalize_phone` helper used at write
time so stored values are guaranteed consistent.
"""

from django.db import migrations, models


def backfill_normalized_phone(apps, schema_editor):
    """Populate ``normalized_phone`` for every existing SystemDonor."""
    from core.services.phone import normalize_phone

    SystemDonor = apps.get_model("donors", "SystemDonor")
    queryset = SystemDonor.objects.exclude(phone="").only("id", "phone")

    batch: list = []
    BATCH_SIZE = 1000
    for donor in queryset.iterator(chunk_size=BATCH_SIZE):
        donor.normalized_phone = normalize_phone(donor.phone)
        batch.append(donor)
        if len(batch) >= BATCH_SIZE:
            SystemDonor.objects.bulk_update(batch, ["normalized_phone"])
            batch.clear()
    if batch:
        SystemDonor.objects.bulk_update(batch, ["normalized_phone"])


def noop_reverse(apps, schema_editor):
    """No-op reverse — dropping the column reverses the data change implicitly."""


class Migration(migrations.Migration):
    dependencies = [
        ("donors", "0004_systemdonor_core_system_client__694633_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="systemdonor",
            name="normalized_phone",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text=(
                    "Digits-only canonical form of phone, populated from phone "
                    "on save. Used by the donor-search API so operator-typed "
                    "variants (e.g. '07700 900 123' vs '07700900123') match "
                    "the same donor."
                ),
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_normalized_phone, noop_reverse),
    ]
