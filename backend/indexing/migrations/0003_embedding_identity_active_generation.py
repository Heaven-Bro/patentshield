import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("indexing", "0002_textchunk_language_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="textchunk",
            name="embedding_model",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="textchunk",
            name="embedding_revision",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="textchunk",
            name="index_generation",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False),
        ),
        migrations.AddField(
            model_name="textchunk",
            name="is_active",
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.RemoveConstraint(
            model_name="textchunk",
            name="chunk_source_range_uniq",
        ),
        migrations.AddConstraint(
            model_name="textchunk",
            constraint=models.UniqueConstraint(
                fields=(
                    "tenant", "document_version", "section_type", "claim_number",
                    "char_start", "char_end", "index_generation",
                ),
                name="chunk_source_range_uniq",
            ),
        ),
    ]
