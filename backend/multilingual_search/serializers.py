"""Request and response validation for multilingual search APIs."""

from __future__ import annotations

from rest_framework import serializers

from multilingual_search.models import TranslationRecord


class MultilingualSearchRequestSerializer(serializers.Serializer):
    query = serializers.CharField(allow_blank=False, trim_whitespace=True, max_length=1000)
    top_n = serializers.IntegerField(min_value=1, max_value=20, default=5)


class TranslateChunkRequestSerializer(serializers.Serializer):
    chunk_id = serializers.UUIDField()
    target_language = serializers.ChoiceField(choices=TranslationRecord.Language.choices)


class TranslationRecordSerializer(serializers.ModelSerializer):
    source_chunk_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = TranslationRecord
        fields = (
            "id",
            "source_chunk_id",
            "source_text_sha256",
            "source_language",
            "target_language",
            "translated_text",
            "translation_sha256",
            "provider",
            "provider_version",
            "created_at",
        )
        read_only_fields = fields
