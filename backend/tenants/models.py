"""Core tenant and tenant-scoped models for PatentShield."""

from __future__ import annotations

import uuid
from uuid import UUID

from django.conf import settings
from django.db import models


class Tenant(models.Model):
    """A workspace that owns tenant-scoped PatentShield data."""

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        DELETED = "DELETED", "Deleted"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    status = models.CharField(
        max_length=9,
        choices=Status.choices,
        default=Status.ACTIVE,
    )
    retention_days = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "tenant"
        verbose_name_plural = "tenants"

    def __str__(self) -> str:
        return self.name


class TenantMembership(models.Model):
    """Associates a user with a tenant and their tenant-specific access role."""

    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        ADMIN = "ADMIN", "Admin"
        ATTORNEY = "ATTORNEY", "Attorney"
        ANALYST = "ANALYST", "Analyst"
        REVIEWER = "REVIEWER", "Reviewer"
        READ_ONLY = "READ_ONLY", "Read only"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INVITED = "INVITED", "Invited"
        REVOKED = "REVOKED", "Revoked"

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tenant_memberships",
    )
    role = models.CharField(max_length=9, choices=Role.choices)
    status = models.CharField(
        max_length=7,
        choices=Status.choices,
        default=Status.ACTIVE,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("tenant", "user"),
                name="unique_tenant_user_membership",
            ),
        ]
        verbose_name = "tenant membership"
        verbose_name_plural = "tenant memberships"

    def __str__(self) -> str:
        return f"{self.user} — {self.tenant} ({self.role})"


class TenantScopedQuerySet(models.QuerySet):
    """QuerySet helper for explicitly filtering records by tenant."""

    def for_tenant(
        self,
        tenant: Tenant | UUID | str,
    ) -> TenantScopedQuerySet:
        tenant_id = tenant.pk if isinstance(tenant, Tenant) else tenant
        return self.filter(tenant_id=tenant_id)


class TenantManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """Default manager for models that contain a tenant foreign key."""

    def for_tenant(
        self,
        tenant: Tenant | UUID | str,
    ) -> TenantScopedQuerySet:
        return self.get_queryset().for_tenant(tenant)


class TenantScopedModel(models.Model):
    """Abstract base for data rows owned by exactly one tenant."""

    tenant = models.ForeignKey(
        Tenant,
        on_delete=models.CASCADE,
        db_index=True,
        related_name="%(app_label)s_%(class)s_records",
    )

    objects = TenantManager()

    class Meta:
        abstract = True


class TenantExternalAIProcessingPolicy(models.Model):
    """Explicit, tenant-approved categories that may leave the workspace."""

    tenant = models.OneToOneField(
        Tenant,
        on_delete=models.CASCADE,
        related_name="external_ai_policy",
    )
    enabled = models.BooleanField(default=False)
    provider = models.CharField(max_length=40, default="meta")
    model_name = models.CharField(max_length=120, blank=True, default="")
    region = models.CharField(max_length=80, blank=True, default="")
    policy_version = models.CharField(max_length=40, default="1")
    allowed_data_categories = models.JSONField(default=list, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_tenant_ai_policies",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "tenant external AI processing policy"
        verbose_name_plural = "tenant external AI processing policies"

    def allows(self, categories: set[str], *, provider: str, model_name: str) -> bool:
        configured = {
            str(value).strip()
            for value in (self.allowed_data_categories or [])
            if isinstance(value, str) and value.strip()
        }
        return bool(
            self.enabled
            and self.approved_at
            and self.approved_by_id
            and self.provider == provider
            and self.model_name == model_name
            and categories.issubset(configured)
        )

    def __str__(self) -> str:
        return f"External AI policy for {self.tenant} (v{self.policy_version})"
