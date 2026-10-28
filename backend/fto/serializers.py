"""API serializers for FTO studies, source features, evidence, and annotations."""
from __future__ import annotations

from rest_framework import serializers

from fto.models import (
    AttorneyAnnotation,
    FTOStudy,
    LimitationAssessment,
    LimitationEvidence,
    ProductFeature,
    ProductMatter,
    ProductVersion,
    StudyClaimLimitation,
)


class ProductMatterSerializer(serializers.ModelSerializer):
    created_by_id = serializers.ReadOnlyField()
    version_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = ProductMatter
        fields = (
            "id", "name", "product_code", "description", "jurisdictions",
            "status", "version_count", "created_by_id", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_by_id", "created_at", "updated_at", "version_count")

    def validate_jurisdictions(self, value):
        if not isinstance(value, list) or len(value) > 30:
            raise serializers.ValidationError("jurisdictions must be a list with at most 30 entries.")
        clean = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > 10:
                raise serializers.ValidationError("Each jurisdiction must be a non-empty code of at most 10 characters.")
            clean.append(item.strip().upper())
        return list(dict.fromkeys(clean))


class ProductVersionSerializer(serializers.ModelSerializer):
    product_id = serializers.UUIDField(read_only=True)
    feature_count = serializers.IntegerField(read_only=True, required=False)
    confirmed_feature_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = ProductVersion
        fields = (
            "id", "product_id", "version_number", "version_label", "source_filename",
            "content_sha256", "mime_type", "page_count", "extraction_status",
            "decomposition_status", "error_code", "feature_count",
            "confirmed_feature_count", "created_at",
        )
        read_only_fields = fields


class ProductFeatureSerializer(serializers.ModelSerializer):
    product_version_id = serializers.UUIDField(read_only=True)
    reviewed_by_id = serializers.ReadOnlyField()

    class Meta:
        model = ProductFeature
        fields = (
            "id", "product_version_id", "title", "description", "source_quote",
            "source_page_index", "source_start_offset", "source_end_offset",
            "extraction_method", "extraction_confidence", "review_status",
            "reviewed_by_id", "reviewed_at", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "product_version_id", "source_quote", "source_page_index",
            "source_start_offset", "source_end_offset", "extraction_method",
            "extraction_confidence", "reviewed_by_id", "reviewed_at", "created_at", "updated_at",
        )


class ManualFeatureCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=240)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    source_page_index = serializers.IntegerField(min_value=0)
    source_start_offset = serializers.IntegerField(min_value=0)
    source_end_offset = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        if attrs["source_end_offset"] <= attrs["source_start_offset"]:
            raise serializers.ValidationError("source_end_offset must be greater than source_start_offset.")
        return attrs


class ProductFeatureReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductFeature
        fields = ("title", "description", "review_status")

    def validate(self, attrs):
        if "review_status" in attrs and attrs["review_status"] not in ProductFeature.ReviewStatus.values:
            raise serializers.ValidationError({"review_status": "Unsupported review status."})
        return attrs


class FTOStudyCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=240, required=False, allow_blank=True)
    product_version_id = serializers.UUIDField()
    patent_id = serializers.UUIDField()
    patent_document_version_id = serializers.UUIDField(required=False)
    jurisdiction = serializers.CharField(max_length=10, required=False, allow_blank=True, default="")
    selected_claim_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=True, max_length=100,
    )


class FTOStudySerializer(serializers.ModelSerializer):
    product_version_id = serializers.UUIDField(read_only=True)
    product_id = serializers.UUIDField(source="product_version.product_id", read_only=True)
    patent_id = serializers.UUIDField(read_only=True)
    patent_document_version_id = serializers.UUIDField(read_only=True)
    created_by_id = serializers.ReadOnlyField()
    reviewed_by_id = serializers.ReadOnlyField()
    product_name = serializers.CharField(source="product_version.product.name", read_only=True)
    product_version_number = serializers.IntegerField(source="product_version.version_number", read_only=True)
    patent_title = serializers.CharField(source="patent.title", read_only=True)
    publication_number = serializers.CharField(source="patent.publication_number", read_only=True)
    limitation_count = serializers.IntegerField(read_only=True, required=False)
    reviewed_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = FTOStudy
        fields = (
            "id", "title", "product_id", "product_version_id", "product_name", "product_version_number",
            "patent_id", "patent_title", "publication_number", "patent_document_version_id", "jurisdiction",
            "selected_claim_ids", "status", "limitation_count", "reviewed_count",
            "created_by_id", "reviewed_by_id", "reviewed_at", "created_at", "updated_at",
        )
        read_only_fields = fields


class StudyClaimLimitationSerializer(serializers.ModelSerializer):
    study_id = serializers.UUIDField(read_only=True)
    claim_id = serializers.UUIDField(read_only=True)
    claim_number = serializers.CharField(source="claim.claim_number", read_only=True)
    patent_id = serializers.UUIDField(source="claim.patent_id", read_only=True)

    class Meta:
        model = StudyClaimLimitation
        fields = (
            "id", "study_id", "claim_id", "claim_number", "patent_id",
            "limitation_index", "limitation_text", "source_start_offset",
            "source_end_offset", "segmentation_method", "needs_attorney_review",
        )
        read_only_fields = fields


