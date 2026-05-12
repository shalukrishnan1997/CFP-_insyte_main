# State-only AlterField + DeleteModel for the PayingInSlip move.
#
# AlterField updates Donation.paying_in_slip's FK target from
# core.PayingInSlip → banking.PayingInSlip. DeleteModel removes
# core.PayingInSlip from the state graph.
#
# The DB column on core_donation (paying_in_slip_id) is unchanged; the
# Donation row continues to reference the same row in core_payinginslip
# (which itself is unchanged). Wrapping in SeparateDatabaseAndState
# prevents Django from issuing any DDL.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("banking", "0001_initial_state_only"),
        ("core", "0060_remove_notification_state_only"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="donation",
                    name="paying_in_slip",
                    field=models.ForeignKey(
                        blank=True,
                        help_text="Paying-in slip for bank statement reconciliation",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="donations",
                        to="banking.payinginslip",
                    ),
                ),
                migrations.DeleteModel(
                    name="PayingInSlip",
                ),
            ],
            database_operations=[],
        ),
    ]
