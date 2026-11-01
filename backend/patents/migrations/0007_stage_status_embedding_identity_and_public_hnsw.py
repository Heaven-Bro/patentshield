from django.db import migrations, models
from pgvector.django import HnswIndex


class Migration(migrations.Migration):
    dependencies = [
        ("patents", "0006_patentpublication_embedding_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="patentdocumentversion",
            name="extraction_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("EXTRACTING", "Extracting"),
                    ("EXTRACTED", "Extracted"),
                    ("OCR_REQUIRED", "OCR required"),
                    ("EXTRACTION_FAILED", "Extraction failed"),
                    ("FAILED", "Failed (legacy)"),
                ],
                default="PENDING",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="extraction_error_code",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="text_indexing_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("PROCESSING", "Processing"),
                    ("INDEXED", "Indexed"),
                    ("FAILED", "Failed"),
                    ("SKIPPED", "Skipped"),
                ],
                db_index=True,
                default="PENDING",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="schematic_indexing_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("PROCESSING", "Processing"),
                    ("INDEXED", "Indexed"),
                    ("FAILED", "Failed"),
                    ("SKIPPED", "Skipped"),
                ],
                db_index=True,
                default="PENDING",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="text_indexing_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="schematic_indexing_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="text_indexing_error_code",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="patentdocumentversion",
            name="schematic_indexing_error_code",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="patentpublication",
            name="embedding_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="patentpublication",
            name="embedding_revision",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddIndex(
            model_name="patentpublication",
            index=HnswIndex(
                name="pat_pub_emb_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ),
    ]
