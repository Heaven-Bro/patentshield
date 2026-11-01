from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("fto", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="productversion",
            name="extraction_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("PROCESSING", "Processing"),
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
