"""Tenant-scoped patent records, document versions, claims, and schematics."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from tenants.models import TenantScopedModel


class Patent(TenantScopedModel):
    """A patent record uploaded to or managed within a tenant workspace."""

    class Status(models.TextChoices):
        UPLOADED = "UPLOADED", "Uploaded"
        PROCESSING = "PROCESSING", "Processing"
        INDEXED = "INDEXED", "Indexed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    publication_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    application_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
    )
    jurisdiction = models.CharField(
        max_length=10,
        blank=True,
        null=True,
    )
    title = models.TextField()
    abstract = models.TextField(blank=True, null=True)
    source_type = models.CharField(max_length=30, default="UPLOAD")
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.UPLOADED,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="patents_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("tenant", "-created_at"),
                name="pat_tenant_created_idx",
            ),
            models.Index(
                fields=("jurisdiction", "publication_number"),
                name="pat_jur_pubnum_idx",
            ),
        ]


class PatentDocumentVersion(TenantScopedModel):
    """An immutable uploaded document version associated with a patent."""

    class ExtractionStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        EXTRACTING = "EXTRACTING", "Extracting"
        EXTRACTED = "EXTRACTED", "Extracted"
        OCR_REQUIRED = "OCR_REQUIRED", "OCR required"
        EXTRACTION_FAILED = "EXTRACTION_FAILED", "Extraction failed"
        FAILED = "FAILED", "Failed (legacy)"

    class StageStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        INDEXED = "INDEXED", "Indexed"
        FAILED = "FAILED", "Failed"
        SKIPPED = "SKIPPED", "Skipped"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    patent = models.ForeignKey(
        Patent,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version_number = models.PositiveIntegerField()
    storage_key = models.TextField()
    content_sha256 = models.CharField(max_length=64)
    mime_type = models.CharField(max_length=100, default="application/pdf")
    page_count = models.IntegerField(null=True, blank=True)
    extraction_status = models.CharField(
        max_length=20,
        choices=ExtractionStatus.choices,
        default=ExtractionStatus.PENDING,
    )
    extraction_started_at = models.DateTimeField(null=True, blank=True)
    extraction_queued_at = models.DateTimeField(null=True, blank=True)
    extraction_error_code = models.CharField(max_length=64, blank=True, default="")
    text_indexing_status = models.CharField(
        max_length=12,
        choices=StageStatus.choices,
        default=StageStatus.PENDING,
        db_index=True,
    )
    schematic_indexing_status = models.CharField(
        max_length=12,
        choices=StageStatus.choices,
        default=StageStatus.PENDING,
        db_index=True,
    )
    text_indexing_attempts = models.PositiveSmallIntegerField(default=0)
    schematic_indexing_attempts = models.PositiveSmallIntegerField(default=0)
    # Persist Celery task IDs so duplicate extraction dispatches are harmless
    # and late-ack redeliveries can resume the same reserved stage.
    text_indexing_task_id = models.CharField(max_length=100, blank=True, default="")
    schematic_indexing_task_id = models.CharField(max_length=100, blank=True, default="")
    text_indexing_started_at = models.DateTimeField(null=True, blank=True)
    schematic_indexing_started_at = models.DateTimeField(null=True, blank=True)
    text_indexing_error_code = models.CharField(max_length=64, blank=True, default="")
    schematic_indexing_error_code = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("extraction_status", "extraction_queued_at"),
                name="patver_queue_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("patent", "version_number"),
                name="patent_version_number_uniq",
            ),
        ]


class Claim(TenantScopedModel):
    """An extracted patent claim with document provenance information."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    patent = models.ForeignKey(
        Patent,
        on_delete=models.CASCADE,
        related_name="claims",
    )
    document_version = models.ForeignKey(
        PatentDocumentVersion,
        on_delete=models.CASCADE,
        related_name="claims",
    )
    claim_number = models.CharField(max_length=30)
    claim_text = models.TextField()
    claim_type = models.CharField(
        max_length=20,
        choices=[
            ("INDEPENDENT", "Independent"),
            ("DEPENDENT", "Dependent"),
        ],
        default="INDEPENDENT",
    )
    depends_on_claim_number = models.CharField(
        max_length=30,
        null=True,
        blank=True,
    )
    source_page_index = models.IntegerField(null=True, blank=True)
    source_start_offset = models.IntegerField(null=True, blank=True)
    source_end_offset = models.IntegerField(null=True, blank=True)
    extraction_confidence = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("patent", "document_version", "claim_number"),
                name="patent_version_claim_num_uniq",
            ),
        ]


class Schematic(TenantScopedModel):
    """A patent figure or schematic and its stored visual annotation."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    patent = models.ForeignKey(
        Patent,
        on_delete=models.CASCADE,
        related_name="schematics",
    )
    document_version = models.ForeignKey(
        PatentDocumentVersion,
        on_delete=models.CASCADE,
        related_name="schematics",
    )
    figure_number = models.CharField(max_length=50, null=True, blank=True)
    page_index = models.IntegerField()
    image_storage_key = models.TextField()
    bbox = models.JSONField(null=True, blank=True)
    caption = models.TextField(null=True, blank=True)
    visual_index_version = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)



# Canonical public patent-source models live in a separate module so public
# data stays distinct from tenant-owned uploads. Import here for Django model
# discovery during app loading and migration generation.
from patents.intelligence_models import (  # noqa: E402
    ConnectorSyncCursor,
    PatentFamily,
    PatentFamilyMember,
    PatentLegalEvent,
    PatentPublication,
    PatentSourceSnapshot,
)

# Tenant-owned surveillance state is registered alongside canonical patent
# intelligence models, while public patent/family/legal-event rows remain global.
from patents.surveillance_models import (  # noqa: E402,F401
    AlertDelivery,
    AlertDeliveryAttempt,
    AlertDestination,
    AlertSubscription,
    SurveillanceAlert,
    SurveillanceWatchRule,
    SurveillanceWatchlist,
    WatchlistObservation,
    WatchlistObservedLegalEvent,
)
