# State-only model move from core → banking app.
#
# The core_payinginslip table physically stays put (preserved via
# Meta.db_table). This migration only updates Django's ORM state graph.
#
# Companion: core/migrations/0061_initial_state_only.py removes the
# model from core's state graph AND alters Donation.paying_in_slip to
# point at the new banking.PayingInSlip target. core.0061 depends on
# this migration so banking.PayingInSlip exists in state by the time
# AlterField runs.
#
# A RunPython step rewrites the existing ContentType row from
# (core, payinginslip) → (banking, payinginslip) BEFORE Django's
# post-migrate hook deletes "stale" ContentTypes — without it, every
# auth_permission grant on PayingInSlip would be cascade-deleted.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _repoint_contenttype_forward(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="core", model="payinginslip").update(
        app_label="banking"
    )


def _repoint_contenttype_reverse(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="banking", model="payinginslip").update(
        app_label="core"
    )


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("core", "0060_remove_notification_state_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="PayingInSlip",
                    fields=[
                        ("id", models.AutoField(primary_key=True, serialize=False)),
                        (
                            "slip_number",
                            models.CharField(
                                db_index=True,
                                help_text="Unique slip number for bank statement reconciliation",
                                max_length=50,
                                unique=True,
                            ),
                        ),
                        (
                            "payment_type",
                            models.CharField(
                                choices=[
                                    ("cash", "Cash"),
                                    ("cheque", "Cheque"),
                                    ("postal_order", "Postal Order"),
                                    ("caf", "CAF Voucher"),
                                    ("mixed", "Mixed"),
                                ],
                                default="mixed",
                                help_text="Type of payments in this slip",
                                max_length=30,
                            ),
                        ),
                        (
                            "banking_date",
                            models.DateField(
                                db_index=True,
                                help_text="Date the payments are being banked",
                            ),
                        ),
                        (
                            "total_amount",
                            models.DecimalField(
                                decimal_places=2,
                                default=0,
                                help_text="Total amount in the slip",
                                max_digits=12,
                            ),
                        ),
                        (
                            "total_items",
                            models.PositiveIntegerField(
                                default=0,
                                help_text="Number of donations in the slip",
                            ),
                        ),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("draft", "Draft"),
                                    ("ready", "Ready for Banking"),
                                    ("submitted_to_bank", "Submitted to Bank"),
                                    ("processed", "Processed"),
                                    ("partially_processed", "Partially Processed"),
                                    ("failed", "Failed"),
                                    ("banked", "Banked"),
                                ],
                                db_index=True,
                                default="draft",
                                help_text="Slip status",
                                max_length=20,
                            ),
                        ),
                        (
                            "notes",
                            models.TextField(
                                blank=True, help_text="Optional notes about the slip"
                            ),
                        ),
                        (
                            "banked_at",
                            models.DateTimeField(
                                blank=True,
                                help_text="When slip was marked as banked",
                                null=True,
                            ),
                        ),
                        (
                            "bank_processed_date",
                            models.DateField(
                                blank=True,
                                help_text="Actual date when bank processed the slip",
                                null=True,
                            ),
                        ),
                        (
                            "processed_amount",
                            models.DecimalField(
                                blank=True,
                                decimal_places=2,
                                help_text="Amount successfully processed by bank",
                                max_digits=12,
                                null=True,
                            ),
                        ),
                        (
                            "completion_status",
                            models.CharField(
                                blank=True,
                                choices=[
                                    ("full_success", "Fully Processed"),
                                    ("partial_success", "Partially Processed"),
                                    ("issues", "Processed with Issues"),
                                    ("failed", "Failed to Process"),
                                ],
                                default="",
                                help_text="Processing outcome status",
                                max_length=20,
                            ),
                        ),
                        (
                            "processing_issues",
                            models.JSONField(
                                blank=True,
                                default=list,
                                help_text="Array of processing issue codes",
                            ),
                        ),
                        (
                            "custom_issue",
                            models.TextField(
                                blank=True,
                                help_text="User-entered description of processing issues",
                            ),
                        ),
                        (
                            "processed_at",
                            models.DateTimeField(
                                blank=True,
                                help_text="When processing information was recorded",
                                null=True,
                            ),
                        ),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        (
                            "banked_by",
                            models.ForeignKey(
                                blank=True,
                                help_text="User who marked the slip as banked",
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="banked_slips",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                        (
                            "client",
                            models.ForeignKey(
                                blank=True,
                                help_text="Primary client (nullable for multi-client slips)",
                                null=True,
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="paying_in_slips",
                                to="core.client",
                            ),
                        ),
                        (
                            "created_by",
                            models.ForeignKey(
                                help_text="User who created the slip",
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="created_paying_in_slips",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                        (
                            "processed_by",
                            models.ForeignKey(
                                blank=True,
                                help_text="User who recorded bank processing results",
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="processed_slips",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Paying-In Slip",
                        "verbose_name_plural": "Paying-In Slips",
                        "db_table": "core_payinginslip",
                        "ordering": ["-banking_date", "-created_at"],
                        "indexes": [
                            models.Index(
                                fields=["client", "banking_date"],
                                name="core_paying_client__638e8c_idx",
                            ),
                            models.Index(
                                fields=["status", "banking_date"],
                                name="core_paying_status_2f8864_idx",
                            ),
                        ],
                    },
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(
            _repoint_contenttype_forward,
            _repoint_contenttype_reverse,
        ),
    ]
