from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from ingestion.tasks import recover_stale_document_extractions
from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.models import Tenant


class PDFExtractionResourceLimitTests(TestCase):
    def test_page_limit_is_checked_before_extraction(self):
        from ingestion.tasks import PDFResourceLimitError, _bounded_join_pages

        class FakePage:
            def extract_text(self):
                raise AssertionError("page extraction must not start above the page limit")

        with patch("ingestion.tasks.MAX_PDF_PAGES", 1), self.assertRaises(PDFResourceLimitError):
            _bounded_join_pages([FakePage(), FakePage()], page_count=2)

    def test_character_limit_is_enforced_during_page_iteration(self):
        from ingestion.tasks import PDFResourceLimitError, _bounded_join_pages

        class FakePage:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                return self.text

        with patch("ingestion.tasks.MAX_EXTRACTED_CHARACTERS", 5), self.assertRaises(PDFResourceLimitError):
            _bounded_join_pages([FakePage("123"), FakePage("456")], page_count=2)


class DocumentExtractionRecoveryTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user_a = user_model.objects.create_user(username="extract-a")
        self.user_b = user_model.objects.create_user(username="extract-b")
        self.tenant_a = Tenant.objects.create(
            name="Extraction A", slug="extraction-a", status=Tenant.Status.ACTIVE
        )
        self.tenant_b = Tenant.objects.create(
            name="Extraction B", slug="extraction-b", status=Tenant.Status.ACTIVE
        )
        self.patent_a = Patent.objects.create(
            tenant=self.tenant_a,
            title="Tenant A patent",
            created_by=self.user_a,
        )
        self.patent_b = Patent.objects.create(
            tenant=self.tenant_b,
            title="Tenant B patent",
            created_by=self.user_b,
        )

    def make_version(self, *, patent=None, status=None, queued_at=None, started_at=None):
        patent = patent or self.patent_a
        return PatentDocumentVersion.objects.create(
            tenant_id=patent.tenant_id,
            patent=patent,
            version_number=1,
            storage_key=f"patents/{patent.tenant_id}/{patent.pk}/version.pdf",
            content_sha256="c" * 64,
            extraction_status=status or PatentDocumentVersion.ExtractionStatus.PENDING,
            extraction_queued_at=queued_at,
            extraction_started_at=started_at,
        )

    @patch("ingestion.tasks.process_patent_document_version.apply_async")
    def test_recovers_stale_pending_queue_and_leaves_recent_queue_alone(self, apply_async):
        now = timezone.now()
        stale = self.make_version(
            queued_at=now - timedelta(minutes=16),
        )
        recent = self.make_version(
            patent=Patent.objects.create(
                tenant=self.tenant_a,
                title="Another patent",
                created_by=self.user_a,
            ),
            queued_at=now,
        )

        result = recover_stale_document_extractions.run()

        self.assertEqual(result["requeued"], 1)
        self.assertEqual(result["queue_failures"], 0)
        apply_async.assert_called_once_with(args=[str(stale.pk)], queue="ingestion")
        stale.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(stale.extraction_status, PatentDocumentVersion.ExtractionStatus.PENDING)
        self.assertGreater(stale.extraction_queued_at, now - timedelta(minutes=1))
        self.assertEqual(recent.extraction_queued_at, now)

    @patch("ingestion.tasks.process_patent_document_version.apply_async")
    def test_recovers_expired_worker_lease_and_does_not_repeat_fresh_reservation(self, apply_async):
        old_start = timezone.now() - timedelta(minutes=46)
        version = self.make_version(
            status=PatentDocumentVersion.ExtractionStatus.EXTRACTING,
            started_at=old_start,
        )

        first = recover_stale_document_extractions.run()
        second = recover_stale_document_extractions.run()

        self.assertEqual(first["requeued"], 1)
        self.assertEqual(second["requeued"], 0)
        apply_async.assert_called_once_with(args=[str(version.pk)], queue="ingestion")
        version.refresh_from_db()
        self.assertEqual(version.extraction_status, PatentDocumentVersion.ExtractionStatus.PENDING)
        self.assertIsNone(version.extraction_started_at)

    @patch("ingestion.tasks.process_patent_document_version.apply_async")
    def test_recovers_legacy_extracting_row_without_worker_start_timestamp(self, apply_async):
        old_created_at = timezone.now() - timedelta(minutes=16)
        version = self.make_version(
            status=PatentDocumentVersion.ExtractionStatus.EXTRACTING,
        )
        PatentDocumentVersion.objects.filter(pk=version.pk).update(created_at=old_created_at)

        result = recover_stale_document_extractions.run()

        self.assertEqual(result["requeued"], 1)
        version.refresh_from_db()
        self.assertEqual(version.extraction_status, PatentDocumentVersion.ExtractionStatus.PENDING)
        self.assertIsNone(version.extraction_started_at)
        self.assertGreater(version.extraction_queued_at, old_created_at)
        apply_async.assert_called_once_with(args=[str(version.pk)], queue="ingestion")

    @patch(
        "ingestion.tasks.process_patent_document_version.apply_async",
        side_effect=RuntimeError("broker unavailable"),
    )
    def test_recovery_publish_failure_is_a_retryable_terminal_state(self, apply_async):
        version = self.make_version(queued_at=timezone.now() - timedelta(minutes=16))

        result = recover_stale_document_extractions.run()

        self.assertEqual(result["queue_failures"], 1)
        version.refresh_from_db()
        self.assertEqual(version.extraction_status, PatentDocumentVersion.ExtractionStatus.FAILED)
        self.assertIsNone(version.extraction_queued_at)
        self.assertEqual(version.extraction_error_code, "QUEUE_FAILED")
        apply_async.assert_called_once()

    def test_extraction_replaces_only_claims_owned_by_the_version_tenant(self):
        version = self.make_version()
        Claim.objects.create(
            tenant=self.tenant_a,
            patent=self.patent_a,
            document_version=version,
            claim_number="1",
            claim_text="Old claim text.",
        )
        foreign_claim = Claim.objects.create(
            tenant=self.tenant_b,
            patent=self.patent_b,
            document_version=version,
            claim_number="99",
            claim_text="Foreign tenant claim row.",
        )
        extraction = SimpleNamespace(pages=["A patent claim."], page_count=1, ocr_pages=0)
        parsed_claims = [{
            "claim_number": "1",
            "claim_text": "A new independent claim.",
            "claim_type": "INDEPENDENT",
            "depends_on_claim_number": None,
        }]

        with (
            patch("ingestion.tasks.read_pdf_storage", return_value=b"%PDF-test"),
            patch("ingestion.tasks.verify_pdf_digest"),
            patch("ingestion.tasks.extract_pdf_pages", return_value=extraction),
            patch("ingestion.tasks.ClaimDeconstructionParser.parse_claims_from_text", return_value=parsed_claims),
            patch("ingestion.tasks._dispatch_pending_index_stages", return_value=0),
        ):
            from ingestion.tasks import process_patent_document_version

            result = process_patent_document_version.run(str(version.pk))

        self.assertEqual(result["claims_created"], 1)
        self.assertTrue(Claim.objects.filter(pk=foreign_claim.pk, tenant=self.tenant_b).exists())
        self.assertTrue(
            Claim.objects.filter(
                tenant=self.tenant_a,
                document_version=version,
                claim_text="A new independent claim.",
            ).exists()
        )
