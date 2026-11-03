"""URL routes for product-to-claim FTO workflows."""
from django.urls import path

from fto.views import (
    AttorneyAnnotationListCreateView,
    AttorneyAnnotationResolveView,
    FTOStudyDetailView,
    FTOStudyHeatmapView,
    FTOCandidateMatchRefreshView,
    FTOStudyListCreateView,
    LimitationAssessmentUpdateView,
    LimitationEvidenceListCreateView,
    ProductFeatureDetailView,
    ProductFeatureListCreateView,
    ProductMatterDetailView,
    ProductMatterListCreateView,
    ProductVersionDetailView,
    ProductVersionFileView,
    ProductVersionListCreateView,
)

urlpatterns = [
    path("products/", ProductMatterListCreateView.as_view(), name="fto-products"),
    path("products/<uuid:product_id>/", ProductMatterDetailView.as_view(), name="fto-product-detail"),
    path("products/<uuid:product_id>/versions/", ProductVersionListCreateView.as_view(), name="fto-product-versions"),
    path("versions/<uuid:version_id>/", ProductVersionDetailView.as_view(), name="fto-version-detail"),
    path("versions/<uuid:version_id>/file/", ProductVersionFileView.as_view(), name="fto-version-file"),
    path("versions/<uuid:version_id>/features/", ProductFeatureListCreateView.as_view(), name="fto-version-features"),
    path("features/<uuid:feature_id>/", ProductFeatureDetailView.as_view(), name="fto-feature-detail"),
    path("studies/", FTOStudyListCreateView.as_view(), name="fto-studies"),
    path("studies/<uuid:study_id>/", FTOStudyDetailView.as_view(), name="fto-study-detail"),
    path("studies/<uuid:study_id>/risk-heatmap/", FTOStudyHeatmapView.as_view(), name="fto-study-risk-heatmap"),
    path("studies/<uuid:study_id>/candidate-matches/refresh/", FTOCandidateMatchRefreshView.as_view(), name="fto-candidate-matches-refresh"),
    path("studies/<uuid:study_id>/limitations/<uuid:limitation_id>/assessment/", LimitationAssessmentUpdateView.as_view(), name="fto-limitation-assessment"),
    path("studies/<uuid:study_id>/limitations/<uuid:limitation_id>/evidence/", LimitationEvidenceListCreateView.as_view(), name="fto-limitation-evidence"),
    path("studies/<uuid:study_id>/annotations/", AttorneyAnnotationListCreateView.as_view(), name="fto-attorney-annotations"),
    path("annotations/<uuid:annotation_id>/resolve/", AttorneyAnnotationResolveView.as_view(), name="fto-annotation-resolve"),
]
