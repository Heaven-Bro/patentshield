"""Regression tests for tenant-isolated patent PDF upload and processing dispatch."""

from __future__ import annotations

from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from patents.models import Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership


class PatentDocumentUploadAPITests(TestCase):
    """Exercise the upload endpoint without requiring a live Celery broker."""

    def setUp(self) -> None:
        self.media_directory = TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.media_directory.cleanup)

        user_model = get_user_model()
        self.user_a = user_model.objects.create_user(
            username="patent-user-a",
            password="test-password-not-used",
        )
        self.user_b = user_model.objects.create_user(
            username="patent-user-b",
            password="test-password-not-used",
        )
        self.tenant_a = Tenant.objects.create(
            name="Tenant A",
            slug="tenant-a-upload-tests",
            status=Tenant.Status.ACTIVE,
        )
        self.tenant_b = Tenant.objects.create(
            name="Tenant B",
            slug="tenant-b-upload-tests",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant_a,
            user=self.user_a,
            role=TenantMembership.Role.OWNER,
            status=TenantMembership.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant_b,
            user=self.user_b,
            role=TenantMembership.Role.OWNER,
            status=TenantMembership.Status.ACTIVE,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user_a)

    @staticmethod
    def pdf_upload(name: str = "sample.pdf") -> SimpleUploadedFile:
        """Return a minimal signature-valid PDF fixture for API validation."""
        pdf_bytes = b"%PDF-1.7\n% PatentShield upload test\n%%EOF\n"
        return SimpleUploadedFile(
            name,
            pdf_bytes,
            content_type="application/pdf",
        )

    @patch("patents.views.process_patent_document_version.apply_async")
    def test_upload_creates_tenant_records_and_enqueues_ingestion(
        self,
        apply_async_mock: MagicMock,
    ) -> None:
        """A valid upload creates records and sends only the version ID to Celery."""
        response = self.client.post(
            "/api/v1/patents/upload/",
            {
                "title": "A test patent",
                "abstract": "A concise abstract.",
                "jurisdiction": "US",
                "file": self.pdf_upload(),
            },
            format="multipart",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )

        self.assertEqual(response.status_code, 202, response.data)
        patent_id = response.data["patent"]["id"]
        version_id = response.data["document_version"]["id"]
        patent = Patent.objects.get(pk=patent_id)
        version = PatentDocumentVersion.objects.get(pk=version_id)

        self.assertEqual(patent.tenant_id, self.tenant_a.pk)
        self.assertEqual(patent.created_by_id, self.user_a.pk)
        self.assertEqual(version.tenant_id, self.tenant_a.pk)
        self.assertEqual(version.patent_id, patent.pk)
        self.assertEqual(len(version.content_sha256), 64)
        self.assertEqual(
            version.extraction_status,
            PatentDocumentVersion.ExtractionStatus.PENDING,
        )
        self.assertIsNotNone(version.extraction_queued_at)
        self.assertEqual(patent.status, Patent.Status.PROCESSING)
        apply_async_mock.assert_called_once_with(
            args=[str(version.pk)],
            queue="ingestion",
        )

    @patch("patents.views.process_patent_document_version.apply_async")
    def test_retry_rejects_a_recent_queue_reservation_without_duplicate_publish(
        self,
        apply_async_mock: MagicMock,
    ) -> None:
        patent = Patent.objects.create(
            tenant=self.tenant_a,
            title="Queued patent",
            created_by=self.user_a,
            status=Patent.Status.PROCESSING,
        )
        version = PatentDocumentVersion.objects.create(
            tenant=self.tenant_a,
            patent=patent,
            version_number=1,
            storage_key="patents/test/queued.pdf",
            content_sha256="a" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.PENDING,
            extraction_queued_at=timezone.now(),
        )

        response = self.client.post(
            reverse(
                "patents:patent-version-process",
                kwargs={"patent_id": patent.pk, "version_id": version.pk},
            ),
            {},
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["extraction_status"], "PENDING")
        apply_async_mock.assert_not_called()

    @patch("patents.views.process_patent_document_version.apply_async")
    def test_stale_pending_queue_can_be_retried_without_changing_version_identity(
        self,
        apply_async_mock: MagicMock,
    ) -> None:
        patent = Patent.objects.create(
            tenant=self.tenant_a,
            title="Stale queued patent",
            created_by=self.user_a,
            status=Patent.Status.PROCESSING,
        )
        stale_time = timezone.now() - timedelta(minutes=16)
        version = PatentDocumentVersion.objects.create(
            tenant=self.tenant_a,
            patent=patent,
            version_number=1,
            storage_key="patents/test/stale.pdf",
            content_sha256="b" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.PENDING,
            extraction_queued_at=stale_time,
        )

        response = self.client.post(
            reverse(
                "patents:patent-version-process",
                kwargs={"patent_id": patent.pk, "version_id": version.pk},
            ),
            {},
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["document_version_id"], str(version.pk))
        self.assertEqual(response.data["extraction_status"], "PENDING")
        version.refresh_from_db()
        self.assertGreater(version.extraction_queued_at, stale_time)
        apply_async_mock.assert_called_once_with(args=[str(version.pk)], queue="ingestion")

    @patch("patents.views.process_patent_document_version.apply_async")
    def test_cross_tenant_upload_is_denied(
        self, apply_async_mock: MagicMock
    ) -> None:
        """User A cannot create records while selecting tenant B."""
        response = self.client.post(
            "/api/v1/patents/upload/",
            {"title": "Must not be stored", "file": self.pdf_upload()},
            format="multipart",
            HTTP_X_TENANT_ID=str(self.tenant_b.pk),
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Patent.objects.filter(title="Must not be stored").exists())
        apply_async_mock.assert_not_called()

    def test_cross_tenant_patent_detail_is_concealed(self) -> None:
        """Tenant A receives a not-found response for a tenant-B patent."""
        patent = Patent.objects.create(
            tenant=self.tenant_b,
            title="Tenant B secret patent",
            created_by=self.user_b,
        )
        response = self.client.get(
            f"/api/v1/patents/{patent.pk}/",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(response.status_code, 404)

    @patch(
        "patents.views.process_patent_document_version.apply_async",
        side_effect=RuntimeError("test broker outage"),
    )
    def test_broker_error_is_persisted_as_retryable_failure(
        self,
        apply_async_mock: MagicMock,
    ) -> None:
        """A failed dispatch leaves traceable rows that can use process/retry."""
        response = self.client.post(
            "/api/v1/patents/upload/",
            {"title": "Retryable upload", "file": self.pdf_upload()},
            format="multipart",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )

        self.assertEqual(response.status_code, 503, response.data)
        version = PatentDocumentVersion.objects.get(
            pk=response.data["document_version_id"]
        )
        self.assertEqual(
            version.extraction_status,
            PatentDocumentVersion.ExtractionStatus.FAILED,
        )
        self.assertIsNone(version.extraction_queued_at)
        self.assertEqual(version.extraction_error_code, "RuntimeError")
        self.assertEqual(version.tenant_id, self.tenant_a.pk)
        apply_async_mock.assert_called_once()
