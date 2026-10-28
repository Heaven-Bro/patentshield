"""URL patterns for Phase 5 multilingual search."""

from django.urls import path

from multilingual_search.views import (
    MultilingualPatentSearchView,
    TranslatePatentChunkView,
    TranslationRecordDetailView,
)

app_name = "multilingual_search"

urlpatterns = [
    path("search/", MultilingualPatentSearchView.as_view(), name="search"),
    path("translate/", TranslatePatentChunkView.as_view(), name="translate"),
    path("translations/<uuid:translation_id>/", TranslationRecordDetailView.as_view(), name="translation-detail"),
]
