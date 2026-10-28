"""FTO workflow tests: source provenance, limitation mapping, and tenant isolation."""
from __future__ import annotations

from urllib import response
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from fto.models import (
    FTOStudy,
    LimitationAssessment,
    LimitationEvidence,
    ProductFeature,
    ProductMatter,
    ProductVersion,
    StudyClaimLimitation,
)
from fto.services import decompose_product_specification, split_claim_limitations
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
