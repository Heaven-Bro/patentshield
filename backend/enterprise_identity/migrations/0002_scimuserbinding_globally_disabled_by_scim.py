# Track whether global account disablement was caused by this SCIM binding.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("enterprise_identity", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="scimuserbinding",
            name="globally_disabled_by_scim",
            field=models.BooleanField(default=False),
        ),
    ]
