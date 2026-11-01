"""Tenant-scoped product specifications and attorney-reviewed FTO triage."""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from tenants.models import TenantScopedModel


class ProductMatter(TenantScopedModel):
    """A product or product family under attorney-led FTO review."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    product_code = models.CharField(max_length=100, blank=True, default="")
    description = models.TextField(blank=True, default="")
    jurisdictions = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fto_product_matters_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)
        indexes = [models.Index(fields=("tenant", "status", "-updated_at"), name="fto_matter_tenant_status_idx")]
        constraints = [models.UniqueConstraint(fields=("tenant", "product_code"), condition=~Q(product_code=""), name="fto_product_code_uniq")]

    def __str__(self) -> str:
        return f"{self.name} ({self.product_code or self.pk})"


class ProductVersion(TenantScopedModel):
    """An immutable uploaded product-specification revision and extracted text."""

    class ExtractionStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        EXTRACTED = "EXTRACTED", "Extracted"
        OCR_REQUIRED = "OCR_REQUIRED", "OCR required"
        EXTRACTION_FAILED = "EXTRACTION_FAILED", "Extraction failed"
        FAILED = "FAILED", "Failed (legacy)"

    class DecompositionStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(ProductMatter, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    version_label = models.CharField(max_length=100, blank=True, default="")
    source_filename = models.CharField(max_length=255)
    storage_key = models.TextField()
    content_sha256 = models.CharField(max_length=64, db_index=True)
    mime_type = models.CharField(max_length=100, default="application/pdf")
    page_count = models.PositiveIntegerField(null=True, blank=True)
    extracted_text = models.TextField(blank=True, default="")
    extracted_pages = models.JSONField(default=list, blank=True)
    extraction_status = models.CharField(max_length=20, choices=ExtractionStatus.choices, default=ExtractionStatus.PENDING, db_index=True)
    decomposition_status = models.CharField(max_length=12, choices=DecompositionStatus.choices, default=DecompositionStatus.PENDING, db_index=True)
    error_code = models.CharField(max_length=80, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fto_product_versions_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-version_number",)
        constraints = [models.UniqueConstraint(fields=("product", "version_number"), name="fto_product_version_uniq")]
        indexes = [models.Index(fields=("tenant", "product", "-version_number"), name="fto_version_tenant_product_idx")]

    def clean(self):
        if self.product_id and self.tenant_id and self.product.tenant_id != self.tenant_id:
            raise ValidationError({"product": "Product and version must belong to the same tenant."})

    def __str__(self) -> str:
        return f"{self.product.name} v{self.version_number}"


class ProductFeature(TenantScopedModel):
    """A candidate or confirmed product feature with source-document provenance."""

    class ReviewStatus(models.TextChoices):
        CANDIDATE = "CANDIDATE", "Candidate — needs review"
        CONFIRMED = "CONFIRMED", "Attorney confirmed"
        REJECTED = "REJECTED", "Rejected as a feature"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product_version = models.ForeignKey(ProductVersion, on_delete=models.PROTECT, related_name="features")
    feature_key = models.CharField(max_length=64)
    title = models.CharField(max_length=240)
    description = models.TextField(blank=True, default="")
    source_quote = models.TextField()
    source_page_index = models.PositiveIntegerField(null=True, blank=True, help_text="Zero-based PDF page index")
    source_start_offset = models.PositiveIntegerField(null=True, blank=True, help_text="Character offset within the source page")
    source_end_offset = models.PositiveIntegerField(null=True, blank=True, help_text="Exclusive character offset within the source page")
    extraction_method = models.CharField(max_length=40, default="RULE_BASED_CANDIDATE")
    extraction_confidence = models.FloatField(null=True, blank=True)
    review_status = models.CharField(max_length=12, choices=ReviewStatus.choices, default=ReviewStatus.CANDIDATE, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fto_features_created")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="fto_features_reviewed", null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("source_page_index", "source_start_offset", "created_at")
        constraints = [
            models.UniqueConstraint(fields=("product_version", "feature_key"), name="fto_feature_version_key_uniq"),
            models.CheckConstraint(condition=Q(extraction_confidence__isnull=True) | (Q(extraction_confidence__gte=0.0) & Q(extraction_confidence__lte=1.0)), name="fto_feat_conf_rng_ck"),
        ]
        indexes = [models.Index(fields=("tenant", "product_version", "review_status"), name="fto_feature_tenant_status_idx")]

    def clean(self):
        if self.product_version_id and self.tenant_id and self.product_version.tenant_id != self.tenant_id:
            raise ValidationError({"product_version": "Feature and product version must belong to the same tenant."})

    def __str__(self) -> str:
        return self.title


class FTOStudy(TenantScopedModel):
    """A version-pinned product-to-patent assessment workspace."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        READY_FOR_REVIEW = "READY_FOR_REVIEW", "Ready for review"
        REVIEWED = "REVIEWED", "Reviewed"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=240)
    product_version = models.ForeignKey(ProductVersion, on_delete=models.PROTECT, related_name="fto_studies")
    patent = models.ForeignKey("patents.Patent", on_delete=models.PROTECT, related_name="fto_studies")
    patent_document_version = models.ForeignKey("patents.PatentDocumentVersion", on_delete=models.PROTECT, related_name="fto_studies")
    jurisdiction = models.CharField(max_length=10, blank=True, default="")
    selected_claim_ids = models.JSONField(default=list, blank=True, help_text="Empty means all claims available at creation")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fto_studies_created")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="fto_studies_reviewed", null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at",)
        indexes = [models.Index(fields=("tenant", "status", "-updated_at"), name="fto_study_tenant_status_idx")]

    def clean(self):
        if self.product_version_id and self.tenant_id and self.product_version.tenant_id != self.tenant_id:
            raise ValidationError({"product_version": "Study and product version must belong to the same tenant."})
        if self.patent_id and self.tenant_id and self.patent.tenant_id != self.tenant_id:
            raise ValidationError({"patent": "Study and patent must belong to the same tenant."})
        if self.patent_document_version_id and self.patent_id:
            if self.patent_document_version.tenant_id != self.tenant_id or self.patent_document_version.patent_id != self.patent_id:
                raise ValidationError({"patent_document_version": "Selected patent version must belong to the study patent and tenant."})

    def __str__(self) -> str:
        return self.title


