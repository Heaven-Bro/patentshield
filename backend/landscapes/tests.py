"""Unit and tenant-isolation tests for Phase 7 landscape analytics."""
from __future__ import annotations

import math
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from landscapes.models import LandscapeStudy
from landscapes.views import _csv_safe_cell
from landscapes.tasks import _mark_recovery_dispatch_failed, recover_stale_landscape_builds
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


class ExportSafetyTests(SimpleTestCase):
    def test_csv_cells_neutralize_formula_leading_values(self):
        for payload in ("=HYPERLINK(\"https://evil.example\")", "+1+1", "-2+3", "@SUM(1,2)", "\t=cmd|'/C calc'!A0"):
            with self.subTest(payload=payload):
                self.assertTrue(_csv_safe_cell(payload).startswith("'"))

    def test_csv_cells_preserve_ordinary_values(self):
        self.assertEqual(_csv_safe_cell("US1234567A1"), "US1234567A1")
        self.assertEqual(_csv_safe_cell(["H04W", "G06F"]), "H04W; G06F")
        self.assertEqual(_csv_safe_cell(None), "")


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

    def test_study_csv_export_is_tenant_scoped_and_contains_pinned_source(self):
        publication = PatentPublication.objects.create(
            provider=PatentPublication.Provider.USPTO,
            provider_record_id="landscape-export-001",
            jurisdiction="US",
            publication_number="US7654321A1",
            title="=Untrusted spreadsheet title",
            abstract="Pinned snapshot abstract",
            applicant_names=["Example Inc."],
            cpc_codes=["H04W4/80"],
        )
        create_study_publication_snapshot(self.study_a, publication)
        response = self.client.get(
            f"/api/v1/landscapes/studies/{self.study_a.pk}/export/",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("private, no-store", response["Cache-Control"])
        payload = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn("US7654321A1", payload)
        self.assertIn("'=Untrusted spreadsheet title", payload)

    def test_study_csv_export_does_not_cross_tenant_boundary(self):
        response = self.client.get(
            f"/api/v1/landscapes/studies/{self.study_b.pk}/export/",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(response.status_code, 404)
    def test_stale_build_recovery_reserves_once_and_dispatches_after_commit(self):
        study = self.study_a
        study.status = LandscapeStudy.Status.BUILDING
        study.started_at = timezone.now() - timedelta(hours=2)
        study.built_by_task_id = "lost-task"
        study.save(update_fields=("status", "started_at", "built_by_task_id", "updated_at"))

        with patch("landscapes.tasks.build_landscape.apply_async") as dispatch:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                result = recover_stale_landscape_builds.run(stale_after_minutes=45, batch_size=100)

            self.assertEqual(result["reserved"], 1)
            self.assertEqual(len(callbacks), 1)
            dispatch.assert_called_once()
            call_kwargs = dispatch.call_args.kwargs
            self.assertEqual(call_kwargs["queue"], "embedding")
            self.assertNotEqual(call_kwargs["task_id"], "lost-task")

            study.refresh_from_db()
            self.assertEqual(study.status, LandscapeStudy.Status.BUILDING)
            self.assertEqual(study.built_by_task_id, call_kwargs["task_id"])

            with self.captureOnCommitCallbacks(execute=True) as second_callbacks:
                second = recover_stale_landscape_builds.run(stale_after_minutes=45, batch_size=100)
            self.assertEqual(second["reserved"], 0)
            self.assertEqual(len(second_callbacks), 0)
            dispatch.assert_called_once()

    def test_recovery_dispatch_failure_is_fenced_by_reserved_task_id(self):
        study = self.study_a
        study.status = LandscapeStudy.Status.BUILDING
        study.built_by_task_id = "new-current-task"
        study.save(update_fields=("status", "built_by_task_id", "updated_at"))

        _mark_recovery_dispatch_failed(str(study.pk), str(study.tenant_id), "old-stale-task")
        study.refresh_from_db()
        self.assertEqual(study.status, LandscapeStudy.Status.BUILDING)

        _mark_recovery_dispatch_failed(str(study.pk), str(study.tenant_id), "new-current-task")
        study.refresh_from_db()
        self.assertEqual(study.status, LandscapeStudy.Status.FAILED)
        self.assertEqual(study.error_code, "RECOVERY_QUEUE_FAILED")
