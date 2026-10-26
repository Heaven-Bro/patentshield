"""Patent REST endpoints."""

from django.urls import path

from patents.views import (
    PatentClaimsView,
    PatentDetailView,
    PatentListView,
    PatentVersionFileView,
)

app_name = "patents"

urlpatterns = [
    path("", PatentListView.as_view(), name="patent-list"),
    path(
        "<uuid:patent_id>/claims/",
        PatentClaimsView.as_view(),
        name="patent-claims",
    ),
    path("<uuid:patent_id>/", PatentDetailView.as_view(), name="patent-detail"),
    path(
        "<uuid:patent_id>/versions/<uuid:version_id>/file/",
        PatentVersionFileView.as_view(),
        name="patent-version-file",
    ),
]
