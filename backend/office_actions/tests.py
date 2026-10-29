"""Regression tests for office-action extraction, tenant isolation and drafts."""
from __future__ import annotations

import hashlib

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from office_actions.models import OfficeAction, ResponseDraft, ResponseDraftRevision
from office_actions.services import create_evidence_grounded_draft, create_revision, parse_cited_references, parse_office_action, parse_rejections
from patents.models import Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership


class OfficeActionParserTests(SimpleTestCase):
    def setUp(self):
        self.text = (
            "Claims 1-3 are rejected under 35 U.S.C. § 103 as being unpatentable "
            "over US 2019/1234567 A1 (Smith). The cited reference describes a "
            "sensor array on page 4. Claims 4 and 5 are objected to for formality."
        )

    def test_extracts_rejection_ground_claim_range_and_exact_quote(self):
        parsed = parse_rejections([self.text], "US")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["ground"], "US_103")
        self.assertEqual(parsed[0]["claim_numbers"], ["1", "2", "3"])
        start = parsed[0]["source_start_offset"]
        end = parsed[0]["source_end_offset"]
        self.assertGreaterEqual(start, 0)
        self.assertGreater(end, start)
        self.assertTrue(parsed[0]["needs_attorney_review"])
        self.assertIn("35 U.S.C. § 103", parsed[0]["source_quote"])

    def test_cited_reference_parser_keeps_source_quote(self):
        refs = parse_cited_references([self.text])
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["identifier"], "US 2019/1234567 A1")
        self.assertIn("Smith", refs[0]["source_quote"])
        self.assertFalse(refs[0]["metadata"]["verified_against_external_source"])

    def test_jurisdiction_filters_out_us_ground_for_ep(self):
        self.assertEqual(parse_rejections([self.text], "EP"), [])


class OfficeActionWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="attorney", password="test-password-123")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="tenant-b")
        TenantMembership.objects.create(tenant=self.tenant_a, user=self.user, role=TenantMembership.Role.ATTORNEY)
        self.patent_a = Patent.objects.create(tenant=self.tenant_a, title="Example patent", application_number="17/123,456", created_by=self.user)
        self.patent_b = Patent.objects.create(tenant=self.tenant_b, title="Another patent", application_number="18/222,333", created_by=self.user)
        self.action_b = OfficeAction.objects.create(
            tenant=self.tenant_b, patent=self.patent_b, created_by=self.user,
            jurisdiction="US", action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="18/222,333", source_filename="other.pdf",
            storage_key="office-actions/private/other.pdf", content_sha256=hashlib.sha256(b"pdf").hexdigest(),
        )

    def test_tenant_cannot_read_another_tenant_office_action(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(
            f"/api/v1/office-actions/{self.action_b.pk}/",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(response.status_code, 404)

    def test_parser_persistence_is_idempotent(self):
        action = OfficeAction.objects.create(
            tenant=self.tenant_a, patent=self.patent_a, created_by=self.user,
            jurisdiction="US", action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="17/123,456", source_filename="office-action.pdf",
            storage_key="office-actions/tenant-a/office-action.pdf", content_sha256=hashlib.sha256(b"pdf").hexdigest(),
            extracted_text="Claims 1-2 are rejected under 35 U.S.C. § 102 over US 2018/1234567 A1.",
            extracted_pages=["Claims 1-2 are rejected under 35 U.S.C. § 102 over US 2018/1234567 A1."],
            extraction_status=OfficeAction.ExtractionStatus.EXTRACTED,
        )
        parse_office_action(action)
        parse_office_action(action)
        self.assertEqual(action.rejections.count(), 1)
        self.assertEqual(action.cited_references.count(), 1)


    def test_generated_draft_retains_exact_source_evidence_and_hash(self):
        action = OfficeAction.objects.create(
            tenant=self.tenant_a, patent=self.patent_a, created_by=self.user,
            jurisdiction="US", action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="17/123,456", source_filename="office-action.pdf",
            storage_key="office-actions/tenant-a/office-action.pdf", content_sha256=hashlib.sha256(b"pdf").hexdigest(),
            extracted_text="Claim 1 is rejected under 35 U.S.C. § 102 based on US 2018/1234567 A1.",
            extracted_pages=["Claim 1 is rejected under 35 U.S.C. § 102 based on US 2018/1234567 A1."],
            extraction_status=OfficeAction.ExtractionStatus.EXTRACTED,
        )
        draft, revision = create_evidence_grounded_draft(action, self.user)
        self.assertEqual(draft.current_revision_number, 1)
        self.assertIn("35 U.S.C. § 102", revision.content_markdown)
        self.assertEqual(revision.content_sha256, hashlib.sha256(revision.content_markdown.encode("utf-8")).hexdigest())
        self.assertEqual(revision.source_manifest["office_action_sha256"], action.content_sha256)
        self.assertTrue(revision.source_manifest["rejections"])

    def test_attorney_edit_appends_a_new_revision(self):
        action = OfficeAction.objects.create(
            tenant=self.tenant_a, patent=self.patent_a, created_by=self.user,
            jurisdiction="US", action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="17/123,456", source_filename="office-action.pdf",
            storage_key="office-actions/tenant-a/office-action.pdf", content_sha256=hashlib.sha256(b"pdf").hexdigest(),
            extracted_text="Claim 1 is rejected under 35 U.S.C. § 102.",
            extracted_pages=["Claim 1 is rejected under 35 U.S.C. § 102."],
            extraction_status=OfficeAction.ExtractionStatus.EXTRACTED,
        )
        draft, first = create_evidence_grounded_draft(action, self.user)
        second = create_revision(draft, self.user, first.content_markdown + "\n\nAttorney note: reference details to verify.")
        draft.refresh_from_db()
        self.assertEqual(draft.current_revision_number, 2)
        self.assertEqual(draft.revisions.count(), 2)
        self.assertNotEqual(first.content_sha256, second.content_sha256)
        self.assertEqual(first.revision_number, 1)
        self.assertEqual(second.revision_number, 2)

    def test_response_revision_is_immutable(self):
        action = OfficeAction.objects.create(
            tenant=self.tenant_a, patent=self.patent_a, created_by=self.user,
            jurisdiction="US", action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="17/123,456", source_filename="office-action.pdf",
            storage_key="office-actions/tenant-a/office-action.pdf", content_sha256=hashlib.sha256(b"pdf").hexdigest(),
        )
        draft = ResponseDraft.objects.create(tenant=self.tenant_a, office_action=action, created_by=self.user, current_revision_number=1)
        revision = ResponseDraftRevision.objects.create(
            tenant=self.tenant_a, draft=draft, revision_number=1,
            content_markdown="Attorney draft", source_manifest={"office_action_id": str(action.pk)},
            content_sha256=hashlib.sha256(b"Attorney draft").hexdigest(), generated_by=self.user,
        )
        revision.content_markdown = "Modified in place"
        with self.assertRaises(ValidationError):
            revision.save()


class OfficeActionPDFResourceLimitTests(SimpleTestCase):
    def test_page_limit_fails_before_pdf_page_iteration(self):
        from unittest.mock import patch
        from office_actions.tasks import PDFResourceLimitError, _check_page_count

        with patch("office_actions.tasks.MAX_PDF_PAGES", 2), self.assertRaises(PDFResourceLimitError):
            _check_page_count(3)
