"""REST URL patterns for analysis lifecycle and reviewer operations."""

from django.urls import path

from analyses.views import (
    AnalysisListCreateView,
    AnalysisDetailView,
    AnalysisRetryView,
    AnalysisReviewView,
)

app_name = "analyses"

urlpatterns = [
    path("analyses/", AnalysisListCreateView.as_view(), name="analysis-list-create"),
    path(
        "analyses/<uuid:analysis_id>/",
        AnalysisDetailView.as_view(),
        name="analysis-detail",
    ),
    path(
        "analyses/<uuid:analysis_id>/retry/",
        AnalysisRetryView.as_view(),
        name="analysis-retry",
    ),
    path(
        "analyses/<uuid:analysis_id>/review/",
        AnalysisReviewView.as_view(),
        name="analysis-review",
    ),
]
