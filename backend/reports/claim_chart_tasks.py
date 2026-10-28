"""Asynchronous claim-chart export task with ownership, retry, and audit guards."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from asgiref.sync import async_to_sync
from celery import Task, shared_task
from channels.layers import get_channel_layer
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from analyses.models import AnalysisReport
from audit.models import AuditLog
from reports.claim_chart_exports import render_claim_chart
from reports.models import ClaimChartExport

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue="exports", max_retries=2)
def generate_claim_chart_export(
    self: Task,
    export_id: str,
) -> dict[str, str | int | bool]:
    """Render a selected immutable chart revision into DOCX/XLSX/PDF/JSON."""
    export = None
    storage_key: str | None = None
    try:
        with transaction.atomic():
            export = (
                ClaimChartExport.objects.select_for_update(of=("self",))
                .select_related("chart", "chart__analysis", "revision", "requested_by")
                .get(pk=export_id)
            )
            if export.task_id != str(self.request.id or ""):
                return {
                    "export_id": str(export.pk),
                    "status": export.status,
                    "skipped": True,
                    "reason": "STALE_TASK",
                }
            if (
                export.status == ClaimChartExport.Status.COMPLETED
                and export.storage_key
            ):
                if default_storage.exists(export.storage_key):
                    return {
                        "export_id": str(export.pk),
                        "status": export.status,
                        "skipped": True,
                    }
            if (
                export.status == ClaimChartExport.Status.RUNNING
                and export.started_at is not None
                and export.started_at >= timezone.now() - timedelta(minutes=45)
                and int(getattr(self.request, "retries", 0) or 0) == 0
            ):
                return {
                    "export_id": str(export.pk),
                    "status": export.status,
                    "skipped": True,
                    "reason": "EXPORT_ALREADY_ACTIVE",
                }
            _validate_export(export)
            export.status = ClaimChartExport.Status.RUNNING
            export.started_at = timezone.now()
            export.error_code = None
            export.save(
                update_fields=("status", "started_at", "error_code", "updated_at")
            )

        _publish_event(
            export,
            "claim_chart_export_progress",
            {
                "export_id": str(export.pk),
                "chart_id": str(export.chart_id),
                "format": export.format,
                "status": "RUNNING",
                "progress": 15,
                "stage": "loading_revision",
            },
        )
        snapshot = export.revision.snapshot
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("schema_version") != "patentshield-claim-chart-v1"
        ):
            raise ValueError("The selected claim-chart snapshot is invalid.")
        if snapshot.get("tenant_id") != str(export.tenant_id):
            raise ValueError(
                "The claim-chart snapshot tenant does not match its export."
            )
        if snapshot.get("chart", {}).get("chart_id") != str(export.chart_id):
            raise ValueError(
                "The claim-chart snapshot identity does not match its export."
            )

        _publish_event(
            export,
            "claim_chart_export_progress",
            {
                "export_id": str(export.pk),
                "chart_id": str(export.chart_id),
                "format": export.format,
                "status": "RUNNING",
                "progress": 35,
                "stage": "rendering_document",
            },
        )
        content, content_type, extension = render_claim_chart(snapshot, export.format)
        target = (
            f"claim-charts/{export.tenant_id}/{export.chart_id}/"
            f"revision-{export.revision.revision_number}/{export.pk}.{extension}"
        )
        _publish_event(
            export,
            "claim_chart_export_progress",
            {
                "export_id": str(export.pk),
                "chart_id": str(export.chart_id),
                "format": export.format,
                "status": "RUNNING",
                "progress": 80,
                "stage": "writing_artifact",
            },
        )
        storage_key = default_storage.save(target, ContentFile(content))

        with transaction.atomic():
            locked = (
                ClaimChartExport.objects.for_tenant(export.tenant_id)
                .select_for_update(of=("self",))
                .select_related("chart", "revision", "requested_by")
                .get(pk=export.pk)
            )
            if locked.task_id != str(self.request.id or ""):
                raise RuntimeError("Claim-chart export task ownership changed.")
            if (
                locked.chart.tenant_id != locked.tenant_id
                or locked.revision.tenant_id != locked.tenant_id
            ):
                raise ValueError("Claim-chart export crossed tenant boundaries.")
            locked.storage_key = storage_key
            locked.status = ClaimChartExport.Status.COMPLETED
            locked.error_code = None
            locked.completed_at = timezone.now()
            locked.save(
                update_fields=(
                    "storage_key",
                    "status",
                    "error_code",
                    "completed_at",
                    "updated_at",
                )
            )
            AuditLog.objects.create(
                tenant_id=locked.tenant_id,
                actor_user_id=locked.requested_by.pk,
                action="CLAIM_CHART_EXPORT_COMPLETED",
                resource_type="ClaimChartExport",
                resource_id=locked.pk,
                request_id=str(self.request.id or "")[:100] or None,
                outcome=AuditLog.Outcome.SUCCESS,
                metadata={
                    "chart_id": str(locked.chart_id),
                    "revision": locked.revision.revision_number,
                    "format": locked.format,
                    "snapshot_sha256": locked.revision.snapshot_sha256,
                    "size_bytes": len(content),
                    "content_type": content_type,
                },
            )

        _publish_event(
            export,
            "claim_chart_export_completed",
            {
                "export_id": str(export.pk),
                "chart_id": str(export.chart_id),
                "format": export.format,
                "status": "COMPLETED",
                "progress": 100,
            },
        )
        return {
            "export_id": str(export.pk),
            "chart_id": str(export.chart_id),
            "tenant_id": str(export.tenant_id),
            "format": export.format,
            "status": ClaimChartExport.Status.COMPLETED,
            "storage_key": storage_key,
        }
    except Exception as exc:
        if storage_key:
            try:
                if default_storage.exists(storage_key):
                    default_storage.delete(storage_key)
            except Exception:
                logger.warning(
                    "Could not remove partial claim-chart artifact.",
                    exc_info=True,
                )
        if export is None:
            try:
                export = (
                    ClaimChartExport.objects.select_related(
                        "chart", "revision", "requested_by"
                    ).get(pk=export_id)
                )
            except (ClaimChartExport.DoesNotExist, ValueError, TypeError):
                export = None

        retries = int(getattr(self.request, "retries", 0))
        should_retry = retries < int(getattr(self, "max_retries", 2))
        error_state_persisted = False
        if export is not None:
            try:
                with transaction.atomic():
                    locked = (
                        ClaimChartExport.objects.for_tenant(export.tenant_id)
                        .select_for_update(of=("self",))
                        .get(pk=export.pk)
                    )
                    if (
                        locked.task_id == str(self.request.id or "")
                        and locked.status != ClaimChartExport.Status.COMPLETED
                    ):
                        locked.status = (
                            ClaimChartExport.Status.QUEUED
                            if should_retry
                            else ClaimChartExport.Status.FAILED
                        )
                        locked.error_code = (
                            "RETRY_SCHEDULED"
                            if should_retry
                            else type(exc).__name__[:100]
                        )
                        locked.storage_key = None
                        locked.save(
                            update_fields=(
                                "status",
                                "error_code",
                                "storage_key",
                                "updated_at",
                            )
                        )
                        AuditLog.objects.create(
                            tenant_id=locked.tenant_id,
                            actor_user_id=locked.requested_by.pk,
                            action=(
                                "CLAIM_CHART_EXPORT_RETRY_SCHEDULED"
                                if should_retry
                                else "CLAIM_CHART_EXPORT_FAILED"
                            ),
                            resource_type="ClaimChartExport",
                            resource_id=locked.pk,
                            request_id=str(self.request.id or "")[:100] or None,
                            outcome=AuditLog.Outcome.FAILURE,
                            metadata={
                                "chart_id": str(locked.chart_id),
                                "format": locked.format,
                                "error_type": type(exc).__name__,
                                "retry": retries + 1 if should_retry else None,
                            },
                        )
                        error_state_persisted = True
            except Exception as persistence_exc:
                # Do not mask the original rendering/IO error. Celery still
                # retries while permitted; a later retry can reclaim RUNNING.
                logger.error(
                    "Could not persist claim-chart export failure state "
                    "for export_id=%s error_type=%s",
                    export_id,
                    type(persistence_exc).__name__,
                )

        if should_retry:
            if export is not None and error_state_persisted:
                _publish_event(
                    export,
                    "claim_chart_export_progress",
                    {
                        "export_id": str(export.pk),
                        "chart_id": str(export.chart_id),
                        "format": export.format,
                        "status": "QUEUED",
                        "stage": "retry_scheduled",
                    },
                )
            raise self.retry(
                exc=exc,
                countdown=min(15 * (2 ** retries), 120),
            )

        if export is not None and error_state_persisted:
            _publish_event(
                export,
                "claim_chart_export_completed",
                {
                    "export_id": str(export.pk),
                    "chart_id": str(export.chart_id),
                    "format": export.format,
                    "status": "FAILED",
                },
            )
        logger.error(
            "Claim-chart export failed for export_id=%s error_type=%s",
            export_id,
            type(exc).__name__,
        )
        raise


def _validate_export(export: ClaimChartExport) -> None:
    """Validate chart, immutable revision, and analysis tenancy before rendering."""
    if (
        export.tenant_id != export.chart.tenant_id
        or export.revision.tenant_id != export.tenant_id
    ):
        raise ValueError("Claim-chart export tenant relationships do not match.")
    if export.revision.chart_id != export.chart_id:
        raise ValueError("Claim-chart export revision does not belong to the chart.")
    analysis = export.chart.analysis
    if analysis.tenant_id != export.tenant_id:
        raise ValueError("Claim-chart analysis tenant does not match export tenant.")
    if analysis.status not in {
        AnalysisReport.Status.COMPLETED,
        AnalysisReport.Status.COMPLETED_WITH_WARNINGS,
    }:
        raise ValueError("Only completed analysis results may be exported.")


def _publish_event(
    export: ClaimChartExport,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Best-effort progress event; persisted status remains authoritative."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            f"analysis_{export.chart.analysis_id}",
            {"type": event_type, "data": data},
        )
    except Exception:
        logger.warning("Unable to publish claim-chart export event.", exc_info=True)
