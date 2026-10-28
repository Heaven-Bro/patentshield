# Generated for PatentShield Phase 6 — FTO and Product-to-Claim Risk Mapping.
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
            name="ProductMatter",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("product_code", models.CharField(blank=True, default="", max_length=100)),
                ("description", models.TextField(blank=True, default="")),
                ("jurisdictions", models.JSONField(blank=True, default=list)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("ACTIVE", "Active"), ("ARCHIVED", "Archived")], db_index=True, default="DRAFT", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_product_matters_created", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("-updated_at",),
                "indexes": [models.Index(fields=["tenant", "status", "-updated_at"], name="fto_matter_tenant_status_idx")],
                "constraints": [models.UniqueConstraint(condition=~models.Q(("product_code", "")), fields=("tenant", "product_code"), name="fto_product_code_uniq")],
            },
        ),
        migrations.CreateModel(
            name="ProductVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version_number", models.PositiveIntegerField()),
                ("version_label", models.CharField(blank=True, default="", max_length=100)),
                ("source_filename", models.CharField(max_length=255)),
                ("storage_key", models.TextField()),
                ("content_sha256", models.CharField(db_index=True, max_length=64)),
                ("mime_type", models.CharField(default="application/pdf", max_length=100)),
                ("page_count", models.PositiveIntegerField(blank=True, null=True)),
                ("extracted_text", models.TextField(blank=True, default="")),
                ("extracted_pages", models.JSONField(blank=True, default=list)),
                ("extraction_status", models.CharField(choices=[("PENDING", "Pending"), ("PROCESSING", "Processing"), ("EXTRACTED", "Extracted"), ("FAILED", "Failed")], db_index=True, default="PENDING", max_length=12)),
                ("decomposition_status", models.CharField(choices=[("PENDING", "Pending"), ("PROCESSING", "Processing"), ("COMPLETED", "Completed"), ("FAILED", "Failed")], db_index=True, default="PENDING", max_length=12)),
                ("error_code", models.CharField(blank=True, default="", max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_product_versions_created", to=settings.AUTH_USER_MODEL)),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="versions", to="fto.productmatter")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("-version_number",),
                "indexes": [models.Index(fields=["tenant", "product", "-version_number"], name="fto_version_tenant_product_idx")],
                "constraints": [models.UniqueConstraint(fields=("product", "version_number"), name="fto_product_version_uniq")],
            },
        ),
        migrations.CreateModel(
            name="ProductFeature",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("feature_key", models.CharField(max_length=64)),
                ("title", models.CharField(max_length=240)),
                ("description", models.TextField(blank=True, default="")),
                ("source_quote", models.TextField()),
                ("source_page_index", models.PositiveIntegerField(blank=True, help_text="Zero-based PDF page index", null=True)),
                ("source_start_offset", models.PositiveIntegerField(blank=True, help_text="Character offset within the source page", null=True)),
                ("source_end_offset", models.PositiveIntegerField(blank=True, help_text="Exclusive character offset within the source page", null=True)),
                ("extraction_method", models.CharField(default="RULE_BASED_CANDIDATE", max_length=40)),
                ("extraction_confidence", models.FloatField(blank=True, null=True)),
                ("review_status", models.CharField(choices=[("CANDIDATE", "Candidate — needs review"), ("CONFIRMED", "Attorney confirmed"), ("REJECTED", "Rejected as a feature")], db_index=True, default="CANDIDATE", max_length=12)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_features_created", to=settings.AUTH_USER_MODEL)),
                ("product_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="features", to="fto.productversion")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fto_features_reviewed", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("source_page_index", "source_start_offset", "created_at"),
                "indexes": [models.Index(fields=["tenant", "product_version", "review_status"], name="fto_feature_tenant_status_idx")],
                "constraints": [
                    models.UniqueConstraint(fields=("product_version", "feature_key"), name="fto_feature_version_key_uniq"),
                    models.CheckConstraint(condition=(models.Q(("extraction_confidence__isnull", True)) | (models.Q(("extraction_confidence__gte", 0.0)) & models.Q(("extraction_confidence__lte", 1.0)))), name="fto_feat_conf_rng_ck"),
                ],
            },
        ),
        migrations.CreateModel(
            name="FTOStudy",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=240)),
                ("jurisdiction", models.CharField(blank=True, default="", max_length=10)),
                ("selected_claim_ids", models.JSONField(blank=True, default=list, help_text="Empty means all claims available at creation")),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("IN_PROGRESS", "In progress"), ("READY_FOR_REVIEW", "Ready for review"), ("REVIEWED", "Reviewed"), ("ARCHIVED", "Archived")], db_index=True, default="DRAFT", max_length=20)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_studies_created", to=settings.AUTH_USER_MODEL)),
                ("patent", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_studies", to="patents.patent")),
                ("patent_document_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_studies", to="patents.patentdocumentversion")),
                ("product_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_studies", to="fto.productversion")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fto_studies_reviewed", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("-updated_at",),
                "indexes": [models.Index(fields=["tenant", "status", "-updated_at"], name="fto_study_tenant_status_idx")],
            },
        ),
        migrations.CreateModel(
            name="StudyClaimLimitation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("limitation_index", models.PositiveIntegerField()),
                ("limitation_text", models.TextField()),
                ("source_start_offset", models.PositiveIntegerField()),
                ("source_end_offset", models.PositiveIntegerField()),
                ("segmentation_method", models.CharField(default="CLAUSE_HEURISTIC", max_length=40)),
                ("needs_attorney_review", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("claim", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_study_limitations", to="patents.claim")),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="limitations", to="fto.ftostudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("claim__claim_number", "limitation_index"),
                "indexes": [models.Index(fields=["tenant", "study", "claim"], name="fto_limit_tenant_study_idx")],
                "constraints": [models.UniqueConstraint(fields=("study", "claim", "limitation_index"), name="fto_study_claim_limit_uniq")],
            },
        ),
        migrations.CreateModel(
            name="LimitationAssessment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("triage_status", models.CharField(choices=[("UNASSESSED", "Unassessed"), ("POTENTIAL_MATCH", "Potential correspondence identified"), ("PARTIAL_MATCH", "Partial correspondence identified"), ("NO_MATCH_IDENTIFIED", "No feature identified in reviewed scope"), ("COUNTER_EVIDENCE_PRESENT", "Counter-evidence present"), ("CONFLICTING_EVIDENCE", "Conflicting evidence"), ("UNCERTAIN", "Uncertain — further review needed")], db_index=True, default="UNASSESSED", max_length=24)),
                ("triage_rationale", models.TextField(blank=True, default="")),
                ("review_status", models.CharField(choices=[("UNREVIEWED", "Unreviewed"), ("IN_REVIEW", "In review"), ("REVIEWED", "Reviewed by attorney")], db_index=True, default="UNREVIEWED", max_length=12)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("limitation", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="assessment", to="fto.studyclaimlimitation")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fto_limitation_reviews", to=settings.AUTH_USER_MODEL)),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assessments", to="fto.ftostudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fto_limitation_updates", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ("limitation__claim__claim_number", "limitation__limitation_index"),
                "indexes": [models.Index(fields=["tenant", "study", "triage_status"], name="fto_assess_tenant_state_idx")],
            },
        ),
        migrations.CreateModel(
            name="LimitationEvidence",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("SUPPORTING", "Potentially supports correspondence"), ("COUNTER", "Potentially counters correspondence"), ("CONTEXT", "Context only")], max_length=12)),
                ("source_quote", models.TextField()),
                ("source_page_index", models.PositiveIntegerField(blank=True, null=True)),
                ("source_start_offset", models.PositiveIntegerField(blank=True, null=True)),
                ("source_end_offset", models.PositiveIntegerField(blank=True, null=True)),
                ("attorney_rationale", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assessment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="evidence", to="fto.limitationassessment")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_evidence_created", to=settings.AUTH_USER_MODEL)),
                ("feature", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="limitation_evidence", to="fto.productfeature")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("created_at",),
                "indexes": [models.Index(fields=["tenant", "assessment", "kind"], name="fto_evidence_tenant_kind_idx")],
                "constraints": [models.UniqueConstraint(fields=("assessment", "feature", "kind"), name="fto_evidence_feat_kind_uniq")],
            },
        ),
        migrations.CreateModel(
            name="AttorneyAnnotation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("category", models.CharField(choices=[("NOTE", "Attorney note"), ("COUNTER_EVIDENCE", "Counter-evidence note"), ("QUESTION", "Question for review"), ("DECISION", "Review decision context")], default="NOTE", max_length=20)),
                ("body", models.TextField()),
                ("resolved", models.BooleanField(db_index=True, default=False)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assessment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="annotations", to="fto.limitationassessment")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_annotations_created", to=settings.AUTH_USER_MODEL)),
                ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fto_annotations_resolved", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("resolved", "created_at"),
                "indexes": [models.Index(fields=["tenant", "assessment", "resolved"], name="fto_annot_tenant_resolved_idx")],
            },
        ),
    ]
