"""Tenant-scoped competitor surveillance, alerting, and delivery state.

Public patent/family/event rows remain in ``intelligence_models``. This module
stores tenant-specific watch criteria, observations, alerts, and notification
state only; it intentionally does not duplicate canonical provider records.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from tenants.models import TenantScopedModel


class SurveillanceWatchlist(TenantScopedModel):
    """A tenant-owned collection of competitor/publication monitoring rules."""

    class PollStatus(models.TextChoices):
        IDLE = "IDLE", "Idle"
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=180)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    poll_interval_minutes = models.PositiveSmallIntegerField(default=360)
    poll_status = models.CharField(
        max_length=12,
        choices=PollStatus.choices,
        default=PollStatus.IDLE,
        db_index=True,
    )
    next_poll_at = models.DateTimeField(null=True, blank=True, db_index=True)
    poll_started_at = models.DateTimeField(null=True, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    active_task_id = models.CharField(max_length=100, blank=True, default="")
    last_error_code = models.CharField(max_length=80, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="surveillance_watchlists_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="surveillance_watchlists_updated",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "name"),
                name="surv_watch_tenant_name_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(poll_interval_minutes__gte=15)
                & models.Q(poll_interval_minutes__lte=10080),
                name="surv_watch_interval_range",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "is_active", "next_poll_at"),
                name="surv_watch_due_idx",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class SurveillanceWatchRule(TenantScopedModel):
    """One criterion to discover and match public patent records."""

    class RuleType(models.TextChoices):
        ASSIGNEE = "ASSIGNEE", "Competitor / assignee"
        PUBLICATION = "PUBLICATION", "Publication or application number"
        FAMILY = "FAMILY", "Patent family"
        CLASSIFICATION = "CLASSIFICATION", "IPC/CPC classification"
        SAVED_SEARCH = "SAVED_SEARCH", "Saved provider search"

    class MatchMode(models.TextChoices):
        EXACT = "EXACT", "Exact"
        CONTAINS = "CONTAINS", "Contains"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    watchlist = models.ForeignKey(
        SurveillanceWatchlist,
        on_delete=models.CASCADE,
        related_name="rules",
    )
    rule_type = models.CharField(max_length=16, choices=RuleType.choices)
    value = models.CharField(max_length=500)
    match_mode = models.CharField(
        max_length=8,
        choices=MatchMode.choices,
        default=MatchMode.CONTAINS,
    )
    providers = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("rule_type", "value", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "watchlist", "rule_type", "value"),
                name="surv_rule_value_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "watchlist", "is_active"),
                name="surv_rule_active_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.watchlist_id and self.tenant_id and self.watchlist.tenant_id != self.tenant_id:
            raise ValidationError("Watch rule and watchlist must belong to the same tenant.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.watchlist}: {self.rule_type} {self.value}"


class WatchlistObservation(TenantScopedModel):
    """Per-rule state used to detect repeatable, meaningful record changes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    watchlist = models.ForeignKey(
        SurveillanceWatchlist,
        on_delete=models.CASCADE,
        related_name="observations",
    )
    rule = models.ForeignKey(
        SurveillanceWatchRule,
        on_delete=models.CASCADE,
        related_name="observations",
    )
    publication = models.ForeignKey(
        "patents.PatentPublication",
        on_delete=models.CASCADE,
        related_name="surveillance_observations",
    )
    last_snapshot = models.ForeignKey(
        "patents.PatentSourceSnapshot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="surveillance_observations",
    )
    last_status = models.CharField(max_length=20, blank=True, default="")
    last_family_system = models.CharField(max_length=20, blank=True, default="")
    last_family_identifier = models.CharField(max_length=200, blank=True, default="")
    last_title = models.CharField(max_length=500, blank=True, default="")
    first_observed_at = models.DateTimeField(auto_now_add=True)
    last_observed_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        super().clean()
        if self.watchlist_id and self.tenant_id and self.watchlist.tenant_id != self.tenant_id:
            raise ValidationError("Observation and watchlist must belong to the same tenant.")
        if self.rule_id and self.tenant_id and self.rule.tenant_id != self.tenant_id:
            raise ValidationError("Observation and rule must belong to the same tenant.")
        if self.rule_id and self.watchlist_id and self.rule.watchlist_id != self.watchlist_id:
            raise ValidationError("Observation rule must belong to the same watchlist.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "rule", "publication"),
                name="surv_observe_rule_pub_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "watchlist", "last_observed_at"),
                name="surv_observe_recent_idx",
            ),
        ]


