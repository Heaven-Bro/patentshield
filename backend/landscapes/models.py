"""Tenant-scoped, reproducible patent landscape analytics."""
from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from pgvector.django import HnswIndex, VectorField

from tenants.models import TenantScopedModel


class AssigneeEntity(TenantScopedModel):
    """A tenant's reviewed identity for one normalized applicant/assignee."""

    class Status(models.TextChoices):
        UNREVIEWED = "UNREVIEWED", "Unreviewed"
        CONFIRMED = "CONFIRMED", "Confirmed"
        DO_NOT_MERGE = "DO_NOT_MERGE", "Do not merge"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    canonical_name = models.CharField(max_length=400)
    normalized_key = models.CharField(max_length=500)
    country_code = models.CharField(max_length=2, blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNREVIEWED)
    notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="landscape_assignee_entities_created")
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="landscape_assignee_entities_reviewed")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "normalized_key"), name="land_assignee_key_uniq")]
        indexes = [models.Index(fields=("tenant", "status"), name="land_assignee_status_idx")]
        ordering = ("canonical_name",)

    def __str__(self) -> str:
        return self.canonical_name


class AssigneeAlias(TenantScopedModel):
    """Exact normalized source-name alias mapped to a tenant-owned entity."""

    class Source(models.TextChoices):
        AUTO = "AUTO", "Automatic normalization"
        ATTORNEY = "ATTORNEY", "Attorney supplied"
        IMPORT = "IMPORT", "Imported mapping"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    entity = models.ForeignKey(AssigneeEntity, on_delete=models.PROTECT, related_name="aliases")
    original_name = models.CharField(max_length=400)
    normalized_key = models.CharField(max_length=500)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.AUTO)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="landscape_assignee_aliases_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "normalized_key"), name="land_assignee_alias_key_uniq")]
        indexes = [models.Index(fields=("tenant", "entity"), name="land_alias_entity_idx")]
        ordering = ("original_name",)

    def clean(self) -> None:
        if self.entity_id and self.tenant_id and self.entity.tenant_id != self.tenant_id:
            raise ValidationError({"entity": "Assignee alias and entity must belong to the same tenant."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)


class TechnologyTaxonomy(TenantScopedModel):
    """Tenant-specific taxonomy node, based on a source classification or a custom label."""

    class Source(models.TextChoices):
        CPC = "CPC", "Cooperative Patent Classification"
        IPC = "IPC", "International Patent Classification"
        CUSTOM = "CUSTOM", "Custom taxonomy"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=8, choices=Source.choices)
    code = models.CharField(max_length=100)
    label = models.CharField(max_length=300)
    description = models.TextField(blank=True, default="")
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children")
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "source", "code"), name="land_tax_source_code_uniq")]
        indexes = [models.Index(fields=("tenant", "source", "code"), name="land_tax_tenant_code_idx")]
        ordering = ("source", "code")

    def clean(self) -> None:
        if self.parent_id and self.tenant_id and self.parent.tenant_id != self.tenant_id:
            raise ValidationError({"parent": "Taxonomy parent must belong to the same tenant."})
        if self.parent_id and self.parent_id == self.pk:
            raise ValidationError({"parent": "A taxonomy node cannot be its own parent."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)


class LandscapeStudy(TenantScopedModel):
    """Analysis over explicitly selected, version-pinned public publications."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        BUILDING = "BUILDING", "Building"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"
        ARCHIVED = "ARCHIVED", "Archived"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title = models.CharField(max_length=240)
    description = models.TextField(blank=True, default="")
    scope_filters = models.JSONField(default=dict, blank=True)
    analysis_parameters = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    algorithm_version = models.CharField(max_length=60, default="bge-m3-kmeans-rp-v1")
    error_code = models.CharField(max_length=80, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="landscape_studies_created")
    built_by_task_id = models.CharField(max_length=100, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("tenant", "status", "-updated_at"), name="land_study_status_idx")]
        ordering = ("-updated_at",)

    def __str__(self) -> str:
        return self.title


class LandscapeCluster(TenantScopedModel):
    """Deterministic semantic cluster, with a source-derived descriptive label."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(LandscapeStudy, on_delete=models.CASCADE, related_name="clusters")
    cluster_number = models.PositiveSmallIntegerField()
    label = models.CharField(max_length=300)
    explanation = models.TextField(blank=True, default="")
    top_terms = models.JSONField(default=list, blank=True)
    top_classification_codes = models.JSONField(default=list, blank=True)
    publication_count = models.PositiveIntegerField(default=0)
    algorithm = models.CharField(max_length=80, default="deterministic-kmeans")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "study", "cluster_number"), name="land_cluster_num_uniq")]
        indexes = [models.Index(fields=("tenant", "study"), name="land_cluster_study_idx")]
        ordering = ("cluster_number",)

    def clean(self) -> None:
        if self.study_id and self.tenant_id and self.study.tenant_id != self.tenant_id:
            raise ValidationError({"study": "Cluster and study must belong to the same tenant."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)


class LandscapeStudyPublication(TenantScopedModel):
    """Snapshot row retaining canonical bibliographic fields for reproducibility."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(LandscapeStudy, on_delete=models.CASCADE, related_name="publications")
    publication = models.ForeignKey("patents.PatentPublication", on_delete=models.PROTECT, related_name="landscape_snapshots")
    source_snapshot = models.ForeignKey("patents.PatentSourceSnapshot", on_delete=models.SET_NULL, null=True, blank=True, related_name="landscape_publication_snapshots")
    publication_number = models.CharField(max_length=100, blank=True, default="")
    application_number = models.CharField(max_length=100, blank=True, default="")
    jurisdiction = models.CharField(max_length=10, blank=True, default="")
    title = models.TextField()
    abstract = models.TextField(blank=True, default="")
    filing_date = models.DateField(null=True, blank=True)
    publication_date = models.DateField(null=True, blank=True)
    grant_date = models.DateField(null=True, blank=True)
    legal_status = models.CharField(max_length=20, blank=True, default="UNKNOWN")
    applicant_names = models.JSONField(default=list, blank=True)
    normalized_assignee_names = models.JSONField(default=list, blank=True)
    ipc_codes = models.JSONField(default=list, blank=True)
    cpc_codes = models.JSONField(default=list, blank=True)
    family_system = models.CharField(max_length=20, blank=True, default="")
    family_identifier = models.CharField(max_length=200, blank=True, default="")
    source_url = models.URLField(max_length=2000, blank=True, default="")
    source_payload_hash = models.CharField(max_length=64, blank=True, default="")
    snapshot_sha256 = models.CharField(max_length=64)
    cluster = models.ForeignKey(LandscapeCluster, on_delete=models.SET_NULL, null=True, blank=True, related_name="publications")
    projection_x = models.FloatField(null=True, blank=True)
    projection_y = models.FloatField(null=True, blank=True)
    projection_method = models.CharField(max_length=40, blank=True, default="")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "study", "publication"), name="land_study_pub_uniq")]
        indexes = [
            models.Index(fields=("tenant", "study", "filing_date"), name="land_pub_timeline_idx"),
            models.Index(fields=("tenant", "study", "jurisdiction"), name="land_pub_geo_idx"),
        ]
        ordering = ("filing_date", "publication_number", "pk")

    def clean(self) -> None:
        if self.study_id and self.tenant_id and self.study.tenant_id != self.tenant_id:
            raise ValidationError({"study": "Publication snapshot and study must belong to the same tenant."})
        if self.source_snapshot_id and self.source_snapshot.publication_id != self.publication_id:
            raise ValidationError({"source_snapshot": "Source snapshot must belong to the selected publication."})
        if self.cluster_id and (self.cluster.tenant_id != self.tenant_id or self.cluster.study_id != self.study_id):
            raise ValidationError({"cluster": "Cluster must belong to the same tenant and study."})

    def save(self, *args, **kwargs) -> None:
        """Enforce tenant consistency and prevent source-snapshot mutation."""
        self.clean()
        if not self._state.adding:
            immutable_fields = (
                "tenant_id", "study_id", "publication_id", "source_snapshot_id",
                "publication_number", "application_number", "jurisdiction", "title",
                "abstract", "filing_date", "publication_date", "grant_date",
                "legal_status", "applicant_names", "ipc_codes", "cpc_codes",
                "family_system", "family_identifier", "source_url",
                "source_payload_hash", "snapshot_sha256",
            )
            previous = type(self).objects.filter(pk=self.pk).values(*immutable_fields).first()
            if previous is not None:
                for field_name in immutable_fields:
                    if previous[field_name] != getattr(self, field_name):
                        raise ValidationError(
                            f"Landscape publication source snapshot field '{field_name}' is immutable."
                        )
        super().save(*args, **kwargs)


class LandscapePublicationEmbedding(TenantScopedModel):
    """BGE-M3 vector and input checksum for a pinned publication snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study_publication = models.OneToOneField(LandscapeStudyPublication, on_delete=models.CASCADE, related_name="vector_record")
    text_sha256 = models.CharField(max_length=64)
    embedding_model = models.CharField(max_length=100, default="BAAI/bge-m3")
    embedding = VectorField(dimensions=1024)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [HnswIndex(name="land_pub_emb_hnsw", fields=("embedding",), m=16, ef_construction=64, opclasses=("vector_cosine_ops",))]

    def clean(self) -> None:
        if self.study_publication_id and self.tenant_id and self.study_publication.tenant_id != self.tenant_id:
            raise ValidationError({"study_publication": "Embedding and publication snapshot must belong to the same tenant."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)


class LandscapePublicationAssignee(TenantScopedModel):
    """Resolved tenant-owned assignee entity for one study publication."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study_publication = models.ForeignKey(LandscapeStudyPublication, on_delete=models.CASCADE, related_name="assignee_links")
    entity = models.ForeignKey(AssigneeEntity, on_delete=models.PROTECT, related_name="publication_links")
    raw_name = models.CharField(max_length=400)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "study_publication", "entity"), name="land_pub_assignee_uniq")]
        indexes = [models.Index(fields=("tenant", "entity"), name="land_pub_assignee_entity_idx")]

    def clean(self) -> None:
        if self.study_publication_id and self.entity_id and (self.study_publication.tenant_id != self.tenant_id or self.entity.tenant_id != self.tenant_id):
            raise ValidationError("Publication and assignee entity must belong to the same tenant.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)


class LandscapePublicationTechnology(TenantScopedModel):
    """Classification taxonomy assignments retained for each publication snapshot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study_publication = models.ForeignKey(LandscapeStudyPublication, on_delete=models.CASCADE, related_name="technology_links")
    taxonomy = models.ForeignKey(TechnologyTaxonomy, on_delete=models.PROTECT, related_name="publication_links")
    source_code = models.CharField(max_length=100)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "study_publication", "taxonomy"), name="land_pub_taxonomy_uniq")]
        indexes = [models.Index(fields=("tenant", "taxonomy"), name="land_pub_taxonomy_node_idx")]

    def clean(self) -> None:
        if self.study_publication_id and self.taxonomy_id and (self.study_publication.tenant_id != self.tenant_id or self.taxonomy.tenant_id != self.tenant_id):
            raise ValidationError("Publication and taxonomy node must belong to the same tenant.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)


class LandscapeAnnotation(TenantScopedModel):
    """Persistent attorney/analyst annotation attached to a study or publication."""

    class Kind(models.TextChoices):
        OVERVIEW = "OVERVIEW", "Study overview"
        ASSIGNEE = "ASSIGNEE", "Assignee normalization"
        TAXONOMY = "TAXONOMY", "Technology taxonomy"
        CLUSTER = "CLUSTER", "Cluster interpretation"
        DATA_QUALITY = "DATA_QUALITY", "Data quality or coverage limitation"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        RESOLVED = "RESOLVED", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    study = models.ForeignKey(LandscapeStudy, on_delete=models.CASCADE, related_name="annotations")
    study_publication = models.ForeignKey(LandscapeStudyPublication, on_delete=models.SET_NULL, null=True, blank=True, related_name="annotations")
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OVERVIEW)
    body = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="landscape_annotations_created")
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="landscape_annotations_resolved")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("tenant", "study", "status"), name="land_annotation_status_idx")]
        ordering = ("-updated_at",)

    def clean(self) -> None:
        if self.study_id and self.study.tenant_id != self.tenant_id:
            raise ValidationError({"study": "Annotation and study must belong to the same tenant."})
        if self.study_publication_id and (self.study_publication.tenant_id != self.tenant_id or self.study_publication.study_id != self.study_id):
            raise ValidationError({"study_publication": "Annotation target must be a publication in the same tenant study."})

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)
