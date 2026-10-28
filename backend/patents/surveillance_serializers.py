"""REST serializers for tenant-scoped competitor surveillance resources."""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from patents.intelligence_models import PatentPublication
from patents.surveillance_models import (
    AlertDelivery,
    AlertDestination,
    AlertSubscription,
    SurveillanceAlert,
    SurveillanceWatchRule,
    SurveillanceWatchlist,
)
from patents.surveillance_services import (
    UnsafeWebhookDestination,
    validate_watch_rule_input,
    validate_webhook_url,
)


class SurveillanceWatchRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = SurveillanceWatchRule
        fields = (
            "id",
            "rule_type",
            "value",
            "match_mode",
            "providers",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        source = self.instance
        rule_type = attrs.get("rule_type", source.rule_type if source else "")
        value = attrs.get("value", source.value if source else "")
        match_mode = attrs.get(
            "match_mode",
            source.match_mode if source else SurveillanceWatchRule.MatchMode.CONTAINS,
        )
        providers = attrs.get("providers", source.providers if source else [])
        try:
            value, providers = validate_watch_rule_input(
                rule_type=rule_type,
                value=value,
                match_mode=match_mode,
                providers=providers,
            )
        except DjangoValidationError as exc:
            details = exc.message_dict if hasattr(exc, "message_dict") else exc.messages
            raise serializers.ValidationError(details) from exc
        attrs["rule_type"] = rule_type
        attrs["value"] = value
        attrs["match_mode"] = match_mode
        attrs["providers"] = providers
        return attrs


class SurveillanceWatchlistSerializer(serializers.ModelSerializer):
    rules = SurveillanceWatchRuleSerializer(many=True, read_only=True)
    rule_count = serializers.SerializerMethodField()
    alert_count = serializers.SerializerMethodField()

    def get_rule_count(self, obj):
        return obj.rules.count()

    def get_alert_count(self, obj):
        return obj.alerts.count()

    class Meta:
        model = SurveillanceWatchlist
        fields = (
            "id",
            "name",
            "description",
            "is_active",
            "poll_interval_minutes",
            "poll_status",
            "next_poll_at",
            "last_polled_at",
            "last_success_at",
            "last_error_code",
            "rule_count",
            "alert_count",
            "rules",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "poll_status",
            "next_poll_at",
            "last_polled_at",
            "last_success_at",
            "last_error_code",
            "rule_count",
            "alert_count",
            "rules",
            "created_at",
            "updated_at",
        )

    def validate_poll_interval_minutes(self, value):
        if value < 15 or value > 10080:
            raise serializers.ValidationError("Polling interval must be between 15 and 10080 minutes.")
        return value

    def validate_name(self, value):
        value = " ".join(value.split())
        if not value:
            raise serializers.ValidationError("A watchlist name is required.")
        return value


class AlertDestinationWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160)
    channel = serializers.ChoiceField(choices=AlertDestination.Channel.choices)
    email_address = serializers.EmailField(required=False, allow_blank=True, default="")
    webhook_url = serializers.CharField(required=False, allow_blank=True, write_only=True)
    is_active = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        channel = attrs["channel"]
        webhook_url = attrs.get("webhook_url", "")
        email_address = attrs.get("email_address", "")
        instance = self.context.get("instance")
        has_secret = bool(instance and instance.webhook_secret_ciphertext)
        if channel == AlertDestination.Channel.EMAIL:
            if not email_address:
                raise serializers.ValidationError({"email_address": "An email address is required."})
            if webhook_url:
                raise serializers.ValidationError({"webhook_url": "Email destinations do not use webhooks."})
        else:
            if not webhook_url and not has_secret:
                raise serializers.ValidationError({"webhook_url": "A webhook URL is required."})
            if webhook_url:
                try:
                    validate_webhook_url(webhook_url, resolve_dns=False)
                except UnsafeWebhookDestination as exc:
                    raise serializers.ValidationError({"webhook_url": str(exc)}) from exc
            if email_address:
                raise serializers.ValidationError({"email_address": "Webhook destinations do not use email addresses."})
        return attrs


class AlertDestinationReadSerializer(serializers.ModelSerializer):
    has_webhook_secret = serializers.BooleanField(read_only=True)

    class Meta:
        model = AlertDestination
        fields = (
            "id",
            "name",
            "channel",
            "email_address",
            "has_webhook_secret",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class AlertSubscriptionSerializer(serializers.ModelSerializer):
    destination_id = serializers.UUIDField(write_only=True)
    destination = AlertDestinationReadSerializer(read_only=True)

    class Meta:
        model = AlertSubscription
        fields = (
            "id",
            "destination_id",
            "destination",
            "alert_types",
            "is_active",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "destination", "created_at", "updated_at")

    def validate_alert_types(self, value):
        allowed = set(SurveillanceAlert.AlertType.values)
        if not isinstance(value, list) or any(item not in allowed for item in value):
            raise serializers.ValidationError("alert_types must contain supported alert type values.")
        return list(dict.fromkeys(value))


class PatentPublicationBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatentPublication
        fields = (
            "id",
            "provider",
            "provider_record_id",
            "publication_number",
            "application_number",
            "jurisdiction",
            "title",
            "legal_status",
            "family_system",
            "family_identifier",
            "source_url",
            "publication_date",
            "last_synced_at",
        )
        read_only_fields = fields


class SurveillanceAlertSerializer(serializers.ModelSerializer):
    publication = PatentPublicationBriefSerializer(read_only=True)
    watchlist_name = serializers.CharField(source="watchlist.name", read_only=True)
    rule_value = serializers.CharField(source="rule.value", read_only=True, allow_null=True)
    delivery_count = serializers.SerializerMethodField()

    def get_delivery_count(self, obj):
        return obj.deliveries.count()

    class Meta:
        model = SurveillanceAlert
        fields = (
            "id",
            "watchlist",
            "watchlist_name",
            "rule",
            "rule_value",
            "publication",
            "source_snapshot",
            "legal_event",
            "alert_type",
            "title",
            "summary",
            "evidence",
            "source_observed_at",
            "state",
            "acknowledged_by",
            "acknowledged_at",
            "resolved_at",
            "delivery_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class AlertDeliverySerializer(serializers.ModelSerializer):
    destination_name = serializers.CharField(source="destination.name", read_only=True)
    channel = serializers.CharField(source="destination.channel", read_only=True)
    alert_title = serializers.CharField(source="alert.title", read_only=True)
    attempt_history = serializers.SerializerMethodField()

    class Meta:
        model = AlertDelivery
        fields = (
            "id",
            "alert",
            "alert_title",
            "destination",
            "destination_name",
            "channel",
            "status",
            "attempt_count",
            "next_attempt_at",
            "last_attempt_at",
            "delivered_at",
            "last_http_status",
            "last_error_code",
            "created_at",
            "updated_at",
            "attempt_history",
        )
        read_only_fields = fields

    def get_attempt_history(self, instance):
        return [
            {
                "attempt_number": attempt.attempt_number,
                "outcome": attempt.outcome,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "response_status": attempt.response_status,
                "error_code": attempt.error_code,
                "response_summary": attempt.response_summary,
            }
            for attempt in instance.attempts.order_by("attempt_number")[:20]
        ]
