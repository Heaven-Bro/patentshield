"""Django admin registration for tenant and membership provisioning."""

from __future__ import annotations

from django.contrib import admin

from tenants.models import Tenant, TenantMembership, TenantExternalAIProcessingPolicy


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    """Manage tenant workspaces and their lifecycle status."""

    list_display = ("name", "slug", "status", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "slug")
    readonly_fields = ("id", "created_at", "updated_at")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(TenantMembership)
class TenantMembershipAdmin(admin.ModelAdmin):
    """Assign users to tenants and manage their RBAC role/status."""

    list_display = ("user", "tenant", "role", "status")
    list_filter = ("role", "status", "tenant__status")
    search_fields = (
        "user__username",
        "user__email",
        "tenant__name",
        "tenant__slug",
    )
    autocomplete_fields = ("user", "tenant")
    list_select_related = ("user", "tenant")


@admin.register(TenantExternalAIProcessingPolicy)
class TenantExternalAIProcessingPolicyAdmin(admin.ModelAdmin):
    """Explicitly approve per-tenant outbound AI/translation data categories."""

    list_display = ("tenant", "enabled", "provider", "model_name", "policy_version", "approved_by", "approved_at")
    list_filter = ("enabled", "provider", "region")
    search_fields = ("tenant__name", "tenant__slug", "model_name", "provider")
    autocomplete_fields = ("tenant", "approved_by")
    readonly_fields = ("updated_at",)
