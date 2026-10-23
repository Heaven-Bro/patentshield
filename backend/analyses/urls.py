"""REST URL patterns for analysis review operations."""

from django.urls import path

from analyses.views import AnalysisReviewView

app_name = "analyses"

urlpatterns = [
    path(
        "analyses/<uuid:analysis_id>/review/",
        AnalysisReviewView.as_view(),
        name="analysis-review",
    ),
]
