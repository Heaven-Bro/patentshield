"""REST contracts for authorized live patent-source search."""

from __future__ import annotations

from rest_framework import serializers


SUPPORTED_PROVIDERS = ("USPTO", "EPO")


class LivePatentSearchRequestSerializer(serializers.Serializer):
    """Validate a bounded interactive search request."""

    query = serializers.CharField(
        max_length=1_000,
        trim_whitespace=True,
        allow_blank=False,
    )
    providers = serializers.ListField(
        child=serializers.ChoiceField(choices=SUPPORTED_PROVIDERS),
        min_length=1,
        max_length=2,
        allow_empty=False,
    )
    max_results = serializers.IntegerField(
        min_value=1,
        max_value=25,
        default=10,
        required=False,
    )

    def validate_providers(self, value: list[str]) -> list[str]:
        """Normalize duplicate provider choices without changing order."""
        return list(dict.fromkeys(value))
