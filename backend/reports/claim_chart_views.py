"""Tenant-authorized claim-chart lifecycle, revision, and export APIs."""

from __future__ import annotations

import logging
from uuid import UUID, uuid4

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import AnalysisReport
from audit.models import AuditLog
from reports.claim_chart_serializers import (
    ClaimChartCreateSerializer,
    ClaimChartExportCreateSerializer,
    ClaimChartPatchSerializer,
    ClaimChartRowPatchSerializer,
)
from reports.claim_chart_services import (
    create_chart_from_analysis,
    record_revision,
    serialize_chart,
    validate_analysis_integrity,
)
from reports.models import (
    ClaimChart,
    ClaimChartExport,
    ClaimChartRevision,
    ClaimChartRow,
)
from reports.claim_chart_tasks import generate_claim_chart_export
from enterprise_identity.services import filter_visible_matter_queryset, require_matter_access
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)

_EDIT_ROLES = ("OWNER", "ADMIN", "ATTORNEY", "ANALYST")
_REVIEW_ROLES = ("OWNER", "ADMIN", "ATTORNEY", "ANALYST", "REVIEWER")
_EXPORT_ROLES = ("OWNER", "ADMIN", "ATTORNEY", "ANALYST", "REVIEWER")


class ClaimChartAPIView(APIView):
    """Shared tenant authorization and parent relationship checks."""

    permission_classes = [IsAuthenticated, HasTenantAccess]

    def tenant_id(self, request: Request):
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")
        return tenant.pk

    def get_chart(
        self,
        request: Request,
        chart_id: UUID,
        *,
        lock: bool = False,
    ) -> ClaimChart:
        tenant_id = self.tenant_id(request)
        queryset = ClaimChart.objects.for_tenant(tenant_id).select_related(
            "analysis",
            "analysis__patent",
            "analysis__target_document_version",
            "analysis__target_claim",
            "created_by",
            "updated_by",
        )
        if lock:
            queryset = queryset.select_for_update(of=("self",))
        chart = get_object_or_404(queryset, pk=chart_id)
        try:
            validate_analysis_integrity(chart.analysis, tenant_id)
        except DjangoValidationError as exc:
            logger.error("Claim chart %s has invalid parent relationships.", chart.pk)
            raise NotFound("Claim chart was not found.") from exc
        require_matter_access(request=request, matter_type="claim_chart", matter_id=chart.pk, required="READ")
        require_matter_access(request=request, matter_type="analysis", matter_id=chart.analysis_id, required="READ")
        require_matter_access(request=request, matter_type="patent", matter_id=chart.analysis.patent_id, required="READ")
        return chart

    def audit(
        self,
        request: Request,
        *,
        tenant_id,
        action: str,
        resource_type: str,
        resource_id,
        outcome: str = AuditLog.Outcome.SUCCESS,
        metadata: dict | None = None,
    ) -> None:
        request_id = getattr(request, "request_id", None) or request.headers.get(
            "X-Request-ID"
        )
        AuditLog.objects.create(
            tenant_id=tenant_id,
            actor_user_id=request.user.id if getattr(request.user, "is_authenticated", False) else None,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=str(request_id)[:100] if request_id else None,
            outcome=outcome,
            metadata=metadata or {},
        )


def _error_message(exc: DjangoValidationError) -> str:
    if hasattr(exc, "messages") and exc.messages:
        return str(exc.messages[0])
    return "The requested claim-chart operation is invalid."


