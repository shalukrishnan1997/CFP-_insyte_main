# State-only DeleteModel for the Notification move from core → notifications.
#
# The DeleteModel removes Notification from core's Django ORM state graph
# but leaves the underlying core_notification table untouched (no DROP).
# The model is recreated in notifications/migrations/0001_initial_state_only.py,
# also state-only, also pointing at db_table="core_notification".
#
# The two migrations are cross-dependent: notifications.0001 lists this
# migration in its `dependencies`, ensuring Django runs DeleteModel first
# so the state graph doesn't briefly contain two models both bound to
# the same table.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0059_alter_donation_data_file_donor_alter_donation_donor_and_more"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="Notification"),
            ],
            database_operations=[],
        ),
    ]
