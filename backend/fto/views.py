"""Tenant-authorized product FTO, limitation heatmap, evidence, and annotation APIs."""
from __future__ import annotations

import hashlib
import logging
import os
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditLog
from fto.models import (
    AttorneyAnnotation,
    FTOStudy,
    LimitationAssessment,
    LimitationEvidence,
    ProductFeature,
    ProductMatter,
    ProductVersion,
    StudyClaimLimitation,
)
from fto.serializers import (
    AttorneyAnnotationCreateSerializer,
    AttorneyAnnotationSerializer,
    FTOStudyCreateSerializer,
    FTOStudySerializer,
    LimitationAssessmentSerializer,
    LimitationEvidenceCreateSerializer,
    LimitationEvidenceSerializer,
    ManualFeatureCreateSerializer,
    ProductFeatureReviewSerializer,
    ProductFeatureSerializer,
    ProductMatterSerializer,
    ProductVersionSerializer,
)
from fto.services import audit_fto_event, split_claim_limitations
from fto.tasks import process_product_version
from enterprise_identity.services import filter_visible_matter_queryset, require_matter_access
from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership
from tenants.permissions import HasTenantAccess

logger = logging.getLogger(__name__)

_EDITOR_ROLES = frozenset({
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
})
_REVIEW_ROLES = frozenset({
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.REVIEWER,
})
_MAX_SPEC_BYTES = 30 * 1024 * 1024


def _tenant(request: Request) -> Tenant:
    tenant = getattr(request, "tenant", None)
    if not isinstance(tenant, Tenant):
        raise PermissionDenied("Tenant context is unavailable.")
    return tenant


def _require_role(request: Request, allowed_roles: frozenset[str], message: str) -> None:
    membership = getattr(request, "tenant_membership", None)
    if membership is None or membership.status != TenantMembership.Status.ACTIVE or membership.role not in allowed_roles:
        raise PermissionDenied(message)


def _safe_filename(value: str, fallback: str = "product-specification.pdf") -> str:
    name = os.path.basename(value or fallback).replace("\x00", "")[:255]
    return name or fallback


def _enqueue_product_version(version: ProductVersion, actor) -> None:
    """Queue processing after commit and persist enqueue failures honestly."""
    try:
        process_product_version.apply_async(args=[str(version.pk), str(version.tenant_id)], queue="ingestion")
    except Exception as exc:
        ProductVersion.objects.filter(pk=version.pk, tenant_id=version.tenant_id).update(
            extraction_status=ProductVersion.ExtractionStatus.FAILED,
            decomposition_status=ProductVersion.DecompositionStatus.FAILED,
            error_code="ENQUEUE_FAILED",
        )
        logger.warning("Could not enqueue product spec processing (version_id=%s, error_type=%s).", version.pk, type(exc).__name__)
        try:
            audit_fto_event(
                tenant=version.product.tenant,
                actor=actor,
                action="FTO_PRODUCT_SPEC_ENQUEUE_FAILED",
                resource_type="ProductVersion",
                resource_id=version.pk,
                metadata={"error_type": type(exc).__name__},
                outcome=AuditLog.Outcome.FAILURE,
            )
        except Exception:
            logger.exception("Could not audit product-version enqueue failure.")


class FTOAPIView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess]

    def tenant(self, request: Request) -> Tenant:
        return _tenant(request)

    def product(self, request: Request, product_id) -> ProductMatter:
        tenant = self.tenant(request)
        matter = get_object_or_404(ProductMatter.objects.for_tenant(tenant.pk), pk=product_id)
        require_matter_access(request=request, matter_type="product_matter", matter_id=matter.pk, required="READ")
        return matter

    def version(self, request: Request, version_id) -> ProductVersion:
        tenant_id = self.tenant(request).pk
        version = get_object_or_404(
            ProductVersion.objects.for_tenant(tenant_id)
            .filter(product__tenant_id=tenant_id)
            .select_related("product", "created_by"),
            pk=version_id,
        )
        require_matter_access(request=request, matter_type="product_matter", matter_id=version.product_id, required="READ")
        return version

    def study(self, request: Request, study_id) -> FTOStudy:
        tenant_id = self.tenant(request).pk
        study = get_object_or_404(
            FTOStudy.objects.for_tenant(tenant_id)
            .filter(
                product_version__tenant_id=tenant_id,
                product_version__product__tenant_id=tenant_id,
                patent__tenant_id=tenant_id,
                patent_document_version__tenant_id=tenant_id,
                patent_document_version__patent_id=F("patent_id"),
            )
            .select_related("product_version__product", "patent", "patent_document_version", "created_by"),
            pk=study_id,
        )
        require_matter_access(request=request, matter_type="fto_study", matter_id=study.pk, required="READ")
        require_matter_access(request=request, matter_type="product_matter", matter_id=study.product_version.product_id, required="READ")
        require_matter_access(request=request, matter_type="patent", matter_id=study.patent_id, required="READ")
        return study


