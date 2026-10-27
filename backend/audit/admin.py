"""Read-only Django admin view for tenant audit records."""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest

from audit.models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Expose audit records to authorized staff without mutation controls."""

    list_display = (
        "created_at",
        "tenant",
        "actor_user",
        "action",
        "resource_type",
        "outcome",
    )
    list_filter = ("outcome", "resource_type", "created_at")
    search_fields = ("action", "resource_type", "request_id")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    readonly_fields = (
        "id",
        "tenant",
        "actor_user",
        "legacy_actor_user_uuid",
        "action",
        "resource_type",
        "resource_id",
        "request_id",
        "outcome",
        "metadata",
        "created_at",
    )
    fields = readonly_fields
    list_per_page = 100
    actions = None

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self,
        request: HttpRequest,
        obj: AuditLog | None = None,
    ) -> bool:
        # View permission still allows read-only detail pages; never grant edit
        # capability to audit events, including for staff with broad admin roles.
        return False

    def has_delete_permission(
        self,
        request: HttpRequest,
        obj: AuditLog | None = None,
    ) -> bool:
        return False
