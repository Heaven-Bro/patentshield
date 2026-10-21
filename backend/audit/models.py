"""Tenant-scoped audit log records for PatentShield."""

from __future__ import annotations

import uuid

from django.db import models

from tenants.models import TenantScopedModel


class AuditLog(TenantScopedModel):
    """An audit event associated with a tenant workspace."""

    class Outcome(models.TextChoices):
        SUCCESS = "SUCCESS", "Success"
        FAILURE = "FAILURE", "Failure"
        DENIED = "DENIED", "Denied"

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    actor_user_id = models.UUIDField(null=True, blank=True, db_index=True)
    action = models.CharField(max_length=100)
    resource_type = models.CharField(max_length=100)
    resource_id = models.UUIDField(null=True, blank=True)
    request_id = models.CharField(max_length=100, null=True, blank=True)
    outcome = models.CharField(
        max_length=7,
        choices=Outcome.choices,
        default=Outcome.SUCCESS,
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=("tenant", "-created_at"),
                name="audit_tenant_created_idx",
            ),
        ]
