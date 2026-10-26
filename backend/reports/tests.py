"""Integration tests for tenant-scoped report exports and lifecycle semantics."""

from __future__ import annotations

from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from analyses.models import AnalysisReport
from patents.models import Patent, PatentDocumentVersion
from reports.tasks import generate_analysis_report
from tenants.models import Tenant, TenantMembership


class ReportExportAPITests(TestCase):
    """Exercise report API authorization and queue-state behavior."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="report-export-user",
            password="not-used-in-force-auth",
        )
        self.tenant = Tenant.objects.create(
            name="Report Export Tenant",
            slug="report-export-tenant",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.ANALYST,
            status=TenantMembership.Status.ACTIVE,
        )
        self.other_tenant = Tenant.objects.create(
            name="Other Report Tenant",
            slug="other-report-tenant",
            status=Tenant.Status.ACTIVE,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="Export test patent",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        self.version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            version_number=1,
            storage_key="private/export-test.pdf",
            content_sha256="e" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        self.report = self._create_report(self.tenant, self.patent, self.version)

    def _create_report(self, tenant, patent, version, **overrides):
        fields = {
            "tenant": tenant,
            "patent": patent,
            "target_document_version": version,
            "created_by": self.user,
            "status": AnalysisReport.Status.COMPLETED_WITH_WARNINGS,
            "result_summary": {
                "metrics": {
                    "retrieval_relevance": 0.8,
                    "limitation_coverage": 0.5,
                    "evidence_confidence": 0.7,
                    "technical_correspondence": 0.6,
                    "screening_priority_score": 65.0,
                }
            },
        }
        fields.update(overrides)
        return AnalysisReport.objects.create(**fields)

    def test_generate_queues_export_without_overwriting_analysis_status(self) -> None:
        task = SimpleNamespace(id="export-test-task-id")
        with patch(
            "reports.views.generate_analysis_report.apply_async",
            return_value=task,
        ) as dispatch:
            response = self.client.post(
                f"/api/v1/reports/{self.report.pk}/generate/",
                {},
                format="json",
                HTTP_X_TENANT_ID=str(self.tenant.pk),
            )

        self.assertEqual(response.status_code, 202)
        dispatch.assert_called_once()
        self.report.refresh_from_db()
        self.assertEqual(
            self.report.status,
            AnalysisReport.Status.COMPLETED_WITH_WARNINGS,
        )
        self.assertEqual(
            self.report.report_export_status,
            AnalysisReport.ReportExportStatus.QUEUED,
        )
        self.assertEqual(self.report.report_export_task_id, "export-test-task-id")
        self.assertIn("status/", response.data["status_url"])

    def test_queue_failure_changes_only_export_status(self) -> None:
        with patch(
            "reports.views.generate_analysis_report.apply_async",
            side_effect=RuntimeError("redis unavailable"),
        ):
            response = self.client.post(
                f"/api/v1/reports/{self.report.pk}/generate/",
                {},
                format="json",
                HTTP_X_TENANT_ID=str(self.tenant.pk),
            )

        self.assertEqual(response.status_code, 503)
        self.report.refresh_from_db()
        self.assertEqual(
            self.report.status, AnalysisReport.Status.COMPLETED_WITH_WARNINGS
        )
        self.assertEqual(
            self.report.report_export_status,
            AnalysisReport.ReportExportStatus.FAILED,
        )
        self.assertEqual(
            self.report.report_export_error_code,
            "QUEUE_SUBMISSION_FAILED",
        )

    def test_status_does_not_advertise_artifacts_before_export_completes(self) -> None:
        self.report.report_storage_key = "reports/tenant/report.pdf"
        self.report.report_json_storage_key = "reports/tenant/report.json"
        self.report.report_export_status = AnalysisReport.ReportExportStatus.QUEUED
        self.report.save(
            update_fields=(
                "report_storage_key",
                "report_json_storage_key",
                "report_export_status",
            )
        )
        with patch("reports.views.default_storage.exists", return_value=True):
            response = self.client.get(
                f"/api/v1/reports/{self.report.pk}/status/",
                HTTP_X_TENANT_ID=str(self.tenant.pk),
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["artifacts"]["pdf"]["available"])
        self.assertFalse(response.data["artifacts"]["json"]["available"])

    def test_status_endpoint_hides_another_tenants_analysis(self) -> None:
        other_patent = Patent.objects.create(
            tenant=self.other_tenant,
            title="Other tenant patent",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        other_version = PatentDocumentVersion.objects.create(
            tenant=self.other_tenant,
            patent=other_patent,
            version_number=1,
            storage_key="private/other-tenant.pdf",
            content_sha256="f" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        other_report = self._create_report(
            self.other_tenant,
            other_patent,
            other_version,
        )
        response = self.client.get(
            f"/api/v1/reports/{other_report.pk}/status/",
            HTTP_X_TENANT_ID=str(self.tenant.pk),
        )
        self.assertEqual(response.status_code, 404)


class ReportExportTaskTests(TestCase):
    """Verify an export persists both files but preserves analysis status."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="report-task-user",
            password="not-used-in-force-auth",
        )
        self.tenant = Tenant.objects.create(
            name="Report Task Tenant",
            slug="report-task-tenant",
            status=Tenant.Status.ACTIVE,
        )
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="Report task patent",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        self.version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            version_number=1,
            storage_key="private/report-task.pdf",
            content_sha256="a" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        self.report = AnalysisReport.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            target_document_version=self.version,
            created_by=self.user,
            status=AnalysisReport.Status.COMPLETED_WITH_WARNINGS,
            result_summary={"metrics": {"screening_priority_score": 42.0}},
            report_export_status=AnalysisReport.ReportExportStatus.QUEUED,
            report_export_task_id="report-task-test-id",
        )

    def test_export_writes_json_pdf_and_keeps_analysis_status(self) -> None:
        with TemporaryDirectory(prefix="patentshield-reports-") as temp_dir:
            with override_settings(MEDIA_ROOT=temp_dir):
                with (
                    patch("reports.tasks._render_pdf", return_value=b"%PDF-1.4\\nTest"),
                    patch("reports.tasks._publish_report_event"),
                ):
                    result = generate_analysis_report.apply(
                        args=[str(self.report.pk)],
                        task_id="report-task-test-id",
                    ).get()

                self.report.refresh_from_db()
                self.assertEqual(
                    result["export_status"],
                    AnalysisReport.ReportExportStatus.COMPLETED,
                )
                self.assertTrue(self.report.report_json_storage_key)
                self.assertTrue(self.report.report_storage_key)
                self.assertEqual(
                    self.report.status,
                    AnalysisReport.Status.COMPLETED_WITH_WARNINGS,
                )
                self.assertEqual(
                    self.report.report_export_status,
                    AnalysisReport.ReportExportStatus.COMPLETED,
                )

                from django.core.files.storage import default_storage

                self.assertTrue(
                    default_storage.exists(self.report.report_json_storage_key)
                )
                self.assertTrue(default_storage.exists(self.report.report_storage_key))
                with default_storage.open(
                    self.report.report_json_storage_key, "r"
                ) as handle:
                    self.assertIn("patentshield-analysis-report-v1", handle.read())