class LimitationAssessmentSerializer(serializers.ModelSerializer):
    study_id = serializers.UUIDField(read_only=True)
    reviewed_by_id = serializers.ReadOnlyField()
    limitation = StudyClaimLimitationSerializer(read_only=True)
    heatmap_band = serializers.CharField(read_only=True)
    supporting_evidence_count = serializers.SerializerMethodField()
    counter_evidence_count = serializers.SerializerMethodField()
    context_evidence_count = serializers.SerializerMethodField()
    annotation_count = serializers.SerializerMethodField()

    class Meta:
        model = LimitationAssessment
        fields = (
            "id", "study_id", "limitation", "triage_status", "triage_rationale",
            "heatmap_band", "review_status", "reviewed_by_id", "reviewed_at",
            "supporting_evidence_count", "counter_evidence_count", "context_evidence_count",
            "annotation_count", "updated_at",
        )
        read_only_fields = (
            "id", "study_id", "limitation", "heatmap_band", "reviewed_by_id",
            "reviewed_at", "supporting_evidence_count", "counter_evidence_count",
            "context_evidence_count", "annotation_count", "updated_at",
        )

    def get_supporting_evidence_count(self, obj):
        return getattr(obj, "supporting_evidence_count", obj.evidence.filter(tenant_id=obj.tenant_id, kind=LimitationEvidence.Kind.SUPPORTING).count())

    def get_counter_evidence_count(self, obj):
        return getattr(obj, "counter_evidence_count", obj.evidence.filter(tenant_id=obj.tenant_id, kind=LimitationEvidence.Kind.COUNTER).count())

    def get_context_evidence_count(self, obj):
        return getattr(obj, "context_evidence_count", obj.evidence.filter(tenant_id=obj.tenant_id, kind=LimitationEvidence.Kind.CONTEXT).count())

    def get_annotation_count(self, obj):
        return getattr(obj, "annotation_count", obj.annotations.filter(tenant_id=obj.tenant_id).count())

    def validate(self, attrs):
        triage_status = attrs.get("triage_status", getattr(self.instance, "triage_status", LimitationAssessment.TriageStatus.UNASSESSED))
        rationale = attrs.get("triage_rationale", getattr(self.instance, "triage_rationale", ""))
        if triage_status != LimitationAssessment.TriageStatus.UNASSESSED and len(rationale.strip()) < 12:
            raise serializers.ValidationError({"triage_rationale": "Provide a concise evidence-based rationale (at least 12 characters)."})
        if self.instance is not None:
            evidence = self.instance.evidence.filter(tenant_id=self.instance.tenant_id)
            has_support = evidence.filter(kind=LimitationEvidence.Kind.SUPPORTING).exists()
            has_counter = evidence.filter(kind=LimitationEvidence.Kind.COUNTER).exists()
            if triage_status in {LimitationAssessment.TriageStatus.POTENTIAL_MATCH, LimitationAssessment.TriageStatus.PARTIAL_MATCH} and not has_support:
                raise serializers.ValidationError({"triage_status": "Add at least one supporting source feature before marking a potential or partial match."})
            if triage_status == LimitationAssessment.TriageStatus.COUNTER_EVIDENCE_PRESENT and not has_counter:
                raise serializers.ValidationError({"triage_status": "Add counter-evidence before selecting this status."})
            if triage_status == LimitationAssessment.TriageStatus.CONFLICTING_EVIDENCE and not (has_support and has_counter):
                raise serializers.ValidationError({"triage_status": "Conflicting evidence requires both supporting and counter-evidence records."})
        return attrs


class LimitationEvidenceCreateSerializer(serializers.Serializer):
    feature_id = serializers.UUIDField()
    kind = serializers.ChoiceField(choices=LimitationEvidence.Kind.choices)
    attorney_rationale = serializers.CharField(required=False, allow_blank=True, max_length=10000, default="")


class LimitationEvidenceSerializer(serializers.ModelSerializer):
    assessment_id = serializers.UUIDField(read_only=True)
    feature_id = serializers.UUIDField(read_only=True)
    created_by_id = serializers.ReadOnlyField()
    feature_title = serializers.CharField(source="feature.title", read_only=True)
    feature_review_status = serializers.CharField(source="feature.review_status", read_only=True)

    class Meta:
        model = LimitationEvidence
        fields = (
            "id", "assessment_id", "feature_id", "feature_title", "feature_review_status",
            "kind", "source_quote", "source_page_index", "source_start_offset",
            "source_end_offset", "attorney_rationale", "created_by_id", "created_at",
        )
        read_only_fields = fields


class AttorneyAnnotationCreateSerializer(serializers.Serializer):
    assessment_id = serializers.UUIDField()
    category = serializers.ChoiceField(choices=AttorneyAnnotation.Category.choices)
    body = serializers.CharField(min_length=3, max_length=20000, trim_whitespace=True)


class AttorneyAnnotationSerializer(serializers.ModelSerializer):
    assessment_id = serializers.UUIDField(read_only=True)
    created_by_id = serializers.ReadOnlyField()
    resolved_by_id = serializers.ReadOnlyField()
    created_by_username = serializers.CharField(source="created_by.get_username", read_only=True)

    class Meta:
        model = AttorneyAnnotation
        fields = (
            "id", "assessment_id", "category", "body", "created_by_id",
            "created_by_username", "resolved", "resolved_by_id", "resolved_at", "created_at",
        )
        read_only_fields = fields
