"""Canonical records for public patent data synchronized from providers.

These records are deliberately global rather than tenant-scoped: they represent
public provider data and avoid storing duplicate copies per customer. Tenant
private uploads remain in the existing TenantScopedModel hierarchy.
"""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from pgvector.django import VectorField


class PatentPublication(models.Model):
    """A normalized bibliographic publication returned by an authoritative source."""

    class Provider(models.TextChoices):
        USPTO = "USPTO", "USPTO Open Data Portal"
        EPO = "EPO", "European Patent Office OPS"
        DATASET = "DATASET", "Curated Public Patent Dataset"

    class LegalStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        GRANTED = "GRANTED", "Granted"
        EXPIRED = "EXPIRED", "Expired"
        LAPSED = "LAPSED", "Lapsed"
        ABANDONED = "ABANDONED", "Abandoned or withdrawn"
        UNKNOWN = "UNKNOWN", "Unknown"

    class Sector(models.TextChoices):
        MACHINE_LEARNING_DATA_PRIVACY = (
            "machine_learning_data_privacy",
            "Machine Learning Data Privacy",
        )
        CLOUD_COMPUTING_TENANT_ISOLATION = (
            "cloud_computing_tenant_isolation",
            "Cloud Computing Tenant Isolation",
        )
        FEDERATED_LEARNING_ENCRYPTION = (
            "federated_learning_encryption",
            "Federated Learning Encryption",
        )
        FIVE_G_NETWORK_SLICING = (
            "5g_network_slicing",
            "5G Network Slicing",
        )
        WIRELESS_SENSOR_DATA_ROUTING = (
            "wireless_sensor_data_routing",
            "Wireless Sensor Data Routing",
        )
        IOT_DEVICE_AUTHENTICATION = (
            "iot_device_authentication",
            "IoT Device Authentication",
        )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=10, choices=Provider.choices)
    provider_record_id = models.CharField(max_length=200)
    publication_number = models.CharField(max_length=100, blank=True, null=True)
    application_number = models.CharField(max_length=100, blank=True, null=True)
    jurisdiction = models.CharField(max_length=10)
    kind_code = models.CharField(max_length=10, blank=True, null=True)
    title = models.TextField()
    abstract = models.TextField(blank=True, null=True)
    
    sector_label = models.CharField(
        max_length=64,
        choices=Sector.choices,
        blank=True,
        default="",
        db_index=True,
    )
    embedding = VectorField(
        dimensions=384,
        null=True,
        blank=True,
    )

    filing_date = models.DateField(blank=True, null=True)
    publication_date = models.DateField(blank=True, null=True)
    grant_date = models.DateField(blank=True, null=True)
    legal_status = models.CharField(
        max_length=20,
        choices=LegalStatus.choices,
        default=LegalStatus.UNKNOWN,
    )
    applicant_names = models.JSONField(default=list, blank=True)
    inventor_names = models.JSONField(default=list, blank=True)
    ipc_codes = models.JSONField(default=list, blank=True)
    cpc_codes = models.JSONField(default=list, blank=True)
    family_system = models.CharField(max_length=20, blank=True, default="")
    family_identifier = models.CharField(max_length=200, blank=True, default="")
    source_url = models.URLField(max_length=2_000, blank=True, default="")
    source_updated_at = models.DateTimeField(blank=True, null=True)
    source_payload_hash = models.CharField(max_length=64, blank=True, default="")
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    last_synced_at = models.DateTimeField(blank=True, null=True)
    raw_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "provider_record_id"),
                name="pat_pub_provider_record_uniq",
            ),
            models.UniqueConstraint(
                fields=("provider", "jurisdiction", "publication_number"),
                condition=(
                    Q(publication_number__isnull=False)
                    & ~Q(publication_number="")
                ),
                name="pat_pub_source_jur_num_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("provider", "publication_number"),
                name="pat_pub_provider_num_idx",
            ),
            models.Index(
                fields=("jurisdiction", "publication_date"),
                name="pat_pub_jur_pubdate_idx",
            ),
            models.Index(
                fields=("last_synced_at",),
                name="pat_pub_lastsync_idx",
            ),
            models.Index(
                fields=("sector_label",),
                name="pat_pub_sector_idx",
            ),
        ]
        ordering = ("-publication_date", "publication_number")

    def __str__(self) -> str:
        return self.publication_number or self.provider_record_id


