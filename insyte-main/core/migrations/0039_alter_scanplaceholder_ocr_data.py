from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0038_alter_letterbatch_donation_filter"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scanplaceholder",
            name="ocr_data",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Raw Document AI results",
            ),
        ),
    ]