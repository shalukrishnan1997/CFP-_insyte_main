# State-only RemoveField for LetterBatch's FKs in core's state graph.
#
# Django needs to break the back-references on the core LetterBatch state
# (campaign, created_by, failure_template, template) before the
# accompanying core/0063 can DeleteModel cleanly.
#
# Wrapped in SeparateDatabaseAndState so no actual DDL runs — the
# core_letterbatch table itself stays put and is owned by the new
# letters.LetterBatch state created in letters/0001.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0061_remove_payinginslip_state_only"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name="letterbatch",
                    name="campaign",
                ),
                migrations.RemoveField(
                    model_name="letterbatch",
                    name="created_by",
                ),
                migrations.RemoveField(
                    model_name="letterbatch",
                    name="failure_template",
                ),
                migrations.RemoveField(
                    model_name="letterbatch",
                    name="template",
                ),
            ],
            database_operations=[],
        ),
    ]
