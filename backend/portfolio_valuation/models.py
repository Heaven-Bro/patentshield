"""Evidence-pinned portfolio diligence and explicitly assumed valuation scenarios."""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from tenants.models import TenantScopedModel


class PortfolioValuationStudy(TenantScopedModel):
    """Tenant-owned diligence workspace for a selected public patent portfolio."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        READY = "READY", "Ready"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=240)
    description = models.TextField(blank=True, default="")
    currency = models.CharField(max_length=3, default="USD")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT, db_index=True)
    source_landscape_study = models.ForeignKey(
        "landscapes.LandscapeStudy", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="valuation_studies",
    )
    methodology_version = models.CharField(max_length=60, default="diligence-composition-v1")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="portfolio_valuation_studies_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("tenant", "status", "-updated_at"), name="val_study_status_idx")]
        ordering = ("-updated_at",)

    def clean(self) -> None:
        if self.source_landscape_study_id and self.tenant_id and self.source_landscape_study.tenant_id != self.tenant_id:
            raise ValidationError({"source_landscape_study": "Source landscape study must belong to the same tenant."})
        if self.currency and (len(self.currency) != 3 or not self.currency.isalpha() or self.currency.upper() != self.currency):
            raise ValidationError({"currency": "Use an uppercase three-letter currency code."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values("tenant_id").first()
            if previous and previous["tenant_id"] != self.tenant_id:
                raise ValidationError("A portfolio valuation study cannot be reassigned to another tenant.")
        super().save(*args, **kwargs)


class PortfolioAssetSnapshot(TenantScopedModel):
    """Immutable bibliographic/legal snapshot; it is not a legal-status guarantee."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(PortfolioValuationStudy, on_delete=models.CASCADE, related_name="assets")
    publication = models.ForeignKey("patents.PatentPublication", on_delete=models.PROTECT, related_name="portfolio_valuation_snapshots")
    source_snapshot = models.ForeignKey("patents.PatentSourceSnapshot", on_delete=models.PROTECT, null=True, blank=True, related_name="portfolio_valuation_snapshots")
    publication_number = models.CharField(max_length=100, blank=True, default="")
    application_number = models.CharField(max_length=100, blank=True, default="")
    jurisdiction = models.CharField(max_length=10, blank=True, default="")
    title = models.TextField()
    abstract = models.TextField(blank=True, default="")
    filing_date = models.DateField(null=True, blank=True)
    publication_date = models.DateField(null=True, blank=True)
    grant_date = models.DateField(null=True, blank=True)
    legal_status = models.CharField(max_length=20, default="UNKNOWN")
    applicant_names = models.JSONField(default=list, blank=True)
    ipc_codes = models.JSONField(default=list, blank=True)
    cpc_codes = models.JSONField(default=list, blank=True)
    family_system = models.CharField(max_length=20, blank=True, default="")
    family_identifier = models.CharField(max_length=200, blank=True, default="")
    source_url = models.URLField(max_length=2000, blank=True, default="")
    source_payload_hash = models.CharField(max_length=64, blank=True, default="")
    snapshot_sha256 = models.CharField(max_length=64)
    source_last_synced_at = models.DateTimeField(null=True, blank=True)
    forward_citation_count = models.PositiveIntegerField(null=True, blank=True)
    backward_citation_count = models.PositiveIntegerField(null=True, blank=True)
    citation_provenance = models.JSONField(default=dict, blank=True)
    legal_event_count = models.PositiveIntegerField(default=0)
    latest_legal_event = models.JSONField(default=dict, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "study", "publication"), name="val_asset_pub_uniq")]
        indexes = [
            models.Index(fields=("tenant", "study", "legal_status"), name="val_asset_status_idx"),
            models.Index(fields=("tenant", "study", "jurisdiction"), name="val_asset_juris_idx"),
        ]
        ordering = ("publication_number", "pk")

    def clean(self) -> None:
        if self.study_id and self.study.tenant_id != self.tenant_id:
            raise ValidationError({"study": "Asset snapshot and study must belong to the same tenant."})
        if self.source_snapshot_id and self.source_snapshot.publication_id != self.publication_id:
            raise ValidationError({"source_snapshot": "Source snapshot must refer to the selected publication."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        if not self._state.adding:
            raise ValidationError("Portfolio asset source snapshots are immutable; add a new study or asset snapshot for refreshed evidence.")
        super().save(*args, **kwargs)


class PortfolioDiligenceSnapshot(TenantScopedModel):
    """Immutable, reproducible composition and evidence-coverage indicators."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(PortfolioValuationStudy, on_delete=models.CASCADE, related_name="diligence_snapshots")
    asset_manifest_sha256 = models.CharField(max_length=64)
    methodology_version = models.CharField(max_length=60, default="diligence-composition-v1")
    asset_count = models.PositiveIntegerField(default=0)
    composition_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="portfolio_diligence_snapshots_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=("tenant", "study", "-created_at"), name="val_diligence_study_idx")]
        ordering = ("-created_at",)

    def clean(self) -> None:
        if self.study_id and self.study.tenant_id != self.tenant_id:
            raise ValidationError({"study": "Diligence snapshot and study must belong to the same tenant."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        if not self._state.adding:
            raise ValidationError("Diligence snapshots are immutable.")
        super().save(*args, **kwargs)


class PortfolioScenario(TenantScopedModel):
    """Named valuation approach; each edit creates an immutable revision."""

    class Method(models.TextChoices):
        INCOME = "INCOME", "Income — relief from royalty"
        MARKET = "MARKET", "Market — comparable transactions"
        COST = "COST", "Cost — replacement cost less obsolescence"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(PortfolioValuationStudy, on_delete=models.CASCADE, related_name="scenarios")
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True, default="")
    method = models.CharField(max_length=10, choices=Method.choices)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="portfolio_scenarios_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "study", "name"), name="val_scenario_name_uniq")]
        indexes = [models.Index(fields=("tenant", "study", "method"), name="val_scenario_method_idx")]
        ordering = ("name",)

    def clean(self) -> None:
        if self.study_id and self.study.tenant_id != self.tenant_id:
            raise ValidationError({"study": "Scenario and study must belong to the same tenant."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values("tenant_id", "study_id", "method").first()
            if previous and (previous["tenant_id"] != self.tenant_id or previous["study_id"] != self.study_id or previous["method"] != self.method):
                raise ValidationError("A valuation scenario's tenant, study, and method are immutable.")
        super().save(*args, **kwargs)


class PortfolioScenarioRevision(TenantScopedModel):
    """Immutable assumptions and result snapshot; new inputs require a new revision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    scenario = models.ForeignKey(PortfolioScenario, on_delete=models.CASCADE, related_name="revisions")
    revision_number = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="USD")
    assumptions = models.JSONField(default=dict)
    calculated_result = models.JSONField(default=dict)
    input_sha256 = models.CharField(max_length=64)
    asset_manifest_sha256 = models.CharField(max_length=64)
    methodology_version = models.CharField(max_length=60, default="valuation-methods-v1")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="portfolio_scenario_revisions_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "scenario", "revision_number"), name="val_scenario_rev_uniq")]
        indexes = [models.Index(fields=("tenant", "scenario", "-revision_number"), name="val_scenario_revisions_idx")]
        ordering = ("-revision_number",)

    def clean(self) -> None:
        if self.scenario_id and self.scenario.tenant_id != self.tenant_id:
            raise ValidationError({"scenario": "Scenario revision and scenario must belong to the same tenant."})
        if self.scenario_id and self.currency != self.scenario.study.currency:
            raise ValidationError({"currency": "Scenario revision currency must match the parent study currency."})
        if self.currency and (len(self.currency) != 3 or not self.currency.isalpha() or self.currency.upper() != self.currency):
            raise ValidationError({"currency": "Use an uppercase three-letter currency code."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        if not self._state.adding:
            raise ValidationError("Scenario revisions are immutable; create a new revision to change assumptions.")
        super().save(*args, **kwargs)


class PortfolioAnnotation(TenantScopedModel):
    """Diligence note tied to a study, asset, or valuation scenario."""

    class Kind(models.TextChoices):
        DILIGENCE = "DILIGENCE", "Diligence"
        LEGAL_STATUS = "LEGAL_STATUS", "Legal status"
        CITATION = "CITATION", "Citation evidence"
        FINANCIAL_ASSUMPTION = "FINANCIAL_ASSUMPTION", "Financial assumption"
        REVIEW_QUESTION = "REVIEW_QUESTION", "Review question"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(PortfolioValuationStudy, on_delete=models.CASCADE, related_name="annotations")
    asset = models.ForeignKey(PortfolioAssetSnapshot, on_delete=models.CASCADE, null=True, blank=True, related_name="annotations")
    scenario = models.ForeignKey(PortfolioScenario, on_delete=models.CASCADE, null=True, blank=True, related_name="annotations")
    kind = models.CharField(max_length=24, choices=Kind.choices, default=Kind.DILIGENCE)
    body = models.TextField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="portfolio_annotations_created")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("tenant", "study", "-created_at"), name="val_annotation_study_idx")]
        ordering = ("-created_at",)

    def clean(self) -> None:
        if self.study_id and self.study.tenant_id != self.tenant_id:
            raise ValidationError({"study": "Annotation and study must belong to the same tenant."})
        if self.asset_id and (self.asset.tenant_id != self.tenant_id or self.asset.study_id != self.study_id):
            raise ValidationError({"asset": "Annotation asset must belong to the same study and tenant."})
        if self.scenario_id and (self.scenario.tenant_id != self.tenant_id or self.scenario.study_id != self.study_id):
            raise ValidationError({"scenario": "Annotation scenario must belong to the same study and tenant."})
        if self.asset_id and self.scenario_id:
            raise ValidationError("An annotation can target an asset or a scenario, not both.")
        if not self.body.strip():
            raise ValidationError({"body": "Annotation text cannot be empty."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values("tenant_id", "study_id", "asset_id", "scenario_id").first()
            if previous and any(previous[field] != getattr(self, field) for field in ("tenant_id", "study_id", "asset_id", "scenario_id")):
                raise ValidationError("An annotation cannot be reassigned to a different tenant, study, asset, or scenario.")
        super().save(*args, **kwargs)
