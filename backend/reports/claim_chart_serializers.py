"""Validated API payloads for claim-chart creation and review."""

from __future__ import annotations

from rest_framework import serializers

from reports.models import ClaimChart, ClaimChartRow, ClaimChartExport


class ClaimChartCreateSerializer(serializers.Serializer):
    analysis_id = serializers.UUIDField()
    title = serializers.CharField(max_length=240, required=False, allow_blank=False)
    description = serializers.CharField(
        max_length=20_000,
        required=False,
        allow_blank=True,
    )


class ClaimChartPatchSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=240, required=False, allow_blank=False)
    description = serializers.CharField(
        max_length=20_000,
        required=False,
        allow_blank=True,
    )
    status = serializers.ChoiceField(choices=ClaimChart.Status.choices, required=False)

    def validate(self, attrs: dict) -> dict:
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class ClaimChartRowPatchSerializer(serializers.Serializer):
    mapping_summary = serializers.CharField(
        max_length=30_000,
        required=False,
        allow_blank=True,
    )
    attorney_notes = serializers.CharField(
        max_length=30_000,
        required=False,
        allow_blank=True,
    )
    attorney_decision = serializers.ChoiceField(
        choices=ClaimChartRow.AttorneyDecision.choices,
        required=False,
    )

    def validate(self, attrs: dict) -> dict:
        if not attrs:
            raise serializers.ValidationError("At least one field must be provided.")
        return attrs


class ClaimChartExportCreateSerializer(serializers.Serializer):
    format = serializers.ChoiceField(choices=ClaimChartExport.Format.choices)
