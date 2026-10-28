"""API serializers for tenant-scoped landscape resources."""
from __future__ import annotations

from rest_framework import serializers

from landscapes.models import (
    AssigneeAlias,
    AssigneeEntity,
    LandscapeAnnotation,
    LandscapeStudy,
    LandscapeStudyPublication,
    TechnologyTaxonomy,
)
from landscapes.services import normalize_assignee_name


class LandscapeStudyCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LandscapeStudy
        fields = ("title", "description", "scope_filters")
        extra_kwargs = {
            "title": {"max_length": 240},
            "description": {"required": False},
            "scope_filters": {"required": False},
        }

    def validate_scope_filters(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Saved scope metadata must be a JSON object.")
        if len(value) > 30:
            raise serializers.ValidationError("At most 30 scope metadata properties are allowed.")
        return value


class LandscapeStudyUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = LandscapeStudy
        fields = ("title", "description", "scope_filters")
        extra_kwargs = {
            "title": {"required": False, "max_length": 240},
            "description": {"required": False},
            "scope_filters": {"required": False},
        }

    def validate_scope_filters(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Scope filters must be a JSON object.")
        if len(value) > 30:
            raise serializers.ValidationError("At most 30 scope-filter properties are allowed.")
        return value


class PublicationSelectionSerializer(serializers.Serializer):
    publication_ids = serializers.ListField(
        child=serializers.UUIDField(),
        allow_empty=False,
        min_length=1,
        max_length=500,
    )

    def validate_publication_ids(self, values):
        if len(set(values)) != len(values):
            raise serializers.ValidationError("Duplicate publication IDs are not allowed in one request.")
        return values


class AssigneeCreateSerializer(serializers.Serializer):
    canonical_name = serializers.CharField(max_length=400)
    country_code = serializers.CharField(max_length=2, required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_canonical_name(self, value):
        value = value.strip()
        if not normalize_assignee_name(value):
            raise serializers.ValidationError("Enter a non-empty assignee name.")
        return value


class AssigneeAliasCreateSerializer(serializers.Serializer):
    original_name = serializers.CharField(max_length=400)

    def validate_original_name(self, value):
        value = value.strip()
        if not normalize_assignee_name(value):
            raise serializers.ValidationError("Enter a non-empty alias name.")
        return value


class AssigneeEntitySerializer(serializers.ModelSerializer):
    alias_count = serializers.SerializerMethodField()

    class Meta:
        model = AssigneeEntity
        fields = ("id", "canonical_name", "normalized_key", "country_code", "status", "notes", "alias_count", "created_at", "updated_at")
        read_only_fields = fields

    def get_alias_count(self, obj):
        return obj.aliases.filter(tenant_id=obj.tenant_id).count()


class AssigneeAliasSerializer(serializers.ModelSerializer):
    entity_name = serializers.CharField(source="entity.canonical_name", read_only=True)

    class Meta:
        model = AssigneeAlias
        fields = ("id", "entity", "entity_name", "original_name", "normalized_key", "source", "created_at")
        read_only_fields = fields


class TechnologyTaxonomyCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=100)
    label = serializers.CharField(max_length=300)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    parent_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_code(self, value):
        value = "".join(value.upper().split())
        if not value:
            raise serializers.ValidationError("A taxonomy code is required.")
        return value

    def validate_label(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A taxonomy label is required.")
        return value


class TechnologyTaxonomySerializer(serializers.ModelSerializer):
    parent_code = serializers.CharField(source="parent.code", read_only=True, allow_null=True)

    class Meta:
        model = TechnologyTaxonomy
        fields = ("id", "source", "code", "label", "description", "parent", "parent_code", "is_active", "metadata")
        read_only_fields = fields


class LandscapeAnnotationCreateSerializer(serializers.ModelSerializer):
    study_publication_id = serializers.UUIDField(required=False, allow_null=True)

    class Meta:
        model = LandscapeAnnotation
        fields = ("study_publication_id", "kind", "body")

    def validate_body(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Annotation text cannot be empty.")
        if len(value) > 12000:
            raise serializers.ValidationError("Annotation must be 12,000 characters or fewer.")
        return value


class LandscapeAnnotationSerializer(serializers.ModelSerializer):
    study_publication_id = serializers.UUIDField(read_only=True, allow_null=True)
    creator = serializers.CharField(source="created_by.get_username", read_only=True)
    publication_number = serializers.CharField(source="study_publication.publication_number", read_only=True, allow_null=True)

    class Meta:
        model = LandscapeAnnotation
        fields = ("id", "study", "study_publication", "study_publication_id", "publication_number", "kind", "body", "status", "creator", "resolved_by", "resolved_at", "created_at", "updated_at")
        read_only_fields = fields


class LandscapeStudyPublicationSerializer(serializers.ModelSerializer):
    cluster_number = serializers.IntegerField(source="cluster.cluster_number", read_only=True, allow_null=True)
    cluster_label = serializers.CharField(source="cluster.label", read_only=True, allow_null=True)
    assignees = serializers.SerializerMethodField()
    technology_codes = serializers.SerializerMethodField()

    class Meta:
        model = LandscapeStudyPublication
        fields = (
            "id", "publication", "publication_number", "application_number", "jurisdiction",
            "title", "abstract", "filing_date", "publication_date", "grant_date", "legal_status",
            "applicant_names", "normalized_assignee_names", "ipc_codes", "cpc_codes",
            "family_system", "family_identifier", "source_url", "source_payload_hash",
            "snapshot_sha256", "cluster_number", "cluster_label", "projection_x", "projection_y",
            "projection_method", "assignees", "technology_codes", "added_at",
        )
        read_only_fields = fields

    def get_assignees(self, obj):
        return list(obj.assignee_links.filter(tenant_id=obj.tenant_id).select_related("entity").values_list("entity__canonical_name", flat=True))

    def get_technology_codes(self, obj):
        return list(obj.technology_links.filter(tenant_id=obj.tenant_id).select_related("taxonomy").values_list("taxonomy__code", flat=True))
