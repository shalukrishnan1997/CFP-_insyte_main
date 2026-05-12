"""Add scan_form_type and scan_purpose fields to Campaign model.

scan_form_type: controls page-grouping logic for patch-sheet-free scanning.
scan_purpose: controls whether scanned data creates donations or donor updates.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0033_remove_bank_transfer_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="scan_form_type",
            field=models.CharField(
                choices=[
                    ("simplex", "Simplex (1 page per donor)"),
                    ("duplex", "Duplex (2 pages — form front + back)"),
                    (
                        "simplex_with_payment",
                        "Simplex + Payment Doc (2 pages — form front + payment doc)",
                    ),
                    (
                        "duplex_with_payment",
                        "Duplex + Payment Doc (4 pages — form front, form back, payment doc front, payment doc back)",
                    ),
                    (
                        "mixed_mail",
                        "Mixed Mail / Letter — use Patch T sheet (unpredictable page order)",
                    ),
                ],
                db_index=True,
                default="mixed_mail",
                help_text=(
                    "Physical layout of the scanned donation form. "
                    "simplex/duplex/simplex_with_payment/duplex_with_payment allow "
                    "patch-sheet-free scanning; mixed_mail requires a Patch T separator."
                ),
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="scan_purpose",
            field=models.CharField(
                choices=[
                    ("donation", "Donation — create donation records"),
                    (
                        "donor_update",
                        "Donor Update — update donor details only (no payment)",
                    ),
                ],
                db_index=True,
                default="donation",
                help_text=(
                    "What to do with scanned data: 'donation' creates donation records; "
                    "'donor_update' updates donor contact details and creates a "
                    "non-financial donation as an audit trail."
                ),
                max_length=20,
            ),
        ),
    ]
