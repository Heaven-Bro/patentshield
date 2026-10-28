# Generated for PatentShield Phase 4: Office Action Response Assistant.
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("patents", "0003_competitor_surveillance"),
        ("tenants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="OfficeAction",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="office_actions_officeaction_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("jurisdiction", models.CharField(db_index=True, default="US", max_length=10)),
                ("action_type", models.CharField(choices=[("NON_FINAL", "Non-final office action"), ("FINAL", "Final office action"), ("NOTICE_OF_ALLOWANCE", "Notice of allowance"), ("EXAMINER_INTERVIEW", "Examiner interview summary"), ("OTHER", "Other communication")], default="NON_FINAL", max_length=24)),
                ("application_number", models.CharField(blank=True, default="", max_length=100)),
                ("examiner_name", models.CharField(blank=True, default="", max_length=200)),
                ("issued_date", models.DateField(blank=True, null=True)),
                ("response_due_date", models.DateField(blank=True, null=True)),
                ("source_filename", models.CharField(max_length=255)),
                ("storage_key", models.TextField()),
                ("content_sha256", models.CharField(max_length=64)),
                ("mime_type", models.CharField(default="application/pdf", max_length=100)),
                ("page_count", models.PositiveIntegerField(blank=True, null=True)),
                ("extracted_text", models.TextField(blank=True, default="")),
                ("extracted_pages", models.JSONField(blank=True, default=list)),
                ("extraction_status", models.CharField(choices=[("PENDING", "Pending"), ("PROCESSING", "Processing"), ("RETRYING", "Retrying"), ("EXTRACTED", "Extracted"), ("FAILED", "Failed")], db_index=True, default="PENDING", max_length=12)),
                ("parse_status", models.CharField(choices=[("PENDING", "Pending"), ("COMPLETED", "Completed"), ("NEEDS_REVIEW", "Needs attorney review"), ("FAILED", "Failed")], db_index=True, default="PENDING", max_length=12)),
                ("extraction_error_code", models.CharField(blank=True, default="", max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="office_actions_uploaded", to=settings.AUTH_USER_MODEL)),
                ("patent", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="office_actions", to="patents.patent")),
            ],
            options={
                "ordering": ("-issued_date", "-created_at"),
                "indexes": [models.Index(fields=["tenant", "patent", "-created_at"], name="oa_tenant_patent_created_idx")],
                "constraints": [models.CheckConstraint(condition=models.Q(("response_due_date__isnull", True), ("issued_date__isnull", True), ("response_due_date__gte", models.F("issued_date")), _connector="OR"), name="oa_due_date_not_before_issue")],
            },
        ),
        migrations.CreateModel(
            name="CitedReference",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="office_actions_citedreference_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("identifier", models.CharField(max_length=100)),
                ("title", models.CharField(blank=True, default="", max_length=500)),
                ("assignee_or_author", models.CharField(blank=True, default="", max_length=300)),
                ("source_page", models.PositiveIntegerField(blank=True, null=True)),
                ("source_start_offset", models.PositiveIntegerField(blank=True, null=True)),
                ("source_end_offset", models.PositiveIntegerField(blank=True, null=True)),
                ("source_quote", models.TextField(blank=True, default="")),
                ("fingerprint", models.CharField(max_length=64)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("office_action", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="cited_references", to="office_actions.officeaction")),
            ],
            options={
                "ordering": ("identifier",),
                "constraints": [models.UniqueConstraint(fields=("office_action", "identifier"), name="oa_cited_reference_identifier_uniq")],
            },
        ),
        migrations.CreateModel(
            name="Rejection",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="office_actions_rejection_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("ground", models.CharField(choices=[("US_101", "35 U.S.C. §101"), ("US_102", "35 U.S.C. §102"), ("US_103", "35 U.S.C. §103"), ("US_112", "35 U.S.C. §112"), ("EP_52", "EPC Article 52"), ("EP_54", "EPC Article 54"), ("EP_56", "EPC Article 56"), ("PCT", "PCT objection or written opinion"), ("FORMALITY", "Formality objection"), ("OTHER", "Other / unclassified")], db_index=True, max_length=16)),
                ("claim_numbers", models.JSONField(blank=True, default=list)),
                ("examiner_reasoning", models.TextField(blank=True, default="")),
                ("source_page", models.PositiveIntegerField(blank=True, help_text="One-based PDF page number", null=True)),
                ("source_start_offset", models.PositiveIntegerField(blank=True, null=True)),
                ("source_end_offset", models.PositiveIntegerField(blank=True, null=True)),
                ("source_quote", models.TextField()),
                ("parser_confidence", models.FloatField(default=0.5)),
                ("needs_attorney_review", models.BooleanField(db_index=True, default=True)),
                ("fingerprint", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("office_action", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rejections", to="office_actions.officeaction")),
            ],
            options={
                "ordering": ("source_page", "source_start_offset", "created_at"),
                "constraints": [models.UniqueConstraint(fields=("office_action", "fingerprint"), name="oa_rejection_fingerprint_uniq")],
            },
        ),
        migrations.CreateModel(
            name="ResponseDraft",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="office_actions_responsedraft_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(default="Office action response draft", max_length=240)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("IN_REVIEW", "In review"), ("CHANGES_REQUESTED", "Changes requested"), ("APPROVED", "Attorney approved"), ("WITHDRAWN", "Withdrawn")], db_index=True, default="DRAFT", max_length=20)),
                ("current_revision_number", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="office_action_drafts_created", to=settings.AUTH_USER_MODEL)),
                ("office_action", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="drafts", to="office_actions.officeaction")),
            ],
            options={"ordering": ("-updated_at",)},
        ),
        migrations.CreateModel(
            name="AmendmentSupportCheck",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="office_actions_amendmentsupportcheck_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("claim_number", models.CharField(max_length=30)),
                ("proposed_claim_text", models.TextField()),
                ("status", models.CharField(choices=[("SUPPORTED", "Exact text located"), ("PARTIAL", "Some text located"), ("NOT_FOUND", "Text not located"), ("NOT_CHECKED", "Not checked")], default="NOT_CHECKED", max_length=16)),
                ("source_manifest", models.JSONField(blank=True, default=dict)),
                ("explanation", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("checked_document_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="office_action_support_checks", to="patents.patentdocumentversion")),
                ("office_action", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="amendment_checks", to="office_actions.officeaction")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="amendment_support_checks_requested", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="ResponseDraftRevision",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="office_actions_responsedraftrevision_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("revision_number", models.PositiveIntegerField()),
                ("content_markdown", models.TextField()),
                ("source_manifest", models.JSONField(blank=True, default=dict)),
                ("content_sha256", models.CharField(max_length=64)),
                ("generation_method", models.CharField(default="evidence_template", max_length=40)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("draft", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="revisions", to="office_actions.responsedraft")),
                ("generated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="office_action_draft_revisions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("-revision_number",),
                "constraints": [models.UniqueConstraint(fields=("draft", "revision_number"), name="oa_draft_revision_num_uniq")],
            },
        ),
        migrations.CreateModel(
            name="OfficeActionReview",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="office_actions_officeactionreview_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("decision", models.CharField(choices=[("APPROVED", "Approved"), ("CHANGES_REQUESTED", "Changes requested"), ("REJECTED", "Rejected")], max_length=20)),
                ("comments", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reviewer", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="office_action_reviews", to=settings.AUTH_USER_MODEL)),
                ("revision", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="reviews", to="office_actions.responsedraftrevision")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="RejectionReference",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="office_actions_rejectionreference_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("relationship", models.CharField(default="EXAMINER_CITED", max_length=40)),
                ("source_quote", models.TextField(blank=True, default="")),
                ("source_page", models.PositiveIntegerField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("cited_reference", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rejection_links", to="office_actions.citedreference")),
                ("rejection", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reference_links", to="office_actions.rejection")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("rejection", "cited_reference"), name="oa_rejection_ref_link_uniq")],
            },
        ),
    ]
