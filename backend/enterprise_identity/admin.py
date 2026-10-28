from django.contrib import admin

from enterprise_identity.models import (
    FederatedIdentity, IdentityProvider, ManagedSession, MatterGrant,
    MatterPolicy, SCIMToken, SCIMUserBinding,
)


@admin.register(IdentityProvider)
class IdentityProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "tenant", "protocol", "slug", "enabled", "jit_provisioning", "updated_at")
    list_filter = ("protocol", "enabled", "jit_provisioning")
    search_fields = ("name", "slug", "tenant__name", "issuer", "idp_entity_id")
    readonly_fields = ("id", "client_secret_ciphertext", "created_at", "updated_at")


@admin.register(FederatedIdentity)
class FederatedIdentityAdmin(admin.ModelAdmin):
    list_display = ("provider", "tenant", "user", "subject_id", "email_verified", "last_login_at")
    search_fields = ("subject_id", "user__username", "user__email", "provider__name")
    readonly_fields = ("id", "subject_id", "created_at", "last_login_at")


@admin.register(SCIMToken)
class SCIMTokenAdmin(admin.ModelAdmin):
    list_display = ("label", "tenant", "created_by", "created_at", "expires_at", "last_used_at", "revoked_at")
    readonly_fields = ("token_hash", "created_at", "last_used_at")


@admin.register(SCIMUserBinding)
class SCIMUserBindingAdmin(admin.ModelAdmin):
    list_display = ("tenant", "external_id", "user", "created_at", "updated_at")
    search_fields = ("external_id", "user__username", "user__email")


@admin.register(ManagedSession)
class ManagedSessionAdmin(admin.ModelAdmin):
    list_display = ("user", "auth_source", "created_at", "last_seen_at", "revoked_at")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("session_key_hash", "created_at", "last_seen_at")


@admin.register(MatterPolicy)
class MatterPolicyAdmin(admin.ModelAdmin):
    list_display = ("tenant", "matter_type", "matter_id", "enforce_acl", "updated_at")
    list_filter = ("matter_type", "enforce_acl")


@admin.register(MatterGrant)
class MatterGrantAdmin(admin.ModelAdmin):
    list_display = ("tenant", "matter_type", "matter_id", "subject_user", "subject_role", "level", "created_at")
    list_filter = ("matter_type", "level", "subject_role")
