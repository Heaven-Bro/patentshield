"""API routes for landscape studies, canonical-publication selection and review."""
from django.urls import path

from landscapes.views import (
    AssigneeAliasCreateView,
    AssigneeEntityDetailView,
    AssigneeListCreateView,
    LandscapeAnalyticsView,
    LandscapeAnnotationDetailView,
    LandscapeAnnotationListCreateView,
    LandscapeBuildView,
    LandscapeStudyDetailView,
    LandscapeStudyListCreateView,
    LandscapeStudyPublicationsView,
    PublicationSearchView,
    TechnologyTaxonomyListCreateView,
)

urlpatterns = [
    path("publications/search/", PublicationSearchView.as_view(), name="landscape-publication-search"),
    path("studies/", LandscapeStudyListCreateView.as_view(), name="landscape-study-list-create"),
    path("studies/<uuid:study_id>/", LandscapeStudyDetailView.as_view(), name="landscape-study-detail"),
    path("studies/<uuid:study_id>/publications/", LandscapeStudyPublicationsView.as_view(), name="landscape-study-publications"),
    path("studies/<uuid:study_id>/build/", LandscapeBuildView.as_view(), name="landscape-study-build"),
    path("studies/<uuid:study_id>/analytics/", LandscapeAnalyticsView.as_view(), name="landscape-study-analytics"),
    path("studies/<uuid:study_id>/annotations/", LandscapeAnnotationListCreateView.as_view(), name="landscape-study-annotations"),
    path("annotations/<uuid:annotation_id>/", LandscapeAnnotationDetailView.as_view(), name="landscape-annotation-detail"),
    path("assignees/", AssigneeListCreateView.as_view(), name="landscape-assignee-list-create"),
    path("assignees/<uuid:assignee_id>/", AssigneeEntityDetailView.as_view(), name="landscape-assignee-detail"),
    path("assignees/<uuid:assignee_id>/aliases/", AssigneeAliasCreateView.as_view(), name="landscape-assignee-alias-create"),
    path("technology-taxonomy/", TechnologyTaxonomyListCreateView.as_view(), name="landscape-technology-taxonomy"),
]
