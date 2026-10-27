"""Tenant-scoped, versioned attorney claim charts and export jobs."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from tenants.models import TenantScopedModel


class ClaimChart(TenantScopedModel):
    """A reviewable claim chart derived from one completed analysis."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        IN_REVIEW = "IN_REVIEW", "In review"
        APPROVED = "APPROVED", "Approved"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis = models.ForeignKey(
        "analyses.AnalysisReport",
        on_delete=models.PROTECT,
        related_name="claim_charts",
    )
    title = models.CharField(max_length=240)
    description = models.TextField(blank=True)
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="claim_charts_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="claim_charts_updated",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)
        indexes = [
            models.Index(
                fields=("tenant", "status", "-updated_at"),
                name="claimchart_tenant_status_idx",
            ),
            models.Index(
                fields=("tenant", "analysis", "-created_at"),
                name="claimchart_tenant_analysis_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} (revision {self.revision})"


class ClaimChartRow(TenantScopedModel):
    """One analysis limitation mapped to evidence and attorney annotations."""

    class MappingStatus(models.TextChoices):
        DIRECT_SUPPORT = "DIRECT_SUPPORT", "Direct support"
        PARTIAL_SUPPORT = "PARTIAL_SUPPORT", "Partial support"
        POSSIBLE_EQUIVALENT = "POSSIBLE_EQUIVALENT", "Possible equivalent"
        NOT_FOUND = "NOT_FOUND", "Not found"
        CONTRADICTORY = "CONTRADICTORY", "Contradictory"
        UNCERTAIN = "UNCERTAIN", "Uncertain"

    class AttorneyDecision(models.TextChoices):
        UNREVIEWED = "UNREVIEWED", "Unreviewed"
        ACCEPTED = "ACCEPTED", "Accepted"
        CORRECTED = "CORRECTED", "Corrected"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chart = models.ForeignKey(
        ClaimChart,
        on_delete=models.CASCADE,
        related_name="rows",
    )
    assessment = models.ForeignKey(
        "analyses.ClaimAssessment",
        on_delete=models.PROTECT,
        related_name="claim_chart_rows",
    )
    limitation_index = models.PositiveIntegerField()
    limitation_text = models.TextField()
    mapping_status = models.CharField(max_length=24, choices=MappingStatus.choices)
    mapping_summary = models.TextField(blank=True)
    attorney_notes = models.TextField(blank=True)
    attorney_decision = models.CharField(
        max_length=12,
        choices=AttorneyDecision.choices,
        default=AttorneyDecision.UNREVIEWED,
        db_index=True,
    )
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("sort_order", "limitation_index", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("chart", "assessment"),
                name="claimchart_row_assessment_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "chart", "sort_order"),
                name="claimchart_row_order_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.chart_id}: limitation {self.limitation_index}"


class ClaimChartRevision(TenantScopedModel):
    """Immutable, reproducible JSON snapshot of a claim chart revision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chart = models.ForeignKey(
        ClaimChart,
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    revision_number = models.PositiveIntegerField()
    snapshot = models.JSONField()
    snapshot_sha256 = models.CharField(max_length=64)
    change_summary = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="claim_chart_revisions_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-revision_number",)
        constraints = [
            models.UniqueConstraint(
                fields=("chart", "revision_number"),
                name="claimchart_revision_number_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "chart", "-revision_number"),
                name="claimchart_rev_tenant_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.chart_id} revision {self.revision_number}"


class ClaimChartExport(TenantScopedModel):
    """Asynchronous export of an immutable chart revision."""

    class Format(models.TextChoices):
        DOCX = "DOCX", "Editable Word document"
        XLSX = "XLSX", "Editable Excel workbook"
        PDF = "PDF", "PDF"
        JSON = "JSON", "JSON snapshot"

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    chart = models.ForeignKey(
        ClaimChart,
        on_delete=models.CASCADE,
        related_name="exports",
    )
    revision = models.ForeignKey(
        ClaimChartRevision,
        on_delete=models.PROTECT,
        related_name="exports",
    )
    format = models.CharField(max_length=4, choices=Format.choices)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    task_id = models.CharField(max_length=100, unique=True)
    storage_key = models.TextField(blank=True, null=True)
    error_code = models.CharField(max_length=100, blank=True, null=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="claim_chart_exports_requested",
    )
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("tenant", "status", "-created_at"),
                name="chart_export_tenant_idx",
            ),
            models.Index(
                fields=("tenant", "chart", "-created_at"),
                name="chart_export_chart_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.chart_id} {self.format} export ({self.status})"
