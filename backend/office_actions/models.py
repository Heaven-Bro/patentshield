"""Tenant-scoped prosecution records with immutable, evidence-linked drafts."""
from __future__ import annotations

import hashlib
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from tenants.models import TenantScopedModel


class OfficeAction(TenantScopedModel):
    """One examiner communication uploaded against a tenant-owned patent matter."""

    class ExtractionStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        QUEUED = "QUEUED", "Queued"
        PROCESSING = "PROCESSING", "Processing"
        RETRYING = "RETRYING", "Retrying"
        EXTRACTED = "EXTRACTED", "Extracted"
        OCR_REQUIRED = "OCR_REQUIRED", "OCR required"
        EXTRACTION_FAILED = "EXTRACTION_FAILED", "Extraction failed"
        FAILED = "FAILED", "Failed (legacy)"

    class ParseStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        COMPLETED = "COMPLETED", "Completed"
        NEEDS_REVIEW = "NEEDS_REVIEW", "Needs attorney review"
        FAILED = "FAILED", "Failed"

    class ActionType(models.TextChoices):
        NON_FINAL = "NON_FINAL", "Non-final office action"
        FINAL = "FINAL", "Final office action"
        NOTICE_OF_ALLOWANCE = "NOTICE_OF_ALLOWANCE", "Notice of allowance"
        EXAMINER_INTERVIEW = "EXAMINER_INTERVIEW", "Examiner interview summary"
        OTHER = "OTHER", "Other communication"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patent = models.ForeignKey("patents.Patent", on_delete=models.PROTECT, related_name="office_actions")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="office_actions_uploaded")
    jurisdiction = models.CharField(max_length=10, default="US", db_index=True)
    action_type = models.CharField(max_length=24, choices=ActionType.choices, default=ActionType.NON_FINAL)
    application_number = models.CharField(max_length=100, blank=True, default="")
    examiner_name = models.CharField(max_length=200, blank=True, default="")
    issued_date = models.DateField(null=True, blank=True)
    response_due_date = models.DateField(null=True, blank=True)
    source_filename = models.CharField(max_length=255)
    storage_key = models.TextField()
    content_sha256 = models.CharField(max_length=64)
    mime_type = models.CharField(max_length=100, default="application/pdf")
    page_count = models.PositiveIntegerField(null=True, blank=True)
    extracted_text = models.TextField(blank=True, default="")
    extracted_pages = models.JSONField(default=list, blank=True)
    extraction_status = models.CharField(max_length=20, choices=ExtractionStatus.choices, default=ExtractionStatus.PENDING, db_index=True)
    parse_status = models.CharField(max_length=12, choices=ParseStatus.choices, default=ParseStatus.PENDING, db_index=True)
    extraction_error_code = models.CharField(max_length=80, blank=True, default="")
    # Celery task ID acts as a fencing token. A stale worker cannot overwrite
    # state reserved by a newer queue/recovery attempt.
    processing_task_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    processing_reserved_at = models.DateTimeField(null=True, blank=True, db_index=True)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-issued_date", "-created_at")
        indexes = [
            models.Index(fields=("tenant", "patent", "-created_at"), name="oa_tenant_patent_created_idx"),
            models.Index(fields=("tenant", "extraction_status", "processing_reserved_at"), name="oa_tenant_extract_res_idx"),
        ]
        constraints = [models.CheckConstraint(condition=models.Q(response_due_date__isnull=True) | models.Q(issued_date__isnull=True) | models.Q(response_due_date__gte=models.F("issued_date")), name="oa_due_date_not_before_issue")]

    def clean(self):
        if self.patent_id and self.tenant_id and self.patent.tenant_id != self.tenant_id:
            raise ValidationError({"patent": "Patent and office action must belong to the same tenant."})

    def __str__(self):
        return f"Office action {self.application_number or self.patent_id} ({self.jurisdiction})"


