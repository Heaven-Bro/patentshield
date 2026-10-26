"""Tenant-authorized analysis report export and artifact endpoints."""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from django.core.files.storage import default_storage
from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import AnalysisReport, ClaimAssessment
from audit.models import AuditLog
from reports.tasks import generate_analysis_report
from tenants.models import TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)

_EXPORTABLE_ANALYSIS_STATUSES = {
    AnalysisReport.Status.COMPLETED,
    AnalysisReport.Status.COMPLETED_WITH_WARNINGS,
}
_ACTIVE_EXPORT_STATUSES = {
    AnalysisReport.ReportExportStatus.QUEUED,
    AnalysisReport.ReportExportStatus.RUNNING,
}


class ReportAPIView(APIView):
    """Shared tenant authorization and report provenance checks."""

    permission_classes = [IsAuthenticated, HasTenantAccess]

    def get_report(self, request: Request, analysis_id: UUID) -> AnalysisReport:
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")
        try:
            report = (
                AnalysisReport.objects.for_tenant(tenant.pk)
                .select_related(
                    "patent",
                    "target_document_version",
                    "target_claim",
                )
                .get(pk=analysis_id)
            )
        except AnalysisReport.DoesNotExist as exc:
            # Conceal cross-tenant report existence behind the same 404.
            raise NotFound("Analysis report was not found.") from exc

        if (
            report.patent.tenant_id != tenant.pk
            or report.target_document_version.tenant_id != tenant.pk
            or report.target_document_version.patent_id != report.patent_id
        ):
            raise NotFound("Analysis report was not found.")
        if report.target_claim_id and (
            report.target_claim.tenant_id != tenant.pk
            or report.target_claim.patent_id != report.patent_id
            or report.target_claim.document_version_id
            != report.target_document_version_id
        ):
            raise NotFound("Analysis report was not found.")
        return report


