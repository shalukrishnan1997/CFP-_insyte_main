from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0051_alter_client_form_field_mapping"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scanbatch",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="created_scan_batches",
                to="core.user",
            ),
        ),
    ]