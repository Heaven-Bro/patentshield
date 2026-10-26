"""Track report export lifecycle independently from analysis execution."""

from django.db import migrations, models


def restore_legacy_exporting_rows(apps, schema_editor):
    """Separate legacy export-in-progress rows from analysis lifecycle state."""
    AnalysisReport = apps.get_model("analyses", "AnalysisReport")
    legacy_rows = AnalysisReport.objects.filter(status="EXPORTING")
    for report in legacy_rows.iterator():
        # The old exporter overwrote analysis status. Restore the completed
        # analysis state; mark the export complete only when both files exist.
        report.status = "COMPLETED"
        if report.report_storage_key and report.report_json_storage_key:
            report.report_export_status = "COMPLETED"
            report.report_generated_at = report.completed_at or report.updated_at
        else:
            report.report_export_status = "FAILED"
            report.report_export_error_code = "INTERRUPTED_LEGACY_EXPORT"
        report.save(
            update_fields=(
                "status",
                "report_export_status",
                "report_export_error_code",
                "report_generated_at",
            )
        )


def noop(apps, schema_editor):
    """Reverse data operation intentionally leaves export history intact."""
    return None


class Migration(migrations.Migration):
    dependencies = [
        ("analyses", "0002_analysis_target_claim_and_warning_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisreport",
            name="report_export_status",
            field=models.CharField(
                choices=[
                    ("NOT_REQUESTED", "Not requested"),
                    ("QUEUED", "Queued"),
                    ("RUNNING", "Running"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="NOT_REQUESTED",
                max_length=13,
            ),
        ),
        migrations.AddField(
            model_name="analysisreport",
            name="report_export_error_code",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="analysisreport",
            name="report_export_task_id",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="analysisreport",
            name="report_generated_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(restore_legacy_exporting_rows, noop),
    ]
