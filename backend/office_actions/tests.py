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


class OfficeActionParserHardeningTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from tenants.models import Tenant
        User = get_user_model()
        self.user = User.objects.create_user(username="parser-user", password="test-password-123")
        self.tenant = Tenant.objects.create(name="Parser Tenant", slug="parser-tenant")
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="Parser test patent",
            application_number="19/111,222",
            created_by=self.user,
        )

    def _action(self, pages):
        return OfficeAction.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            created_by=self.user,
            jurisdiction="US",
            action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="19/111,222",
            source_filename="office-action.pdf",
            storage_key="office-actions/parser/source.pdf",
            content_sha256=hashlib.sha256(b"pdf").hexdigest(),
            extracted_text="\n".join(pages),
            extracted_pages=pages,
            extraction_status=OfficeAction.ExtractionStatus.EXTRACTED,
        )

    def test_references_are_not_linked_across_different_pdf_pages(self):
        action = self._action([
            "Claims 1-2 are rejected under 35 U.S.C. § 102 as anticipated.",
            "US 2018/1234567 A1 is listed in the separate IDS section.",
        ])
        parse_office_action(action)
        rejection = action.rejections.get()
        self.assertEqual(rejection.reference_links.count(), 0)

    def test_reference_is_linked_when_its_identifier_is_inside_same_page_issue_span(self):
        action = self._action([
            "Claims 1-2 are rejected under 35 U.S.C. § 102 over US 2018/1234567 A1.",
        ])
        parse_office_action(action)
        rejection = action.rejections.get()
        self.assertEqual(rejection.reference_links.count(), 1)

    def test_extracts_supported_us_ep_and_pct_grounds(self):
        us = parse_rejections([
            "Claims 1, 2 are rejected under 35 U.S.C. §101.",
            "Claim 3 is rejected under 35 U.S.C. §102.",
            "Claim 4 is rejected under 35 U.S.C. §103.",
            "Claim 5 is rejected under 35 U.S.C. §112.",
        ], "US")
        ep = parse_rejections([
            "Claim 1 is objected to under Article 54 EPC.",
            "Claim 2 is objected to under Article 56 EPC.",
        ], "EP")
        pct = parse_rejections(["The International Search Authority raises a PCT Rule 33 written opinion objection."], "PCT")
        self.assertEqual({row["ground"] for row in us}, {"US_101", "US_102", "US_103", "US_112"})
        self.assertEqual({row["ground"] for row in ep}, {"EP_54", "EP_56"})
        self.assertEqual({row["ground"] for row in pct}, {"PCT"})


class OfficeActionDispatchTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="dispatch-user", password="test-password-123")
        self.tenant = Tenant.objects.create(name="Dispatch Tenant", slug="dispatch-tenant")
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="Dispatch test patent",
            application_number="19/333,444",
            created_by=self.user,
        )
        self.action = OfficeAction.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            created_by=self.user,
            jurisdiction="US",
            action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="19/333,444",
            source_filename="office-action.pdf",
            storage_key="office-actions/dispatch/source.pdf",
            content_sha256=hashlib.sha256(b"pdf").hexdigest(),
        )

    def test_duplicate_enqueue_reuses_the_recent_task_reservation(self):
        from unittest.mock import patch
        from office_actions.dispatch import enqueue_office_action
        with patch("office_actions.tasks.process_office_action.apply_async") as apply_async:
            first_id, first_state = enqueue_office_action(self.action.pk, self.user)
            second_id, second_state = enqueue_office_action(self.action.pk, self.user)
        self.assertEqual(first_state, "QUEUED")
        self.assertEqual(second_state, "QUEUED")
        self.assertEqual(first_id, second_id)
        self.assertEqual(apply_async.call_count, 1)
        self.action.refresh_from_db()
        self.assertEqual(self.action.processing_task_id, first_id)
        self.assertEqual(self.action.extraction_status, OfficeAction.ExtractionStatus.QUEUED)

    def test_recovery_selects_only_expired_reservations(self):
        from datetime import timedelta
        from django.utils import timezone
        from office_actions.dispatch import recoverable_office_action_ids
        self.action.extraction_status = OfficeAction.ExtractionStatus.QUEUED
        self.action.processing_task_id = "stale-task"
        self.action.processing_reserved_at = timezone.now() - timedelta(minutes=20)
        self.action.save(update_fields=("extraction_status", "processing_task_id", "processing_reserved_at", "updated_at"))
        fresh = OfficeAction.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            created_by=self.user,
            jurisdiction="US",
            action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="19/333,444-FRESH",
            source_filename="fresh.pdf",
            storage_key="office-actions/dispatch/fresh.pdf",
            content_sha256=hashlib.sha256(b"fresh").hexdigest(),
            extraction_status=OfficeAction.ExtractionStatus.QUEUED,
            processing_task_id="fresh-task",
            processing_reserved_at=timezone.now(),
        )
        recoverable = recoverable_office_action_ids(limit=100)
        self.assertIn(str(self.action.pk), recoverable)
        self.assertNotIn(str(fresh.pk), recoverable)


class OfficeActionReviewConcurrencyTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.author = User.objects.create_user(username="draft-author", password="test-password-123")
        self.reviewer = User.objects.create_user(username="independent-reviewer", password="test-password-123")
        self.tenant = Tenant.objects.create(name="Review Tenant", slug="review-tenant")
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="Review test patent",
            application_number="19/555,666",
            created_by=self.author,
        )
        self.action = OfficeAction.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            created_by=self.author,
            jurisdiction="US",
            action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="19/555,666",
            source_filename="office-action.pdf",
            storage_key="office-actions/review/source.pdf",
            content_sha256=hashlib.sha256(b"pdf").hexdigest(),
            extracted_text="Claim 1 is rejected under 35 U.S.C. § 102.",
            extracted_pages=["Claim 1 is rejected under 35 U.S.C. § 102."],
            extraction_status=OfficeAction.ExtractionStatus.EXTRACTED,
        )

    def test_draft_author_cannot_approve_even_if_another_user_created_revision(self):
        from office_actions.services import record_review
        draft, first = create_evidence_grounded_draft(self.action, self.author)
        revision = create_revision(draft, self.reviewer, first.content_markdown + "\n\nReview update.")
        from office_actions.models import OfficeActionReview
        with self.assertRaises(ValueError):
            record_review(revision, self.author, OfficeActionReview.Decision.APPROVED, "Self approval")

    def test_review_decision_updates_draft_state(self):
        from office_actions.services import record_review, submit_for_review
        from office_actions.models import OfficeActionReview
        draft, revision = create_evidence_grounded_draft(self.action, self.author)
        draft = submit_for_review(draft, self.author)
        record_review(revision, self.reviewer, OfficeActionReview.Decision.APPROVED, "Approved after attorney review")
        draft.refresh_from_db()
        self.assertEqual(draft.status, ResponseDraft.Status.APPROVED)

    def test_reviewed_revision_cannot_be_resubmitted_without_new_revision(self):
        from office_actions.services import record_review, submit_for_review
        from office_actions.models import OfficeActionReview
        draft, revision = create_evidence_grounded_draft(self.action, self.author)
        submit_for_review(draft, self.author)
        record_review(revision, self.reviewer, OfficeActionReview.Decision.CHANGES_REQUESTED, "Please revise the argument.")
        draft.refresh_from_db()
        with self.assertRaises(ValueError):
            submit_for_review(draft, self.author)
        next_revision = create_revision(draft, self.author, revision.content_markdown + "\n\nRevised argument.")
        submit_for_review(draft, self.author)
        self.assertEqual(next_revision.revision_number, 2)


class OfficeActionMatterACLTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="matter-reader", password="test-password-123")
        self.tenant = Tenant.objects.create(name="Matter ACL Tenant", slug="matter-acl-tenant")
        TenantMembership.objects.create(tenant=self.tenant, user=self.user, role=TenantMembership.Role.ANALYST)
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="ACL protected patent",
            application_number="19/777,888",
            created_by=self.user,
        )
        self.action = OfficeAction.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            created_by=self.user,
            jurisdiction="US",
            action_type=OfficeAction.ActionType.NON_FINAL,
            application_number="19/777,888",
            source_filename="office-action.pdf",
            storage_key="office-actions/acl/source.pdf",
            content_sha256=hashlib.sha256(b"pdf").hexdigest(),
        )

    def test_office_action_read_requires_both_office_action_and_patent_grants(self):
        from enterprise_identity.models import MatterGrant, MatterPolicy
        MatterPolicy.objects.create(
            tenant=self.tenant, matter_type="office_action", matter_id=self.action.pk,
            enforce_acl=True, created_by=self.user,
        )
        MatterPolicy.objects.create(
            tenant=self.tenant, matter_type="patent", matter_id=self.patent.pk,
            enforce_acl=True, created_by=self.user,
        )
        client = APIClient()
        client.force_authenticate(self.user)
        url = f"/api/v1/office-actions/{self.action.pk}/"
        headers = {"HTTP_X_TENANT_ID": str(self.tenant.pk)}
        self.assertEqual(client.get(url, **headers).status_code, 403)

        MatterGrant.objects.create(
            tenant=self.tenant, matter_type="office_action", matter_id=self.action.pk,
            subject_user=self.user, level=MatterGrant.Level.READ, created_by=self.user,
        )
        self.assertEqual(client.get(url, **headers).status_code, 403)

        MatterGrant.objects.create(
            tenant=self.tenant, matter_type="patent", matter_id=self.patent.pk,
            subject_user=self.user, level=MatterGrant.Level.READ, created_by=self.user,
        )
        self.assertEqual(client.get(url, **headers).status_code, 200)