class ClaimChartListCreateView(ClaimChartAPIView):
    """List charts in the selected tenant or create one from a completed analysis."""

    def get_permissions(self):
        self.permission_classes = [IsAuthenticated, HasTenantAccess]
        if self.request.method == "POST":
            self.permission_classes.append(RequireTenantRole(*_EDIT_ROLES))
        return super().get_permissions()

    def get(self, request: Request) -> Response:
        tenant_id = self.tenant_id(request)
        queryset = (
            ClaimChart.objects.for_tenant(tenant_id)
            .select_related("analysis")
            .annotate(
                row_count=Count("rows", filter=Q(rows__tenant_id=tenant_id))
            )
            .order_by("-updated_at")
        )
        queryset = filter_visible_matter_queryset(queryset, tenant=request.tenant, user=request.user, matter_type="claim_chart")
        queryset = filter_visible_matter_queryset(queryset, tenant=request.tenant, user=request.user, matter_type="analysis", id_field="analysis_id")
        queryset = filter_visible_matter_queryset(queryset, tenant=request.tenant, user=request.user, matter_type="patent", id_field="analysis__patent_id")
        analysis_id = request.query_params.get("analysis_id")
        if analysis_id:
            try:
                UUID(analysis_id)
            except (ValueError, TypeError, AttributeError):
                return Response({"detail": "analysis_id must be a UUID."}, status=400)
            queryset = queryset.filter(analysis_id=analysis_id)
        results = []
        for chart in queryset[:100]:
            results.append(
                {
                    "chart_id": str(chart.pk),
                    "analysis_id": str(chart.analysis_id),
                    "title": chart.title,
                    "description": chart.description,
                    "status": chart.status,
                    "revision": chart.revision,
                    "row_count": chart.row_count,
                    "created_at": chart.created_at.isoformat(),
                    "updated_at": chart.updated_at.isoformat(),
                }
            )
        return Response({"results": results, "count": len(results)})

    def post(self, request: Request) -> Response:
        serializer = ClaimChartCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        require_matter_access(request=request, matter_type="analysis", matter_id=serializer.validated_data["analysis_id"], required="READ")
        try:
            chart = create_chart_from_analysis(
                tenant_id=self.tenant_id(request),
                analysis_id=serializer.validated_data["analysis_id"],
                user=request.user,
                title=serializer.validated_data.get("title"),
                description=serializer.validated_data.get("description", ""),
                request_id=getattr(request, "request_id", None),
            )
        except DjangoValidationError as exc:
            return Response(
                {"detail": _error_message(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {"chart": serialize_chart(chart)},
            status=status.HTTP_201_CREATED,
        )


class ClaimChartDetailView(ClaimChartAPIView):
    """Retrieve or update chart metadata and controlled review status."""

    def get(self, request: Request, chart_id: UUID) -> Response:
        chart = self.get_chart(request, chart_id)
        return Response({"chart": serialize_chart(chart)})

    def patch(self, request: Request, chart_id: UUID) -> Response:
        membership = getattr(request, "tenant_membership", None)
        role = getattr(membership, "role", None)
        if role not in _EDIT_ROLES:
            raise PermissionDenied("Your tenant role cannot edit claim charts.")
        serializer = ClaimChartPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        changes = serializer.validated_data

        with transaction.atomic():
            chart = self.get_chart(request, chart_id, lock=True)
            requested_status = changes.get("status", chart.status)
            require_matter_access(request=request, matter_type="claim_chart", matter_id=chart.pk, required="REVIEW" if requested_status in {ClaimChart.Status.IN_REVIEW, ClaimChart.Status.APPROVED} else "EDIT")
            if chart.status == ClaimChart.Status.ARCHIVED:
                raise PermissionDenied("Archived claim charts are immutable.")
            metadata_changes = set(changes) & {"title", "description"}
            if chart.status == ClaimChart.Status.APPROVED and metadata_changes:
                raise PermissionDenied("Approved claim-chart content is immutable.")
            next_status = changes.get("status", chart.status)
            allowed_transitions = {
                ClaimChart.Status.DRAFT: {
                    ClaimChart.Status.DRAFT,
                    ClaimChart.Status.IN_REVIEW,
                    ClaimChart.Status.ARCHIVED,
                },
                ClaimChart.Status.IN_REVIEW: {
                    ClaimChart.Status.DRAFT,
                    ClaimChart.Status.IN_REVIEW,
                    ClaimChart.Status.APPROVED,
                    ClaimChart.Status.ARCHIVED,
                },
                ClaimChart.Status.APPROVED: {
                    ClaimChart.Status.APPROVED,
                    ClaimChart.Status.ARCHIVED,
                },
                ClaimChart.Status.ARCHIVED: {ClaimChart.Status.ARCHIVED},
            }
            if next_status not in allowed_transitions[chart.status]:
                return Response(
                    {"detail": "Invalid claim-chart status transition."},
                    status=400,
                )
            if next_status == ClaimChart.Status.APPROVED:
                if role not in {"OWNER", "ADMIN", "ATTORNEY"}:
                    raise PermissionDenied(
                        "Only an owner, admin, or attorney may approve a claim chart."
                    )
                unreviewed_exists = (
                    ClaimChartRow.objects.for_tenant(chart.tenant_id)
                    .filter(
                        chart=chart,
                        attorney_decision=ClaimChartRow.AttorneyDecision.UNREVIEWED,
                    )
                    .exists()
                )
                if unreviewed_exists:
                    return Response(
                        {
                            "detail": (
                                "Every limitation must have an attorney decision "
                                "before approval."
                            )
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
            for field in ("title", "description", "status"):
                if field in changes:
                    setattr(chart, field, changes[field])
            chart.updated_by = request.user
            chart.save(
                update_fields=(
                    "title",
                    "description",
                    "status",
                    "updated_by",
                    "updated_at",
                )
            )
            record_revision(
                chart=chart,
                user=request.user,
                change_summary="Chart metadata/status updated.",
                bump_revision=True,
            )
            self.audit(
                request,
                tenant_id=chart.tenant_id,
                action="CLAIM_CHART_UPDATED",
                resource_type="ClaimChart",
                resource_id=chart.pk,
                metadata={
                    "revision": chart.revision,
                    "status": chart.status,
                    "changed_fields": sorted(changes),
                },
            )
        return Response({"chart": serialize_chart(chart)})


class ClaimChartRowUpdateView(ClaimChartAPIView):
    """Update attorney annotations on one chart limitation."""

    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole(*_REVIEW_ROLES),
    ]

    def patch(self, request: Request, chart_id: UUID, row_id: UUID) -> Response:
        serializer = ClaimChartRowPatchSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        changes = serializer.validated_data
        membership = getattr(request, "tenant_membership", None)
        role = getattr(membership, "role", None)
        if role == "REVIEWER" and set(changes) - {
            "attorney_notes",
            "attorney_decision",
        }:
            raise PermissionDenied(
                "Reviewers may update decision and notes, not mapping summaries."
            )
        tenant_id = self.tenant_id(request)

        with transaction.atomic():
            chart = self.get_chart(request, chart_id, lock=True)
            require_matter_access(request=request, matter_type="claim_chart", matter_id=chart.pk, required="REVIEW" if role == "REVIEWER" or "attorney_decision" in changes else "EDIT")
            if chart.status not in {
                ClaimChart.Status.DRAFT,
                ClaimChart.Status.IN_REVIEW,
            }:
                raise PermissionDenied("Only draft or in-review charts can be edited.")
            row = get_object_or_404(
                ClaimChartRow.objects.for_tenant(tenant_id).select_for_update(),
                pk=row_id,
                chart=chart,
            )
            for field, value in changes.items():
                setattr(row, field, value)
            row.save(update_fields=(*changes.keys(), "updated_at"))
            record_revision(
                chart=chart,
                user=request.user,
                change_summary=f"Limitation {row.limitation_index} updated.",
                bump_revision=True,
            )
            self.audit(
                request,
                tenant_id=tenant_id,
                action="CLAIM_CHART_ROW_UPDATED",
                resource_type="ClaimChartRow",
                resource_id=row.pk,
                metadata={
                    "chart_id": str(chart.pk),
                    "revision": chart.revision,
                    "changed_fields": sorted(changes),
                },
            )
        return Response({"row_id": str(row.pk), "chart": serialize_chart(chart)})


class ClaimChartRevisionListView(ClaimChartAPIView):
    """List immutable chart revisions with hashes and optional snapshots."""

    def get(self, request: Request, chart_id: UUID) -> Response:
        chart = self.get_chart(request, chart_id)
        include_snapshot = (
            request.query_params.get("include_snapshot", "false").lower()
            == "true"
        )
        revisions = ClaimChartRevision.objects.for_tenant(
            chart.tenant_id
        ).filter(chart=chart)
        return Response(
            {
                "chart_id": str(chart.pk),
                "results": [
                    {
                        "revision_id": str(revision.pk),
                        "revision_number": revision.revision_number,
                        "snapshot_sha256": revision.snapshot_sha256,
                        "change_summary": revision.change_summary,
                        "created_by_id": str(revision.created_by_id),
                        "created_at": revision.created_at.isoformat(),
                        **({"snapshot": revision.snapshot} if include_snapshot else {}),
                    }
                    for revision in revisions
                ],
            }
        )


class ClaimChartExportCreateView(ClaimChartAPIView):
    """Queue DOCX, XLSX, PDF, or JSON export of a fixed chart revision."""

    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole(*_EXPORT_ROLES),
    ]

    def post(self, request: Request, chart_id: UUID) -> Response:
        serializer = ClaimChartExportCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant_id = self.tenant_id(request)
        task_id = str(uuid4())
        with transaction.atomic():
            chart = self.get_chart(request, chart_id, lock=True)
            revision = get_object_or_404(
                ClaimChartRevision.objects.for_tenant(tenant_id),
                chart=chart,
                revision_number=chart.revision,
            )
            export = ClaimChartExport.objects.create(
                tenant_id=tenant_id,
                chart=chart,
                revision=revision,
                format=serializer.validated_data["format"],
                status=ClaimChartExport.Status.QUEUED,
                task_id=task_id,
                requested_by=request.user,
            )
            self.audit(
                request,
                tenant_id=tenant_id,
                action="CLAIM_CHART_EXPORT_QUEUED",
                resource_type="ClaimChartExport",
                resource_id=export.pk,
                metadata={
                    "chart_id": str(chart.pk),
                    "revision": revision.revision_number,
                    "format": export.format,
                    "task_id": task_id,
                },
            )
        try:
            generate_claim_chart_export.apply_async(
                args=[str(export.pk)],
                task_id=task_id,
                queue="exports",
            )
        except Exception as exc:
            logger.error(
                "Claim-chart export dispatch failed for %s (error_type=%s).",
                export.pk,
                type(exc).__name__,
            )
            with transaction.atomic():
                ClaimChartExport.objects.for_tenant(tenant_id).filter(
                    pk=export.pk,
                    task_id=task_id,
                ).update(
                    status=ClaimChartExport.Status.FAILED,
                    error_code="QUEUE_SUBMISSION_FAILED",
                    updated_at=timezone.now(),
                )
                self.audit(
                    request,
                    tenant_id=tenant_id,
                    action="CLAIM_CHART_EXPORT_QUEUE_FAILED",
                    resource_type="ClaimChartExport",
                    resource_id=export.pk,
                    outcome=AuditLog.Outcome.FAILURE,
                    metadata={
                        "format": export.format,
                        "error_type": type(exc).__name__,
                    },
                )
            return Response(
                {
                    "detail": "The export worker is unavailable. Please retry.",
                    "export_status": "FAILED",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                "export_id": str(export.pk),
                "chart_id": str(chart.pk),
                "revision": revision.revision_number,
                "format": export.format,
                "status": export.status,
                "task_id": task_id,
                "status_url": f"/api/v1/reports/claim-chart-exports/{export.pk}/",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class ClaimChartExportStatusView(ClaimChartAPIView):
    """Return tenant-scoped status and safe download availability."""

    def get(self, request: Request, export_id: UUID) -> Response:
        tenant_id = self.tenant_id(request)
        export = get_object_or_404(
            ClaimChartExport.objects.for_tenant(tenant_id).select_related(
                "chart",
                "revision",
            ),
            pk=export_id,
        )
        if (
            export.chart.tenant_id != tenant_id
            or export.revision.tenant_id != tenant_id
        ):
            raise NotFound("Claim-chart export was not found.")
        require_matter_access(request=request, matter_type="claim_chart", matter_id=export.chart_id, required="READ")
        require_matter_access(request=request, matter_type="analysis", matter_id=export.chart.analysis_id, required="READ")
        require_matter_access(request=request, matter_type="patent", matter_id=export.chart.analysis.patent_id, required="READ")
        available = (
            export.status == ClaimChartExport.Status.COMPLETED
            and _storage_exists(export.storage_key)
        )
        return Response(
            {
                "export_id": str(export.pk),
                "chart_id": str(export.chart_id),
                "revision": export.revision.revision_number,
                "format": export.format,
                "status": export.status,
                "error_code": export.error_code,
                "created_at": export.created_at.isoformat(),
                "completed_at": (
                    export.completed_at.isoformat()
                    if export.completed_at
                    else None
                ),
                "download_available": available,
                "download_url": (
                    f"/api/v1/reports/claim-chart-exports/{export.pk}/download/"
                    if available
                    else None
                ),
            }
        )


class ClaimChartExportDownloadView(ClaimChartAPIView):
    """Stream a completed export after tenant and storage checks."""

    def get(self, request: Request, export_id: UUID):
        tenant_id = self.tenant_id(request)
        export = get_object_or_404(
            ClaimChartExport.objects.for_tenant(tenant_id).select_related(
                "chart",
                "revision",
            ),
            pk=export_id,
            status=ClaimChartExport.Status.COMPLETED,
        )
        if (
            export.chart.tenant_id != tenant_id
            or export.revision.tenant_id != tenant_id
        ):
            raise NotFound("Claim-chart export was not found.")
        require_matter_access(request=request, matter_type="claim_chart", matter_id=export.chart_id, required="READ")
        require_matter_access(request=request, matter_type="analysis", matter_id=export.chart.analysis_id, required="READ")
        require_matter_access(request=request, matter_type="patent", matter_id=export.chart.analysis.patent_id, required="READ")
        if not _storage_exists(export.storage_key):
            raise NotFound("The requested claim-chart artifact is unavailable.")
        try:
            handle = default_storage.open(export.storage_key, "rb")
        except (OSError, ValueError) as exc:
            raise NotFound(
                "The requested claim-chart artifact is unavailable."
            ) from exc
        mime_type, extension = _export_mime_type(export.format)
        response = FileResponse(
            handle,
            content_type=mime_type,
            as_attachment=True,
            filename=(
                f"patentshield-claim-chart-{export.chart_id}-"
                f"r{export.revision.revision_number}.{extension}"
            ),
        )
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


def _storage_exists(storage_key: str | None) -> bool:
    if not storage_key:
        return False
    try:
        return default_storage.exists(storage_key)
    except Exception:
        logger.warning("Claim-chart storage availability check failed.", exc_info=True)
        return False


def _export_mime_type(export_format: str) -> tuple[str, str]:
    formats = {
        "DOCX": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "docx",
        ),
        "XLSX": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx",
        ),
        "PDF": ("application/pdf", "pdf"),
        "JSON": ("application/json", "json"),
    }
    try:
        return formats[export_format]
    except KeyError as exc:
        raise NotFound("Claim-chart export format was not found.") from exc