def _version_queryset(tenant_id, product_id):
    return ProductVersion.objects.for_tenant(tenant_id).filter(product__tenant_id=tenant_id, product_id=product_id).annotate(
        feature_count=Count("features", filter=Q(features__tenant_id=tenant_id), distinct=True),
        confirmed_feature_count=Count("features", filter=Q(features__tenant_id=tenant_id, features__review_status=ProductFeature.ReviewStatus.CONFIRMED), distinct=True),
    )


class ProductMatterListCreateView(FTOAPIView):
    def get(self, request: Request) -> Response:
        tenant_id = self.tenant(request).pk
        queryset = ProductMatter.objects.for_tenant(tenant_id).annotate(version_count=Count("versions", filter=Q(versions__tenant_id=tenant_id), distinct=True))
        queryset = filter_visible_matter_queryset(queryset, tenant=self.tenant(request), user=request.user, matter_type="product_matter")
        search = request.query_params.get("search", "").strip()[:100]
        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(product_code__icontains=search))
        return Response({"count": queryset.count(), "results": ProductMatterSerializer(queryset[:100], many=True).data})

    def post(self, request: Request) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _EDITOR_ROLES, "Your tenant role cannot create product matters.")
        serializer = ProductMatterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        matter = serializer.save(tenant=tenant, created_by=request.user)
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_PRODUCT_MATTER_CREATED", resource_type="ProductMatter", resource_id=matter.pk, metadata={"product_code_present": bool(matter.product_code)})
        return Response(ProductMatterSerializer(matter).data, status=status.HTTP_201_CREATED)


class ProductMatterDetailView(FTOAPIView):
    def get(self, request: Request, product_id) -> Response:
        matter = self.product(request, product_id)
        return Response(ProductMatterSerializer(matter).data)

    def patch(self, request: Request, product_id) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _EDITOR_ROLES, "Your tenant role cannot edit product matters.")
        matter = self.product(request, product_id)
        require_matter_access(request=request, matter_type="product_matter", matter_id=matter.pk, required="EDIT")
        serializer = ProductMatterSerializer(matter, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        matter = serializer.save()
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_PRODUCT_MATTER_UPDATED", resource_type="ProductMatter", resource_id=matter.pk, metadata={"fields": sorted(serializer.validated_data.keys())})
        return Response(ProductMatterSerializer(matter).data)


class ProductVersionListCreateView(FTOAPIView):
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request: Request, product_id) -> Response:
        matter = self.product(request, product_id)
        queryset = _version_queryset(self.tenant(request).pk, matter.pk).order_by("-version_number")
        count = queryset.count()
        versions = queryset[:100]
        return Response({"count": count, "results": ProductVersionSerializer(versions, many=True).data})

    def post(self, request: Request, product_id) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _EDITOR_ROLES, "Your tenant role cannot upload product specifications.")
        matter = self.product(request, product_id)
        require_matter_access(request=request, matter_type="product_matter", matter_id=matter.pk, required="EDIT")
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Upload a product specification PDF."})
        limit = int(getattr(settings, "PATENTSHIELD_FTO_MAX_UPLOAD_BYTES", _MAX_SPEC_BYTES))
        if upload.size > min(max(limit, 1024), 100 * 1024 * 1024):
            raise ValidationError({"file": "The product specification exceeds the configured upload size limit."})
        upload.seek(0)
        file_bytes = upload.read(limit + 1)
        upload.seek(0)
        if len(file_bytes) > limit:
            raise ValidationError({"file": "The product specification exceeds the configured upload size limit."})
        if b"%PDF-" not in file_bytes[:1024]:
            raise ValidationError({"file": "Only PDF product specifications are supported in this phase."})
        digest = hashlib.sha256(file_bytes).hexdigest()
        filename = _safe_filename(getattr(upload, "name", "product-specification.pdf"))
        storage_key = f"fto/{tenant.pk}/{matter.pk}/{uuid.uuid4().hex}.pdf"
        stored_key = default_storage.save(storage_key, ContentFile(file_bytes))
        try:
            with transaction.atomic():
                locked_matter = ProductMatter.objects.select_for_update().get(pk=matter.pk, tenant_id=tenant.pk)
                latest = ProductVersion.objects.filter(tenant_id=tenant.pk, product=locked_matter).order_by("-version_number").values_list("version_number", flat=True).first() or 0
                label = str(request.data.get("version_label", "")).strip()[:100]
                version = ProductVersion.objects.create(
                    tenant=tenant,
                    product=locked_matter,
                    version_number=latest + 1,
                    version_label=label,
                    source_filename=filename,
                    storage_key=stored_key,
                    content_sha256=digest,
                    mime_type="application/pdf",
                    created_by=request.user,
                )
                transaction.on_commit(lambda: _enqueue_product_version(version, request.user))
        except Exception:
            try:
                default_storage.delete(stored_key)
            except Exception:
                logger.exception("Could not clean up failed product specification upload.")
            raise
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_PRODUCT_SPEC_UPLOADED", resource_type="ProductVersion", resource_id=version.pk, metadata={"product_id": str(matter.pk), "content_sha256": digest, "file_size": len(file_bytes)})
        version.feature_count = 0
        version.confirmed_feature_count = 0
        return Response(ProductVersionSerializer(version).data, status=status.HTTP_201_CREATED)


