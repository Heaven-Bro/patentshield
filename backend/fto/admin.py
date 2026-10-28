"""Admin views for operational inspection of FTO records."""
from django.contrib import admin

from fto.models import (
    AttorneyAnnotation, FTOStudy, LimitationAssessment, LimitationEvidence,
    ProductFeature, ProductMatter, ProductVersion, StudyClaimLimitation,
)


@admin.register(ProductMatter)
class ProductMatterAdmin(admin.ModelAdmin):
    list_display = ("name", "product_code", "tenant", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "product_code")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(ProductVersion)
class ProductVersionAdmin(admin.ModelAdmin):
    list_display = ("product", "version_number", "extraction_status", "decomposition_status", "created_at")
    list_filter = ("extraction_status", "decomposition_status")
    search_fields = ("product__name", "source_filename", "content_sha256")
    readonly_fields = ("id", "storage_key", "content_sha256", "extracted_text", "extracted_pages", "created_at")


@admin.register(ProductFeature)
class ProductFeatureAdmin(admin.ModelAdmin):
    list_display = ("title", "product_version", "review_status", "source_page_index", "created_at")
    list_filter = ("review_status", "extraction_method")
    search_fields = ("title", "source_quote")
    readonly_fields = ("source_quote", "feature_key", "created_at", "updated_at")


class StudyClaimLimitationInline(admin.TabularInline):
    model = StudyClaimLimitation
    extra = 0
    readonly_fields = ("claim", "limitation_index", "limitation_text", "source_start_offset", "source_end_offset")
    can_delete = False


@admin.register(FTOStudy)
class FTOStudyAdmin(admin.ModelAdmin):
    list_display = ("title", "product_version", "patent", "status", "created_at")
    list_filter = ("status", "jurisdiction")
    search_fields = ("title", "patent__title", "patent__publication_number", "product_version__product__name")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = (StudyClaimLimitationInline,)


@admin.register(StudyClaimLimitation)
class StudyClaimLimitationAdmin(admin.ModelAdmin):
    list_display = ("study", "claim", "limitation_index", "needs_attorney_review")
    search_fields = ("limitation_text", "claim__claim_number", "study__title")
    readonly_fields = ("limitation_text", "source_start_offset", "source_end_offset", "created_at")


@admin.register(LimitationAssessment)
class LimitationAssessmentAdmin(admin.ModelAdmin):
    list_display = ("study", "limitation", "triage_status", "review_status", "reviewed_at")
    list_filter = ("triage_status", "review_status")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(LimitationEvidence)
class LimitationEvidenceAdmin(admin.ModelAdmin):
    list_display = ("assessment", "feature", "kind", "created_at")
    list_filter = ("kind",)
    readonly_fields = ("source_quote", "source_page_index", "source_start_offset", "source_end_offset", "created_at")


@admin.register(AttorneyAnnotation)
class AttorneyAnnotationAdmin(admin.ModelAdmin):
    list_display = ("assessment", "category", "created_by", "resolved", "created_at")
    list_filter = ("category", "resolved")
    search_fields = ("body",)
    readonly_fields = ("body", "created_by", "created_at")
