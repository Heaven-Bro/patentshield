
    
  
"""Tenant-scoped patent document REST endpoints."""

from django.urls import path

from patents.views import (
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
    path("<uuid:patent_id>/", PatentDetailView.as_view(), name="patent-detail"),
    path(
        "<uuid:patent_id>/versions/<uuid:version_id>/file/",
        PatentVersionFileView.as_view(),
        name="patent-version-file",
    ),
    path(
        "<uuid:patent_id>/versions/<uuid:version_id>/process/",
        PatentVersionProcessView.as_view(),
        name="patent-version-process",
    ),
]

