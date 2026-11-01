"""Tenant-scoped patent document REST endpoints."""

from django.urls import path

from patents.ai_views import (
    InfringementRiskAnalysisView,
    LandscapeSummaryView,
    PatentRAGChatView,
    SemanticPatentSearchView,
)
from patents.intelligence_views import LivePatentSearchView

from patents.surveillance_views import (
    AlertDeliveryListView,
    AlertDeliveryRetryView,
    AlertDestinationDetailView,
    AlertDestinationListCreateView,
    AlertSubscriptionDetailView,
    AlertSubscriptionListCreateView,
    SurveillanceAlertListView,
    SurveillanceAlertTransitionView,
    SurveillanceWatchlistDetailView,
    SurveillanceWatchlistListCreateView,
    SurveillanceWatchlistPollView,
    SurveillanceWatchRuleDetailView,
    SurveillanceWatchRuleListCreateView,
)

from patents.views import (
    PatentClaimsView,
    PatentDetailView,
    PatentDocumentUploadView,
    PatentListView,
    PatentVersionFileView,
    PatentVersionProcessView,
    PatentVersionIndexStageRetryView,
)

app_name = "patents"

urlpatterns = [
    path("", PatentListView.as_view(), name="patent-list"),
    path("upload/", PatentDocumentUploadView.as_view(), name="patent-upload"),
    path("search-live/", LivePatentSearchView.as_view(), name="patent-search-live"),
    path("semantic-search/", SemanticPatentSearchView.as_view(), name="patent-semantic-search"),
    path("ai/landscape-summary/", LandscapeSummaryView.as_view(), name="patent-ai-landscape-summary"),
    path("ai/infringement-risk/", InfringementRiskAnalysisView.as_view(), name="patent-ai-infringement-risk"),
    path("ai/chat/", PatentRAGChatView.as_view(), name="patent-ai-rag-chat"),
    path("surveillance/watchlists/", SurveillanceWatchlistListCreateView.as_view(), name="surveillance-watchlists"),
    path("surveillance/watchlists/<uuid:watchlist_id>/", SurveillanceWatchlistDetailView.as_view(), name="surveillance-watchlist-detail"),
    path("surveillance/watchlists/<uuid:watchlist_id>/poll/", SurveillanceWatchlistPollView.as_view(), name="surveillance-watchlist-poll"),
    path("surveillance/watchlists/<uuid:watchlist_id>/rules/", SurveillanceWatchRuleListCreateView.as_view(), name="surveillance-watchlist-rules"),
    path("surveillance/rules/<uuid:rule_id>/", SurveillanceWatchRuleDetailView.as_view(), name="surveillance-watch-rule-detail"),
    path("surveillance/destinations/", AlertDestinationListCreateView.as_view(), name="surveillance-destinations"),
    path("surveillance/destinations/<uuid:destination_id>/", AlertDestinationDetailView.as_view(), name="surveillance-destination-detail"),
    path("surveillance/watchlists/<uuid:watchlist_id>/subscriptions/", AlertSubscriptionListCreateView.as_view(), name="surveillance-subscriptions"),
    path("surveillance/subscriptions/<uuid:subscription_id>/", AlertSubscriptionDetailView.as_view(), name="surveillance-subscription-detail"),
    path("surveillance/alerts/", SurveillanceAlertListView.as_view(), name="surveillance-alerts"),
    path("surveillance/alerts/<uuid:alert_id>/<str:action>/", SurveillanceAlertTransitionView.as_view(), name="surveillance-alert-transition"),
    path("surveillance/deliveries/", AlertDeliveryListView.as_view(), name="surveillance-deliveries"),
    path("surveillance/deliveries/<uuid:delivery_id>/retry/", AlertDeliveryRetryView.as_view(), name="surveillance-delivery-retry"),
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
        "<uuid:patent_id>/versions/<uuid:version_id>/index/<str:stage>/retry/",
        PatentVersionIndexStageRetryView.as_view(),
        name="patent-version-index-stage-retry",
    ),
    path(
        "<uuid:patent_id>/versions/<uuid:version_id>/file/",
        PatentVersionFileView.as_view(),
        name="patent-version-file",
    ),
    path("<uuid:patent_id>/", PatentDetailView.as_view(), name="patent-detail"),
]