class StudyClaimLimitation(TenantScopedModel):
    """One heuristic, source-claim-derived limitation in a specific FTO study."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(FTOStudy, on_delete=models.PROTECT, related_name="limitations")
    claim = models.ForeignKey("patents.Claim", on_delete=models.PROTECT, related_name="fto_study_limitations")
    limitation_index = models.PositiveIntegerField()
    limitation_text = models.TextField()
    source_start_offset = models.PositiveIntegerField()
    source_end_offset = models.PositiveIntegerField()
    segmentation_method = models.CharField(max_length=40, default="CLAUSE_HEURISTIC")
    needs_attorney_review = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("claim__claim_number", "limitation_index")
        constraints = [models.UniqueConstraint(fields=("study", "claim", "limitation_index"), name="fto_study_claim_limit_uniq")]
        indexes = [models.Index(fields=("tenant", "study", "claim"), name="fto_limit_tenant_study_idx")]

    def clean(self):
        if self.study_id and self.claim_id:
            if self.study.tenant_id != self.tenant_id or self.claim.tenant_id != self.tenant_id:
                raise ValidationError("Study limitation and claim must share the current tenant.")
            if self.study.patent_id != self.claim.patent_id or self.study.patent_document_version_id != self.claim.document_version_id:
                raise ValidationError("Limitation claim must belong to the selected patent document version.")

    def __str__(self) -> str:
        return f"Claim {self.claim.claim_number}, limitation {self.limitation_index}"


class LimitationAssessment(TenantScopedModel):
    """Attorney-triable state for one claim limitation within an FTO study."""

    class TriageStatus(models.TextChoices):
        UNASSESSED = "UNASSESSED", "Unassessed"
        POTENTIAL_MATCH = "POTENTIAL_MATCH", "Potential correspondence identified"
        PARTIAL_MATCH = "PARTIAL_MATCH", "Partial correspondence identified"
        NO_MATCH_IDENTIFIED = "NO_MATCH_IDENTIFIED", "No feature identified in reviewed scope"
        COUNTER_EVIDENCE_PRESENT = "COUNTER_EVIDENCE_PRESENT", "Counter-evidence present"
        CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE", "Conflicting evidence"
        UNCERTAIN = "UNCERTAIN", "Uncertain — further review needed"

    class ReviewStatus(models.TextChoices):
        UNREVIEWED = "UNREVIEWED", "Unreviewed"
        IN_REVIEW = "IN_REVIEW", "In review"
        REVIEWED = "REVIEWED", "Reviewed by attorney"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(FTOStudy, on_delete=models.PROTECT, related_name="assessments")
    limitation = models.OneToOneField(StudyClaimLimitation, on_delete=models.PROTECT, related_name="assessment")
    triage_status = models.CharField(max_length=24, choices=TriageStatus.choices, default=TriageStatus.UNASSESSED, db_index=True)
    triage_rationale = models.TextField(blank=True, default="")
    review_status = models.CharField(max_length=12, choices=ReviewStatus.choices, default=ReviewStatus.UNREVIEWED, db_index=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="fto_limitation_reviews", null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="fto_limitation_updates", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("limitation__claim__claim_number", "limitation__limitation_index")
        indexes = [models.Index(fields=("tenant", "study", "triage_status"), name="fto_assess_tenant_state_idx")]

    def clean(self):
        if self.study_id and self.limitation_id:
            if self.study_id != self.limitation.study_id or self.tenant_id != self.limitation.tenant_id:
                raise ValidationError("Assessment and limitation must share the same study and tenant.")

    @property
    def heatmap_band(self) -> str:
        """Display priority only; deliberately not an infringement/FTO conclusion."""
        return {
            self.TriageStatus.POTENTIAL_MATCH: "HIGH_ATTENTION",
            self.TriageStatus.PARTIAL_MATCH: "MODERATE_ATTENTION",
            self.TriageStatus.NO_MATCH_IDENTIFIED: "LOWER_ATTENTION",
            self.TriageStatus.COUNTER_EVIDENCE_PRESENT: "MODERATE_ATTENTION",
            self.TriageStatus.CONFLICTING_EVIDENCE: "HIGH_ATTENTION",
            self.TriageStatus.UNCERTAIN: "ELEVATED_ATTENTION",
        }.get(self.triage_status, "UNASSESSED")

    def __str__(self) -> str:
        return f"Assessment for {self.limitation}"


class LimitationEvidence(TenantScopedModel):
    """A pinned product-specification feature used as support, counter-evidence, or context."""

    class Kind(models.TextChoices):
        SUPPORTING = "SUPPORTING", "Potentially supports correspondence"
        COUNTER = "COUNTER", "Potentially counters correspondence"
        CONTEXT = "CONTEXT", "Context only"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(LimitationAssessment, on_delete=models.PROTECT, related_name="evidence")
    feature = models.ForeignKey(ProductFeature, on_delete=models.PROTECT, related_name="limitation_evidence")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    source_quote = models.TextField()
    source_page_index = models.PositiveIntegerField(null=True, blank=True)
    source_start_offset = models.PositiveIntegerField(null=True, blank=True)
    source_end_offset = models.PositiveIntegerField(null=True, blank=True)
    attorney_rationale = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fto_evidence_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [models.UniqueConstraint(fields=("assessment", "feature", "kind"), name="fto_evidence_feat_kind_uniq")]
        indexes = [models.Index(fields=("tenant", "assessment", "kind"), name="fto_evidence_tenant_kind_idx")]

    def clean(self):
        if self.assessment_id and self.feature_id:
            if self.assessment.tenant_id != self.tenant_id or self.feature.tenant_id != self.tenant_id:
                raise ValidationError("Evidence, assessment, and feature must share a tenant.")
            if self.assessment.study.product_version_id != self.feature.product_version_id:
                raise ValidationError("Evidence feature must belong to this study's product version.")


class AttorneyAnnotation(TenantScopedModel):
    """Append-only attorney notes attached to a particular limitation assessment."""

    class Category(models.TextChoices):
        NOTE = "NOTE", "Attorney note"
        COUNTER_EVIDENCE = "COUNTER_EVIDENCE", "Counter-evidence note"
        QUESTION = "QUESTION", "Question for review"
        DECISION = "DECISION", "Review decision context"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(LimitationAssessment, on_delete=models.PROTECT, related_name="annotations")
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.NOTE)
    body = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fto_annotations_created")
    resolved = models.BooleanField(default=False, db_index=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, related_name="fto_annotations_resolved", null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("resolved", "created_at")
        indexes = [models.Index(fields=("tenant", "assessment", "resolved"), name="fto_annot_tenant_resolved_idx")]

    def clean(self):
        if self.assessment_id and self.assessment.tenant_id != self.tenant_id:
            raise ValidationError({"assessment": "Annotation and assessment must belong to the same tenant."})

    def __str__(self) -> str:
        return f"Annotation for {self.assessment_id}"
