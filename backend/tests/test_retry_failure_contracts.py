"""Failure-path contracts for asynchronous analysis orchestration."""

from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from analyses.models import AnalysisReport
from analyses.tasks import (
    _claim_analysis_for_execution,
    _mark_analysis_failed,
    _mark_analysis_retry_scheduled,
)
from audit.models import AuditLog
from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.models import Tenant


class AnalysisRetryFailureContractTests(TestCase):
    """Ensure retryable errors and duplicate worker delivery remain safe."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="phase6-retry-contract-user",
            password="not-used-in-force-auth",
        )
        self.tenant = Tenant.objects.create(
            name="Phase 6 Retry Tenant",
            slug="phase6-retry-tenant",
            status=Tenant.Status.ACTIVE,
        )
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="Retry-contract patent",
            created_by=self.user,
        )
        self.version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            version_number=1,
            storage_key="phase6/retry-contract.pdf",
            content_sha256="c" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        self.claim = Claim.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            document_version=self.version,
            claim_number="1",
            claim_text="A processor and a wireless sensor.",
            claim_type="INDEPENDENT",
        )
        self.report = AnalysisReport.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            target_document_version=self.version,
            target_claim=self.claim,
            created_by=self.user,
            status=AnalysisReport.Status.RUNNING,
            started_at=timezone.now(),
            execution_task_id="phase6-task-id",
        )

    @staticmethod
    def _task(task_id: str, retries: int = 0, redelivered: bool = False):
        return SimpleNamespace(
            request=SimpleNamespace(
                id=task_id,
                retries=retries,
                delivery_info={"redelivered": redelivered},
            )
        )

    def test_retryable_failure_returns_analysis_to_queued_without_error_details(
        self,
    ) -> None:
        error = RuntimeError(
            "confidential patent text must not be logged into metadata"
        )

        _mark_analysis_retry_scheduled(
            self.report,
            self._task("phase6-task-id", retries=0),
            error,
        )

        self.report.refresh_from_db()
        self.assertEqual(self.report.status, AnalysisReport.Status.QUEUED)
        self.assertEqual(self.report.error_code, "RETRY_SCHEDULED")
        self.assertIsNone(self.report.completed_at)
        event = AuditLog.objects.get(
            tenant=self.tenant,
            action="PATENT_ANALYSIS_RETRY_SCHEDULED",
            resource_id=self.report.pk,
        )
        self.assertEqual(event.outcome, AuditLog.Outcome.FAILURE)
        self.assertEqual(event.metadata["error_type"], "RuntimeError")
        self.assertNotIn("confidential", str(event.metadata).casefold())

    def test_late_failure_does_not_overwrite_completed_analysis(self) -> None:
        self.report.status = AnalysisReport.Status.COMPLETED
        self.report.completed_at = timezone.now()
        self.report.error_code = None
        self.report.save(
            update_fields=("status", "completed_at", "error_code", "updated_at")
        )

        _mark_analysis_failed(
            self.report,
            self._task("phase6-task-id", retries=3),
            RuntimeError("stale worker failure"),
        )

        self.report.refresh_from_db()
        self.assertEqual(self.report.status, AnalysisReport.Status.COMPLETED)
        self.assertIsNone(self.report.error_code)
        self.assertFalse(
            AuditLog.objects.filter(
                tenant=self.tenant,
                action="PATENT_ANALYSIS_FAILED",
                resource_id=self.report.pk,
            ).exists()
        )

    def test_stale_worker_cannot_reset_a_newer_execution(self) -> None:
        self.report.execution_task_id = "newer-task-id"
        self.report.save(update_fields=("execution_task_id", "updated_at"))
        stale_task = self._task("stale-task-id", retries=1)

        _mark_analysis_retry_scheduled(
            self.report,
            stale_task,
            RuntimeError("stale worker retry"),
        )
        _mark_analysis_failed(
            self.report,
            stale_task,
            RuntimeError("stale worker terminal failure"),
        )

        self.report.refresh_from_db()
        self.assertEqual(self.report.status, AnalysisReport.Status.RUNNING)
        self.assertEqual(self.report.execution_task_id, "newer-task-id")
        self.assertIsNone(self.report.error_code)
        self.assertFalse(
            AuditLog.objects.filter(
                tenant=self.tenant,
                resource_id=self.report.pk,
                action__in=(
                    "PATENT_ANALYSIS_RETRY_SCHEDULED",
                    "PATENT_ANALYSIS_FAILED",
                ),
            ).exists()
        )

    def test_active_duplicate_delivery_is_skipped(self) -> None:
        report, claimed = _claim_analysis_for_execution(
            str(self.report.pk),
            self._task("different-task-id", retries=0),
        )

        self.assertFalse(claimed)
        self.assertEqual(report.execution_task_id, "phase6-task-id")
        self.report.refresh_from_db()
        self.assertEqual(self.report.execution_task_id, "phase6-task-id")

    def test_same_task_retry_can_reclaim_its_running_analysis(self) -> None:
        report, claimed = _claim_analysis_for_execution(
            str(self.report.pk),
            self._task("phase6-task-id", retries=1),
        )

        self.assertTrue(claimed)
        self.assertEqual(report.status, AnalysisReport.Status.RUNNING)
        self.assertEqual(report.execution_task_id, "phase6-task-id")
        self.assertTrue(
            AuditLog.objects.filter(
                tenant=self.tenant,
                action="PATENT_ANALYSIS_STARTED",
                resource_id=self.report.pk,
            ).exists()
        )
