"""Read-only admin views for sensitive prosecution records."""
from django.contrib import admin

from office_actions.models import (
    AmendmentSupportCheck,
    CitedReference,
    OfficeAction,
    OfficeActionReview,
    Rejection,
    RejectionReference,
    ResponseDraft,
    ResponseDraftRevision,
)


@admin.register(OfficeAction)
class OfficeActionAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "patent", "jurisdiction", "action_type", "extraction_status", "parse_status", "created_at")
    list_filter = ("jurisdiction", "action_type", "extraction_status", "parse_status")
    search_fields = ("application_number", "source_filename", "patent__title")
    readonly_fields = ("id", "tenant", "patent", "created_by", "storage_key", "content_sha256", "extracted_text", "extracted_pages", "page_count", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Rejection)
class RejectionAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "office_action", "ground", "source_page", "parser_confidence", "needs_attorney_review")
    list_filter = ("ground", "needs_attorney_review")
    readonly_fields = tuple(field.name for field in Rejection._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CitedReference)
class CitedReferenceAdmin(admin.ModelAdmin):
    list_display = ("identifier", "tenant", "office_action", "source_page", "created_at")
    search_fields = ("identifier", "office_action__application_number")
    readonly_fields = tuple(field.name for field in CitedReference._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RejectionReference)
class RejectionReferenceAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "rejection", "cited_reference", "source_page")
    readonly_fields = tuple(field.name for field in RejectionReference._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ResponseDraft)
class ResponseDraftAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "office_action", "status", "current_revision_number", "updated_at")
    list_filter = ("status",)
    readonly_fields = tuple(field.name for field in ResponseDraft._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ResponseDraftRevision)
class ResponseDraftRevisionAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "draft", "revision_number", "generation_method", "content_sha256", "created_at")
    readonly_fields = tuple(field.name for field in ResponseDraftRevision._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OfficeActionReview)
class OfficeActionReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "revision", "reviewer", "decision", "created_at")
    list_filter = ("decision",)
    readonly_fields = tuple(field.name for field in OfficeActionReview._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AmendmentSupportCheck)
class AmendmentSupportCheckAdmin(admin.ModelAdmin):
    list_display = ("id", "tenant", "office_action", "claim_number", "status", "created_at")
    list_filter = ("status",)
    readonly_fields = tuple(field.name for field in AmendmentSupportCheck._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
