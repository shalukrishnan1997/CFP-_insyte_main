# State-only AlterField on Donation.letter_batch + DeleteModel for both
# LetterTemplate and LetterBatch in core's state graph.
#
# After this runs:
# - core's state graph no longer contains LetterTemplate or LetterBatch.
# - Donation.letter_batch points at letters.LetterBatch in state.
# - The underlying core_letterbatch / core_lettertemplate tables are
#   unchanged on disk (owned by letters.LetterBatch / LetterTemplate
#   created in letters/0001 with the same db_table values).
#
# Cross-app dependency on letters/0001 ensures letters.LetterBatch
# exists in state by the time AlterField runs.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0062_remove_letterbatch_fks_state_only"),
        ("letters", "0001_initial_state_only"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="donation",
                    name="letter_batch",
                    field=models.ForeignKey(
                        blank=True,
                        help_text="Letter batch this donation belongs to",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="donations",
                        to="letters.letterbatch",
                    ),
                ),
                migrations.RemoveField(
                    model_name="lettertemplate",
                    name="campaign",
                ),
                migrations.RemoveField(
                    model_name="lettertemplate",
                    name="created_by",
                ),
                migrations.DeleteModel(
                    name="LetterBatch",
                ),
                migrations.DeleteModel(
                    name="LetterTemplate",
                ),
            ],
            database_operations=[],
        ),
    ]
