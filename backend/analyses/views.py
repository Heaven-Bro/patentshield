"""Reviewer decision API for tenant-scoped patent claim assessments."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import AnalysisReport, ClaimAssessment, EvidenceReference
from analyses.tasks import run_patent_analysis
from enterprise_identity.services import filter_visible_matter_queryset, require_matter_access
from audit.models import AuditLog
from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.models import TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)


class AnalysisCreateRequestSerializer(serializers.Serializer):
    """Validate the selected tenant-owned patent, version, and claim."""

    patent_id = serializers.UUIDField()
    document_version_id = serializers.UUIDField()
    claim_id = serializers.UUIDField()
    analysis_type = serializers.ChoiceField(
        choices=(("PRIOR_ART_OVERLAP", "Prior-art overlap"),),
        default="PRIOR_ART_OVERLAP",
    )


class AnalysisListCreateView(APIView):
    """List tenant analyses and enqueue a new claim-level analysis run."""

    permission_classes = [IsAuthenticated, HasTenantAccess]

    def get_permissions(self) -> list[Any]:
        """Require an analysis-capable role for creation, not for listing."""
        permissions = [IsAuthenticated(), HasTenantAccess()]
        if self.request.method == "POST":
            role_permission = RequireTenantRole(
                TenantMembership.Role.OWNER,
                TenantMembership.Role.ADMIN,
                TenantMembership.Role.ATTORNEY,
                TenantMembership.Role.ANALYST,
            )
            permissions.append(role_permission())
        return permissions

    def get(self, request: Request) -> Response:
        """Return recent analyses whose target relations remain tenant-safe."""
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (TypeError, ValueError):
            return Response(
                {"detail": "limit must be an integer between 1 and 50."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not 1 <= limit <= 50:
            return Response(
                {"detail": "limit must be an integer between 1 and 50."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        analyses = (
            AnalysisReport.objects.for_tenant(tenant.pk)
            .filter(
                patent__tenant_id=tenant.pk,
                target_document_version__tenant_id=tenant.pk,
                target_document_version__patent_id=F("patent_id"),
            )
            .select_related("patent", "target_document_version", "target_claim")
            .order_by("-created_at")
        )
        analyses = filter_visible_matter_queryset(analyses, tenant=tenant, user=request.user, matter_type="analysis")
        analyses = filter_visible_matter_queryset(analyses, tenant=tenant, user=request.user, matter_type="patent", id_field="patent_id")
        analyses = analyses[:limit]
        return Response({"results": [_serialize_analysis(item) for item in analyses]})

    def post(self, request: Request) -> Response:
        """Create a queued analysis and dispatch it to the Celery analysis queue."""
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")

        payload = AnalysisCreateRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        values = payload.validated_data

        try:
            patent = Patent.objects.for_tenant(tenant.pk).get(pk=values["patent_id"])
            version = (
                PatentDocumentVersion.objects.for_tenant(tenant.pk)
                .select_related("patent")
                .get(
                    pk=values["document_version_id"],
                    patent_id=patent.pk,
                )
            )
            claim = (
                Claim.objects.for_tenant(tenant.pk)
                .select_related("patent", "document_version")
                .get(
                    pk=values["claim_id"],
                    patent_id=patent.pk,
                    document_version_id=version.pk,
                )
            )
        except (
            Patent.DoesNotExist,
            PatentDocumentVersion.DoesNotExist,
            Claim.DoesNotExist,
        ) as exc:
            raise NotFound(
                "The selected patent, version, or claim was not found."
            ) from exc

        require_matter_access(request=request, matter_type="patent", matter_id=patent.pk, required="EDIT")
        if (
            patent.tenant_id != tenant.pk
            or version.tenant_id != tenant.pk
            or version.patent_id != patent.pk
            or claim.tenant_id != tenant.pk
            or claim.patent_id != patent.pk
            or claim.document_version_id != version.pk
        ):
            raise NotFound("The selected patent, version, or claim was not found.")

        if (
            version.extraction_status
            != PatentDocumentVersion.ExtractionStatus.EXTRACTED
            or patent.status != Patent.Status.INDEXED
        ):
            return Response(
                {
                    "detail": (
                        "The selected document version must finish extraction and "
                        "text indexing before analysis can start."
                    ),
                    "patent_status": patent.status,
                    "extraction_status": version.extraction_status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            analysis = AnalysisReport.objects.create(
                tenant_id=tenant.pk,
                patent=patent,
                target_document_version=version,
                target_claim=claim,
                analysis_type=values["analysis_type"],
                configuration={"target_claim_number": claim.claim_number},
                created_by=request.user,
                status=AnalysisReport.Status.QUEUED,
            )
            AuditLog.objects.create(
                tenant_id=tenant.pk,
                actor_user_id=request.user.pk,
                action="CREATE_PATENT_ANALYSIS",
                resource_type="AnalysisReport",
                resource_id=analysis.pk,
                request_id=_request_id(request),
                outcome=AuditLog.Outcome.SUCCESS,
                metadata={
                    "patent_id": str(patent.pk),
                    "document_version_id": str(version.pk),
                    "claim_id": str(claim.pk),
                    "analysis_type": analysis.analysis_type,
                },
            )

        try:
            run_patent_analysis.apply_async(
                args=[str(analysis.pk)],
                queue="analysis",
            )
        except Exception as exc:
            # The DB transaction has committed. Persist a clear retryable failure
            # rather than returning success when no Celery message was accepted.
            logger.error(
                "Analysis dispatch failed for analysis_id=%s error_type=%s.",
                analysis.pk,
                type(exc).__name__,
            )
            with transaction.atomic():
                analysis.status = AnalysisReport.Status.FAILED
                analysis.error_code = "QUEUE_SUBMISSION_FAILED"
                analysis.completed_at = timezone.now()
                analysis.execution_task_id = None
                analysis.save(
                    update_fields=(
                        "status",
                        "error_code",
                        "completed_at",
                        "execution_task_id",
                        "updated_at",
                    )
                )
                AuditLog.objects.create(
                    tenant_id=tenant.pk,
                    actor_user_id=request.user.pk,
                    action="QUEUE_PATENT_ANALYSIS",
                    resource_type="AnalysisReport",
                    resource_id=analysis.pk,
                    request_id=_request_id(request),
                    outcome=AuditLog.Outcome.FAILURE,
                    metadata={"error_type": type(exc).__name__},
                )
            return Response(
                {
                    "detail": (
                        "Analysis could not be queued. "
                        "You may retry the analysis."
                    ),
                    "analysis": _serialize_analysis(analysis),
                    "retry_url": f"/api/v1/analyses/{analysis.pk}/retry/",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "analysis": _serialize_analysis(analysis),
                "progress_ws_path": f"/ws/v1/analyses/{analysis.pk}/",
                "detail": "Analysis queued.",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class AnalysisRetryView(APIView):
    """Requeue a previously failed analysis without changing its target claim."""

    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole(
            TenantMembership.Role.OWNER,
            TenantMembership.Role.ADMIN,
            TenantMembership.Role.ATTORNEY,
            TenantMembership.Role.ANALYST,
        ),
    ]

    def post(self, request: Request, analysis_id: UUID) -> Response:
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")
        try:
            with transaction.atomic():
                analysis = (
                    AnalysisReport.objects.for_tenant(tenant.pk)
                    .select_for_update()
                    .select_related("patent", "target_document_version", "target_claim")
                    .get(pk=analysis_id)
                )
                if (
                    analysis.patent.tenant_id != tenant.pk
                    or analysis.target_document_version.tenant_id != tenant.pk
                    or analysis.target_document_version.patent_id != analysis.patent_id
                ):
                    raise NotFound("Analysis was not found.")
                require_matter_access(request=request, matter_type="analysis", matter_id=analysis.pk, required="EDIT")
                require_matter_access(request=request, matter_type="patent", matter_id=analysis.patent_id, required="EDIT")
                if analysis.status != AnalysisReport.Status.FAILED:
                    return Response(
                        {
                            "detail": "Only failed analyses can be retried.",
                            "status": analysis.status,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                analysis.status = AnalysisReport.Status.QUEUED
                analysis.error_code = None
                analysis.completed_at = None
                analysis.execution_task_id = None
                analysis.save(
                    update_fields=(
                        "status",
                        "error_code",
                        "completed_at",
                        "execution_task_id",
                        "updated_at",
                    )
                )
                AuditLog.objects.create(
                    tenant_id=tenant.pk,
                    actor_user_id=request.user.pk,
                    action="RETRY_PATENT_ANALYSIS",
                    resource_type="AnalysisReport",
                    resource_id=analysis.pk,
                    request_id=_request_id(request),
                    outcome=AuditLog.Outcome.SUCCESS,
                    metadata={"analysis_id": str(analysis.pk)},
                )
        except AnalysisReport.DoesNotExist as exc:
            raise NotFound("Analysis was not found.") from exc

        try:
            run_patent_analysis.apply_async(args=[str(analysis.pk)], queue="analysis")
        except Exception as exc:
            logger.error(
                "Analysis retry dispatch failed for analysis_id=%s error_type=%s.",
                analysis.pk,
                type(exc).__name__,
            )
            with transaction.atomic():
                analysis.status = AnalysisReport.Status.FAILED
                analysis.error_code = "QUEUE_SUBMISSION_FAILED"
                analysis.completed_at = timezone.now()
                analysis.execution_task_id = None
                analysis.save(
                    update_fields=(
                        "status",
                        "error_code",
                        "completed_at",
                        "execution_task_id",
                        "updated_at",
                    )
                )
                AuditLog.objects.create(
                    tenant_id=tenant.pk,
                    actor_user_id=request.user.pk,
                    action="QUEUE_PATENT_ANALYSIS_RETRY",
                    resource_type="AnalysisReport",
                    resource_id=analysis.pk,
                    request_id=_request_id(request),
                    outcome=AuditLog.Outcome.FAILURE,
                    metadata={"error_type": type(exc).__name__},
                )
            return Response(
                {
                    "detail": "Analysis retry could not be queued.",
                    "analysis": _serialize_analysis(analysis),
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "analysis": _serialize_analysis(analysis),
                "progress_ws_path": f"/ws/v1/analyses/{analysis.pk}/",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class AnalysisReviewRequestSerializer(serializers.Serializer):
    """Validate one review decision and its accompanying notes."""

    assessment_id = serializers.UUIDField()
    review_status = serializers.ChoiceField(
        choices=(
            ClaimAssessment.ReviewStatus.ACCEPTED,
            ClaimAssessment.ReviewStatus.CORRECTED,
            ClaimAssessment.ReviewStatus.REJECTED,
        )
    )
    reviewer_notes = serializers.CharField(
        max_length=10000,
        allow_blank=True,
        trim_whitespace=False,
    )


class AnalysisReviewView(APIView):
    """Record an explicit reviewer decision for an analysis assessment."""

    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole(
            "OWNER",
            "ADMIN",
            "ATTORNEY",
            "ANALYST",
            "REVIEWER",
        ),
    ]

    def post(self, request: Request, analysis_id: UUID) -> Response:
        """Validate the assessment belongs to this analysis and tenant."""
        payload = AnalysisReviewRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        validated = payload.validated_data

        tenant = getattr(request, "tenant", None)
        membership = getattr(request, "tenant_membership", None)
        if tenant is None or membership is None:
            raise PermissionDenied("Tenant context missing.")
        if membership.status != TenantMembership.Status.ACTIVE:
            raise PermissionDenied("An active tenant membership is required.")

        try:
            analysis = (
                AnalysisReport.objects.for_tenant(tenant.pk)
                .select_related("patent", "target_document_version", "target_claim")
                .get(pk=analysis_id)
            )
        except AnalysisReport.DoesNotExist as exc:
            raise NotFound("Analysis was not found.") from exc

        if (
            analysis.patent.tenant_id != tenant.pk
            or analysis.target_document_version.tenant_id != tenant.pk
            or analysis.target_document_version.patent_id != analysis.patent_id
        ):
            raise PermissionDenied(
                "Analysis provenance does not match tenant context."
            )
        require_matter_access(request=request, matter_type="analysis", matter_id=analysis.pk, required="REVIEW")
        require_matter_access(request=request, matter_type="patent", matter_id=analysis.patent_id, required="READ")

        try:
            with transaction.atomic():
                assessment = (
                    ClaimAssessment.objects.for_tenant(tenant.pk)
                    .select_for_update()
                    .select_related("analysis", "claim", "claim__patent")
                    .get(
                        pk=validated["assessment_id"],
                        analysis_id=analysis.pk,
                    )
                )
                if (
                    assessment.analysis.tenant_id != tenant.pk
                    or assessment.claim.tenant_id != tenant.pk
                    or assessment.claim.patent_id != analysis.patent_id
                    or assessment.claim.patent.tenant_id != tenant.pk
                ):
                    # This should be impossible with well-formed data, but
                    # re-check FK tenant consistency before changing anything.
                    raise PermissionDenied(
                        "Assessment provenance does not match tenant context."
                    )

                assessment.review_status = validated["review_status"]
                assessment.reviewer_notes = validated["reviewer_notes"]
                assessment.reviewed_by = request.user
                assessment.reviewed_at = timezone.now()
                assessment.save(
                    update_fields=(
                        "review_status",
                        "reviewer_notes",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    )
                )

                AuditLog.objects.create(
                    tenant_id=tenant.pk,
                    actor_user_id=request.user.pk,
                    action="REVIEW_CLAIM_ASSESSMENT",
                    resource_type="ClaimAssessment",
                    resource_id=assessment.pk,
                    request_id=_request_id(request),
                    outcome=AuditLog.Outcome.SUCCESS,
                    metadata={
                        "analysis_id": str(analysis.pk),
                        "review_status": assessment.review_status,
                    },
                )
        except ClaimAssessment.DoesNotExist as exc:
            raise NotFound(
                "Assessment was not found in the requested analysis."
            ) from exc

        return Response(
            {
                "analysis_id": str(analysis.pk),
                "assessment_id": str(assessment.pk),
                "review_status": assessment.review_status,
                "reviewer_notes": assessment.reviewer_notes,
                "reviewed_at": assessment.reviewed_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class AnalysisDetailView(APIView):
    """Return a tenant-safe analysis payload shaped for the React workspace."""

    permission_classes = [IsAuthenticated, HasTenantAccess]

    def get(self, request: Request, analysis_id: UUID) -> Response:
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")

        try:
            analysis = (
                AnalysisReport.objects.for_tenant(tenant.pk)
                .select_related("patent", "target_document_version", "target_claim")
                .get(pk=analysis_id)
            )
        except AnalysisReport.DoesNotExist as exc:
            raise NotFound("Analysis was not found.") from exc

        if (
            analysis.patent.tenant_id != tenant.pk
            or analysis.target_document_version.tenant_id != tenant.pk
            or analysis.target_document_version.patent_id != analysis.patent_id
        ):
            raise PermissionDenied("Analysis provenance does not match tenant context.")

        assessments = list(
            ClaimAssessment.objects.for_tenant(tenant.pk)
            .filter(analysis=analysis)
            .select_related("claim")
            .order_by("limitation_index")
        )
        claim = analysis.target_claim or (
            assessments[0].claim if assessments else (
                Claim.objects.for_tenant(tenant.pk)
                .filter(
                    patent_id=analysis.patent_id,
                    document_version_id=analysis.target_document_version_id,
                )
                .order_by("claim_number")
                .first()
            )
        )
        require_matter_access(request=request, matter_type="analysis", matter_id=analysis.pk, required="READ")
        require_matter_access(request=request, matter_type="patent", matter_id=analysis.patent_id, required="READ")

        if claim is not None and (
            claim.tenant_id != tenant.pk
            or claim.patent_id != analysis.patent_id
            or claim.document_version_id != analysis.target_document_version_id
        ):
            raise PermissionDenied(
                "Target claim provenance does not match tenant context."
            )

        claim_assessments = [
            item for item in assessments if claim and item.claim_id == claim.pk
        ]
        evidence_rows = list(
            EvidenceReference.objects.for_tenant(tenant.pk)
            .filter(assessment_id__in=[item.pk for item in claim_assessments])
            .select_related("source_patent", "source_document_version", "assessment")
            .order_by("source_page_index", "created_at")
        )
        evidence_by_assessment: dict[str, list[dict[str, Any]]] = {}
        for evidence in evidence_rows:
            if evidence.assessment.tenant_id != tenant.pk:
                raise PermissionDenied(
                    "Evidence provenance does not match tenant context."
                )
            if (
                evidence.source_patent_id
                and evidence.source_patent.tenant_id != tenant.pk
            ):
                raise PermissionDenied(
                    "Evidence source belongs to a different tenant."
                )
            if (
                evidence.source_document_version_id
                and evidence.source_document_version.tenant_id != tenant.pk
            ):
                raise PermissionDenied(
                    "Evidence version belongs to a different tenant."
                )
            if (
                evidence.source_patent_id
                and evidence.source_document_version_id
                and evidence.source_document_version.patent_id
                != evidence.source_patent_id
            ):
                raise PermissionDenied(
                    "Evidence patent and document-version references conflict."
                )
            retrieval_metadata = evidence.retrieval_metadata or {}
            evidence_by_assessment.setdefault(str(evidence.assessment_id), []).append(
                {
                    "evidence_id": str(evidence.pk),
                    "chunk_id": retrieval_metadata.get("chunk_id"),
                    "source_patent_id": (
                        str(evidence.source_patent_id)
                        if evidence.source_patent_id
                        else None
                    ),
                    "source_version_id": (
                        str(evidence.source_document_version_id)
                        if evidence.source_document_version_id else None
                    ),
                    "page_index": evidence.source_page_index,
                    "section_type": evidence.source_section or "",
                    "exact_quote": evidence.exact_quote,
                    "quote_verified": evidence.quote_verified,
                    "bbox": evidence.bbox,
                }
            )

        serialized_assessments = [
            {
                "assessment_id": str(item.pk),
                "limitation_id": str(item.pk),
                "limitation_index": item.limitation_index,
                "limitation_text": item.limitation_text,
                "text": item.limitation_text,
                "status": item.assessment_status,
                "explanation": item.explanation,
                "confidence": item.confidence,
                "quote_verified": all(
                    row["quote_verified"]
                    for row in evidence_by_assessment.get(str(item.pk), [])
                ) if evidence_by_assessment.get(str(item.pk)) else False,
                "evidence": evidence_by_assessment.get(str(item.pk), []),
                "metrics": item.metrics,
                "review_status": item.review_status,
            }
            for item in claim_assessments
        ]
        limitations = [
            {
                "limitation_id": str(item.pk),
                "limitation_index": item.limitation_index,
                "text": item.limitation_text,
            }
            for item in claim_assessments
        ]
        summary = (
            analysis.result_summary
            if isinstance(analysis.result_summary, dict)
            else {}
        )
        metrics = summary.get("metrics", summary.get("screening_metrics", {}))
        if not isinstance(metrics, dict):
            metrics = {}

        return Response(
            {
                "analysis": {
                    "id": str(analysis.pk),
                    "status": analysis.status,
                    "patent_id": str(analysis.patent_id),
                    "target_document_version_id": str(
                        analysis.target_document_version_id
                    ),
                    "analysis_type": analysis.analysis_type,
                    "created_at": analysis.created_at.isoformat(),
                    "completed_at": analysis.completed_at.isoformat()
                    if analysis.completed_at else None,
                    "report_export_status": analysis.report_export_status,
                    "report_export_error_code": analysis.report_export_error_code,
                    "report_generated_at": (
                        analysis.report_generated_at.isoformat()
                        if analysis.report_generated_at else None
                    ),
                    "report_available": {
                        "pdf": (
                            analysis.report_export_status
                            == AnalysisReport.ReportExportStatus.COMPLETED
                            and bool(analysis.report_storage_key)
                        ),
                        "json": (
                            analysis.report_export_status
                            == AnalysisReport.ReportExportStatus.COMPLETED
                            and bool(analysis.report_json_storage_key)
                        ),
                    },
                },
                "selectedClaim": {
                    "id": str(claim.pk) if claim else None,
                    "claim_number": claim.claim_number if claim else None,
                    "claim_text": claim.claim_text if claim else "",
                    "limitations": limitations,
                } if claim else None,
                "assessments": serialized_assessments,
                "screeningMetrics": metrics,
            }
        )

def _serialize_analysis(analysis: AnalysisReport) -> dict[str, Any]:
    """Serialize an analysis run without including internal storage keys."""
    summary = (
        analysis.result_summary
        if isinstance(analysis.result_summary, dict)
        else {}
    )
    metrics = summary.get("metrics", {})
    return {
        "id": str(analysis.pk),
        "status": analysis.status,
        "analysis_type": analysis.analysis_type,
        "patent_id": str(analysis.patent_id),
        "patent_title": analysis.patent.title,
        "document_version_id": str(analysis.target_document_version_id),
        "claim_id": str(analysis.target_claim_id) if analysis.target_claim_id else None,
        "claim_number": (
            analysis.target_claim.claim_number
            if analysis.target_claim_id
            else None
        ),
        "metrics": metrics if isinstance(metrics, dict) else {},
        "created_at": analysis.created_at.isoformat(),
        "started_at": (
            analysis.started_at.isoformat() if analysis.started_at else None
        ),
        "completed_at": (
            analysis.completed_at.isoformat() if analysis.completed_at else None
        ),
        "error_code": analysis.error_code,
        "report_export_status": analysis.report_export_status,
        "report_export_error_code": analysis.report_export_error_code,
        "report_generated_at": (
            analysis.report_generated_at.isoformat()
            if analysis.report_generated_at else None
        ),
        "report_available": {
            "pdf": (
                analysis.report_export_status
                == AnalysisReport.ReportExportStatus.COMPLETED
                and bool(analysis.report_storage_key)
            ),
            "json": (
                analysis.report_export_status
                == AnalysisReport.ReportExportStatus.COMPLETED
                and bool(analysis.report_json_storage_key)
            ),
        },
    }


def _request_id(request: Request) -> str | None:
    """Return the middleware request ID or a bounded incoming request ID."""
    value = getattr(request, "request_id", None) or request.headers.get(
        "X-Request-ID"
    )
    return str(value)[:100] if value else None
