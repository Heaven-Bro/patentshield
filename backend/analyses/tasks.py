"""Celery orchestration for LangGraph patent analysis runs."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from asgiref.sync import async_to_sync
from celery import Task, shared_task
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

from analyses.models import AnalysisReport
from audit.models import AuditLog
from patents.models import Claim

logger = logging.getLogger(__name__)

_NODE_PROGRESS = {
    "deconstruct_claim": 15,
    "hybrid_retrieval": 38,
    "limitation_evaluator": 62,
    "groundedness_guardrail": 82,
    "scoring": 94,
}
_TERMINAL_STATUSES = {
    AnalysisReport.Status.COMPLETED,
    AnalysisReport.Status.COMPLETED_WITH_WARNINGS,
    AnalysisReport.Status.CANCELLED,
}


@shared_task(bind=True, queue="analysis", max_retries=3)
def run_patent_analysis(
    self: Task,
    analysis_id: str,
) -> dict[str, Any]:
    """Run the graph and persist its assessments, evidence, and screening metrics.

    The task is tenant-bound through the already-persisted AnalysisReport. It
    never accepts patent text, tenant ownership, or evidence payloads from a
    Celery argument. It is safe to redeliver completed tasks and serializes
    competing workers through a row lock on the analysis record.
    """
    report: AnalysisReport | None = None
    try:
        report, claimed = _claim_analysis_for_execution(analysis_id, self)
        if not claimed:
            return {
                "analysis_id": str(analysis_id),
                "status": report.status,
                "skipped": True,
            }

        _publish_progress(
            str(report.pk),
            "job.progress",
            {
                "analysis_id": str(report.pk),
                "status": AnalysisReport.Status.RUNNING,
                "stage": "starting",
                "progress": 0,
            },
        )

        from ai_engine.graph import compile_analysis_graph

        claim = _load_target_claim(report)
        initial_state: dict[str, Any] = {
            "analysis_id": str(report.pk),
            "tenant_id": str(report.tenant_id),
            "target_patent_id": str(report.patent_id),
            "target_claim_number": claim.claim_number,
            "claim_text": claim.claim_text,
            "limitations": [],
            "retrieved_evidence": [],
            "assessments": [],
            "status": "PENDING",
            "error_message": None,
            "metrics": {},
        }

        graph = compile_analysis_graph()
        graph_state = dict(initial_state)
        completed_nodes: list[str] = []
        for update in graph.stream(initial_state, stream_mode="updates"):
            if not isinstance(update, dict):
                logger.warning(
                    "Ignoring invalid graph update for analysis_id=%s.",
                    report.pk,
                )
                continue
            for node_name, node_payload in update.items():
                if isinstance(node_payload, dict):
                    graph_state.update(node_payload)
                completed_nodes.append(str(node_name))
                progress = _NODE_PROGRESS.get(str(node_name), 90)
                _publish_progress(
                    str(report.pk),
                    "node.completed",
                    {
                        "analysis_id": str(report.pk),
                        "node": str(node_name),
                        "completed_nodes": list(completed_nodes),
                        "total_nodes": len(_NODE_PROGRESS),
                        "progress": progress,
                        "status": str(graph_state.get("status", "RUNNING")),
                    },
                )
                _publish_progress(
                    str(report.pk),
                    "job.progress",
                    {
                        "analysis_id": str(report.pk),
                        "stage": str(node_name),
                        "progress": progress,
                        "status": str(graph_state.get("status", "RUNNING")),
                    },
                )

        if not graph_state.get("limitations") and graph_state.get("status") != "FAILED":
            graph_state["status"] = "FAILED"
            graph_state["error_message"] = (
                "The analysis graph did not produce any usable limitations."
            )

        graph_status = str(graph_state.get("status", "UNKNOWN"))
        error_message = graph_state.get("error_message")
        if graph_status == "FAILED":
            final_status = AnalysisReport.Status.FAILED
        elif error_message or graph_status == "COMPLETED_WITH_WARNINGS":
            final_status = AnalysisReport.Status.COMPLETED_WITH_WARNINGS
        else:
            final_status = AnalysisReport.Status.COMPLETED

        from analyses.services import persist_analysis_result

        result = persist_analysis_result(
            report.pk,
            graph_state,
            final_status=final_status,
            task_id=self.request.id,
        )
        _publish_progress(
            str(report.pk),
            "job.completed",
            {
                "analysis_id": str(report.pk),
                "status": result["status"],
                "progress": 100,
                "assessment_count": result["assessment_count"],
                "evidence_reference_count": result["evidence_reference_count"],
                "verified_evidence_count": result["verified_evidence_count"],
                "metrics": result["metrics"],
            },
        )
        return result

    except Exception as exc:
        logger.error(
            "Patent analysis task failed for analysis_id=%s error_type=%s.",
            analysis_id,
            type(exc).__name__,
        )
        if report is None:
            try:
                report = AnalysisReport.objects.select_related("created_by").get(
                    pk=analysis_id
                )
            except (AnalysisReport.DoesNotExist, ValueError, TypeError):
                report = None
        retries = int(getattr(self.request, "retries", 0))
        max_retries = int(getattr(self, "max_retries", 3))
        if report is not None:
            if retries < max_retries:
                _mark_analysis_retry_scheduled(report, self, exc)
            else:
                _mark_analysis_failed(report, self, exc)

        if retries < max_retries:
            if report is not None:
                _publish_progress(
                    str(report.pk),
                    "job.progress",
                    {
                        "analysis_id": str(report.pk),
                        "status": "RETRYING",
                        "stage": "retry_scheduled",
                        "retry_attempt": retries + 1,
                    },
                )
            raise self.retry(exc=exc, countdown=min(60 * (retries + 1), 300))

        if report is not None:
            _publish_progress(
                str(report.pk),
                "job.completed",
                {
                    "analysis_id": str(report.pk),
                    "status": AnalysisReport.Status.FAILED,
                    "progress": 100,
                    "error_code": type(exc).__name__,
                },
            )
        return {
            "analysis_id": str(analysis_id),
            "status": AnalysisReport.Status.FAILED,
            "error_code": type(exc).__name__,
        }


def _claim_analysis_for_execution(
    analysis_id: str,
    task: Task,
) -> tuple[AnalysisReport, bool]:
    """Claim a queued/failed report or safely skip an active duplicate delivery."""
    stale_before = timezone.now() - timedelta(minutes=45)
    with transaction.atomic():
        report = (
            AnalysisReport.objects.select_for_update()
            .select_related("patent", "target_document_version", "created_by")
            .get(pk=analysis_id)
        )
        if report.status in _TERMINAL_STATUSES:
            return report, False
        if report.status == AnalysisReport.Status.RUNNING:
            if report.started_at is not None and report.started_at >= stale_before:
                delivery_info = getattr(task.request, "delivery_info", None) or {}
                is_redelivered = bool(delivery_info.get("redelivered"))
                same_task = report.execution_task_id == task.request.id
                is_celery_retry = int(getattr(task.request, "retries", 0)) > 0
                # Active duplicate tasks are skipped. The same task may reclaim
                # its run after broker redelivery or a scheduled retry.
                if not ((is_redelivered or is_celery_retry) and same_task):
                    return report, False

        if (
            report.patent.tenant_id != report.tenant_id
            or report.target_document_version.tenant_id != report.tenant_id
            or report.target_document_version.patent_id != report.patent_id
        ):
            raise ValueError("Analysis target provenance failed tenant validation.")

        report.status = AnalysisReport.Status.RUNNING
        report.started_at = timezone.now()
        report.completed_at = None
        report.error_code = None
        report.execution_task_id = task.request.id
        report.save(
            update_fields=(
                "status",
                "started_at",
                "completed_at",
                "error_code",
                "execution_task_id",
                "updated_at",
            )
        )
        AuditLog.objects.create(
            tenant_id=report.tenant_id,
            actor_user=report.created_by,
            action="PATENT_ANALYSIS_STARTED",
            resource_type="AnalysisReport",
            resource_id=report.pk,
            request_id=task.request.id,
            outcome=AuditLog.Outcome.SUCCESS,
            metadata={"task_id": task.request.id},
        )
        return report, True


def _load_target_claim(report: AnalysisReport) -> Claim:
    """Resolve and validate the chosen claim within the report's document."""
    if report.target_claim_id:
        claim = (
            Claim.objects.for_tenant(report.tenant_id)
            .select_related("patent", "document_version")
            .filter(
                pk=report.target_claim_id,
                patent_id=report.patent_id,
                document_version_id=report.target_document_version_id,
            )
            .first()
        )
    else:
        claim_number = str((report.configuration or {}).get("target_claim_number", ""))
        claim = (
            Claim.objects.for_tenant(report.tenant_id)
            .select_related("patent", "document_version")
            .filter(
                patent_id=report.patent_id,
                document_version_id=report.target_document_version_id,
                claim_number=claim_number,
            )
            .first()
        )
    if claim is None:
        raise ValueError(
            "Analysis target claim does not exist in the selected version."
        )
    if (
        claim.tenant_id != report.tenant_id
        or claim.patent.tenant_id != report.tenant_id
        or claim.document_version.tenant_id != report.tenant_id
        or claim.document_version.patent_id != report.patent_id
    ):
        raise ValueError("Analysis claim provenance failed tenant validation.")
    return claim