class ProductVersionDetailView(FTOAPIView):
    def get(self, request: Request, version_id) -> Response:
        version = self.version(request, version_id)
        version = _version_queryset(self.tenant(request).pk, version.product_id).get(pk=version.pk)
        return Response(ProductVersionSerializer(version).data)


class ProductVersionFileView(FTOAPIView):
    def get(self, request: Request, version_id) -> FileResponse:
        version = self.version(request, version_id)
        try:
            handle = default_storage.open(version.storage_key, "rb")
        except (FileNotFoundError, OSError) as exc:
            raise NotFound("The product specification file is not available.") from exc
        response = FileResponse(handle, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{_safe_filename(version.source_filename).replace(chr(34), "_").replace(chr(13), "").replace(chr(10), "")}"'
        response["X-Content-Type-Options"] = "nosniff"
        return response


class ProductFeatureListCreateView(FTOAPIView):
    def get(self, request: Request, version_id) -> Response:
        version = self.version(request, version_id)
        queryset = ProductFeature.objects.for_tenant(version.tenant_id).filter(product_version=version, product_version__product__tenant_id=version.tenant_id)
        review_status = request.query_params.get("review_status")
        if review_status in ProductFeature.ReviewStatus.values:
            queryset = queryset.filter(review_status=review_status)
        count = queryset.count()
        results = ProductFeatureSerializer(queryset[:500], many=True).data
        return Response({"count": count, "results": results})

    def post(self, request: Request, version_id) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _EDITOR_ROLES, "Your tenant role cannot add product features.")
        version = self.version(request, version_id)
        require_matter_access(request=request, matter_type="product_matter", matter_id=version.product_id, required="EDIT")
        if version.extraction_status != ProductVersion.ExtractionStatus.EXTRACTED:
            raise ValidationError({"product_version": "Wait for product-specification extraction to complete before adding a feature."})
        serializer = ManualFeatureCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        pages = version.extracted_pages or []
        page_index = values["source_page_index"]
        if page_index >= len(pages):
            raise ValidationError({"source_page_index": "Page index is outside the extracted specification."})
        page_text = pages[page_index]
        start, end = values["source_start_offset"], values["source_end_offset"]
        if end > len(page_text):
            raise ValidationError({"source_end_offset": "Source offset exceeds the selected page's extracted text."})
        quote = page_text[start:end]
        if not quote.strip():
            raise ValidationError({"source_quote": "The selected source range is empty."})
        feature_key = hashlib.sha256(f"manual:{page_index}:{start}:{end}:{quote}".encode("utf-8")).hexdigest()
        feature, created = ProductFeature.objects.get_or_create(
            tenant=tenant,
            product_version=version,
            feature_key=feature_key,
            defaults={
                "title": values["title"],
                "description": values.get("description", ""),
                "source_quote": quote,
                "source_page_index": page_index,
                "source_start_offset": start,
                "source_end_offset": end,
                "extraction_method": "ATTORNEY_SELECTED_SOURCE_SPAN",
                "review_status": ProductFeature.ReviewStatus.CANDIDATE,
                "created_by": request.user,
            },
        )
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_PRODUCT_FEATURE_ADDED", resource_type="ProductFeature", resource_id=feature.pk, metadata={"product_version_id": str(version.pk), "created": created})
        return Response(ProductFeatureSerializer(feature).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ProductFeatureDetailView(FTOAPIView):
    def get(self, request: Request, feature_id) -> Response:
        tenant_id = self.tenant(request).pk
        feature = get_object_or_404(ProductFeature.objects.for_tenant(tenant_id).select_related("product_version").filter(product_version__tenant_id=tenant_id, product_version__product__tenant_id=tenant_id), pk=feature_id)
        require_matter_access(request=request, matter_type="product_matter", matter_id=feature.product_version.product_id, required="READ")
        return Response(ProductFeatureSerializer(feature).data)

    def patch(self, request: Request, feature_id) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _EDITOR_ROLES | _REVIEW_ROLES, "Your tenant role cannot review product features.")
        feature = get_object_or_404(ProductFeature.objects.for_tenant(tenant.pk).select_related("product_version"), pk=feature_id, product_version__tenant_id=tenant.pk, product_version__product__tenant_id=tenant.pk)
        required = "REVIEW" if "review_status" in request.data else "EDIT"
        require_matter_access(request=request, matter_type="product_matter", matter_id=feature.product_version.product_id, required=required)
        serializer = ProductFeatureReviewSerializer(feature, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        cited = LimitationEvidence.objects.for_tenant(tenant.pk).filter(feature=feature, assessment__tenant_id=tenant.pk).exists()
        if cited and any(field in serializer.validated_data for field in ("title", "description")):
            raise ValidationError({"title": "This feature is already cited by evidence. Its interpretation must remain stable; add a new candidate in a new product version if needed."})
        if "review_status" in serializer.validated_data and serializer.validated_data["review_status"] != feature.review_status:
            if cited:
                raise ValidationError({"review_status": "This feature is already cited by an assessment. Keep its review state stable to preserve evidence provenance; add a new candidate in a new product version if needed."})
        status_before = feature.review_status
        feature = serializer.save()
        if feature.review_status != status_before:
            feature.reviewed_by = request.user
            feature.reviewed_at = timezone.now()
            feature.save(update_fields=("reviewed_by", "reviewed_at", "updated_at"))
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_PRODUCT_FEATURE_REVIEWED", resource_type="ProductFeature", resource_id=feature.pk, metadata={"review_status": feature.review_status})
        return Response(ProductFeatureSerializer(feature).data)


class FTOStudyListCreateView(FTOAPIView):
    def get(self, request: Request) -> Response:
        tenant_id = self.tenant(request).pk
        queryset = FTOStudy.objects.for_tenant(tenant_id).filter(product_version__tenant_id=tenant_id, patent__tenant_id=tenant_id, patent_document_version__tenant_id=tenant_id)
        queryset = filter_visible_matter_queryset(queryset, tenant=self.tenant(request), user=request.user, matter_type="fto_study")
        queryset = filter_visible_matter_queryset(queryset, tenant=self.tenant(request), user=request.user, matter_type="product_matter", id_field="product_version__product_id")
        queryset = filter_visible_matter_queryset(queryset, tenant=self.tenant(request), user=request.user, matter_type="patent", id_field="patent_id")
        queryset = queryset.annotate(
            limitation_count=Count("limitations", filter=Q(limitations__tenant_id=tenant_id), distinct=True),
            reviewed_count=Count("assessments", filter=Q(assessments__tenant_id=tenant_id, assessments__review_status=LimitationAssessment.ReviewStatus.REVIEWED), distinct=True),
        ).select_related("product_version__product", "patent", "patent_document_version")
        status_filter = request.query_params.get("status")
        if status_filter in FTOStudy.Status.values:
            queryset = queryset.filter(status=status_filter)
        return Response({"count": queryset.count(), "results": FTOStudySerializer(queryset[:100], many=True).data})

    def post(self, request: Request) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _EDITOR_ROLES, "Your tenant role cannot create an FTO study.")
        payload = FTOStudyCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        product_version = get_object_or_404(ProductVersion.objects.for_tenant(tenant.pk).filter(product__tenant_id=tenant.pk), pk=values["product_version_id"])
        require_matter_access(request=request, matter_type="product_matter", matter_id=product_version.product_id, required="EDIT")
        if product_version.extraction_status != ProductVersion.ExtractionStatus.EXTRACTED:
            raise ValidationError({"product_version_id": "The selected product specification must finish text extraction before an FTO study can be created."})
        patent = get_object_or_404(Patent.objects.for_tenant(tenant.pk), pk=values["patent_id"])
        require_matter_access(request=request, matter_type="patent", matter_id=patent.pk, required="READ")
        selected_version_id = values.get("patent_document_version_id")
        if selected_version_id:
            patent_version = get_object_or_404(PatentDocumentVersion.objects.for_tenant(tenant.pk).filter(patent=patent, patent__tenant_id=tenant.pk), pk=selected_version_id)
        else:
            patent_version = PatentDocumentVersion.objects.for_tenant(tenant.pk).filter(patent=patent, patent__tenant_id=tenant.pk).order_by("-version_number").first()
            if patent_version is None:
                raise ValidationError({"patent_id": "The selected patent has no document version available for claim mapping."})
        claims = Claim.objects.for_tenant(tenant.pk).filter(
            patent=patent, document_version=patent_version,
            patent__tenant_id=tenant.pk, document_version__tenant_id=tenant.pk,
        ).order_by("claim_number")
        selected_claim_ids = list(dict.fromkeys(values.get("selected_claim_ids", [])))
        if selected_claim_ids:
            claims = claims.filter(pk__in=selected_claim_ids)
            if claims.count() != len(selected_claim_ids):
                raise ValidationError({"selected_claim_ids": "Every selected claim must belong to the chosen tenant, patent, and document version."})
        claims = list(claims)
        if not claims:
            raise ValidationError({"patent_id": "No claims were found in the selected patent version. Extract or add claims before creating a study."})
        if len(claims) > 100:
            raise ValidationError({"patent_id": "A single study is limited to 100 claims. Select a smaller claim set."})

        with transaction.atomic():
            study = FTOStudy.objects.create(
                tenant=tenant,
                title=(values.get("title") or f"{product_version.product.name} × {patent.publication_number or patent.title}")[:240],
                product_version=product_version,
                patent=patent,
                patent_document_version=patent_version,
                jurisdiction=(values.get("jurisdiction") or patent.jurisdiction or "")[:10].upper(),
                selected_claim_ids=[str(claim.pk) for claim in claims],
                status=FTOStudy.Status.IN_PROGRESS,
                created_by=request.user,
            )
            assessment_count = 0
            for claim in claims:
                segments = split_claim_limitations(claim.claim_text)
                for index, (start, end, text, method) in enumerate(segments, start=1):
                    limitation = StudyClaimLimitation.objects.create(
                        tenant=tenant,
                        study=study,
                        claim=claim,
                        limitation_index=index,
                        limitation_text=text,
                        source_start_offset=start,
                        source_end_offset=end,
                        segmentation_method=method,
                        needs_attorney_review=True,
                    )
                    LimitationAssessment.objects.create(
                        tenant=tenant,
                        study=study,
                        limitation=limitation,
                        triage_status=LimitationAssessment.TriageStatus.UNASSESSED,
                    )
                    assessment_count += 1
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_STUDY_CREATED", resource_type="FTOStudy", resource_id=study.pk, metadata={"product_version_id": str(product_version.pk), "patent_id": str(patent.pk), "patent_document_version_id": str(patent_version.pk), "claim_count": len(claims), "limitation_assessment_count": assessment_count})
        study = FTOStudy.objects.select_related("product_version__product", "patent", "patent_document_version").get(pk=study.pk)
        study.limitation_count = assessment_count
        study.reviewed_count = 0
        return Response(FTOStudySerializer(study).data, status=status.HTTP_201_CREATED)