class Rejection(TenantScopedModel):
    """Structured rejection/objection extracted from a source page and quote."""

    class Ground(models.TextChoices):
        US_101 = "US_101", "35 U.S.C. §101"
        US_102 = "US_102", "35 U.S.C. §102"
        US_103 = "US_103", "35 U.S.C. §103"
        US_112 = "US_112", "35 U.S.C. §112"
        EP_52 = "EP_52", "EPC Article 52"
        EP_54 = "EP_54", "EPC Article 54"
        EP_56 = "EP_56", "EPC Article 56"
        PCT = "PCT", "PCT objection or written opinion"
        FORMALITY = "FORMALITY", "Formality objection"
        OTHER = "OTHER", "Other / unclassified"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    office_action = models.ForeignKey(OfficeAction, on_delete=models.CASCADE, related_name="rejections")
    ground = models.CharField(max_length=16, choices=Ground.choices, db_index=True)
    claim_numbers = models.JSONField(default=list, blank=True)
    examiner_reasoning = models.TextField(blank=True, default="")
    source_page = models.PositiveIntegerField(null=True, blank=True, help_text="One-based PDF page number")
    source_start_offset = models.PositiveIntegerField(null=True, blank=True)
    source_end_offset = models.PositiveIntegerField(null=True, blank=True)
    source_quote = models.TextField()
    parser_confidence = models.FloatField(default=0.5)
    needs_attorney_review = models.BooleanField(default=True, db_index=True)
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("source_page", "source_start_offset", "created_at")
        constraints = [models.UniqueConstraint(fields=("office_action", "fingerprint"), name="oa_rejection_fingerprint_uniq")]

    def clean(self):
        if self.office_action_id and self.tenant_id and self.office_action.tenant_id != self.tenant_id:
            raise ValidationError({"office_action": "Office action and rejection must belong to the same tenant."})
        if self.source_start_offset is not None and self.source_end_offset is not None and self.source_end_offset < self.source_start_offset:
            raise ValidationError({"source_end_offset": "End offset must be at or after start offset."})


class CitedReference(TenantScopedModel):
    """A patent-style identifier explicitly observed in an office-action source."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    office_action = models.ForeignKey(OfficeAction, on_delete=models.CASCADE, related_name="cited_references")
    identifier = models.CharField(max_length=100)
    title = models.CharField(max_length=500, blank=True, default="")
    assignee_or_author = models.CharField(max_length=300, blank=True, default="")
    source_page = models.PositiveIntegerField(null=True, blank=True)
    source_start_offset = models.PositiveIntegerField(null=True, blank=True)
    source_end_offset = models.PositiveIntegerField(null=True, blank=True)
    source_quote = models.TextField(blank=True, default="")
    fingerprint = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("identifier",)
        constraints = [models.UniqueConstraint(fields=("office_action", "identifier"), name="oa_cited_reference_identifier_uniq")]

    def clean(self):
        if self.office_action_id and self.tenant_id and self.office_action.tenant_id != self.tenant_id:
            raise ValidationError({"office_action": "Reference and office action must belong to the same tenant."})


class RejectionReference(TenantScopedModel):
    """Links a rejection to an identifier cited in its nearby source passage."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rejection = models.ForeignKey(Rejection, on_delete=models.CASCADE, related_name="reference_links")
    cited_reference = models.ForeignKey(CitedReference, on_delete=models.CASCADE, related_name="rejection_links")
    relationship = models.CharField(max_length=40, default="EXAMINER_CITED")
    source_quote = models.TextField(blank=True, default="")
    source_page = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("rejection", "cited_reference"), name="oa_rejection_ref_link_uniq")]

    def clean(self):
        if self.rejection_id and self.cited_reference_id:
            if self.rejection.tenant_id != self.tenant_id or self.cited_reference.tenant_id != self.tenant_id:
                raise ValidationError("Rejection, cited reference, and link must share a tenant.")
            if self.rejection.office_action_id != self.cited_reference.office_action_id:
                raise ValidationError("A rejection can only link to references from the same office action.")


