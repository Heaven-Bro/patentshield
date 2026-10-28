# Generated for Phase 5 multilingual cross-lingual retrieval.
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("indexing", "0002_textchunk_language_code"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="TranslationRecord",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="multilingual_search_translationrecord_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_text_sha256", models.CharField(max_length=64)),
                ("source_language", models.CharField(default="und", max_length=8)),
                ("target_language", models.CharField(choices=[("en", "English"), ("zh", "Chinese"), ("ja", "Japanese"), ("ko", "Korean")], max_length=8)),
                ("translated_text", models.TextField()),
                ("translation_sha256", models.CharField(max_length=64)),
                ("provider", models.CharField(default="libretranslate", max_length=40)),
                ("provider_version", models.CharField(default="configured-v1", max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="patent_text_translations_created", to=settings.AUTH_USER_MODEL)),
                ("source_chunk", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="translation_records", to="indexing.textchunk")),
            ],
            options={
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(fields=["tenant", "-created_at"], name="ms_tenant_created_idx"),
                    models.Index(fields=["tenant", "source_chunk"], name="ms_tenant_chunk_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("tenant", "source_chunk", "source_text_sha256", "source_language", "target_language", "provider", "provider_version"), name="ms_translation_cache_uniq"),
                ],
            },
        ),
    ]
