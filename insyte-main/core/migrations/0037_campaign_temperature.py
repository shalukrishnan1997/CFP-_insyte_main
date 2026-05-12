from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0036_move_scan_form_type_to_scanbatch"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="campaign_temperature",
            field=models.CharField(
                choices=[("warm", "Warm"), ("cold", "Cold")],
                db_index=True,
                default="cold",
                help_text=(
                    "Campaign type: Warm campaigns target known supporters and "
                    "expect QR-backed forms whose donors already exist in the "
                    "configured source. Cold campaigns handle handwritten or "
                    "new-supporter forms where QR is not expected and new donor "
                    "creation is allowed."
                ),
                max_length=20,
            ),
        ),
    ]