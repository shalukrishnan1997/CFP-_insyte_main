from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("custom_admin", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(
            name="Template",
        ),
    ]