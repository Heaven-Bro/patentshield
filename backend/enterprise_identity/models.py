"""Enterprise federation, SCIM, managed sessions, and matter ACLs."""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from tenants.models import Tenant, TenantScopedModel


class IdentityProvider(TenantScopedModel):
    class Protocol(models.TextChoices):
        OIDC = "OIDC", "OpenID Connect"
        SAML = "SAML", "SAML 2.0"

    class ClientAuthMethod(models.TextChoices):
        BASIC = "client_secret_basic", "Client secret (HTTP Basic)"
        POST = "client_secret_post", "Client secret (POST body)"

    class ProvisionedRole(models.TextChoices):
        OWNER = "OWNER", "Owner"
        ADMIN = "ADMIN", "Admin"
        ATTORNEY = "ATTORNEY", "Attorney"
        ANALYST = "ANALYST", "Analyst"
        REVIEWER = "REVIEWER", "Reviewer"
        READ_ONLY = "READ_ONLY", "Read only"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=80)
    name = models.CharField(max_length=160)
    protocol = models.CharField(max_length=4, choices=Protocol.choices)
    enabled = models.BooleanField(default=False, db_index=True)
    jit_provisioning = models.BooleanField(default=False)
    allow_existing_email_link = models.BooleanField(default=False)
    trust_email_from_idp = models.BooleanField(default=False)
    default_role = models.CharField(
        max_length=9, choices=ProvisionedRole.choices, default=ProvisionedRole.READ_ONLY
    )
    allowed_email_domains = models.JSONField(default=list, blank=True)
    group_claim = models.CharField(max_length=120, default="groups")
    group_role_mapping = models.JSONField(default=dict, blank=True)

    # OIDC settings. Secret material is encrypted with a deployment-managed key.
    issuer = models.URLField(max_length=500, blank=True)
    discovery_url = models.URLField(max_length=500, blank=True)
    client_id = models.CharField(max_length=300, blank=True)
    client_secret_ciphertext = models.TextField(blank=True, editable=False)
    client_auth_method = models.CharField(
        max_length=32, choices=ClientAuthMethod.choices, default=ClientAuthMethod.BASIC
    )
    scopes = models.JSONField(default=list, blank=True)

    # SAML settings. IdP certificate is public verification material, not a secret.
    sp_entity_id = models.URLField(max_length=500, blank=True)
    idp_entity_id = models.CharField(max_length=500, blank=True)
    sso_url = models.URLField(max_length=500, blank=True)
    idp_x509_certificate = models.TextField(blank=True)
    saml_email_attribute = models.CharField(max_length=250, default="email")
    saml_groups_attribute = models.CharField(max_length=250, default="groups")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="identity_providers_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "slug"), name="entidp_tenant_slug_uniq")]
        indexes = [models.Index(fields=("tenant", "protocol", "enabled"), name="entidp_tenant_protocol_idx")]
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name} ({self.protocol})"


class FederatedIdentity(TenantScopedModel):
    """A stable issuer/provider subject binding to a local Django account."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.ForeignKey(IdentityProvider, on_delete=models.PROTECT, related_name="identities")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="federated_identities")
    subject_id = models.CharField(max_length=512)
    email_verified = models.BooleanField(default=False)
    groups = models.JSONField(default=list, blank=True)
    last_login_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("provider", "subject_id"), name="fedid_provider_subject_uniq")]
        indexes = [models.Index(fields=("tenant", "user"), name="fedid_tenant_user_idx")]


class SCIMToken(TenantScopedModel):
    """Hashed bearer token for a tenant's inbound SCIM provisioning client."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    label = models.CharField(max_length=120)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    scopes = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="scim_tokens_created")
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def is_active(self) -> bool:
        from django.utils import timezone
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > timezone.now())


class SCIMUserBinding(TenantScopedModel):
    """Maps an enterprise SCIM externalId to a local user within one tenant."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    external_id = models.CharField(max_length=255)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="scim_bindings")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("tenant", "external_id"), name="scim_tenant_externalid_uniq"),
            models.UniqueConstraint(fields=("tenant", "user"), name="scim_tenant_user_uniq"),
        ]


class ManagedSession(models.Model):
    """Registry row for a Django session; raw bearer session keys are never stored."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="managed_sessions")
    session_key_hash = models.CharField(max_length=64, unique=True, editable=False)
    auth_source = models.CharField(max_length=24, default="PASSWORD")
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="managed_sessions_revoked")

    class Meta:
        indexes = [models.Index(fields=("user", "revoked_at", "last_seen_at"), name="managed_session_user_idx")]
        ordering = ("-last_seen_at",)


class MatterPolicy(TenantScopedModel):
    """Per-matter switch for explicit access grants; default preserves tenant RBAC."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    matter_type = models.CharField(max_length=40)
    matter_id = models.UUIDField()
    enforce_acl = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="matter_policies_created")
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="matter_policies_updated")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("tenant", "matter_type", "matter_id"), name="matter_policy_target_uniq")]
        indexes = [models.Index(fields=("tenant", "matter_type", "matter_id"), name="matter_policy_lookup_idx")]


class MatterGrant(TenantScopedModel):
    class Level(models.TextChoices):
        READ = "READ", "Read"
        EDIT = "EDIT", "Edit"
        REVIEW = "REVIEW", "Review"
        ADMIN = "ADMIN", "Administer access"

    class SubjectRole(models.TextChoices):
        OWNER = "OWNER", "Owner"
        ADMIN = "ADMIN", "Admin"
        ATTORNEY = "ATTORNEY", "Attorney"
        ANALYST = "ANALYST", "Analyst"
        REVIEWER = "REVIEWER", "Reviewer"
        READ_ONLY = "READ_ONLY", "Read only"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    matter_type = models.CharField(max_length=40)
    matter_id = models.UUIDField()
    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="matter_grants")
    subject_role = models.CharField(max_length=9, choices=SubjectRole.choices, null=True, blank=True)
    level = models.CharField(max_length=6, choices=Level.choices)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="matter_grants_created")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=(Q(subject_user__isnull=False, subject_role__isnull=True) | Q(subject_user__isnull=True, subject_role__isnull=False)), name="matter_grant_one_subject_ck"),
            models.UniqueConstraint(fields=("tenant", "matter_type", "matter_id", "subject_user"), condition=Q(subject_user__isnull=False), name="matter_grant_user_uniq"),
            models.UniqueConstraint(fields=("tenant", "matter_type", "matter_id", "subject_role"), condition=Q(subject_role__isnull=False), name="matter_grant_role_uniq"),
        ]
        indexes = [models.Index(fields=("tenant", "matter_type", "matter_id", "level"), name="matter_grant_target_idx")]
