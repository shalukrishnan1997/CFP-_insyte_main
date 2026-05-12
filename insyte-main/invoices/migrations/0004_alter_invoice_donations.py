# State-only AlterField to update Invoice.donations M2M target after Phase 4
# moved Donation from core to donations app.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("invoices", "0003_alter_invoice_campaign"),
        ("donations", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="invoice",
                    name="donations",
                    field=models.ManyToManyField(
                        blank=True,
                        help_text="Specific donations included in this invoice for detailed line items",
                        related_name="invoices",
                        to="donations.donation",
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
