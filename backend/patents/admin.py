"""Read-only admin views for canonical public patent intelligence records."""

from __future__ import annotations

from django.contrib import admin

from patents.intelligence_models import (
    ConnectorSyncCursor,
    PatentFamily,
    PatentFamilyMember,
    PatentLegalEvent,
    PatentPublication,
    PatentSourceSnapshot,
)


class ImmutableSourceAdmin(admin.ModelAdmin):
    """Allow staff to inspect source records without mutating provenance."""

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return bool(obj is not None and super().has_view_permission(request, obj))

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PatentPublication)
class PatentPublicationAdmin(ImmutableSourceAdmin):
    list_display = (
        "publication_number",
        "application_number",
        "jurisdiction",
        "provider",
        "legal_status",
        "publication_date",
        "last_synced_at",
    )
    list_filter = ("provider", "jurisdiction", "legal_status")
    search_fields = ("publication_number", "application_number", "title")
    ordering = ("-publication_date",)


@admin.register(PatentFamily)
class PatentFamilyAdmin(ImmutableSourceAdmin):
    list_display = ("family_system", "family_identifier", "display_name")
    search_fields = ("family_identifier", "display_name")
    ordering = ("family_system", "family_identifier")


@admin.register(PatentFamilyMember)
class PatentFamilyMemberAdmin(ImmutableSourceAdmin):
    list_display = ("family", "publication", "relation_type", "priority_date")
    list_filter = ("relation_type",)
    search_fields = (
        "family__family_identifier",
        "publication__publication_number",
    )


@admin.register(PatentLegalEvent)
class PatentLegalEventAdmin(ImmutableSourceAdmin):
    list_display = (
        "publication",
        "provider",
        "event_code",
        "event_date",
        "legal_status_after",
    )
    list_filter = ("provider", "legal_status_after")
    search_fields = ("publication__publication_number", "event_code", "title")
    ordering = ("-event_date",)


@admin.register(PatentSourceSnapshot)
class PatentSourceSnapshotAdmin(ImmutableSourceAdmin):
    list_display = ("publication", "provider", "payload_sha256", "fetched_at")
    list_filter = ("provider",)
    search_fields = ("source_record_id", "payload_sha256")
    ordering = ("-fetched_at",)


@admin.register(ConnectorSyncCursor)
class ConnectorSyncCursorAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "scope_key",
        "status",
        "last_attempt_at",
        "last_success_at",
        "records_saved",
        "error_code",
    )
    list_filter = ("provider", "status")
    search_fields = ("scope_key", "error_code")
    readonly_fields = tuple(field.name for field in ConnectorSyncCursor._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return bool(obj is not None and super().has_view_permission(request, obj))

    def has_delete_permission(self, request, obj=None):
        return False
