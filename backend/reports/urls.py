"""Tenant-authorized analysis report and attorney claim-chart endpoints."""

from django.urls import path

from reports.claim_chart_views import (
    ClaimChartDetailView,
    ClaimChartExportCreateView,
    ClaimChartExportDownloadView,
    ClaimChartExportStatusView,
    ClaimChartListCreateView,
    ClaimChartRevisionListView,
    ClaimChartRowUpdateView,
)
from reports.views import ReportArtifactView, ReportGenerateView, ReportStatusView

app_name = "reports"

urlpatterns = [
    path(
        "claim-charts/",
        ClaimChartListCreateView.as_view(),
        name="claim-chart-list-create",
    ),
    path(
        "claim-charts/<uuid:chart_id>/",
        ClaimChartDetailView.as_view(),
        name="claim-chart-detail",
    ),
    path(
        "claim-charts/<uuid:chart_id>/rows/<uuid:row_id>/",
        ClaimChartRowUpdateView.as_view(),
        name="claim-chart-row-update",
    ),
    path(
        "claim-charts/<uuid:chart_id>/revisions/",
        ClaimChartRevisionListView.as_view(),
        name="claim-chart-revisions",
    ),
    path(
        "claim-charts/<uuid:chart_id>/exports/",
        ClaimChartExportCreateView.as_view(),
        name="claim-chart-export-create",
    ),
    path(
        "claim-chart-exports/<uuid:export_id>/",
        ClaimChartExportStatusView.as_view(),
        name="claim-chart-export-status",
    ),
    path(
        "claim-chart-exports/<uuid:export_id>/download/",
        ClaimChartExportDownloadView.as_view(),
        name="claim-chart-export-download",
    ),
    path(
        "<uuid:analysis_id>/generate/",
        ReportGenerateView.as_view(),
        name="report-generate",
    ),
    path(
        "<uuid:analysis_id>/status/",
        ReportStatusView.as_view(),
        name="report-status",
    ),
    path(
        "<uuid:analysis_id>/<str:artifact_format>/",
        ReportArtifactView.as_view(),
        name="report-artifact",
    ),
]