class WatchlistObservedLegalEvent(TenantScopedModel):
    """Tracks which source legal events have been seen for one watch rule."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    observation = models.ForeignKey(
        WatchlistObservation,
        on_delete=models.CASCADE,
        related_name="observed_legal_events",
    )
    legal_event = models.ForeignKey(
        "patents.PatentLegalEvent",
        on_delete=models.CASCADE,
        related_name="surveillance_observations",
    )
    first_observed_at = models.DateTimeField(auto_now_add=True)

    def clean(self) -> None:
        super().clean()
        if self.observation_id and self.tenant_id and self.observation.tenant_id != self.tenant_id:
            raise ValidationError("Observed event and observation must belong to the same tenant.")
        if self.legal_event_id and self.observation_id:
            if self.legal_event.publication_id != self.observation.publication_id:
                raise ValidationError("Observed legal event must belong to the observed publication.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "observation", "legal_event"),
                name="surv_observe_event_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "observation", "first_observed_at"),
                name="surv_obs_event_recent_idx",
            ),
        ]


class AlertDestination(TenantScopedModel):
    """A tenant-authorized email, Slack, or Microsoft Teams destination.

    Webhook URLs are encrypted before persistence and are never serialized back
    to the browser. The encryption key is supplied independently by deployment
    configuration; missing keys fail closed when a webhook destination is set.
    """

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "Email"
        SLACK = "SLACK", "Slack incoming webhook"
        TEAMS = "TEAMS", "Microsoft Teams webhook"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=160)
    channel = models.CharField(max_length=8, choices=Channel.choices)
    email_address = models.EmailField(blank=True, default="")
    webhook_secret_ciphertext = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="surveillance_destinations_created",
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "name"),
                name="surv_dest_tenant_name_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "channel", "is_active"),
                name="surv_dest_active_idx",
            ),
        ]

    @property
    def has_webhook_secret(self) -> bool:
        return bool(self.webhook_secret_ciphertext)

    def __str__(self) -> str:
        return f"{self.name} ({self.channel})"


class AlertSubscription(TenantScopedModel):
    """Connect a watchlist to a configured delivery destination."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    watchlist = models.ForeignKey(
        SurveillanceWatchlist,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    destination = models.ForeignKey(
        AlertDestination,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    # Empty means all alert types. Values are validated at the API/service layer.
    alert_types = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        super().clean()
        for related in (self.watchlist if self.watchlist_id else None, self.destination if self.destination_id else None):
            if related is not None and self.tenant_id and related.tenant_id != self.tenant_id:
                raise ValidationError("Subscription, watchlist, and destination must belong to the same tenant.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "watchlist", "destination"),
                name="surv_sub_watch_dest_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "watchlist", "is_active"),
                name="surv_sub_active_idx",
            ),
        ]


