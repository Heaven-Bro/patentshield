from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("patents", "0010_extraction_dispatch_recovery"),
    ]

    operations = [
        migrations.AlterField(
            model_name="surveillancewatchlist",
            name="poll_status",
            field=models.CharField(
                choices=[
                    ("IDLE", "Idle"),
                    ("QUEUED", "Queued"),
                    ("RUNNING", "Running"),
                    ("SUCCEEDED", "Succeeded"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="IDLE",
                max_length=12,
            ),
        ),
    ]