def _mark_analysis_retry_scheduled(
    report: AnalysisReport,
    task: Task,
    error: Exception,
) -> None:
    """Return a retryable run to QUEUED without exposing it as terminally failed."""
    try:
        with transaction.atomic():
            locked = AnalysisReport.objects.select_for_update().get(pk=report.pk)
            if locked.status in _TERMINAL_STATUSES:
                return
            locked.status = AnalysisReport.Status.QUEUED
            locked.error_code = "RETRY_SCHEDULED"
            locked.completed_at = None
            locked.save(
                update_fields=("status", "error_code", "completed_at", "updated_at")
            )
            AuditLog.objects.create(
                tenant_id=locked.tenant_id,
                actor_user=locked.created_by,
                action="PATENT_ANALYSIS_RETRY_SCHEDULED",
                resource_type="AnalysisReport",
                resource_id=locked.pk,
                request_id=task.request.id,
                outcome=AuditLog.Outcome.FAILURE,
                metadata={
                    "error_type": type(error).__name__,
                    "task_id": task.request.id,
                    "retry_attempt": task.request.retries + 1,
                },
            )
    except Exception as audit_exc:
        # Celery retry must still be attempted if the status update or audit DB
        # write fails; the same-task retry recovery path handles a RUNNING row.
        logger.error(
            "Could not persist analysis retry state for analysis_id=%s "
            "error_type=%s.",
            report.pk,
            type(audit_exc).__name__,
        )