class ResponseDraft(TenantScopedModel):
    """Mutable draft workflow container; each content snapshot is immutable."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        IN_REVIEW = "IN_REVIEW", "In review"
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
        APPROVED = "APPROVED", "Attorney approved"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    office_action = models.ForeignKey(OfficeAction, on_delete=models.PROTECT, related_name="drafts")
    title = models.CharField(max_length=240, default="Office action response draft")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="office_action_drafts_created")
    current_revision_number = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)

    def clean(self):
        if self.office_action_id and self.tenant_id and self.office_action.tenant_id != self.tenant_id:
            raise ValidationError({"office_action": "Draft and office action must belong to the same tenant."})


class ResponseDraftRevision(TenantScopedModel):
    """Immutable revision snapshot; mutable editorial activity is a new row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    draft = models.ForeignKey(ResponseDraft, on_delete=models.PROTECT, related_name="revisions")
    revision_number = models.PositiveIntegerField()
    content_markdown = models.TextField()
    source_manifest = models.JSONField(default=dict, blank=True)
    content_sha256 = models.CharField(max_length=64)
    generation_method = models.CharField(max_length=40, default="evidence_template")
    generated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="office_action_draft_revisions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-revision_number",)
        constraints = [models.UniqueConstraint(fields=("draft", "revision_number"), name="oa_draft_revision_num_uniq")]

    def clean(self):
        if self.draft_id and self.draft.tenant_id != self.tenant_id:
            raise ValidationError({"draft": "Draft revision must belong to the same tenant as the draft."})
        expected = hashlib.sha256(self.content_markdown.encode("utf-8")).hexdigest()
        if self.content_sha256 and self.content_sha256 != expected:
            raise ValidationError({"content_sha256": "Revision content hash does not match content."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Draft revisions are immutable; create a new revision instead.")
        if not self.content_sha256:
            self.content_sha256 = hashlib.sha256(self.content_markdown.encode("utf-8")).hexdigest()
        self.full_clean(exclude=None)
        return super().save(*args, **kwargs)


class OfficeActionReview(TenantScopedModel):
    """Append-only reviewer decision and comments pinned to a draft revision."""

    class Decision(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
        REJECTED = "REJECTED", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    revision = models.ForeignKey(ResponseDraftRevision, on_delete=models.PROTECT, related_name="reviews")
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="office_action_reviews")
    decision = models.CharField(max_length=20, choices=Decision.choices)
    comments = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def clean(self):
        if self.revision_id and self.revision.tenant_id != self.tenant_id:
            raise ValidationError({"revision": "Review and revision must belong to the same tenant."})

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Review records are append-only; create a new decision instead.")
        self.full_clean()
        return super().save(*args, **kwargs)


class AmendmentSupportCheck(TenantScopedModel):
    """Literal specification-text support lookup, not a legal sufficiency ruling."""

    class Status(models.TextChoices):
        SUPPORTED = "SUPPORTED", "Exact text located"
        PARTIAL = "PARTIAL", "Some text located"
        NOT_FOUND = "NOT_FOUND", "Text not located"
        NOT_CHECKED = "NOT_CHECKED", "Not checked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    office_action = models.ForeignKey(OfficeAction, on_delete=models.CASCADE, related_name="amendment_checks")
    checked_document_version = models.ForeignKey("patents.PatentDocumentVersion", on_delete=models.SET_NULL, null=True, blank=True, related_name="office_action_support_checks")
    claim_number = models.CharField(max_length=30)
    proposed_claim_text = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NOT_CHECKED)
    source_manifest = models.JSONField(default=dict, blank=True)
    explanation = models.TextField(blank=True, default="")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="amendment_support_checks_requested")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def clean(self):
        if self.office_action_id and self.office_action.tenant_id != self.tenant_id:
            raise ValidationError({"office_action": "Support check and office action must belong to the same tenant."})
        if self.checked_document_version_id and self.checked_document_version.tenant_id != self.tenant_id:
            raise ValidationError({"checked_document_version": "Specification version must belong to the same tenant."})
