# State-only model move from core → notifications app.
#
# The Notification table physically stays at `core_notification` (preserved
# via Meta.db_table). This migration tells Django's state graph that the
# model now lives in the `notifications` app without running any DDL.
#
# Companion: core/migrations/0060_remove_notification_state_only.py removes
# the model from core's state graph. Both must apply together (cross-app
# dependencies enforce the ordering).
#
# A RunPython step rewrites the existing ContentType row from
# (core, notification) → (notifications, notification) BEFORE Django's
# post-migrate hook (which deletes "stale" ContentTypes) runs. Without
# this, the old ContentType would be cascade-deleted, taking its
# auth_permission rows — and any group/user permission grants — with it.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _repoint_contenttype_forward(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="core", model="notification").update(
        app_label="notifications"
    )


def _repoint_contenttype_reverse(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ContentType.objects.filter(app_label="notifications", model="notification").update(
        app_label="core"
    )


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        # Must run AFTER core deletes the model from its state, so that
        # both states don't simultaneously claim ownership of the table.
        ("core", "0060_remove_notification_state_only"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="Notification",
                    fields=[
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("updated_at", models.DateTimeField(auto_now=True)),
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
                            "title",
                            models.CharField(
                                help_text="Short notification title", max_length=200
                            ),
                        ),
                        (
                            "message",
                            models.TextField(help_text="Full notification message"),
                        ),
                        (
                            "notification_type",
                            models.CharField(
                                choices=[
                                    ("info", "Info"),
                                    ("success", "Success"),
                                    ("warning", "Warning"),
                                    ("error", "Error"),
                                ],
                                db_index=True,
                                default="info",
                                help_text="Type of notification",
                                max_length=20,
                            ),
                        ),
                        (
                            "related_object_type",
                            models.CharField(
                                blank=True,
                                default="",
                                help_text="Model name of related object",
                                max_length=50,
                            ),
                        ),
                        (
                            "related_object_id",
                            models.CharField(
                                blank=True,
                                default="",
                                help_text="ID of related object",
                                max_length=255,
                            ),
                        ),
                        (
                            "link",
                            models.CharField(
                                blank=True,
                                default="",
                                help_text="Link to navigate to",
                                max_length=500,
                            ),
                        ),
                        (
                            "is_read",
                            models.BooleanField(
                                db_index=True,
                                default=False,
                                help_text="Whether notification has been read",
                            ),
                        ),
                        (
                            "read_at",
                            models.DateTimeField(
                                blank=True,
                                help_text="When notification was read",
                                null=True,
                            ),
                        ),
                        (
                            "user",
                            models.ForeignKey(
                                help_text="User who receives this notification",
                                on_delete=django.db.models.deletion.CASCADE,
                                related_name="notifications",
                                to=settings.AUTH_USER_MODEL,
                            ),
                        ),
                    ],
                    options={
                        "db_table": "core_notification",
                        "ordering": ["-created_at"],
                        "indexes": [
                            models.Index(
                                fields=["user", "-created_at"],
                                name="core_notifi_user_id_1cc5b6_idx",
                            ),
                            models.Index(
                                fields=["user", "is_read", "-created_at"],
                                name="core_notifi_user_id_f286cd_idx",
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
