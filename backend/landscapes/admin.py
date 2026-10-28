"""Admin views for landscape auditability and operator inspection."""
from django.contrib import admin

from landscapes.models import (
    AssigneeAlias,
    AssigneeEntity,
    LandscapeAnnotation,
    LandscapeCluster,
    LandscapePublicationAssignee,
    LandscapePublicationEmbedding,
    LandscapePublicationTechnology,
    LandscapeStudy,
    LandscapeStudyPublication,
    TechnologyTaxonomy,
)


@admin.register(LandscapeStudy)
class LandscapeStudyAdmin(admin.ModelAdmin):
    list_display = ("title", "tenant", "status", "algorithm_version", "created_by", "updated_at")
    list_filter = ("status", "created_at")
    search_fields = ("title", "tenant__name", "created_by__username")
    readonly_fields = ("id", "status", "analysis_parameters", "algorithm_version", "error_code", "built_by_task_id", "started_at", "completed_at", "created_at", "updated_at")


@admin.register(AssigneeEntity)
class AssigneeEntityAdmin(admin.ModelAdmin):
    list_display = ("canonical_name", "tenant", "status", "country_code", "updated_at")
    list_filter = ("status", "country_code")
    search_fields = ("canonical_name", "normalized_key", "tenant__name")
    readonly_fields = ("id", "normalized_key", "created_at", "updated_at")


@admin.register(AssigneeAlias)
class AssigneeAliasAdmin(admin.ModelAdmin):
    list_display = ("original_name", "entity", "tenant", "source", "created_at")
    list_filter = ("source",)
    search_fields = ("original_name", "normalized_key", "entity__canonical_name", "tenant__name")
    readonly_fields = ("id", "normalized_key", "created_at")


@admin.register(TechnologyTaxonomy)
class TechnologyTaxonomyAdmin(admin.ModelAdmin):
    list_display = ("code", "label", "source", "tenant", "parent", "is_active")
    list_filter = ("source", "is_active")
    search_fields = ("code", "label", "tenant__name")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LandscapeStudyPublication)
class LandscapeStudyPublicationAdmin(admin.ModelAdmin):
    list_display = ("publication_number", "study", "tenant", "jurisdiction", "filing_date", "cluster")
    list_filter = ("jurisdiction", "legal_status")
    search_fields = ("publication_number", "title", "study__title", "tenant__name")
    readonly_fields = ("id", "tenant", "study", "publication", "source_snapshot", "publication_number", "application_number", "jurisdiction", "title", "abstract", "filing_date", "publication_date", "grant_date", "legal_status", "applicant_names", "ipc_codes", "cpc_codes", "family_system", "family_identifier", "source_url", "source_payload_hash", "snapshot_sha256", "added_at")


@admin.register(LandscapeCluster)
class LandscapeClusterAdmin(admin.ModelAdmin):
    list_display = ("study", "cluster_number", "label", "publication_count", "algorithm")
    search_fields = ("study__title", "label", "tenant__name")
    readonly_fields = ("id", "top_terms", "top_classification_codes", "created_at")


@admin.register(LandscapePublicationEmbedding)
class LandscapePublicationEmbeddingAdmin(admin.ModelAdmin):
    list_display = ("study_publication", "tenant", "embedding_model", "text_sha256", "updated_at")
    search_fields = ("study_publication__publication_number", "tenant__name", "text_sha256")
    readonly_fields = ("id", "embedding", "text_sha256", "embedding_model", "created_at", "updated_at")


@admin.register(LandscapePublicationAssignee)
class LandscapePublicationAssigneeAdmin(admin.ModelAdmin):
    list_display = ("study_publication", "entity", "tenant", "raw_name")
    search_fields = ("study_publication__publication_number", "entity__canonical_name", "raw_name", "tenant__name")


@admin.register(LandscapePublicationTechnology)
class LandscapePublicationTechnologyAdmin(admin.ModelAdmin):
    list_display = ("study_publication", "taxonomy", "source_code", "tenant")
    search_fields = ("study_publication__publication_number", "taxonomy__code", "taxonomy__label", "tenant__name")


@admin.register(LandscapeAnnotation)
class LandscapeAnnotationAdmin(admin.ModelAdmin):
    list_display = ("study", "kind", "status", "created_by", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("study__title", "body", "tenant__name")
    readonly_fields = ("id", "created_at", "updated_at", "resolved_at")
