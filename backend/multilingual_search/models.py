"""Tenant-scoped cache for explicitly requested patent-text translations."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from tenants.models import TenantScopedModel


class TranslationRecord(TenantScopedModel):
    """A translated view of one indexed chunk, retaining original provenance.

    The source text is never replaced. The text hash and chunk foreign key make
    stale translations invalid when a re-indexed source chunk is replaced.
    Translations are tenant-scoped because the translated text is confidential
    patent content too.
    """

    class Language(models.TextChoices):
        ENGLISH = "en", "English"
        CHINESE = "zh", "Chinese"
        JAPANESE = "ja", "Japanese"
        KOREAN = "ko", "Korean"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_chunk = models.ForeignKey(
        "indexing.TextChunk",
        on_delete=models.CASCADE,
        related_name="translation_records",
    )
    source_text_sha256 = models.CharField(max_length=64)
    source_language = models.CharField(max_length=8, default="und")
    target_language = models.CharField(max_length=8, choices=Language.choices)
    translated_text = models.TextField()
    translation_sha256 = models.CharField(max_length=64)
    provider = models.CharField(max_length=40, default="libretranslate")
    provider_version = models.CharField(max_length=80, default="configured-v1")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="patent_text_translations_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("tenant", "-created_at"),
                name="ms_tenant_created_idx",
            ),
            models.Index(
                fields=("tenant", "source_chunk"),
                name="ms_tenant_chunk_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "tenant",
                    "source_chunk",
                    "source_text_sha256",
                    "source_language",
                    "target_language",
                    "provider",
                    "provider_version",
                ),
                name="ms_translation_cache_uniq",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_language} → {self.target_language} ({self.source_chunk_id})"
