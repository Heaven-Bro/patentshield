"""Bounded request serializers for semantic search and patent AI endpoints."""

from __future__ import annotations

from rest_framework import serializers

from patents.intelligence_models import PatentPublication


class SemanticSearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=2_000, trim_whitespace=True, allow_blank=False)
    sector_label = serializers.ChoiceField(
        choices=PatentPublication.Sector.choices,
        required=False,
        allow_blank=False,
    )
    top_k = serializers.IntegerField(min_value=1, max_value=50, default=10, required=False)
    similarity_threshold = serializers.FloatField(min_value=0.0, max_value=1.0, default=0.2, required=False)


class LandscapeSummaryRequestSerializer(serializers.Serializer):
    patent_ids = serializers.ListField(
        child=serializers.UUIDField(), min_length=1, max_length=50, allow_empty=False
    )
    query = serializers.CharField(max_length=1_500, required=False, allow_blank=True, default="")


class TargetPatentDetailsSerializer(serializers.Serializer):
    patent_identifier = serializers.CharField(max_length=200, required=False, allow_blank=True)
    publication_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    title = serializers.CharField(max_length=4_000, required=False, allow_blank=True)
    abstract = serializers.CharField(max_length=20_000, required=False, allow_blank=True)
    claims = serializers.ListField(
        child=serializers.CharField(max_length=20_000, allow_blank=False),
        required=False,
        max_length=100,
        allow_empty=True,
    )

    def validate(self, attrs):
        if not any(attrs.get(key) for key in ("patent_identifier", "publication_number", "title", "abstract")):
            raise serializers.ValidationError(
                "Provide a patent_identifier/publication_number or patent title/abstract details."
            )
        return attrs


class InfringementRiskRequestSerializer(serializers.Serializer):
    invention_description = serializers.CharField(
        max_length=20_000, trim_whitespace=True, allow_blank=False
    )
    target_patent_details = TargetPatentDetailsSerializer(required=False)
    target_patent_identifier = serializers.CharField(
        max_length=200, required=False, allow_blank=False
    )

    def validate(self, attrs):
        if not attrs.get("target_patent_details") and not attrs.get("target_patent_identifier"):
            raise serializers.ValidationError(
                "Provide target_patent_identifier or target_patent_details."
            )
        return attrs


class ChatHistoryMessageSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=("user", "assistant"))
    content = serializers.CharField(max_length=4_000, trim_whitespace=True, allow_blank=False)


class PatentRAGChatRequestSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=8_000, trim_whitespace=True, allow_blank=False)
    conversation_history = ChatHistoryMessageSerializer(
        many=True, required=False, max_length=20, allow_empty=True
    )
    target_patent_identifiers = serializers.ListField(
        child=serializers.CharField(max_length=200, trim_whitespace=True, allow_blank=False),
        min_length=1,
        max_length=25,
        allow_empty=False,
    )

    def validate_target_patent_identifiers(self, value):
        return list(dict.fromkeys(value))
