from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("office_actions", "0003_shared_ocr_extraction_statuses"),
    ]

    operations = [
        migrations.AlterField(
            model_name="officeaction",
            name="extraction_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("QUEUED", "Queued"),
                    ("PROCESSING", "Processing"),
                    ("RETRYING", "Retrying"),
                    ("EXTRACTED", "Extracted"),
                    ("OCR_REQUIRED", "OCR required"),
                    ("EXTRACTION_FAILED", "Extraction failed"),
                    ("FAILED", "Failed (legacy)"),
                ],
                db_index=True,
                default="PENDING",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="officeaction",
            name="processing_task_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="officeaction",
            name="processing_reserved_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="officeaction",
            name="processing_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="officeaction",
            index=models.Index(
                fields=["tenant", "extraction_status", "processing_reserved_at"],
                name="oa_tenant_extract_res_idx",
            ),
        ),
    ]
