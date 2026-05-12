"""Add verification_status field to Donor model.

Tracks whether a donor is verified, unverified (created during
data-file campaign entry), or pending export (created during cold
campaign entry for a donor not in the house file).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0022_add_campaign_donor_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="donor",
            name="verification_status",
            field=models.CharField(
                choices=[
                    ("verified", "Verified"),
                    ("unverified", "Unverified"),
                    ("pending_export", "Pending Export"),
                ],
                default="verified",
                help_text=(
                    "Verified: confirmed donor in the house file. "
                    "Unverified: newly created during data-file campaign entry, "
                    "not yet confirmed. "
                    "Pending Export: created during cold campaign entry for a donor "
                    "not in the house file — reference to be exported to the charity."
                ),
                max_length=20,
            ),
        ),
        migrations.AddIndex(
            model_name="donor",
            index=models.Index(
                fields=["verification_status"],
                name="core_donor_verific_a98710_idx",
            ),
        ),
    ]
