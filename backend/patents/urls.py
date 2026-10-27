"""Tenant-scoped patent document REST endpoints."""

from django.urls import path

from patents.intelligence_views import LivePatentSearchView

from patents.views import (
    PatentClaimsView,
    PatentDetailView,
    PatentDocumentUploadView,
    PatentListView,
    PatentVersionFileView,
    PatentVersionProcessView,
)

app_name = "patents"

urlpatterns = [
    path("", PatentListView.as_view(), name="patent-list"),
    path("upload/", PatentDocumentUploadView.as_view(), name="patent-upload"),
    path("search-live/", LivePatentSearchView.as_view(), name="patent-search-live"),
    path(
        "<uuid:patent_id>/claims/",
        PatentClaimsView.as_view(),
        name="patent-claims",
    ),
    path(
        "<uuid:patent_id>/versions/<uuid:version_id>/process/",
        PatentVersionProcessView.as_view(),
        name="patent-version-process",
    ),
    path(
        "<uuid:patent_id>/versions/<uuid:version_id>/file/",
        PatentVersionFileView.as_view(),
        name="patent-version-file",
    ),
    path("<uuid:patent_id>/", PatentDetailView.as_view(), name="patent-detail"),
]