class ReportGenerateView(ReportAPIView):
    """Queue a JSON/PDF snapshot without changing analysis execution status."""

    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole("OWNER", "ADMIN", "ATTORNEY", "ANALYST", "REVIEWER"),
    ]

    def post(self, request: Request, analysis_id: UUID) -> Response:
        report = self.get_report(request, analysis_id)
        if report.status not in _EXPORTABLE_ANALYSIS_STATUSES:
            return Response(
                {
                    "detail": "Only completed analyses can be exported.",
                    "analysis_status": report.status,
                },
                status=status.HTTP_409_CONFLICT,
            )
        has_assessments = ClaimAssessment.objects.for_tenant(
            report.tenant_id
        ).filter(analysis_id=report.pk).exists()
        if not has_assessments and not report.result_summary:
            return Response(
                {"detail": "Analysis has no persisted results to export."},
                status=status.HTTP_409_CONFLICT,
            )
        if report.report_export_status in _ACTIVE_EXPORT_STATUSES:
            return Response(
                {
                    "detail": "A report export is already in progress.",
                    "export_status": report.report_export_status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        task_id = str(uuid4())
        tenant_id = report.tenant_id
        with transaction.atomic():
            locked_report = (
                AnalysisReport.objects.for_tenant(tenant_id)
                .select_for_update()
                .get(pk=report.pk)
            )
            if locked_report.status not in _EXPORTABLE_ANALYSIS_STATUSES:
                return Response(
                    {
                        "detail": "Analysis is no longer in an exportable state.",
                        "analysis_status": locked_report.status,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            if locked_report.report_export_status in _ACTIVE_EXPORT_STATUSES:
                return Response(
                    {
                        "detail": "A report export is already in progress.",
                        "export_status": locked_report.report_export_status,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            locked_report.report_export_status = (
                AnalysisReport.ReportExportStatus.QUEUED
            )
            locked_report.report_export_error_code = None
            locked_report.report_export_task_id = task_id
            locked_report.save(
                update_fields=(
                    "report_export_status",
                    "report_export_error_code",
                    "report_export_task_id",
                    "updated_at",
                )
            )
            AuditLog.objects.create(
                tenant_id=tenant_id,
                actor_user_id=request.user.pk,
                action="QUEUE_ANALYSIS_REPORT_EXPORT",
                resource_type="AnalysisReport",
                resource_id=locked_report.pk,
                request_id=_request_id(request),
                outcome=AuditLog.Outcome.SUCCESS,
                metadata={"task_id": task_id, "formats": ["JSON", "PDF"]},
            )

        try:
            task = generate_analysis_report.apply_async(
                args=[str(report.pk)],
                queue="exports",
            )
            if task and getattr(task, "id", None):
                task_id = str(task.id)
                AnalysisReport.objects.for_tenant(tenant_id).filter(
                    pk=report.pk
                ).update(report_export_task_id=task_id)
        except Exception as exc:
            logger.error(
                "Report export dispatch failed for analysis_id=%s error_type=%s.",
                report.pk,
                type(exc).__name__,
            )
            with transaction.atomic():
                AnalysisReport.objects.for_tenant(tenant_id).filter(
                    pk=report.pk,
                    report_export_task_id=task_id,
                ).update(
                    report_export_status=AnalysisReport.ReportExportStatus.FAILED,
                    report_export_error_code="QUEUE_SUBMISSION_FAILED",
                    updated_at=timezone.now(),
                )
                AuditLog.objects.create(
                    tenant_id=tenant_id,
                    actor_user_id=request.user.pk,
                    action="QUEUE_ANALYSIS_REPORT_EXPORT",
                    resource_type="AnalysisReport",
                    resource_id=report.pk,
                    request_id=_request_id(request),
                    outcome=AuditLog.Outcome.FAILURE,
                    metadata={"task_id": task_id, "error_type": type(exc).__name__},
                )
            return Response(
                {
                    "detail": (
                        "The report export could not be queued. "
                        "Please retry when the export worker is available."
                    ),
                    "analysis_id": str(report.pk),
                    "export_status": AnalysisReport.ReportExportStatus.FAILED,
                    "export_error_code": "QUEUE_SUBMISSION_FAILED",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "analysis_id": str(report.pk),
                "export_status": AnalysisReport.ReportExportStatus.QUEUED,
                "task_id": task_id,
                "status_url": f"/api/v1/reports/{report.pk}/status/",
                "pdf_url": f"/api/v1/reports/{report.pk}/pdf/",
                "json_url": f"/api/v1/reports/{report.pk}/json/",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ReportStatusView(ReportAPIView):
    """Return export state and artifact availability without storage keys."""

    def get(self, request: Request, analysis_id: UUID) -> Response:
        report = self.get_report(request, analysis_id)
        export_complete = (
            report.report_export_status == AnalysisReport.ReportExportStatus.COMPLETED
        )
        return Response(
            {
                "analysis_id": str(report.pk),
                "analysis_status": report.status,
                "export_status": report.report_export_status,
                "export_error_code": report.report_export_error_code,
                "generated_at": (
                    report.report_generated_at.isoformat()
                    if report.report_generated_at
                    else None
                ),
                "artifacts": {
                    "pdf": {
                        "available": export_complete
                        and _storage_key_exists(report.report_storage_key),
                        "url": f"/api/v1/reports/{report.pk}/pdf/",
                    },
                    "json": {
                        "available": export_complete
                        and _storage_key_exists(report.report_json_storage_key),
                        "url": f"/api/v1/reports/{report.pk}/json/",
                    },
                },
            }
        )


class ReportArtifactView(ReportAPIView):
    """Stream a completed artifact after validating tenant ownership."""

    def get(
        self,
        request: Request,
        analysis_id: UUID,
        artifact_format: str,
    ) -> HttpResponse:
        report = self.get_report(request, analysis_id)
        if report.report_export_status != AnalysisReport.ReportExportStatus.COMPLETED:
            raise NotFound("The requested report artifact is not available.")

        if artifact_format == "pdf":
            storage_key = report.report_storage_key
            content_type = "application/pdf"
            filename = f"patentshield-analysis-{report.pk}.pdf"
            inline = True
        elif artifact_format == "json":
            storage_key = report.report_json_storage_key
            content_type = "application/json"
            filename = f"patentshield-analysis-{report.pk}.json"
            inline = False
        else:
            raise NotFound("Report format was not found.")

        if not _storage_key_exists(storage_key):
            raise NotFound("The requested report artifact is not available.")
        try:
            handle = default_storage.open(storage_key, "rb")
        except (OSError, ValueError) as exc:
            raise NotFound("The requested report artifact is not available.") from exc

        response = FileResponse(
            handle,
            content_type=content_type,
            as_attachment=not inline,
            filename=filename,
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        if inline:
            response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


def _storage_key_exists(storage_key: str | None) -> bool:
    """Check artifact presence without surfacing storage backend exceptions."""
    if not storage_key:
        return False
    try:
        return default_storage.exists(storage_key)
    except Exception:
        logger.warning("Report storage availability check failed.", exc_info=True)
        return False


def _request_id(request: Request) -> str | None:
    """Return the middleware's bounded request identifier when available."""
    value = getattr(request, "request_id", None) or request.headers.get("X-Request-ID")
    return str(value)[:100] if value else None
