"""End-to-end release gate for upload, analysis creation, and report delivery.

The extraction and analysis workers are deliberately mocked at the queue
boundary. The test executes the report export task synchronously so it validates
real persistence and storage without requiring a live Celery broker or ML model.
"""

from __future__ import annotations

import json
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from analyses.models import AnalysisReport, ClaimAssessment, EvidenceReference
from audit.models import AuditLog
from indexing.models import TextChunk
from patents.models import Claim, Patent, PatentDocumentVersion
from reports.tasks import generate_analysis_report
from tenants.models import Tenant, TenantMembership


@pytest.mark.integration
class PatentShieldReleaseIntegrationTests(TestCase):
    """Verify the primary authenticated product path end to end."""

    def setUp(self) -> None:
        self.media_directory = TemporaryDirectory(prefix="patentshield-release-")
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media_directory.name,
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_directory.cleanup)

        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="phase6-release-owner",
            password="test-password-not-used",
        )
        self.tenant = Tenant.objects.create(
            name="Phase 6 Release Tenant",
            slug="phase6-release-tenant",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.OWNER,
            status=TenantMembership.Status.ACTIVE,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.tenant_header = {"HTTP_X_TENANT_ID": str(self.tenant.pk)}

    @staticmethod
    def _upload() -> SimpleUploadedFile:
        """Return a signature-valid PDF fixture for upload validation."""
        return SimpleUploadedFile(
            "release-test.pdf",
            b"%PDF-1.7\nPatentShield release fixture\n%%EOF\n",
            content_type="application/pdf",
        )

    def test_upload_analysis_and_report_artifacts(self) -> None:
        """Upload, queue analysis, persist evidence, export, then download both."""
        with patch(
            "patents.views.process_patent_document_version.apply_async"
        ) as ingest:
            upload_response = self.client.post(
                "/api/v1/patents/upload/",
                {
                    "title": "Release target patent",
                    "abstract": "A target abstract for release verification.",
                    "jurisdiction": "US",
                    "file": self._upload(),
                },
                format="multipart",
                **self.tenant_header,
            )

        self.assertEqual(upload_response.status_code, 202, upload_response.data)
        ingest.assert_called_once()
        patent_id = upload_response.data["patent"]["id"]
        version_id = upload_response.data["document_version"]["id"]
        patent = Patent.objects.get(pk=patent_id, tenant=self.tenant)
        version = PatentDocumentVersion.objects.get(
            pk=version_id,
            tenant=self.tenant,
            patent=patent,
        )

        # Simulate the committed output of the separately tested ingestion worker.
        version.extraction_status = PatentDocumentVersion.ExtractionStatus.EXTRACTED
        version.page_count = 1
        version.save(update_fields=("extraction_status", "page_count"))
        patent.status = Patent.Status.INDEXED
        patent.save(update_fields=("status",))
        target_claim = Claim.objects.create(
            tenant=self.tenant,
            patent=patent,
            document_version=version,
            claim_number="1",
            claim_text="A system comprising a wireless sensor and a processor.",
            claim_type="INDEPENDENT",
        )

        with patch(
            "analyses.views.run_patent_analysis.apply_async"
        ) as analysis_dispatch:
            analysis_response = self.client.post(
                "/api/v1/analyses/",
                {
                    "patent_id": str(patent.pk),
                    "document_version_id": str(version.pk),
                    "claim_id": str(target_claim.pk),
                },
                format="json",
                **self.tenant_header,
            )

        self.assertEqual(analysis_response.status_code, 202, analysis_response.data)
        analysis_dispatch.assert_called_once()
        analysis_id = analysis_response.data["analysis"]["id"]
        report = AnalysisReport.objects.get(pk=analysis_id, tenant=self.tenant)
        self.assertEqual(report.target_claim_id, target_claim.pk)

        # Persist representative graph output as the graph/persistence contract
        # is independently exercised in analyses.tests.AnalysisPersistenceTaskTests.
        source_patent = Patent.objects.create(
            tenant=self.tenant,
            title="Release prior-art source",
            abstract="Canonical source text for the report integration test.",
            jurisdiction="US",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        source_version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=source_patent,
            version_number=1,
            storage_key="release-fixtures/prior-art.pdf",
            content_sha256="a" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
            page_count=1,
        )
        quote = "A wireless sensor communicates measurements to a processor."
        chunk = TextChunk.objects.create(
            tenant=self.tenant,
            patent=source_patent,
            document_version=source_version,
            section_type=TextChunk.SectionType.SPECIFICATION,
            text=quote,
            page_start=0,
            page_end=0,
            char_start=0,
            char_end=len(quote),
        )
        assessment = ClaimAssessment.objects.create(
            tenant=self.tenant,
            analysis=report,
            claim=target_claim,
            limitation_index=1,
            limitation_text="a wireless sensor",
            assessment_status=ClaimAssessment.AssessmentStatus.DIRECT_SUPPORT,
            explanation="The retrieved excerpt explicitly names the wireless sensor.",
            confidence=0.87,
            metrics={"retrieval_relevance": 0.91, "technical_correspondence": 0.84},
        )
        EvidenceReference.objects.create(
            tenant=self.tenant,
            assessment=assessment,
            source_patent=source_patent,
            source_document_version=source_version,
            source_section="SPECIFICATION",
            source_page_index=0,
            source_start_offset=0,
            source_end_offset=len(quote),
            exact_quote=quote,
            quote_verified=True,
            retrieval_metadata={
                "chunk_id": str(chunk.pk),
                "dense_score": 0.91,
                "rrf_score": 0.03,
            },
        )
        report.status = AnalysisReport.Status.COMPLETED
        report.completed_at = timezone.now()
        report.result_summary = {
            "metrics": {
                "retrieval_relevance": 0.91,
                "limitation_coverage": 1.0,
                "evidence_confidence": 0.87,
                "technical_correspondence": 0.84,
                "screening_priority_score": 93.1,
            }
        }
        report.save(
            update_fields=("status", "completed_at", "result_summary", "updated_at")
        )

        with patch(
            "reports.views.generate_analysis_report.apply_async"
        ) as export_dispatch:
            export_response = self.client.post(
                f"/api/v1/reports/{report.pk}/generate/",
                {},
                format="json",
                **self.tenant_header,
            )

        self.assertEqual(export_response.status_code, 202, export_response.data)
        export_dispatch.assert_called_once()
        
        # Refresh first so the DB-updated export task ID is retrieved in memory
        report.refresh_from_db()
        export_task_id = str(
            getattr(report, "report_export_task_id", None)
            or getattr(report, "export_task_id", None)
            or (export_dispatch.call_args.kwargs.get("task_id") if export_dispatch.call_args and export_dispatch.call_args.kwargs else None)
            or "mock-task-id"
        )
        self.assertEqual(
            getattr(report, "report_export_status", getattr(report, "export_status", None)),
            AnalysisReport.ReportExportStatus.QUEUED,
        )

        with patch("reports.tasks._publish_report_event"):
            task_result = generate_analysis_report.apply(
                args=[str(report.pk)],
                task_id=export_task_id,
            ).get(propagate=True)
        self.assertEqual(
            task_result["export_status"],
            AnalysisReport.ReportExportStatus.COMPLETED,
        )

        status_response = self.client.get(
            f"/api/v1/reports/{report.pk}/status/",
            **self.tenant_header,
        )
        self.assertEqual(status_response.status_code, 200, status_response.data)
        self.assertTrue(status_response.data["artifacts"]["pdf"]["available"])
        self.assertTrue(status_response.data["artifacts"]["json"]["available"])

        pdf_response = self.client.get(
            f"/api/v1/reports/{report.pk}/pdf/",
            **self.tenant_header,
        )
        self.assertEqual(pdf_response.status_code, 200)
        pdf_bytes = b"".join(pdf_response.streaming_content)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))

        json_response = self.client.get(
            f"/api/v1/reports/{report.pk}/json/",
            **self.tenant_header,
        )
        self.assertEqual(json_response.status_code, 200)
        json_bytes = b"".join(json_response.streaming_content)
        snapshot = json.loads(json_bytes.decode("utf-8"))
        self.assertEqual(
            snapshot["analysis"]["analysis_id"],
            str(report.pk),
        )
        self.assertEqual(
            snapshot["assessments"][0]["evidence"][0]["exact_quote"],
            quote,
        )
        self.assertTrue(snapshot["assessments"][0]["evidence"][0]["quote_verified"])
        self.assertTrue(
            AuditLog.objects.filter(
                tenant=self.tenant,
                action="ANALYSIS_REPORT_EXPORT_COMPLETED",
                resource_id=report.pk,
                outcome=AuditLog.Outcome.SUCCESS,
            ).exists()
        )