class SurveillanceAlert(TenantScopedModel):
    """Deduplicated, evidence-linked event observed for a tenant watchlist."""

    class AlertType(models.TextChoices):
        PUBLICATION_MATCHED = "PUBLICATION_MATCHED", "Publication matched"
        PUBLICATION_UPDATED = "PUBLICATION_UPDATED", "Publication metadata changed"
        FAMILY_CHANGED = "FAMILY_CHANGED", "Patent family changed"
        STATUS_CHANGED = "STATUS_CHANGED", "Legal status changed"
        LEGAL_EVENT = "LEGAL_EVENT", "New legal event"

    class State(models.TextChoices):
        OPEN = "OPEN", "Open"
        ACKNOWLEDGED = "ACKNOWLEDGED", "Acknowledged"
        RESOLVED = "RESOLVED", "Resolved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    watchlist = models.ForeignKey(
        SurveillanceWatchlist,
        on_delete=models.CASCADE,
        related_name="alerts",
    )
    rule = models.ForeignKey(
        SurveillanceWatchRule,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="alerts",
    )
    publication = models.ForeignKey(
        "patents.PatentPublication",
        on_delete=models.CASCADE,
        related_name="surveillance_alerts",
    )
    source_snapshot = models.ForeignKey(
        "patents.PatentSourceSnapshot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="surveillance_alerts",
    )
    legal_event = models.ForeignKey(
        "patents.PatentLegalEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="surveillance_alerts",
    )
    alert_type = models.CharField(max_length=24, choices=AlertType.choices)
    fingerprint = models.CharField(max_length=64)
    title = models.CharField(max_length=300)
    summary = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=dict, blank=True)
    source_observed_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(
        max_length=14,
        choices=State.choices,
        default=State.OPEN,
        db_index=True,
    )
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="surveillance_alerts_acknowledged",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        super().clean()
        if self.watchlist_id and self.tenant_id and self.watchlist.tenant_id != self.tenant_id:
            raise ValidationError("Alert and watchlist must belong to the same tenant.")
        if self.rule_id and self.watchlist_id and self.rule.watchlist_id != self.watchlist_id:
            raise ValidationError("Alert rule must belong to the alert watchlist.")
        if self.rule_id and self.tenant_id and self.rule.tenant_id != self.tenant_id:
            raise ValidationError("Alert rule must belong to the same tenant.")
        if self.source_snapshot_id and self.publication_id:
            if self.source_snapshot.publication_id != self.publication_id:
                raise ValidationError("Alert snapshot must belong to the alert publication.")
        if self.legal_event_id and self.publication_id:
            if self.legal_event.publication_id != self.publication_id:
                raise ValidationError("Alert legal event must belong to the alert publication.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "watchlist", "fingerprint"),
                name="surv_alert_fingerprint_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "state", "-created_at"),
                name="surv_alert_state_idx",
            ),
            models.Index(
                fields=("tenant", "watchlist", "-created_at"),
                name="surv_alert_watch_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.alert_type}: {self.title}"


class AlertDelivery(TenantScopedModel):
    """Durable per-alert/per-destination delivery state and retry checkpoint."""

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        SENDING = "SENDING", "Sending"
        DELIVERED = "DELIVERED", "Delivered"
        RETRYING = "RETRYING", "Retrying"
        DEAD_LETTER = "DEAD_LETTER", "Dead letter"
        CANCELLED = "CANCELLED", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alert = models.ForeignKey(
        SurveillanceAlert,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    destination = models.ForeignKey(
        AlertDestination,
        on_delete=models.PROTECT,
        related_name="deliveries",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    idempotency_key = models.CharField(max_length=64, unique=True)
    # Durable broker-dispatch lease prevents Beat from enqueueing the same
    # queued/recoverable delivery on every scheduler tick while workers lag.
    active_task_id = models.CharField(max_length=100, blank=True, default="")
    dispatch_reserved_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error_code = models.CharField(max_length=80, blank=True, default="")
    provider_message_id = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        super().clean()
        if self.alert_id and self.tenant_id and self.alert.tenant_id != self.tenant_id:
            raise ValidationError("Delivery and alert must belong to the same tenant.")
        if self.destination_id and self.tenant_id and self.destination.tenant_id != self.tenant_id:
            raise ValidationError("Delivery and destination must belong to the same tenant.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "alert", "destination"),
                name="surv_delivery_alert_dest_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "status", "next_attempt_at"),
                name="surv_delivery_due_idx",
            ),
            models.Index(
                fields=("status", "dispatch_reserved_at"),
                name="surv_deliv_dispatch_idx",
            ),
        ]


class AlertDeliveryAttempt(TenantScopedModel):
    """Append-only record of each actual delivery attempt."""

    class Outcome(models.TextChoices):
        STARTED = "STARTED", "Started"
        DELIVERED = "DELIVERED", "Delivered"
        FAILED = "FAILED", "Failed"
        RETRYING = "RETRYING", "Retry scheduled"
        DEAD_LETTER = "DEAD_LETTER", "Dead letter"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    delivery = models.ForeignKey(
        AlertDelivery,
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    attempt_number = models.PositiveSmallIntegerField()
    outcome = models.CharField(max_length=12, choices=Outcome.choices)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True, default="")
    response_summary = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self) -> None:
        super().clean()
        if self.delivery_id and self.tenant_id and self.delivery.tenant_id != self.tenant_id:
            raise ValidationError("Delivery attempt and delivery must belong to the same tenant.")

    def save(self, *args, **kwargs) -> None:
        self.clean()
        super().save(*args, **kwargs)

    class Meta:
        ordering = ("attempt_number",)
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "delivery", "attempt_number"),
                name="surv_attempt_no_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("tenant", "delivery", "started_at"),
                name="surv_attempt_recent_idx",
            ),
        ]
