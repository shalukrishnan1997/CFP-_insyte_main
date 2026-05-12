from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0037_campaign_temperature"),
    ]

    operations = [
        migrations.AlterField(
            model_name="letterbatch",
            name="donation_filter",
            field=models.CharField(
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
    ]