"""Add page_keys field to ScanPlaceholder for multi-page document support.

page_keys stores all R2 object keys that make up a single donor's document when
scanning multi-page form types (duplex, simplex_with_payment, duplex_with_payment).
Empty list is backward-compatible with single-page / mixed_mail batch processing.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_campaign_scan_config"),
    ]

    operations = [
        migrations.AddField(
            model_name="scanplaceholder",
            name="page_keys",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text=(
                    "All R2 keys for this donor's document pages. "
                    "Empty = single page; populated only for multi-page form types."
                ),
            ),
        ),
    ]