class PatentFamily(models.Model):
    """A provider's family identifier without assuming universal family equivalence."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family_system = models.CharField(max_length=20)
    family_identifier = models.CharField(max_length=200)
    display_name = models.CharField(max_length=300, blank=True, default="")
    source_url = models.URLField(max_length=2_000, blank=True, default="")
    raw_metadata = models.JSONField(default=dict, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("family_system", "family_identifier"),
                name="pat_family_system_id_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("family_system", "family_identifier"),
                name="pat_family_system_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.family_system}:{self.family_identifier}"


class PatentFamilyMember(models.Model):
    """Links a publication to a family with source-derived priority details."""

    class RelationType(models.TextChoices):
        MEMBER = "MEMBER", "Family member"
        PRIORITY = "PRIORITY", "Priority document"
        RELATED = "RELATED", "Related publication"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    family = models.ForeignKey(
        PatentFamily,
        on_delete=models.CASCADE,
        related_name="members",
    )
    publication = models.ForeignKey(
        PatentPublication,
        on_delete=models.CASCADE,
        related_name="family_memberships",
    )
    relation_type = models.CharField(
        max_length=12,
        choices=RelationType.choices,
        default=RelationType.MEMBER,
    )
    priority_number = models.CharField(max_length=100, blank=True, default="")
    priority_date = models.DateField(blank=True, null=True)
    source_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("family", "publication"),
                name="pat_family_pub_member_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("publication",),
                name="pat_familymember_pub_idx",
            ),
        ]


class PatentLegalEvent(models.Model):
    """An idempotently ingested legal event with original source provenance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        PatentPublication,
        on_delete=models.CASCADE,
        related_name="legal_events",
    )
    provider = models.CharField(
        max_length=10,
        choices=PatentPublication.Provider.choices,
    )
    source_event_id = models.CharField(max_length=200, blank=True, default="")
    event_fingerprint = models.CharField(max_length=64)
    event_code = models.CharField(max_length=100)
    event_date = models.DateField(blank=True, null=True)
    title = models.CharField(max_length=500)
    description = models.TextField(blank=True, default="")
    legal_status_after = models.CharField(
        max_length=20,
        choices=PatentPublication.LegalStatus.choices,
        default=PatentPublication.LegalStatus.UNKNOWN,
    )
    source_url = models.URLField(max_length=2_000, blank=True, default="")
    source_payload_hash = models.CharField(max_length=64, blank=True, default="")
    observed_at = models.DateTimeField(auto_now_add=True)
    raw_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("publication", "provider", "event_fingerprint"),
                name="pat_levent_fingerprint_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("publication", "event_date"),
                name="pat_event_pub_date_idx",
            ),
            models.Index(
                fields=("provider", "event_date"),
                name="pat_event_provider_date_idx",
            ),
        ]
        ordering = ("-event_date", "-observed_at")


class PatentSourceSnapshot(models.Model):
    """Immutable provider payload snapshot used to explain data changes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(
        PatentPublication,
        on_delete=models.CASCADE,
        related_name="source_snapshots",
    )
    provider = models.CharField(
        max_length=10,
        choices=PatentPublication.Provider.choices,
    )
    source_record_id = models.CharField(max_length=200)
    payload_sha256 = models.CharField(max_length=64)
    source_url = models.URLField(max_length=2_000, blank=True, default="")
    source_license = models.CharField(max_length=200, blank=True, default="")
    fetched_at = models.DateTimeField(auto_now_add=True)
    payload = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("publication", "payload_sha256"),
                name="pat_source_snapshot_hash_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("publication", "-fetched_at"),
                name="pat_snapshot_pub_fetched_idx",
            ),
        ]
        ordering = ("-fetched_at",)

    def save(self, *args, **kwargs) -> None:
        """Treat a previously stored source snapshot as append-only provenance."""
        if not self._state.adding and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Patent source snapshots are immutable.")
        super().save(*args, **kwargs)


class ConnectorSyncCursor(models.Model):
    """Operational checkpoint for provider/source synchronization jobs."""

    class Provider(models.TextChoices):
        USPTO = "USPTO", "USPTO Open Data Portal"
        EPO = "EPO", "European Patent Office OPS"

    class Status(models.TextChoices):
        IDLE = "IDLE", "Idle"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        PARTIAL = "PARTIAL", "Succeeded with warnings"
        FAILED = "FAILED", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=10, choices=Provider.choices)
    scope_key = models.CharField(max_length=300)
    checkpoint = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.IDLE,
    )
    started_at = models.DateTimeField(blank=True, null=True)
    last_attempt_at = models.DateTimeField(blank=True, null=True)
    last_success_at = models.DateTimeField(blank=True, null=True)
    records_seen = models.PositiveIntegerField(default=0)
    records_saved = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=80, blank=True, default="")
    active_task_id = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("provider", "scope_key"),
                name="pat_sync_provider_scope_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "-last_attempt_at"),
                name="pat_synccursor_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}:{self.scope_key} ({self.status})"