# Generated migration for adding new donation payment methods
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_change_campaign_manager_email_to_emails"),
    ]

    operations = [
        # Update payment_method field choices
        migrations.AlterField(
            model_name="donation",
            name="payment_method",
            field=models.CharField(
                choices=[
                    ("card", "Card"),
                    ("direct_debit", "Direct Debit"),
                    ("bank_transfer", "Bank Transfer"),
                    ("cash", "Cash"),
                    ("caf", "CAF Voucher/Card"),
                    ("other_charity_voucher", "Other Charity Voucher"),
                    ("cheque", "Cheque"),
                    ("postal_order", "Postal Order"),
                    ("non_financial", "Non Financial/No Payment"),
                ],
                default="card",
                help_text="Payment method used",
                max_length=30,
            ),
        ),
        # Add Other Charity Voucher fields
        migrations.AddField(
            model_name="donation",
            name="other_charity_voucher_number",
            field=models.CharField(
                blank=True,
                help_text="Other charity voucher/cheque number",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="donation",
            name="other_charity_name",
            field=models.CharField(
                blank=True,
                help_text="Name of the charity issuing the voucher",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="donation",
            name="other_charity_voucher_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Other charity voucher amount",
                max_digits=12,
                null=True,
            ),
        ),
        # Add Non-Financial/No Payment fields
        migrations.AddField(
            model_name="donation",
            name="non_financial_reason",
            field=models.CharField(
                blank=True,
                help_text="Reason for non-financial donation (e.g., In-Kind, Volunteering)",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="donation",
            name="non_financial_notes",
            field=models.TextField(
                blank=True,
                help_text="Additional notes for non-financial donations",
            ),
        ),
    ]
