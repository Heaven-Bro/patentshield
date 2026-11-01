from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("office_actions", "0002_alter_amendmentsupportcheck_tenant_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="officeaction",
            name="extraction_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
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
    ]
