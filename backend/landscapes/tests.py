"""Unit and tenant-isolation tests for Phase 7 landscape analytics."""
from __future__ import annotations

import math
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from landscapes.models import LandscapeStudy
from landscapes.services import (
    create_study_publication_snapshot,
    deterministic_kmeans,
    normalize_assignee_name,
    project_vectors_2d,
    snapshot_sha256,
    taxonomy_code,
    taxonomy_prefix,
)
from patents.intelligence_models import PatentPublication
from tenants.models import Tenant, TenantMembership


class NormalizationTests(SimpleTestCase):
    def test_assignee_normalization_is_deterministic_and_trailing_suffix_only(self):
        self.assertEqual(normalize_assignee_name("  Example, Inc. "), "example")
        self.assertEqual(normalize_assignee_name("Example COMPANY Limited"), "example")
        self.assertEqual(normalize_assignee_name("Müller & Söhne GmbH"), "müller and söhne")
        self.assertEqual(normalize_assignee_name("   !!!   "), "")

    def test_classification_code_normalization_and_prefix(self):
        self.assertEqual(taxonomy_code("g06f 16/00"), "G06F16/00")
        self.assertEqual(taxonomy_prefix("G06F16/00"), "G06")
        self.assertEqual(taxonomy_prefix("H04W 4/80"), "H04")


class ClusteringTests(SimpleTestCase):
    def test_kmeans_is_deterministic_and_covers_every_row(self):
        vectors = [[1.0, 0.0, 0.0], [0.95, 0.05, 0.0], [0.0, 1.0, 0.0], [0.0, 0.9, 0.1], [0.0, 0.0, 1.0]]
        labels1 = deterministic_kmeans(vectors, max_clusters=3)
        labels2 = deterministic_kmeans(vectors, max_clusters=3)
        self.assertEqual(labels1, labels2)
        self.assertEqual(len(labels1), len(vectors))
        self.assertGreaterEqual(len(set(labels1)), 2)
        self.assertLessEqual(len(set(labels1)), 3)

    def test_projection_returns_finite_coordinates(self):
        coords, method = project_vectors_2d([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        self.assertEqual(len(coords), 3)
        self.assertIn(method, {"PCA_SKLEARN", "FIXED_RANDOM_PROJECTION"})
        self.assertTrue(all(math.isfinite(x) and math.isfinite(y) for x, y in coords))

    def test_snapshot_hash_is_stable(self):
        snapshot = {"title": "A test", "applicant_names": ["Example"]}
        self.assertEqual(snapshot_sha256(snapshot), snapshot_sha256({"applicant_names": ["Example"], "title": "A test"}))


class LandscapeTenantIsolationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="landscape-analyst", password="not-used-in-force-auth")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="landscape-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="landscape-b")
        TenantMembership.objects.create(tenant=self.tenant_a, user=self.user, role=TenantMembership.Role.ANALYST, status=TenantMembership.Status.ACTIVE)
        self.study_a = LandscapeStudy.objects.create(tenant=self.tenant_a, title="A only", created_by=self.user)
        self.study_b = LandscapeStudy.objects.create(tenant=self.tenant_b, title="B only", created_by=self.user)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_pinned_publication_source_fields_are_immutable(self):
        publication = PatentPublication.objects.create(
            provider=PatentPublication.Provider.USPTO,
            provider_record_id="landscape-test-001",
            jurisdiction="US",
            publication_number="US1234567A1",
            title="Wireless power control",
            abstract="A source description.",
            applicant_names=["Example Incorporated"],
            cpc_codes=["H04W4/80"],
        )
        row, created = create_study_publication_snapshot(self.study_a, publication)
        self.assertTrue(created)
        row.title = "Silently altered title"
        with self.assertRaises(ValidationError):
            row.save()

    def test_study_detail_cannot_cross_tenant_boundary(self):
        response = self.client.get(
            f"/api/v1/landscapes/studies/{self.study_b.pk}/",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(response.status_code, 404)

    def test_study_list_only_returns_active_tenant_records(self):
        response = self.client.get("/api/v1/landscapes/studies/", HTTP_X_TENANT_ID=str(self.tenant_a.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["results"]], [str(self.study_a.pk)])

    def test_publication_search_requires_active_tenant_membership(self):
        response = self.client.get("/api/v1/landscapes/publications/search/?q=wireless", HTTP_X_TENANT_ID=str(self.tenant_b.pk))
        self.assertEqual(response.status_code, 403)

    def test_reviewer_can_review_assignee_but_cannot_edit_metadata(self):
        from landscapes.models import AssigneeEntity

        entity = AssigneeEntity.objects.create(
            tenant=self.tenant_a,
            canonical_name="Example Corporation",
            normalized_key="example",
            created_by=self.user,
        )
        membership = TenantMembership.objects.get(tenant=self.tenant_a, user=self.user)
        membership.role = TenantMembership.Role.REVIEWER
        membership.save(update_fields=("role",))

        metadata_response = self.client.patch(
            f"/api/v1/landscapes/assignees/{entity.pk}/",
            {"country_code": "US"},
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(metadata_response.status_code, 403)

        review_response = self.client.patch(
            f"/api/v1/landscapes/assignees/{entity.pk}/",
            {"status": AssigneeEntity.Status.CONFIRMED},
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(review_response.status_code, 200)

    def test_editing_study_metadata_preserves_ready_analysis(self):
        self.study_a.status = LandscapeStudy.Status.READY
        self.study_a.save(update_fields=("status", "updated_at"))
        response = self.client.patch(
            f"/api/v1/landscapes/studies/{self.study_a.pk}/",
            {"title": "Updated title"},
            format="json",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], LandscapeStudy.Status.READY)
