from django.contrib import admin

from portfolio_valuation.models import (
    PortfolioAnnotation,
    PortfolioAssetSnapshot,
    PortfolioDiligenceSnapshot,
    PortfolioScenario,
    PortfolioScenarioRevision,
    PortfolioValuationStudy,
)


@admin.register(PortfolioValuationStudy)
class PortfolioValuationStudyAdmin(admin.ModelAdmin):
    list_display = ("title", "tenant", "currency", "status", "created_by", "created_at")
    list_filter = ("status", "currency")
    search_fields = ("title", "tenant__name", "created_by__username")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(PortfolioAssetSnapshot)
class PortfolioAssetSnapshotAdmin(admin.ModelAdmin):
    list_display = ("publication_number", "study", "jurisdiction", "legal_status", "captured_at")
    list_filter = ("jurisdiction", "legal_status")
    search_fields = ("publication_number", "title", "study__title")
    readonly_fields = tuple(field.name for field in PortfolioAssetSnapshot._meta.fields)


@admin.register(PortfolioDiligenceSnapshot)
class PortfolioDiligenceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("study", "asset_count", "composition_score", "methodology_version", "created_at")
    readonly_fields = tuple(field.name for field in PortfolioDiligenceSnapshot._meta.fields)


@admin.register(PortfolioScenario)
class PortfolioScenarioAdmin(admin.ModelAdmin):
    list_display = ("name", "study", "method", "created_by", "created_at")
    list_filter = ("method",)
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(PortfolioScenarioRevision)
class PortfolioScenarioRevisionAdmin(admin.ModelAdmin):
    list_display = ("scenario", "revision_number", "currency", "methodology_version", "created_at")
    readonly_fields = tuple(field.name for field in PortfolioScenarioRevision._meta.fields)


@admin.register(PortfolioAnnotation)
class PortfolioAnnotationAdmin(admin.ModelAdmin):
    list_display = ("study", "kind", "created_by", "created_at")
    list_filter = ("kind",)
    search_fields = ("body", "study__title")
    readonly_fields = ("id", "created_at", "updated_at")
