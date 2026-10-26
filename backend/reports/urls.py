"""Tenant-authorized report export endpoints."""

from django.urls import path

from reports.views import ReportArtifactView, ReportGenerateView, ReportStatusView

app_name = "reports"

urlpatterns = [
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
