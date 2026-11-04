"""FTO workflow tests: source provenance, limitation mapping, and tenant isolation."""
from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from unittest.mock import patch
from rest_framework.test import APIClient

from fto.models import (
    FTOStudy,
    FTOCandidateMatch,
    LimitationAssessment,
    LimitationEvidence,
    ProductFeature,
    ProductMatter,
    ProductVersion,
    StudyClaimLimitation,
)
from fto.services import decompose_product_specification, rank_product_features_for_limitation, split_claim_limitations
from fto.tasks import enqueue_fto_candidate_match_generation
from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership


class FTOServiceTests(TestCase):
    def test_product_feature_candidates_retain_exact_source_offsets(self):
        page = "The controller receives sensor data and transmits a status message to the remote server."
        candidates = decompose_product_specification([page])
        self.assertTrue(candidates)
        for item in candidates:
            self.assertEqual(page[item.start_offset:item.end_offset], item.source_quote)
            self.assertEqual(item.page_index, 0)
            self.assertTrue(item.feature_key)

    def test_claim_segmentation_retains_claim_relative_spans(self):
        source = "A controller comprising: a sensor; a processor coupled to the sensor; and a transmitter."
        segments = split_claim_limitations(source)
        self.assertEqual(len(segments), 3)
        for start, end, text, method in segments:
            self.assertEqual(source[start:end], text)
            self.assertIn(method, {"SEMICOLON_CLAUSES", "TRANSITION_CLAUSES", "WHOLE_CLAIM_UNSPLIT"})

    def test_unsplit_claim_is_kept_as_one_review_required_candidate(self):
        source = "A device having a housing and a power connector."
        segments = split_claim_limitations(source)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0][2], source)
        self.assertEqual(segments[0][3], "WHOLE_CLAIM_UNSPLIT")

    def test_candidate_ranking_prefers_shared_technical_terms_and_is_bounded(self):
        from types import SimpleNamespace

        features = [
            SimpleNamespace(pk="feature-a", title="Sensor processor", description="Receives sensor data and transmits readings", source_quote="A sensor processor transmits readings."),
            SimpleNamespace(pk="feature-b", title="Battery housing", description="Mechanical enclosure and connector", source_quote="A housing contains a connector."),
        ]
        ranked = rank_product_features_for_limitation(
            "A processor receives sensor data and transmits a control signal",
            features,
            max_results=10,
        )
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["feature"].pk, "feature-a")
        self.assertGreater(ranked[0]["score"], 0)
        self.assertGreater(ranked[0]["limitation_coverage"], 0)
        self.assertIn("sensor", ranked[0]["matched_terms"])
        self.assertIn("not an element-by-element", ranked[0]["rationale"])

    def test_candidate_ranking_does_not_invent_matches_for_stopwords_only(self):
        from types import SimpleNamespace

        feature = SimpleNamespace(pk="feature-a", title="A device", description="The system is configured", source_quote="A system comprising components.")
        self.assertEqual(rank_product_features_for_limitation("A device comprising the system", [feature]), [])

    def test_candidate_ranking_ignores_generic_claim_nouns_but_keeps_technical_matches(self):
        from types import SimpleNamespace

        feature = SimpleNamespace(
            pk="feature-sensor",
            title="A sensor system",
            description="The device measures optical signals.",
            source_quote="An apparatus comprising an optical sensor.",
        )
        ranked = rank_product_features_for_limitation(
            "A device comprising an optical sensor system",
            [feature],
        )
        repeated = rank_product_features_for_limitation(
            "A device comprising an optical sensor system",
            [feature],
        )

        self.assertEqual(len(ranked), 1)
        self.assertEqual(repeated, ranked)
        self.assertEqual(ranked[0]["feature"].pk, "feature-sensor")
        self.assertIn("sensor", ranked[0]["matched_terms"])
        self.assertIn("optical", ranked[0]["matched_terms"])
        self.assertNotIn("device", ranked[0]["matched_terms"])
        self.assertNotIn("system", ranked[0]["matched_terms"])

    def test_candidate_ranking_rejects_unbounded_result_count(self):
        with self.assertRaises(ValueError):
            rank_product_features_for_limitation("sensor", [], max_results=5000)


class FTOApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="fto-attorney", password="test-password-123")
        self.reviewer = User.objects.create_user(username="fto-reviewer", password="test-password-123")
        self.tenant = Tenant.objects.create(name="FTO Tenant", slug=f"fto-{uuid.uuid4().hex[:8]}")
        TenantMembership.objects.create(tenant=self.tenant, user=self.user, role=TenantMembership.Role.ATTORNEY, status=TenantMembership.Status.ACTIVE)
        TenantMembership.objects.create(tenant=self.tenant, user=self.reviewer, role=TenantMembership.Role.REVIEWER, status=TenantMembership.Status.ACTIVE)
        self.other_tenant = Tenant.objects.create(name="Other Tenant", slug=f"other-{uuid.uuid4().hex[:8]}")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.headers = {"HTTP_X_TENANT_ID": str(self.tenant.pk)}

        self.product = ProductMatter.objects.create(
            tenant=self.tenant,
            name="Adaptive control module",
            product_code=f"ACM-{uuid.uuid4().hex[:6]}",
            created_by=self.user,
        )
        self.product_version = ProductVersion.objects.create(
            tenant=self.tenant,
            product=self.product,
            version_number=1,
            source_filename="spec.pdf",
            storage_key=f"test/{uuid.uuid4().hex}.pdf",
            content_sha256="a" * 64,
            page_count=1,
            extracted_text="The controller receives sensor data and transmits a status message.",
            extracted_pages=["The controller receives sensor data and transmits a status message."],
            extraction_status=ProductVersion.ExtractionStatus.EXTRACTED,
            decomposition_status=ProductVersion.DecompositionStatus.COMPLETED,
            created_by=self.user,
        )
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="A sensor controller",
            publication_number="US-2026-1234567-A1",
            jurisdiction="US",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        self.patent_version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            version_number=1,
            storage_key=f"test/{uuid.uuid4().hex}.pdf",
            content_sha256="b" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        self.claim = Claim.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            document_version=self.patent_version,
            claim_number="1",
            claim_text="A controller comprising: a sensor; a processor coupled to the sensor; and a transmitter.",
        )

    def _create_study(self):
        response = self.client.post(
            "/api/v1/fto/studies/",
            {
                "product_version_id": str(self.product_version.pk),
                "patent_id": str(self.patent.pk),
                "patent_document_version_id": str(self.patent_version.pk),
                "jurisdiction": "US",
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        return FTOStudy.objects.get(pk=response.data["id"])

    def test_study_creation_pins_version_and_creates_limitation_assessments(self):
        study = self._create_study()
        self.assertEqual(study.product_version_id, self.product_version.pk)
        self.assertEqual(study.patent_document_version_id, self.patent_version.pk)
        self.assertEqual(StudyClaimLimitation.objects.filter(study=study).count(), 3)
        self.assertEqual(LimitationAssessment.objects.filter(study=study).count(), 3)
        self.assertTrue(StudyClaimLimitation.objects.filter(study=study, needs_attorney_review=True).exists())

    @patch("fto.tasks.generate_fto_candidate_matches.apply_async")
    def test_candidate_match_dispatch_uses_one_fenced_reservation(self, apply_async):
        study = self._create_study()
        first_task_id = enqueue_fto_candidate_match_generation(str(study.pk), str(self.tenant.pk))
        second_task_id = enqueue_fto_candidate_match_generation(str(study.pk), str(self.tenant.pk))
        study.refresh_from_db()
        self.assertIsNotNone(first_task_id)
        self.assertIsNone(second_task_id)
        self.assertEqual(study.matching_status, FTOStudy.MatchingStatus.QUEUED)
        self.assertEqual(study.matching_task_id, first_task_id)
        apply_async.assert_called_once()

    def test_counter_evidence_and_support_quote_are_pinned_from_confirmed_feature(self):
        study = self._create_study()
        limitation = StudyClaimLimitation.objects.filter(study=study).first()
        assessment = LimitationAssessment.objects.get(limitation=limitation)
        quote = self.product_version.extracted_pages[0][:48]
        feature = ProductFeature.objects.create(
            tenant=self.tenant,
            product_version=self.product_version,
            feature_key=uuid.uuid4().hex * 2,
            title="Sensor data pathway",
            description="A manually confirmed product feature.",
            source_quote=quote,
            source_page_index=0,
            source_start_offset=0,
            source_end_offset=len(quote),
            review_status=ProductFeature.ReviewStatus.CONFIRMED,
            created_by=self.user,
            reviewed_by=self.reviewer,
        )
        response = self.client.post(
            f"/api/v1/fto/studies/{study.pk}/limitations/{limitation.pk}/evidence/",
            {"feature_id": str(feature.pk), "kind": "COUNTER", "attorney_rationale": "This passage describes a different data path."},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        evidence = LimitationEvidence.objects.get(pk=response.data["id"])
        self.assertEqual(evidence.source_quote, feature.source_quote)

    def test_unextracted_product_version_cannot_start_study(self):
        pending_version = ProductVersion.objects.create(
            tenant=self.tenant,
            product=self.product,
            version_number=2,
            source_filename="pending.pdf",
            storage_key=f"test/{uuid.uuid4().hex}.pdf",
            content_sha256="c" * 64,
            extraction_status=ProductVersion.ExtractionStatus.PENDING,
            created_by=self.user,
        )
        response = self.client.post(
            "/api/v1/fto/studies/",
            {
                "product_version_id": str(pending_version.pk),
                "patent_id": str(self.patent.pk),
                "patent_document_version_id": str(self.patent_version.pk),
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("extraction", str(response.data).casefold())

    def test_triage_cannot_be_changed_in_same_request_as_review_signoff(self):
        study = self._create_study()
        limitation = StudyClaimLimitation.objects.filter(study=study).first()
        self.client.force_authenticate(user=self.reviewer)
        response = self.client.patch(
            f"/api/v1/fto/studies/{study.pk}/limitations/{limitation.pk}/assessment/",
            {"triage_rationale": "Clarify the comparison before signoff.", "review_status": "REVIEWED"},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("separate review-state action", str(response.data))

    def test_attorney_annotation_is_persisted_and_source_scoped(self):
        study = self._create_study()
        assessment = LimitationAssessment.objects.filter(study=study).first()
        response = self.client.post(
            f"/api/v1/fto/studies/{study.pk}/annotations/",
            {"assessment_id": str(assessment.pk), "category": "QUESTION", "body": "Confirm whether the spec discloses this pathway."},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["assessment_id"], str(assessment.pk))
        self.assertEqual(response.data["category"], "QUESTION")

    def test_potential_match_requires_supporting_source_feature(self):
        study = self._create_study()
        limitation = StudyClaimLimitation.objects.filter(study=study).first()
        response = self.client.patch(
            f"/api/v1/fto/studies/{study.pk}/limitations/{limitation.pk}/assessment/",
            {"triage_status": "POTENTIAL_MATCH", "triage_rationale": "Preliminary correspondence needs review."},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_cross_tenant_resource_is_not_disclosed(self):
        other_product = ProductMatter.objects.create(
            tenant=self.other_tenant,
            name="Secret other tenant product",
            product_code=f"OT-{uuid.uuid4().hex[:6]}",
            created_by=self.user,
        )
        response = self.client.get(f"/api/v1/fto/products/{other_product.pk}/", **self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Secret other tenant product", str(response.data))

    def test_heatmap_exposes_triage_disclaimer(self):
        study = self._create_study()
        response = self.client.get(f"/api/v1/fto/studies/{study.pk}/risk-heatmap/", **self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("not findings of infringement", response.data["disclaimer"])
        self.assertEqual(len(response.data["claims"]), 1)

    def test_heatmap_surfaces_explainable_candidate_matches_without_changing_triage(self):
        study = self._create_study()
        limitation = StudyClaimLimitation.objects.filter(study=study).first()
        assessment = LimitationAssessment.objects.get(limitation=limitation)
        feature = ProductFeature.objects.create(
            tenant=self.tenant,
            product_version=self.product_version,
            feature_key=uuid.uuid4().hex * 2,
            title="Sensor interface",
            description="Receives sensor readings.",
            source_quote=self.product_version.extracted_pages[0][:48],
            source_page_index=0,
            source_start_offset=0,
            source_end_offset=48,
            review_status=ProductFeature.ReviewStatus.CANDIDATE,
            created_by=self.user,
        )
        FTOCandidateMatch.objects.create(
            tenant=self.tenant,
            study=study,
            assessment=assessment,
            feature=feature,
            feature_title_snapshot=feature.title,
            feature_review_status_snapshot=feature.review_status,
            source_quote_snapshot=feature.source_quote,
            source_page_index_snapshot=feature.source_page_index,
            source_start_offset_snapshot=feature.source_start_offset,
            source_end_offset_snapshot=feature.source_end_offset,
            score=0.42,
            limitation_coverage=0.5,
            matched_terms=["sensor"],
            rationale="Lexical retrieval candidate only.",
        )
        response = self.client.get(f"/api/v1/fto/studies/{study.pk}/risk-heatmap/", **self.headers)
        self.assertEqual(response.status_code, 200, response.data)
        heatmap_rows = [
            limitation_row
            for claim_row in response.data["claims"]
            for limitation_row in claim_row["limitations"]
        ]
        row = next(
            (
                limitation_row
                for limitation_row in heatmap_rows
                if limitation_row["limitation_id"] == str(limitation.pk)
            ),
            None,
        )
        self.assertIsNotNone(
            row,
            f"Heatmap omitted the persisted study limitation {limitation.pk}",
        )
        self.assertEqual(row["candidate_matches"][0]["feature_id"], str(feature.pk))
        self.assertEqual(row["candidate_matches"][0]["feature_title"], feature.title)
        self.assertEqual(row["candidate_matches"][0]["source_quote"], feature.source_quote)
        self.assertEqual(row["triage_status"], LimitationAssessment.TriageStatus.UNASSESSED)
        self.assertIn("lexical overlap only", response.data["candidate_match_disclaimer"].casefold())
        self.assertIn("500", response.data["candidate_match_disclaimer"])
        feature.refresh_from_db()
        self.assertEqual(feature.review_status, ProductFeature.ReviewStatus.CANDIDATE)
