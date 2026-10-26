"""Regression tests for analysis creation, tenant isolation, and persistence."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from analyses.models import AnalysisReport, ClaimAssessment, EvidenceReference
from analyses.tasks import run_patent_analysis
from indexing.models import TextChunk
from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership


class AnalysisLifecycleAPITests(TestCase):
    """Exercise tenant-scoped creation and background dispatch contracts."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user_a = user_model.objects.create_user(
            username="phase3-analyst-a",
            password="test-password-not-used",
        )
        self.user_b = user_model.objects.create_user(
            username="phase3-analyst-b",
            password="test-password-not-used",
        )
        self.tenant_a = Tenant.objects.create(
            name="Phase 3 Tenant A",
            slug="phase3-tenant-a",
            status=Tenant.Status.ACTIVE,
        )
        self.tenant_b = Tenant.objects.create(
            name="Phase 3 Tenant B",
            slug="phase3-tenant-b",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant_a,
            user=self.user_a,
            role=TenantMembership.Role.ANALYST,
            status=TenantMembership.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant_b,
            user=self.user_b,
            role=TenantMembership.Role.OWNER,
            status=TenantMembership.Status.ACTIVE,
        )
        self.patent_a = Patent.objects.create(
            tenant=self.tenant_a,
            title="Target patent for phase 3",
            status=Patent.Status.INDEXED,
            created_by=self.user_a,
        )
        self.version_a = PatentDocumentVersion.objects.create(
            tenant=self.tenant_a,
            patent=self.patent_a,
            version_number=1,
            storage_key="private/tenant-a/target.pdf",
            content_sha256="a" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
            page_count=1,
        )
        self.claim_a = Claim.objects.create(
            tenant=self.tenant_a,
            patent=self.patent_a,
            document_version=self.version_a,
            claim_number="1",
            claim_text="A wireless sensor comprising a processor and a memory.",
            claim_type="INDEPENDENT",
        )
        self.patent_b = Patent.objects.create(
            tenant=self.tenant_b,
            title="Tenant B private patent",
            status=Patent.Status.INDEXED,
            created_by=self.user_b,
        )
        self.version_b = PatentDocumentVersion.objects.create(
            tenant=self.tenant_b,
            patent=self.patent_b,
            version_number=1,
            storage_key="private/tenant-b/secret.pdf",
            content_sha256="b" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
            page_count=1,
        )
        self.claim_b = Claim.objects.create(
            tenant=self.tenant_b,
            patent=self.patent_b,
            document_version=self.version_b,
            claim_number="1",
            claim_text="A confidential device comprising a restricted component.",
            claim_type="INDEPENDENT",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user_a)

    @patch("analyses.views.run_patent_analysis.apply_async")
    def test_create_analysis_persists_target_and_enqueues_task(
        self,
        dispatch_mock: MagicMock,
    ) -> None:
        response = self.client.post(
            "/api/v1/analyses/",
            {
                "patent_id": str(self.patent_a.pk),
                "document_version_id": str(self.version_a.pk),
                "claim_id": str(self.claim_a.pk),
            },
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )

        self.assertEqual(response.status_code, 202, response.data)
        report = AnalysisReport.objects.get(pk=response.data["analysis"]["id"])
        self.assertEqual(report.tenant_id, self.tenant_a.pk)
        self.assertEqual(report.target_claim_id, self.claim_a.pk)
        self.assertEqual(report.status, AnalysisReport.Status.QUEUED)
        dispatch_mock.assert_called_once_with(
            args=[str(report.pk)],
            queue="analysis",
        )

    @patch("analyses.views.run_patent_analysis.apply_async")
    def test_cross_tenant_target_claim_is_not_disclosed(
        self,
        dispatch_mock: MagicMock,
    ) -> None:
        response = self.client.post(
            "/api/v1/analyses/",
            {
                "patent_id": str(self.patent_b.pk),
                "document_version_id": str(self.version_b.pk),
                "claim_id": str(self.claim_b.pk),
            },
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(AnalysisReport.objects.exists())
        dispatch_mock.assert_not_called()

    @patch(
        "analyses.views.run_patent_analysis.apply_async",
        side_effect=RuntimeError("simulated broker outage"),
    )
    def test_broker_failure_is_visible_and_retryable(
        self,
        dispatch_mock: MagicMock,
    ) -> None:
        response = self.client.post(
            "/api/v1/analyses/",
            {
                "patent_id": str(self.patent_a.pk),
                "document_version_id": str(self.version_a.pk),
                "claim_id": str(self.claim_a.pk),
            },
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )

        self.assertEqual(response.status_code, 503)
        report = AnalysisReport.objects.get(pk=response.data["analysis"]["id"])
        self.assertEqual(report.status, AnalysisReport.Status.FAILED)
        self.assertEqual(report.error_code, "QUEUE_SUBMISSION_FAILED")
        self.assertIn("retry/", response.data["retry_url"])
        dispatch_mock.assert_called_once()


class AnalysisPersistenceTaskTests(TestCase):
    """Verify graph output becomes durable assessments and canonical citations."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="phase3-persistence-user",
            password="test-password-not-used",
        )
        self.tenant = Tenant.objects.create(
            name="Phase 3 Persistence Tenant",
            slug="phase3-persistence-tenant",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.ANALYST,
            status=TenantMembership.Status.ACTIVE,
        )
        self.target_patent = Patent.objects.create(
            tenant=self.tenant,
            title="Target patent",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        self.target_version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=self.target_patent,
            version_number=1,
            storage_key="private/target.pdf",
            content_sha256="c" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        self.target_claim = Claim.objects.create(
            tenant=self.tenant,
            patent=self.target_patent,
            document_version=self.target_version,
            claim_number="1",
            claim_text="A processor configured to control a wireless sensor.",
            claim_type="INDEPENDENT",
        )
        self.source_patent = Patent.objects.create(
            tenant=self.tenant,
            title="Prior-art source",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        self.source_version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=self.source_patent,
            version_number=1,
            storage_key="private/source.pdf",
            content_sha256="d" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        self.source_text = (
            "A wireless sensor communicates with a processor that controls operation."
        )
        self.chunk = TextChunk.objects.create(
            tenant=self.tenant,
            patent=self.source_patent,
            document_version=self.source_version,
            section_type=TextChunk.SectionType.SPECIFICATION,
            text=self.source_text,
            page_start=0,
            page_end=0,
            char_start=0,
            char_end=len(self.source_text),
        )
        self.analysis = AnalysisReport.objects.create(
            tenant=self.tenant,
            patent=self.target_patent,
            target_document_version=self.target_version,
            target_claim=self.target_claim,
            analysis_type="PRIOR_ART_OVERLAP",
            configuration={"target_claim_number": self.target_claim.claim_number},
            created_by=self.user,
            status=AnalysisReport.Status.QUEUED,
        )

    def test_graph_output_persists_assessment_and_verified_evidence(self) -> None:
        limitation_id = "limitation-one"
        assessment_payload = {
            "limitation_id": limitation_id,
            "status": "DIRECT_SUPPORT",
            "explanation": "The candidate mentions a wireless sensor and processor.",
            "confidence": 0.82,
            "retrieval_relevance": 0.88,
            "technical_correspondence": 0.8,
            "quote_verified": True,
            "evidence": [
                {
                    "evidence_id": "evidence-one",
                    "chunk_id": str(self.chunk.pk),
                    "source_patent_id": str(self.source_patent.pk),
                    "source_version_id": str(self.source_version.pk),
                    "exact_quote": self.source_text,
                    "page_index": 0,
                    "section_type": "SPECIFICATION",
                    "retrieval_score": 0.88,
                    "dense_score": 0.88,
                    "lexical_score": 1.5,
                    "rrf_score": 0.03,
                    "rerank_score": 0.91,
                    "quote_verified": True,
                }
            ],
        }

        class FakeGraph:
            def stream(self, initial_state: dict, stream_mode: str):
                assert stream_mode == "updates"
                yield {
                    "deconstruct_claim": {
                        "limitations": [
                            {
                                "limitation_id": limitation_id,
                                "limitation_index": 1,
                                "text": (
                                    "A processor configured to control a wireless "
                                    "sensor."
                                ),
                                "evidence_requirement": (
                                    "Find support for a processor and wireless sensor."
                                ),
                            }
                        ],
                        "status": "DECONSTRUCTED",
                        "error_message": None,
                    }
                }
                yield {
                    "hybrid_retrieval": {
                        "status": "RETRIEVED",
                        "retrieved_evidence": [],
                    }
                }
                yield {
                    "limitation_evaluator": {
                        "assessments": [assessment_payload],
                        "status": "COMPLETED",
                    }
                }
                yield {
                    "groundedness_guardrail": {
                        "assessments": [assessment_payload],
                        "status": "COMPLETED",
                    }
                }
                yield {
                    "scoring": {
                        "metrics": {
                            "retrieval_relevance": 0.88,
                            "limitation_coverage": 1.0,
                            "evidence_confidence": 0.82,
                            "technical_correspondence": 0.8,
                            "screening_priority_score": 88.0,
                        }
                    }
                }

        with (
            patch("ai_engine.graph.compile_analysis_graph", return_value=FakeGraph()),
            patch("analyses.tasks._publish_progress"),
        ):
            result = run_patent_analysis.run(str(self.analysis.pk))

        self.assertEqual(result["status"], AnalysisReport.Status.COMPLETED)
        self.assertEqual(
            ClaimAssessment.objects.filter(analysis=self.analysis).count(),
            1,
        )
        persisted_assessment = ClaimAssessment.objects.get(analysis=self.analysis)
        self.assertEqual(persisted_assessment.claim_id, self.target_claim.pk)
        self.assertEqual(persisted_assessment.assessment_status, "DIRECT_SUPPORT")
        evidence = EvidenceReference.objects.get(assessment=persisted_assessment)
        self.assertEqual(evidence.source_patent_id, self.source_patent.pk)
        self.assertEqual(evidence.source_document_version_id, self.source_version.pk)
        self.assertTrue(evidence.quote_verified)
        self.assertEqual(evidence.retrieval_metadata["chunk_id"], str(self.chunk.pk))
        self.analysis.refresh_from_db()
        self.assertEqual(self.analysis.status, AnalysisReport.Status.COMPLETED)
        self.assertEqual(
            self.analysis.result_summary["metrics"]["screening_priority_score"],
            88.0,
        )