def _mark_analysis_failed(
    report: AnalysisReport,
    task: Task,
    error: Exception,
) -> None:
    """Persist a sanitized task failure and tenant-scoped audit record."""
    try:
        with transaction.atomic():
            locked = AnalysisReport.objects.select_for_update().get(pk=report.pk)
            # A task that failed after another execution completed must not
            # overwrite the successful terminal result.
            if locked.status in _TERMINAL_STATUSES:
                return
            locked.status = AnalysisReport.Status.FAILED
            locked.error_code = type(error).__name__[:100]
            locked.completed_at = timezone.now()
            locked.save(
                update_fields=("status", "error_code", "completed_at", "updated_at")
            )
            AuditLog.objects.create(
                tenant_id=locked.tenant_id,
                actor_user=locked.created_by,
                action="PATENT_ANALYSIS_FAILED",
                resource_type="AnalysisReport",
                resource_id=locked.pk,
                request_id=task.request.id,
                outcome=AuditLog.Outcome.FAILURE,
                metadata={
                    "error_type": type(error).__name__,
                    "task_id": task.request.id,
                    "retry_attempt": task.request.retries,
                },
            )
    except Exception:
        logger.exception(
            "Could not persist analysis failure state for analysis_id=%s.",
            report.pk,
        )


def _publish_progress(
    analysis_id: str,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Broadcast progress without allowing Redis outages to fail analysis work."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            f"analysis_{analysis_id}",
            {"type": event_type, "data": data},
        )
    except Exception as exc:
        # Progress is best-effort. The persistent analysis state is authoritative.
        logger.warning(
            "Analysis progress event was not delivered for analysis_id=%s "
            "event=%s error_type=%s.",
            analysis_id,
            event_type,
            type(exc).__name__,
        )
