from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0053_datafiledonor_donor_gift_aid_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="campaign",
            name="client",
            field=models.ForeignKey(
                help_text="Client/charity this campaign belongs to",
                on_delete=models.deletion.CASCADE,
                related_name="campaigns",
                to="core.client",
            ),
        ),
    ]