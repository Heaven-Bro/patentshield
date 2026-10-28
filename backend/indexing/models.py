"""Tenant-scoped text chunks and vector storage for PatentShield."""

from __future__ import annotations

import uuid

from django.db import models
from pgvector.django import HnswIndex, VectorField

from tenants.models import TenantScopedModel


class TextChunk(TenantScopedModel):
    """An addressable text span with optional dense-vector representation."""

    class SectionType(models.TextChoices):
        ABSTRACT = "ABSTRACT", "Abstract"
        CLAIM = "CLAIM", "Claim"
        SPECIFICATION = "SPECIFICATION", "Specification"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    patent = models.ForeignKey(
        "patents.Patent",
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    document_version = models.ForeignKey(
        "patents.PatentDocumentVersion",
        on_delete=models.CASCADE,
        related_name="chunks",
    )
    section_type = models.CharField(
        max_length=50,
        choices=SectionType.choices,
    )
    claim_number = models.CharField(max_length=30, null=True, blank=True)
    text = models.TextField()
    language_code = models.CharField(max_length=8, default="und", db_index=True)
    page_start = models.IntegerField(null=True, blank=True)
    page_end = models.IntegerField(null=True, blank=True)
    char_start = models.IntegerField(null=True, blank=True)
    char_end = models.IntegerField(null=True, blank=True)
    embedding = VectorField(dimensions=1024, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("tenant", "document_version"),
                name="chunk_tenant_doc_idx",
            ),
            HnswIndex(
                name="chunk_embedding_hnsw",
                fields=("embedding",),
                m=16,
                ef_construction=64,
                opclasses=("vector_cosine_ops",),
            ),
        ]
        constraints = [
            # A document version intentionally has many chunks. Enforce
            # uniqueness at the chunk-range level, not at document level.
            models.UniqueConstraint(
                fields=(
                    "tenant",
                    "document_version",
                    "section_type",
                    "claim_number",
                    "char_start",
                    "char_end",
                ),
                name="chunk_source_range_uniq",
            ),
        ]
        ordering = (
            "document_version_id",
            "section_type",
            "claim_number",
            "char_start",
        )

    def __str__(self) -> str:
        return (
            f"{self.section_type} chunk for patent {self.patent_id} "
            f"(version {self.document_version_id})"
        )
