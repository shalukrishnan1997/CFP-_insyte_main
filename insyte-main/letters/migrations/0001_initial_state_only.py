# State-only model move from core → letters app.
#
# core_lettertemplate and core_letterbatch tables stay put on disk
# (preserved via Meta.db_table). This migration only updates Django's
# ORM state graph.
#
# Companion: core/0062_remove_letterbatch_fks_state_only.py + 0063_initial_state_only.py
# remove the models from core's state graph and re-point Donation.letter_batch.
#
# RunPython steps repoint the auth.Permission ContentType rows from
# (core, lettertemplate / letterbatch) → (letters, lettertemplate /
# letterbatch) so existing permission grants survive.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _repoint_contenttype_forward(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    for model in ("lettertemplate", "letterbatch"):
        ContentType.objects.filter(app_label="core", model=model).update(
            app_label="letters"
        )


def _repoint_contenttype_reverse(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    for model in ("lettertemplate", "letterbatch"):
        ContentType.objects.filter(app_label="letters", model=model).update(
            app_label="core"
        )


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("core", "0062_remove_letterbatch_fks_state_only"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="LetterTemplate",
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
                            "name",
                            models.CharField(
                                default="Template",
                                help_text="User-defined name for this template",
                                max_length=255,
                            ),
                        ),
                        (
                            "template_type",
                            models.CharField(
                                choices=[
                                    ("thank_you", "Thank You Letter"),
                                    ("issue", "Issue Letter"),
                                ],
                                db_index=True,
                                default="thank_you",
                                help_text="Type of letter: thank you (successful payments) or issue (failed payments)",
                                max_length=20,
                            ),
                        ),
                        (
                            "file",
                            models.FileField(upload_to="uploads/letter_templates/"),
                        ),
                        (
                            "format",
                            models.CharField(
                                choices=[("docx", "DOCX"), ("pdf", "PDF")],
                                default="docx",
                                max_length=10,
                            ),
                        ),
                        (
                            "placeholder_mapping",
                            models.JSONField(
                                default=dict,
                                help_text="Mapping of placeholders to model fields",
                            ),
                        ),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        (
                            "campaign",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="letter_templates",
                                to="core.campaign",
                            ),
                        ),
                        (
                            "created_by",
                            models.ForeignKey(
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "db_table": "core_lettertemplate",
                    },
                ),
                migrations.CreateModel(
                    name="LetterBatch",
                    fields=[
                        (
                            "id",
                            models.UUIDField(
                                default=uuid.uuid4,
                                editable=False,
                                primary_key=True,
                                serialize=False,
                            ),
                        ),
                        (
                            "batch_number",
                            models.PositiveIntegerField(
                                help_text="Sequential batch number for this campaign"
                            ),
                        ),
                        (
                            "status",
                            models.CharField(
                                choices=[
                                    ("pending", "Pending"),
                                    ("processing", "Processing"),
                                    ("completed", "Completed"),
                                    ("failed", "Failed"),
                                    ("cancelled", "Cancelled"),
                                ],
                                default="pending",
                                max_length=20,
                            ),
                        ),
                        (
                            "total_letters",
                            models.PositiveIntegerField(
                                default=0,
                                help_text="Total number of letters to generate in this batch",
                            ),
                        ),
                        (
                            "generated_count",
                            models.PositiveIntegerField(
                                default=0,
                                help_text="Number of letters successfully generated",
                            ),
                        ),
                        (
                            "failed_count",
                            models.PositiveIntegerField(
                                default=0,
                                help_text="Number of letters that failed to generate",
                            ),
                        ),
                        (
                            "progress_percent",
                            models.PositiveIntegerField(
                                default=0,
                                help_text="Generation progress percentage (0-100)",
                            ),
                        ),
                        (
                            "letters_per_file",
                            models.PositiveIntegerField(
                                default=100,
                                help_text="Number of letters per merged document file",
                            ),
                        ),
                        (
                            "output_files",
                            models.JSONField(
                                blank=True,
                                default=list,
                                help_text="List of generated file paths",
                            ),
                        ),
                        (
                            "file_count",
                            models.PositiveIntegerField(
                                default=0,
                                help_text="Number of output files generated",
                            ),
                        ),
                        (
                            "donation_filter",
                            models.CharField(
                                choices=[
                                    ("all", "All Donations"),
                                    ("exclude_lgv", "Exclude LGV"),
                                    ("only_hgv", "Only HGV"),
                                ],
                                default="all",
                                help_text="Filter donations by gift value (HGV/LGV)",
                                max_length=20,
                            ),
                        ),
                        (
                            "regenerate_mode",
                            models.BooleanField(
                                default=False,
                                help_text="If True, regenerate all letters. If False, only generate pending letters.",
                            ),
                        ),
                        (
                            "error_log",
                            models.JSONField(
                                blank=True,
                                default=list,
                                help_text="List of errors encountered",
                            ),
                        ),
                        (
                            "celery_task_id",
                            models.CharField(
                                blank=True,
                                help_text="Celery task ID for tracking progress",
                                max_length=255,
                            ),
                        ),
                        ("started_at", models.DateTimeField(blank=True, null=True)),
                        ("completed_at", models.DateTimeField(blank=True, null=True)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
                        (
                            "campaign",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="letter_batches",
                                to="core.campaign",
                            ),
                        ),
                        (
                            "created_by",
                            models.ForeignKey(
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="letter_batches_created",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                        (
                            "failure_template",
                            models.ForeignKey(
                                blank=True,
                                help_text="Template used for issue letters (failed payments). If blank, failed donations are skipped.",
                                null=True,
                                on_delete=django.db.models.deletion.SET_NULL,
                                related_name="failure_letter_batches",
                                to="letters.lettertemplate",
                            ),
                        ),
                        (
                            "template",
                            models.ForeignKey(
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="letter_batches",
                                to="letters.lettertemplate",
                            ),
                        ),
                    ],
                    options={
                        "verbose_name": "Letter Batch",
                        "verbose_name_plural": "Letter Batches",
                        "db_table": "core_letterbatch",
                        "ordering": ["-created_at"],
                        "indexes": [
                            models.Index(
                                fields=["campaign", "status"],
                                name="core_letter_campaig_4b29d9_idx",
                            ),
                            models.Index(
                                fields=["status"],
                                name="core_letter_status_01f067_idx",
                            ),
                        ],
                        "constraints": [
                            models.UniqueConstraint(
                                fields=("campaign", "batch_number"),
                                name="unique_campaign_batch_number",
                            )
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
