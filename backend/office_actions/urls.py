from django.urls import path

from office_actions.views import (
    AmendmentSupportView,
    OfficeActionDetailView,
    OfficeActionDraftListGenerateView,
    OfficeActionFileView,
    OfficeActionListCreateView,
    OfficeActionProcessView,
    ProsecutionHistoryView,
    ResponseDraftDetailView,
    ResponseDraftRevisionListCreateView,
    ResponseDraftSubmitForReviewView,
    ResponseDraftReviewView,
)

app_name = "office_actions"
urlpatterns = [
    path("", OfficeActionListCreateView.as_view(), name="list-create"),
    path("prosecution-history/", ProsecutionHistoryView.as_view(), name="prosecution-history"),
    path("<uuid:action_id>/", OfficeActionDetailView.as_view(), name="detail"),
    path("<uuid:action_id>/file/", OfficeActionFileView.as_view(), name="file"),
    path("<uuid:action_id>/process/", OfficeActionProcessView.as_view(), name="process"),
    path("<uuid:action_id>/drafts/", OfficeActionDraftListGenerateView.as_view(), name="drafts"),
    path("<uuid:action_id>/drafts/generate/", OfficeActionDraftListGenerateView.as_view(), name="draft-generate"),
    path("<uuid:action_id>/amendment-support/", AmendmentSupportView.as_view(), name="amendment-support"),
    path("drafts/<uuid:draft_id>/", ResponseDraftDetailView.as_view(), name="draft-detail"),
    path("drafts/<uuid:draft_id>/revisions/", ResponseDraftRevisionListCreateView.as_view(), name="draft-revisions"),
    path("drafts/<uuid:draft_id>/submit-for-review/", ResponseDraftSubmitForReviewView.as_view(), name="draft-submit-review"),
    path("revisions/<uuid:revision_id>/review/", ResponseDraftReviewView.as_view(), name="revision-review"),
]
