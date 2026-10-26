"""Persist the exact target claim and allow warning-completed analyses."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("analyses", "0001_initial"),
        ("patents", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisreport",
            name="target_claim",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="target_analyses",
                to="patents.claim",
            ),
        ),
        migrations.AddField(
            model_name="analysisreport",
            name="execution_task_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AlterField(
            model_name="analysisreport",
            name="status",
            field=models.CharField(
                choices=[
                    ("QUEUED", "Queued"),
                    ("RUNNING", "Running"),
                    ("EXPORTING", "Exporting report"),
                    ("COMPLETED", "Completed"),
                    ("COMPLETED_WITH_WARNINGS", "Completed with warnings"),
                    ("FAILED", "Failed"),
                    ("CANCELLED", "Cancelled"),
                ],
                db_index=True,
                default="QUEUED",
                max_length=24,
            ),
        ),
    ]