class FTOStudyDetailView(FTOAPIView):
    def get(self, request: Request, study_id) -> Response:
        study = self.study(request, study_id)
        study.limitation_count = study.limitations.filter(tenant_id=study.tenant_id).count()
        study.reviewed_count = study.assessments.filter(tenant_id=study.tenant_id, review_status=LimitationAssessment.ReviewStatus.REVIEWED).count()
        return Response(FTOStudySerializer(study).data)

    def patch(self, request: Request, study_id) -> Response:
        tenant = self.tenant(request)
        study = self.study(request, study_id)
        new_status = request.data.get("status")
        require_matter_access(request=request, matter_type="fto_study", matter_id=study.pk, required="REVIEW" if new_status == FTOStudy.Status.REVIEWED else "EDIT")
        allowed = {FTOStudy.Status.DRAFT, FTOStudy.Status.IN_PROGRESS, FTOStudy.Status.READY_FOR_REVIEW, FTOStudy.Status.ARCHIVED}
        if new_status not in allowed and new_status != FTOStudy.Status.REVIEWED:
            raise ValidationError({"status": "Unsupported study status transition."})
        if new_status == FTOStudy.Status.REVIEWED:
            _require_role(request, _REVIEW_ROLES, "Your tenant role cannot mark an FTO study reviewed.")
            if study.created_by_id == request.user.pk:
                raise PermissionDenied("The study creator cannot mark their own study as reviewed.")
            if study.assessments.filter(tenant_id=tenant.pk).exclude(review_status=LimitationAssessment.ReviewStatus.REVIEWED).exists():
                raise ValidationError({"status": "Every limitation assessment must be reviewed before the study can be marked reviewed."})
            study.reviewed_by = request.user
            study.reviewed_at = timezone.now()
        else:
            _require_role(request, _EDITOR_ROLES, "Your tenant role cannot change study status.")
            # Any transition away from REVIEWED invalidates the prior overall sign-off.
            study.reviewed_by = None
            study.reviewed_at = None
            if new_status == FTOStudy.Status.READY_FOR_REVIEW and study.assessments.filter(
                tenant_id=tenant.pk,
                triage_status=LimitationAssessment.TriageStatus.UNASSESSED,
            ).exists():
                raise ValidationError({"status": "Triage every claim limitation before submitting this study for review."})
        study.status = new_status
        study.save(update_fields=("status", "reviewed_by", "reviewed_at", "updated_at"))
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_STUDY_STATUS_CHANGED", resource_type="FTOStudy", resource_id=study.pk, metadata={"status": study.status})
        return self.get(request, study_id)


