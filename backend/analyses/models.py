"""Tenant-scoped analysis, assessment, and evidence persistence models.

These supporting models are included because the earlier Sprint 1 files do not
currently define AnalysisReport, ClaimAssessment, or EvidenceReference.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from tenants.models import TenantScopedModel


class AnalysisReport(TenantScopedModel):
    """One analysis run and its generated JSON/PDF report artifacts."""

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        EXPORTING = "EXPORTING", "Exporting report"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    patent = models.ForeignKey(
        "patents.Patent",
        on_delete=models.PROTECT,
        related_name="analyses",
    )
    target_document_version = models.ForeignKey(
        "patents.PatentDocumentVersion",
        on_delete=models.PROTECT,
        related_name="analyses",
    )
    analysis_type = models.CharField(max_length=40, default="PRIOR_ART_OVERLAP")
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    configuration = models.JSONField(default=dict, blank=True)
    model_versions = models.JSONField(default=dict, blank=True)
    score_version = models.CharField(max_length=50, blank=True, null=True)
    result_summary = models.JSONField(null=True, blank=True)
    report_storage_key = models.TextField(blank=True, null=True)
    report_json_storage_key = models.TextField(blank=True, null=True)
    error_code = models.CharField(max_length=100, blank=True, null=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="patentshield_analyses_created",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("tenant", "status", "-created_at"),
                name="analysis_tenant_status_idx",
            ),
        ]
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"Analysis {self.pk} ({self.status})"


class ClaimAssessment(TenantScopedModel):
    """Assessment for one limitation of a claim in a given analysis run."""

    class AssessmentStatus(models.TextChoices):
        DIRECT_SUPPORT = "DIRECT_SUPPORT", "Direct support"
        PARTIAL_SUPPORT = "PARTIAL_SUPPORT", "Partial support"
        POSSIBLE_EQUIVALENT = "POSSIBLE_EQUIVALENT", "Possible equivalent"
        NOT_FOUND = "NOT_FOUND", "Not found"
        CONTRADICTORY = "CONTRADICTORY", "Contradictory"
        UNCERTAIN = "UNCERTAIN", "Uncertain"

    class ReviewStatus(models.TextChoices):
        UNREVIEWED = "UNREVIEWED", "Unreviewed"
        ACCEPTED = "ACCEPTED", "Accepted"
        CORRECTED = "CORRECTED", "Corrected"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    analysis = models.ForeignKey(
        AnalysisReport,
        on_delete=models.CASCADE,
        related_name="assessments",
    )
    claim = models.ForeignKey(
        "patents.Claim",
        on_delete=models.PROTECT,
        related_name="assessments",
    )
    limitation_index = models.PositiveIntegerField()
    limitation_text = models.TextField()
    assessment_status = models.CharField(
        max_length=24,
        choices=AssessmentStatus.choices,
    )
    explanation = models.TextField(blank=True)
    confidence = models.FloatField(null=True, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    review_status = models.CharField(
        max_length=12,
        choices=ReviewStatus.choices,
        default=ReviewStatus.UNREVIEWED,
        db_index=True,
    )
    reviewer_notes = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="claim_assessments_reviewed",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("analysis", "claim", "limitation_index"),
                name="analysis_claim_limitation_uniq",
            ),
            models.CheckConstraint(
                check=(
                    Q(confidence__isnull=True)
                    | (Q(confidence__gte=0.0) & Q(confidence__lte=1.0))
                ),
                name="assess_confidence_range_ck",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "analysis", "limitation_index"),
                name="assessment_tenant_analysis_idx",
            ),
        ]
        ordering = ("claim_id", "limitation_index")

    def __str__(self) -> str:
        return (
            f"Assessment {self.pk}: limitation {self.limitation_index} "
            f"({self.assessment_status})"
        )


class EvidenceReference(TenantScopedModel):
    """A source quotation retained with its patent/version provenance."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    assessment = models.ForeignKey(
        ClaimAssessment,
        on_delete=models.CASCADE,
        related_name="evidence_references",
    )
    source_patent = models.ForeignKey(
        "patents.Patent",
        on_delete=models.SET_NULL,
        related_name="evidence_references",
        null=True,
        blank=True,
    )
    source_document_version = models.ForeignKey(
        "patents.PatentDocumentVersion",
        on_delete=models.SET_NULL,
        related_name="evidence_references",
        null=True,
        blank=True,
    )
    source_section = models.CharField(max_length=100, blank=True, null=True)
    source_page_index = models.PositiveIntegerField(null=True, blank=True)
    source_start_offset = models.IntegerField(null=True, blank=True)
    source_end_offset = models.IntegerField(null=True, blank=True)
    exact_quote = models.TextField()
    quote_verified = models.BooleanField(default=False)
    bbox = models.JSONField(null=True, blank=True)
    retrieval_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("tenant", "assessment"),
                name="evidence_tenant_assessment_idx",
            ),
        ]
        ordering = ("source_page_index", "created_at")

    def __str__(self) -> str:
        return f"Evidence reference {self.pk}"
