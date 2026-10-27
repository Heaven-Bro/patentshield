"""Read-only administration for versioned claim-chart work products."""

from django.contrib import admin

from reports.models import (
    ClaimChart,
    ClaimChartExport,
    ClaimChartRevision,
    ClaimChartRow,
)


class ClaimChartRowInline(admin.TabularInline):
    model = ClaimChartRow
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False
    readonly_fields = (
        "id", "assessment", "limitation_index", "limitation_text",
        "mapping_status", "mapping_summary", "attorney_notes",
        "attorney_decision", "created_at", "updated_at",
    )


@admin.register(ClaimChart)
class ClaimChartAdmin(admin.ModelAdmin):
    list_display = ("title", "tenant", "analysis", "status", "revision", "updated_at")
    list_filter = ("status", "created_at")
    search_fields = (
        "title",
        "analysis__patent__publication_number",
        "analysis__patent__title",
    )
    readonly_fields = (
        "id",
        "tenant",
        "analysis",
        "revision",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
    )
    inlines = (ClaimChartRowInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ClaimChartRevision)
class ClaimChartRevisionAdmin(admin.ModelAdmin):
    list_display = (
        "chart",
        "revision_number",
        "snapshot_sha256",
        "created_by",
        "created_at",
    )
    search_fields = ("chart__title", "snapshot_sha256")
    readonly_fields = tuple(field.name for field in ClaimChartRevision._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ClaimChartExport)
class ClaimChartExportAdmin(admin.ModelAdmin):
    list_display = (
        "chart",
        "revision",
        "format",
        "status",
        "requested_by",
        "created_at",
        "completed_at",
    )
    list_filter = ("format", "status", "created_at")
    readonly_fields = tuple(field.name for field in ClaimChartExport._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