class FTOStudyHeatmapView(FTOAPIView):
    def get(self, request: Request, study_id) -> Response:
        study = self.study(request, study_id)
        tenant_id = study.tenant_id
        assessments = LimitationAssessment.objects.for_tenant(tenant_id).filter(
            study=study,
            limitation__tenant_id=tenant_id,
            limitation__study_id=study.pk,
            limitation__claim__tenant_id=tenant_id,
            limitation__claim__patent_id=study.patent_id,
            limitation__claim__document_version_id=study.patent_document_version_id,
        ).select_related("limitation__claim").annotate(
            supporting_evidence_count=Count("evidence", filter=Q(evidence__tenant_id=tenant_id, evidence__kind=LimitationEvidence.Kind.SUPPORTING), distinct=True),
            counter_evidence_count=Count("evidence", filter=Q(evidence__tenant_id=tenant_id, evidence__kind=LimitationEvidence.Kind.COUNTER), distinct=True),
            context_evidence_count=Count("evidence", filter=Q(evidence__tenant_id=tenant_id, evidence__kind=LimitationEvidence.Kind.CONTEXT), distinct=True),
            annotation_count=Count("annotations", filter=Q(annotations__tenant_id=tenant_id), distinct=True),
        )
        serialized = LimitationAssessmentSerializer(assessments, many=True).data
        evidence_by_assessment: dict[str, list[dict]] = {}
        evidence_rows = LimitationEvidence.objects.for_tenant(tenant_id).filter(
            assessment__study=study,
            assessment__tenant_id=tenant_id,
            feature__tenant_id=tenant_id,
            feature__product_version_id=study.product_version_id,
        ).select_related("feature", "created_by").order_by("created_at")
        for evidence_row in evidence_rows:
            evidence_by_assessment.setdefault(str(evidence_row.assessment_id), []).append(
                LimitationEvidenceSerializer(evidence_row).data
            )
        annotations_by_assessment: dict[str, list[dict]] = {}
        annotation_rows = AttorneyAnnotation.objects.for_tenant(tenant_id).filter(
            assessment__study=study,
            assessment__tenant_id=tenant_id,
            assessment__limitation__tenant_id=tenant_id,
            assessment__limitation__claim__tenant_id=tenant_id,
        ).select_related("created_by").order_by("resolved", "created_at")
        for annotation_row in annotation_rows:
            annotations_by_assessment.setdefault(str(annotation_row.assessment_id), []).append(
                AttorneyAnnotationSerializer(annotation_row).data
            )
        grouped: dict[str, dict] = {}
        for row in serialized:
            limitation = row["limitation"]
            claim_number = limitation["claim_number"]
            claim = grouped.setdefault(claim_number, {"claim_number": claim_number, "claim_id": limitation["claim_id"], "limitations": []})
            claim["limitations"].append({
                "assessment_id": row["id"],
                "limitation_id": limitation["id"],
                "limitation_index": limitation["limitation_index"],
                "text": limitation["limitation_text"],
                "triage_status": row["triage_status"],
                "heatmap_band": row["heatmap_band"],
                "review_status": row["review_status"],
                "supporting_evidence_count": row["supporting_evidence_count"],
                "counter_evidence_count": row["counter_evidence_count"],
                "context_evidence_count": row["context_evidence_count"],
                "annotation_count": row["annotation_count"],
                "triage_rationale": row["triage_rationale"],
                "evidence": evidence_by_assessment.get(str(row["id"]), []),
                "annotations": annotations_by_assessment.get(str(row["id"]), []),
            })
        return Response({
            "study": FTOStudySerializer(study).data,
            "disclaimer": "Heatmap bands prioritize attorney review only. They are not findings of infringement, validity, or freedom to operate; 'no feature identified' is not proof that a feature is absent from the product.",
            "risk_bands": {"HIGH_ATTENTION": "Potential correspondence or conflicting source evidence; prioritize review.", "MODERATE_ATTENTION": "Partial correspondence or counter-evidence requires context.", "ELEVATED_ATTENTION": "Uncertainty requires further evidence or specialist review.", "LOWER_ATTENTION": "No feature identified in the reviewed specification scope; this does not establish product absence.", "UNASSESSED": "No attorney triage status has been selected."},
            "claims": list(grouped.values()),
        })


