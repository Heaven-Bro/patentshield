"""Routes for portfolio valuation and diligence indicators."""
from django.urls import path

from portfolio_valuation.views import (
    StudyAnnotationsView,
    StudyAssetsView,
    StudyDiligenceView,
    StudyDetailView,
    StudyListCreateView,
    StudyScenarioSummaryView,
    StudyScenariosView,
    ScenarioRevisionsView,
)

urlpatterns = [
    path("studies/", StudyListCreateView.as_view(), name="valuation-study-list-create"),
    path("studies/<uuid:study_id>/", StudyDetailView.as_view(), name="valuation-study-detail"),
    path("studies/<uuid:study_id>/assets/", StudyAssetsView.as_view(), name="valuation-study-assets"),
    path("studies/<uuid:study_id>/diligence/", StudyDiligenceView.as_view(), name="valuation-study-diligence"),
    path("studies/<uuid:study_id>/scenarios/", StudyScenariosView.as_view(), name="valuation-study-scenarios"),
    path("studies/<uuid:study_id>/scenario-summary/", StudyScenarioSummaryView.as_view(), name="valuation-study-scenario-summary"),
    path("studies/<uuid:study_id>/annotations/", StudyAnnotationsView.as_view(), name="valuation-study-annotations"),
    path("scenarios/<uuid:scenario_id>/revisions/", ScenarioRevisionsView.as_view(), name="valuation-scenario-revisions"),
]
