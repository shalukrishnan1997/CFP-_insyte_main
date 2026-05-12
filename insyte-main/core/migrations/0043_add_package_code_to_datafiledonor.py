from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0042_scanbatch_layout_compatibility_constraint"),
    ]

    operations = [
        migrations.AddField(
            model_name="datafiledonor",
            name="package_code",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Package code assigned to the donor for this campaign",
                max_length=100,
            ),
            preserve_default=False,
        ),
        migrations.AddIndex(
            model_name="datafiledonor",
            index=models.Index(
                fields=["package_code"], name="core_datafiledonor_package_code_idx"
            ),
        ),
    ]