class LimitationAssessmentUpdateView(FTOAPIView):
    def patch(self, request: Request, study_id, limitation_id) -> Response:
        tenant = self.tenant(request)
        study = self.study(request, study_id)
        assessment = get_object_or_404(
            LimitationAssessment.objects.for_tenant(tenant.pk)
            .filter(
                study=study,
                study__tenant_id=tenant.pk,
                study__product_version__tenant_id=tenant.pk,
                study__patent__tenant_id=tenant.pk,
                limitation__tenant_id=tenant.pk,
                limitation__study_id=study.pk,
                limitation__claim__tenant_id=tenant.pk,
                limitation__claim__patent_id=study.patent_id,
                limitation__claim__document_version_id=study.patent_document_version_id,
            ).select_related("study", "limitation"),
            limitation_id=limitation_id,
        )
        serializer = LimitationAssessmentSerializer(assessment, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        validated = serializer.validated_data
        require_matter_access(request=request, matter_type="fto_study", matter_id=study.pk, required="REVIEW" if "review_status" in validated else "EDIT")
        triage_changed = (
            ("triage_status" in validated and validated["triage_status"] != assessment.triage_status)
            or ("triage_rationale" in validated and validated["triage_rationale"] != assessment.triage_rationale)
        )
        if triage_changed and "review_status" in validated:
            raise ValidationError({"review_status": "Save the triage decision first, then submit a separate review-state action."})
        if validated.get("review_status") == LimitationAssessment.ReviewStatus.REVIEWED:
            _require_role(request, _REVIEW_ROLES, "Your tenant role cannot mark a limitation reviewed.")
            if study.created_by_id == request.user.pk:
                raise PermissionDenied("The study creator cannot independently review their own study.")
            assessment.reviewed_by = request.user
            assessment.reviewed_at = timezone.now()
        elif "review_status" in validated:
            _require_role(request, _REVIEW_ROLES, "Your tenant role cannot change review state.")
            assessment.reviewed_by = None
            assessment.reviewed_at = None
        else:
            _require_role(request, _EDITOR_ROLES, "Your tenant role cannot update limitation triage.")
        assessment.triage_status = validated.get("triage_status", assessment.triage_status)
        assessment.triage_rationale = validated.get("triage_rationale", assessment.triage_rationale)
        assessment.review_status = validated.get("review_status", assessment.review_status)
        if triage_changed and "review_status" not in validated:
            assessment.review_status = LimitationAssessment.ReviewStatus.UNREVIEWED
            assessment.reviewed_by = None
            assessment.reviewed_at = None
        assessment.updated_by = request.user
        assessment.save(update_fields=("triage_status", "triage_rationale", "review_status", "reviewed_by", "reviewed_at", "updated_by", "updated_at"))
        if triage_changed and "review_status" not in validated:
            FTOStudy.objects.filter(pk=study.pk, tenant_id=tenant.pk, status=FTOStudy.Status.REVIEWED).update(
                status=FTOStudy.Status.IN_PROGRESS,
                reviewed_by=None,
                reviewed_at=None,
            )
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_LIMITATION_ASSESSED", resource_type="LimitationAssessment", resource_id=assessment.pk, metadata={"triage_status": assessment.triage_status, "review_status": assessment.review_status})
        assessment = LimitationAssessment.objects.select_related("study", "limitation__claim").get(pk=assessment.pk)
        return Response(LimitationAssessmentSerializer(assessment).data)


class LimitationEvidenceListCreateView(FTOAPIView):
    def get(self, request: Request, study_id, limitation_id) -> Response:
        tenant_id = self.tenant(request).pk
        study = self.study(request, study_id)
        assessment = get_object_or_404(LimitationAssessment.objects.for_tenant(tenant_id).filter(study=study, limitation_id=limitation_id, limitation__tenant_id=tenant_id))
        evidence = LimitationEvidence.objects.for_tenant(tenant_id).filter(assessment=assessment, assessment__study_id=study.pk, feature__tenant_id=tenant_id, feature__product_version_id=study.product_version_id).select_related("feature", "created_by")
        return Response({"count": evidence.count(), "results": LimitationEvidenceSerializer(evidence, many=True).data})

    def post(self, request: Request, study_id, limitation_id) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _EDITOR_ROLES | _REVIEW_ROLES, "Your tenant role cannot add evidence to this assessment.")
        study = self.study(request, study_id)
        require_matter_access(request=request, matter_type="fto_study", matter_id=study.pk, required="EDIT")
        assessment = get_object_or_404(LimitationAssessment.objects.for_tenant(tenant.pk).filter(study=study, limitation_id=limitation_id, limitation__tenant_id=tenant.pk))
        payload = LimitationEvidenceCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        feature = get_object_or_404(ProductFeature.objects.for_tenant(tenant.pk).filter(product_version_id=study.product_version_id, product_version__tenant_id=tenant.pk, product_version__product__tenant_id=tenant.pk), pk=values["feature_id"])
        if feature.review_status != ProductFeature.ReviewStatus.CONFIRMED:
            raise ValidationError({"feature_id": "Only attorney-confirmed product features can be linked as evidence."})
        evidence, created = LimitationEvidence.objects.get_or_create(
            tenant=tenant,
            assessment=assessment,
            feature=feature,
            kind=values["kind"],
            defaults={
                "source_quote": feature.source_quote,
                "source_page_index": feature.source_page_index,
                "source_start_offset": feature.source_start_offset,
                "source_end_offset": feature.source_end_offset,
                "attorney_rationale": values.get("attorney_rationale", ""),
                "created_by": request.user,
            },
        )
        if created:
            # New evidence invalidates any previous limitation/study review state.
            LimitationAssessment.objects.filter(pk=assessment.pk, tenant_id=tenant.pk).update(
                review_status=LimitationAssessment.ReviewStatus.UNREVIEWED,
                reviewed_by=None,
                reviewed_at=None,
                updated_by=request.user,
            )
            FTOStudy.objects.filter(pk=study.pk, tenant_id=tenant.pk, status=FTOStudy.Status.REVIEWED).update(
                status=FTOStudy.Status.IN_PROGRESS,
                reviewed_by=None,
                reviewed_at=None,
            )
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_LIMITATION_EVIDENCE_LINKED", resource_type="LimitationEvidence", resource_id=evidence.pk, metadata={"assessment_id": str(assessment.pk), "feature_id": str(feature.pk), "kind": evidence.kind, "created": created})
        return Response(LimitationEvidenceSerializer(evidence).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class AttorneyAnnotationListCreateView(FTOAPIView):
    def get(self, request: Request, study_id) -> Response:
        tenant_id = self.tenant(request).pk
        study = self.study(request, study_id)
        annotations = AttorneyAnnotation.objects.for_tenant(tenant_id).filter(
            assessment__study=study,
            assessment__tenant_id=tenant_id,
            assessment__study__product_version__tenant_id=tenant_id,
            assessment__study__patent__tenant_id=tenant_id,
            assessment__limitation__tenant_id=tenant_id,
            assessment__limitation__claim__tenant_id=tenant_id,
        ).select_related("created_by", "assessment")
        assessment_id = request.query_params.get("assessment_id")
        if assessment_id:
            annotations = annotations.filter(assessment_id=assessment_id)
        return Response({"count": annotations.count(), "results": AttorneyAnnotationSerializer(annotations[:500], many=True).data})

    def post(self, request: Request, study_id) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _EDITOR_ROLES | _REVIEW_ROLES, "Your tenant role cannot create attorney annotations.")
        study = self.study(request, study_id)
        require_matter_access(request=request, matter_type="fto_study", matter_id=study.pk, required="EDIT")
        payload = AttorneyAnnotationCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        assessment = get_object_or_404(LimitationAssessment.objects.for_tenant(tenant.pk).filter(study=study, study__tenant_id=tenant.pk), pk=values["assessment_id"])
        annotation = AttorneyAnnotation.objects.create(tenant=tenant, assessment=assessment, category=values["category"], body=values["body"], created_by=request.user)
        LimitationAssessment.objects.filter(pk=assessment.pk, tenant_id=tenant.pk, review_status=LimitationAssessment.ReviewStatus.REVIEWED).update(
            review_status=LimitationAssessment.ReviewStatus.IN_REVIEW,
            reviewed_by=None,
            reviewed_at=None,
            updated_by=request.user,
        )
        FTOStudy.objects.filter(pk=study.pk, tenant_id=tenant.pk, status=FTOStudy.Status.REVIEWED).update(
            status=FTOStudy.Status.IN_PROGRESS,
            reviewed_by=None,
            reviewed_at=None,
        )
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_ATTORNEY_ANNOTATION_ADDED", resource_type="AttorneyAnnotation", resource_id=annotation.pk, metadata={"assessment_id": str(assessment.pk), "category": annotation.category})
        return Response(AttorneyAnnotationSerializer(annotation).data, status=status.HTTP_201_CREATED)


