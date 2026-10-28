"""Input validation and tenant-safe read serializers for office-action workflows."""
from __future__ import annotations
from typing import Any

from django.conf import settings
from rest_framework import serializers

from office_actions.models import (
    AmendmentSupportCheck,
    CitedReference,
    OfficeAction,
    OfficeActionReview,
    Rejection,
    RejectionReference,
    ResponseDraft,
    ResponseDraftRevision,
)


class OfficeActionUploadSerializer(serializers.Serializer):
    patent_id = serializers.UUIDField()
    file = serializers.FileField(write_only=True, allow_empty_file=False)
    jurisdiction = serializers.ChoiceField(choices=[("US", "US"), ("EP", "EP"), ("EPO", "EPO"), ("PCT", "PCT"), ("WO", "WO"), ("OTHER", "Other")], default="US")
    action_type = serializers.ChoiceField(choices=OfficeAction.ActionType.choices, default=OfficeAction.ActionType.NON_FINAL)
    application_number = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    examiner_name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    issued_date = serializers.DateField(required=False, allow_null=True)
    response_due_date = serializers.DateField(required=False, allow_null=True)

    def validate_file(self, upload):
        maximum = int(getattr(settings, "PATENTSHIELD_MAX_PDF_UPLOAD_BYTES", 100 * 1024 * 1024))
        if maximum <= 0 or upload.size <= 0 or upload.size > maximum:
            raise serializers.ValidationError("PDF is empty or exceeds the configured upload limit.")
        content_type = (getattr(upload, "content_type", "") or "").lower()
        if content_type and content_type not in {"application/pdf", "application/x-pdf", "application/octet-stream"}:
            raise serializers.ValidationError("Only PDF office-action documents are accepted.")
        try:
            original = upload.tell()
            upload.seek(0)
            header = upload.read(min(1024, upload.size))
            upload.seek(original)
        except (AttributeError, OSError, ValueError) as exc:
            raise serializers.ValidationError("Uploaded file could not be read.") from exc
        if b"%PDF-" not in header:
            raise serializers.ValidationError("File does not contain a valid PDF header.")
        upload.seek(0)
        return upload

    def validate_application_number(self, value: str) -> str:
        return value.strip()

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        issued = attrs.get("issued_date")
        due = attrs.get("response_due_date")
        if issued and due and due < issued:
            raise serializers.ValidationError({"response_due_date": "Response due date cannot precede the issue date."})
        return attrs


class CitedReferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = CitedReference
        fields = ("id", "identifier", "title", "assignee_or_author", "source_page", "source_quote", "metadata", "created_at")
        read_only_fields = fields


class RejectionReferenceSerializer(serializers.ModelSerializer):
    cited_reference = CitedReferenceSerializer(read_only=True)

    class Meta:
        model = RejectionReference
        fields = ("id", "relationship", "source_quote", "source_page", "cited_reference", "created_at")
        read_only_fields = fields


class RejectionSerializer(serializers.ModelSerializer):
    ground_label = serializers.CharField(source="get_ground_display", read_only=True)
    reference_links = RejectionReferenceSerializer(many=True, read_only=True)

    class Meta:
        model = Rejection
        fields = ("id", "ground", "ground_label", "claim_numbers", "examiner_reasoning", "source_page", "source_start_offset", "source_end_offset", "source_quote", "parser_confidence", "needs_attorney_review", "reference_links", "created_at")
        read_only_fields = fields


class OfficeActionListSerializer(serializers.ModelSerializer):
    patent_title = serializers.CharField(source="patent.title", read_only=True)
    rejection_count = serializers.IntegerField(read_only=True)
    cited_reference_count = serializers.IntegerField(read_only=True)
    draft_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = OfficeAction
        fields = ("id", "patent", "patent_title", "jurisdiction", "action_type", "application_number", "examiner_name", "issued_date", "response_due_date", "source_filename", "content_sha256", "page_count", "extraction_status", "parse_status", "extraction_error_code", "rejection_count", "cited_reference_count", "draft_count", "created_at", "updated_at")
        read_only_fields = fields


class OfficeActionDetailSerializer(OfficeActionListSerializer):
    rejections = RejectionSerializer(many=True, read_only=True)
    cited_references = CitedReferenceSerializer(many=True, read_only=True)

    class Meta(OfficeActionListSerializer.Meta):
        fields = OfficeActionListSerializer.Meta.fields + ("rejections", "cited_references")


class OfficeActionReviewSerializer(serializers.ModelSerializer):
    reviewer_username = serializers.CharField(source="reviewer.get_username", read_only=True)

    class Meta:
        model = OfficeActionReview
        fields = ("id", "decision", "comments", "reviewer_username", "created_at")
        read_only_fields = fields


class ResponseDraftRevisionSerializer(serializers.ModelSerializer):
    reviews = OfficeActionReviewSerializer(many=True, read_only=True)

    class Meta:
        model = ResponseDraftRevision
        fields = ("id", "revision_number", "content_markdown", "source_manifest", "content_sha256", "generation_method", "generated_by", "created_at", "reviews")
        read_only_fields = fields


class ResponseDraftSerializer(serializers.ModelSerializer):
    current_revision = serializers.SerializerMethodField()
    office_action_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = ResponseDraft
        fields = ("id", "office_action_id", "title", "status", "current_revision_number", "current_revision", "created_at", "updated_at")
        read_only_fields = fields

    def get_current_revision(self, obj):
        revision = obj.revisions.filter(tenant_id=obj.tenant_id, draft__tenant_id=obj.tenant_id, revision_number=obj.current_revision_number).first()
        return ResponseDraftRevisionSerializer(revision).data if revision else None


class CreateRevisionSerializer(serializers.Serializer):
    content_markdown = serializers.CharField(allow_blank=False, trim_whitespace=True)


class ReviewDecisionSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=OfficeActionReview.Decision.choices)
    comments = serializers.CharField(required=False, allow_blank=True, default="", max_length=10000)


class AmendmentSupportSerializer(serializers.Serializer):
    claim_number = serializers.CharField(max_length=30, trim_whitespace=True)
    proposed_claim_text = serializers.CharField(min_length=8, max_length=30000, trim_whitespace=True)


class AmendmentSupportCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = AmendmentSupportCheck
        fields = ("id", "checked_document_version", "claim_number", "proposed_claim_text", "status", "source_manifest", "explanation", "created_at")
        read_only_fields = fields
