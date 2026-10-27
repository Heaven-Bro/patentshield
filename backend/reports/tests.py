"""Regression tests for attorney claim charts, immutable revisions, and exports."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from openpyxl import load_workbook
from rest_framework.test import APIClient

from analyses.models import AnalysisReport, ClaimAssessment, EvidenceReference
from patents.models import Claim, Patent, PatentDocumentVersion
from reports.claim_chart_exports import render_claim_chart
from reports.models import ClaimChart, ClaimChartExport, ClaimChartRevision
from tenants.models import Tenant, TenantMembership


class ClaimChartAPITests(TestCase):
    """Exercise tenant-scoped chart creation, review edits, and export dispatch."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="chart-attorney",
            password="test-password",
        )
        self.tenant = Tenant.objects.create(name="Chart Tenant", slug="chart-tenant")
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.ATTORNEY,
            status=TenantMembership.Status.ACTIVE,
        )
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            publication_number="US-2026-0123456-A1",
            application_number="US-18/123456",
            jurisdiction="US",
            title="Wireless Sensor Device",
            abstract="An example device with a wireless sensor.",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        self.version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            version_number=1,
            storage_key="tests/chart-patent.pdf",
            content_sha256="a" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
            page_count=4,
        )
        self.claim = Claim.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            document_version=self.version,
            claim_number="1",
            claim_text="A device comprising a wireless sensor and a processor.",
            claim_type="INDEPENDENT",
        )
        self.analysis = AnalysisReport.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            target_document_version=self.version,
            target_claim=self.claim,
            analysis_type="PRIOR_ART_OVERLAP",
            status=AnalysisReport.Status.COMPLETED,
            result_summary={"metrics": {"screening_priority_score": 73.5}},
            created_by=self.user,
        )
        self.assessment = ClaimAssessment.objects.create(
            tenant=self.tenant,
            analysis=self.analysis,
            claim=self.claim,
            limitation_index=1,
            limitation_text="a wireless sensor",
            assessment_status=ClaimAssessment.AssessmentStatus.DIRECT_SUPPORT,
            explanation="The candidate describes a wireless sensor.",
            confidence=0.82,
            metrics={"retrieval_relevance": 0.8, "technical_correspondence": 0.7},
        )
        EvidenceReference.objects.create(
            tenant=self.tenant,
            assessment=self.assessment,
            source_patent=self.patent,
            source_document_version=self.version,
            source_section="CLAIM",
            source_page_index=1,
            source_start_offset=12,
            source_end_offset=32,
            exact_quote="a wireless sensor",
            quote_verified=True,
            bbox={"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.04},
            retrieval_metadata={"dense_score": 0.8, "api_key": "must-not-export"},
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.headers = {"HTTP_X_TENANT_ID": str(self.tenant.pk)}

    def create_chart(self) -> dict:
        response = self.client.post(
            "/api/v1/reports/claim-charts/",
            {"analysis_id": str(self.analysis.pk), "title": "Sensor Claim Chart"},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["chart"]

    def test_create_chart_copies_assessments_and_canonical_evidence(self) -> None:
        chart_payload = self.create_chart()
        self.assertEqual(chart_payload["revision"], 1)
        self.assertEqual(len(chart_payload["snapshot"]["rows"]), 1)
        row = chart_payload["snapshot"]["rows"][0]
        self.assertEqual(row["limitation_text"], "a wireless sensor")
        self.assertEqual(row["evidence"][0]["exact_quote"], "a wireless sensor")
        self.assertTrue(row["evidence"][0]["quote_verified"])
        self.assertEqual(
            row["evidence"][0]["retrieval_metadata"]["api_key"],
            "[REDACTED]",
        )
        revision_count = ClaimChartRevision.objects.filter(
            chart_id=chart_payload["chart_id"]
        ).count()
        self.assertEqual(revision_count, 1)

    def test_row_edit_creates_new_immutable_revision(self) -> None:
        chart_payload = self.create_chart()
        chart_id = chart_payload["chart_id"]
        row_id = chart_payload["snapshot"]["rows"][0]["row_id"]
        response = self.client.patch(
            f"/api/v1/reports/claim-charts/{chart_id}/rows/{row_id}/",
            {
                "attorney_notes": "Review the figure reference before filing.",
                "attorney_decision": "CORRECTED",
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["chart"]["revision"], 2)
        revisions = list(
            ClaimChartRevision.objects.filter(chart_id=chart_id).order_by(
                "revision_number"
            )
        )
        self.assertEqual([revision.revision_number for revision in revisions], [1, 2])
        self.assertEqual(revisions[0].snapshot["rows"][0]["attorney_notes"], "")
        self.assertEqual(
            revisions[1].snapshot["rows"][0]["attorney_notes"],
            "Review the figure reference before filing.",
        )
        self.assertEqual(len(revisions[1].snapshot_sha256), 64)

    def test_wrong_tenant_cannot_create_chart_from_foreign_analysis(self) -> None:
        foreign_tenant = Tenant.objects.create(
            name="Foreign Tenant",
            slug="foreign-tenant",
        )
        foreign_patent = Patent.objects.create(
            tenant=foreign_tenant,
            title="Foreign patent",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        foreign_version = PatentDocumentVersion.objects.create(
            tenant=foreign_tenant,
            patent=foreign_patent,
            version_number=1,
            storage_key="tests/foreign.pdf",
            content_sha256="b" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        foreign_analysis = AnalysisReport.objects.create(
            tenant=foreign_tenant,
            patent=foreign_patent,
            target_document_version=foreign_version,
            status=AnalysisReport.Status.COMPLETED,
            created_by=self.user,
        )
        response = self.client.post(
            "/api/v1/reports/claim-charts/",
            {"analysis_id": str(foreign_analysis.pk)},
            format="json",
            **self.headers,
        )
        self.assertIn(response.status_code, (400, 404, 409))
        self.assertFalse(ClaimChart.objects.filter(analysis=foreign_analysis).exists())

    def test_chart_cannot_be_approved_until_every_row_has_a_decision(self) -> None:
        chart_payload = self.create_chart()
        chart_id = chart_payload["chart_id"]
        row_id = chart_payload["snapshot"]["rows"][0]["row_id"]

        review_response = self.client.patch(
            f"/api/v1/reports/claim-charts/{chart_id}/",
            {"status": "IN_REVIEW"},
            format="json",
            **self.headers,
        )
        self.assertEqual(review_response.status_code, 200, review_response.data)

        blocked_response = self.client.patch(
            f"/api/v1/reports/claim-charts/{chart_id}/",
            {"status": "APPROVED"},
            format="json",
            **self.headers,
        )
        self.assertEqual(blocked_response.status_code, 409, blocked_response.data)

        row_response = self.client.patch(
            f"/api/v1/reports/claim-charts/{chart_id}/rows/{row_id}/",
            {"attorney_decision": "ACCEPTED"},
            format="json",
            **self.headers,
        )
        self.assertEqual(row_response.status_code, 200, row_response.data)

        approved_response = self.client.patch(
            f"/api/v1/reports/claim-charts/{chart_id}/",
            {"status": "APPROVED"},
            format="json",
            **self.headers,
        )
        self.assertEqual(approved_response.status_code, 200, approved_response.data)
        self.assertEqual(approved_response.data["chart"]["status"], "APPROVED")

    @patch("reports.claim_chart_views.generate_claim_chart_export.apply_async")
    def test_export_request_pins_the_current_revision(self, apply_async) -> None:
        chart_payload = self.create_chart()
        chart_id = chart_payload["chart_id"]
        response = self.client.post(
            f"/api/v1/reports/claim-charts/{chart_id}/exports/",
            {"format": "XLSX"},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 202, response.data)
        export = ClaimChartExport.objects.get(pk=response.data["export_id"])
        self.assertEqual(export.format, ClaimChartExport.Format.XLSX)
        self.assertEqual(export.status, ClaimChartExport.Status.QUEUED)
        self.assertEqual(export.revision.revision_number, 1)
        apply_async.assert_called_once()

    def test_foreign_tenant_cannot_read_chart_or_export(self) -> None:
        chart_payload = self.create_chart()
        chart_id = chart_payload["chart_id"]
        other_tenant = Tenant.objects.create(name="Other tenant", slug="other-tenant")
        response = self.client.get(
            f"/api/v1/reports/claim-charts/{chart_id}/",
            HTTP_X_TENANT_ID=str(other_tenant.pk),
        )
        self.assertIn(response.status_code, (403, 404))


class ClaimChartRendererTests(TestCase):
    """Verify generated files are structurally readable and contain the chart data."""

    def sample_snapshot(self) -> dict:
        return {
            "schema_version": "patentshield-claim-chart-v1",
            "generated_at": "2026-10-27T00:00:00+00:00",
            "tenant_id": "0b1a9461-e6e2-4df0-9d74-9c41b49f5879",
            "chart": {
                "chart_id": "33f5fb0f-63dc-4ee2-85e7-e6b5bbf46329",
                "title": "Sample Claim Chart",
                "status": "DRAFT",
                "revision": 1,
                "description": "Reviewable snapshot.",
            },
            "analysis": {
                "analysis_id": "2a4e0fbb-df69-49c9-8bd1-7b7a7a311acb",
                "status": "COMPLETED",
                "metrics": {"screening_priority_score": 70.0},
            },
            "target_patent": {
                "publication_number": "US-2026-0123456-A1",
                "jurisdiction": "US",
                "title": "Wireless Sensor",
            },
            "target_document_version": {
                "version_number": 1,
                "content_sha256": "a" * 64,
            },
            "target_claim": {
                "claim_number": "1",
                "claim_text": "A sensor comprising a transceiver.",
            },
            "rows": [
                {
                    "limitation_index": 1,
                    "claim_number": "1",
                    "limitation_text": "a transceiver",
                    "mapping_status": "DIRECT_SUPPORT",
                    "mapping_summary": "Textual overlap for review.",
                    "confidence": 0.8,
                    "attorney_decision": "UNREVIEWED",
                    "attorney_notes": "",
                    "evidence": [
                        {
                            "source_publication_number": "US-2025-0000001-A1",
                            "source_jurisdiction": "US",
                            "source_section": "CLAIM",
                            "page_number": 2,
                            "exact_quote": "a transceiver",
                            "quote_verified": True,
                        }
                    ],
                }
            ],
            "disclaimer": (
                "AI-assisted research work product; attorney review required."
            ),
        }

    def test_all_four_export_formats_are_generated(self) -> None:
        snapshot = self.sample_snapshot()
        docx, docx_type, _ = render_claim_chart(snapshot, "DOCX")
        xlsx, xlsx_type, _ = render_claim_chart(snapshot, "XLSX")
        pdf, pdf_type, _ = render_claim_chart(snapshot, "PDF")
        json_content, json_type, _ = render_claim_chart(snapshot, "JSON")
        self.assertTrue(docx.startswith(b"PK"))
        self.assertTrue(xlsx.startswith(b"PK"))
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(
            json.loads(json_content)["chart"]["title"], "Sample Claim Chart"
        )
        self.assertIn("wordprocessingml", docx_type)
        self.assertIn("spreadsheetml", xlsx_type)
        self.assertEqual(pdf_type, "application/pdf")
        self.assertEqual(json_type, "application/json")

    def test_xlsx_treats_untrusted_text_as_text_not_a_formula(self) -> None:
        snapshot = self.sample_snapshot()
        snapshot["chart"]["title"] = '=HYPERLINK("https://attacker.example")'
        snapshot["rows"][0]["limitation_text"] = "=1+1"
        snapshot["rows"][0]["attorney_notes"] = "@SUM(1,1)"
        snapshot["rows"][0]["evidence"][0]["exact_quote"] = (
            '=WEBSERVICE("https://attacker.example")'
        )

        content, _, _ = render_claim_chart(snapshot, "XLSX")
        workbook = load_workbook(io.BytesIO(content), data_only=False)
        sheet = workbook["Claim Chart"]
        self.assertTrue(sheet["B1"].value.startswith("'=HYPERLINK("))

        header_row = next(
            row_index
            for row_index in range(1, sheet.max_row + 1)
            if sheet.cell(row_index, 1).value == "Limitation Index"
        )
        headers = [cell.value for cell in sheet[header_row]]
        data_row = header_row + 1
        limitation_column = headers.index("Limitation Text") + 1
        notes_column = headers.index("Attorney Notes") + 1
        quote_column = headers.index("Exact Evidence Quote") + 1
        for column, prefix in (
            (limitation_column, "'=1+1"),
            (notes_column, "'@SUM(1,1)"),
            (quote_column, "'=WEBSERVICE("),
        ):
            cell = sheet.cell(data_row, column)
            self.assertTrue(cell.value.startswith(prefix))
            self.assertNotEqual(cell.data_type, "f")

    def test_unsupported_export_format_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render_claim_chart(self.sample_snapshot(), "HTML")