class AttorneyAnnotationResolveView(FTOAPIView):
    def post(self, request: Request, annotation_id) -> Response:
        tenant = self.tenant(request)
        _require_role(request, _REVIEW_ROLES, "Your tenant role cannot resolve attorney annotations.")
        annotation = get_object_or_404(AttorneyAnnotation.objects.for_tenant(tenant.pk).filter(
            assessment__tenant_id=tenant.pk,
            assessment__study__tenant_id=tenant.pk,
            assessment__study__product_version__tenant_id=tenant.pk,
            assessment__study__patent__tenant_id=tenant.pk,
            assessment__limitation__tenant_id=tenant.pk,
            assessment__limitation__claim__tenant_id=tenant.pk,
        ), pk=annotation_id)
        require_matter_access(request=request, matter_type="fto_study", matter_id=annotation.assessment.study_id, required="REVIEW")
        resolved = request.data.get("resolved", True)
        if not isinstance(resolved, bool):
            raise ValidationError({"resolved": "resolved must be a boolean."})
        state_changed = annotation.resolved != resolved
        annotation.resolved = resolved
        annotation.resolved_by = request.user if resolved else None
        annotation.resolved_at = timezone.now() if resolved else None
        annotation.save(update_fields=("resolved", "resolved_by", "resolved_at"))
        if state_changed:
            LimitationAssessment.objects.filter(
                pk=annotation.assessment_id,
                tenant_id=tenant.pk,
                review_status=LimitationAssessment.ReviewStatus.REVIEWED,
            ).update(
                review_status=LimitationAssessment.ReviewStatus.IN_REVIEW,
                reviewed_by=None,
                reviewed_at=None,
                updated_by=request.user,
                updated_at=timezone.now(),
            )
            FTOStudy.objects.filter(
                pk=annotation.assessment.study_id,
                tenant_id=tenant.pk,
                status=FTOStudy.Status.REVIEWED,
            ).update(status=FTOStudy.Status.IN_PROGRESS, reviewed_by=None, reviewed_at=None, updated_at=timezone.now())
        audit_fto_event(tenant=tenant, actor=request.user, action="FTO_ATTORNEY_ANNOTATION_RESOLVED" if resolved else "FTO_ATTORNEY_ANNOTATION_REOPENED", resource_type="AttorneyAnnotation", resource_id=annotation.pk, metadata={"resolved": resolved})
        return Response(AttorneyAnnotationSerializer(annotation).data)
