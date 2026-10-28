# Initial schema for Phase 10 — Portfolio Valuation and Diligence Indicators.
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("landscapes", "0001_initial"),
        ("patents", "0003_competitor_surveillance"),
        ("tenants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PortfolioValuationStudy",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=240)),
                ("description", models.TextField(blank=True, default="")),
                ("currency", models.CharField(default="USD", max_length=3)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("READY", "Ready"), ("ARCHIVED", "Archived")], db_index=True, default="DRAFT", max_length=10)),
                ("methodology_version", models.CharField(default="diligence-composition-v1", max_length=60)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="portfolio_valuation_studies_created", to=settings.AUTH_USER_MODEL)),
                ("source_landscape_study", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="valuation_studies", to="landscapes.landscapestudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={"ordering": ("-updated_at",), "indexes": [models.Index(fields=["tenant", "status", "-updated_at"], name="val_study_status_idx")]},
        ),
        migrations.CreateModel(
            name="PortfolioScenario",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=160)),
                ("description", models.TextField(blank=True, default="")),
                ("method", models.CharField(choices=[("INCOME", "Income — relief from royalty"), ("MARKET", "Market — comparable transactions"), ("COST", "Cost — replacement cost less obsolescence")], max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="portfolio_scenarios_created", to=settings.AUTH_USER_MODEL)),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="scenarios", to="portfolio_valuation.portfoliovaluationstudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={"ordering": ("name",), "indexes": [models.Index(fields=["tenant", "study", "method"], name="val_scenario_method_idx")], "constraints": [models.UniqueConstraint(fields=("tenant", "study", "name"), name="val_scenario_name_uniq")]},
        ),
        migrations.CreateModel(
            name="PortfolioAssetSnapshot",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("publication_number", models.CharField(blank=True, default="", max_length=100)),
                ("application_number", models.CharField(blank=True, default="", max_length=100)),
                ("jurisdiction", models.CharField(blank=True, default="", max_length=10)),
                ("title", models.TextField()),
                ("abstract", models.TextField(blank=True, default="")),
                ("filing_date", models.DateField(blank=True, null=True)),
                ("publication_date", models.DateField(blank=True, null=True)),
                ("grant_date", models.DateField(blank=True, null=True)),
                ("legal_status", models.CharField(default="UNKNOWN", max_length=20)),
                ("applicant_names", models.JSONField(blank=True, default=list)),
                ("ipc_codes", models.JSONField(blank=True, default=list)),
                ("cpc_codes", models.JSONField(blank=True, default=list)),
                ("family_system", models.CharField(blank=True, default="", max_length=20)),
                ("family_identifier", models.CharField(blank=True, default="", max_length=200)),
                ("source_url", models.URLField(blank=True, default="", max_length=2000)),
                ("source_payload_hash", models.CharField(blank=True, default="", max_length=64)),
                ("snapshot_sha256", models.CharField(max_length=64)),
                ("source_last_synced_at", models.DateTimeField(blank=True, null=True)),
                ("forward_citation_count", models.PositiveIntegerField(blank=True, null=True)),
                ("backward_citation_count", models.PositiveIntegerField(blank=True, null=True)),
                ("citation_provenance", models.JSONField(blank=True, default=dict)),
                ("legal_event_count", models.PositiveIntegerField(default=0)),
                ("latest_legal_event", models.JSONField(blank=True, default=dict)),
                ("captured_at", models.DateTimeField(auto_now_add=True)),
                ("publication", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="portfolio_valuation_snapshots", to="patents.patentpublication")),
                ("source_snapshot", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="portfolio_valuation_snapshots", to="patents.patentsourcesnapshot")),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assets", to="portfolio_valuation.portfoliovaluationstudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={"ordering": ("publication_number", "pk"), "indexes": [models.Index(fields=["tenant", "study", "legal_status"], name="val_asset_status_idx"), models.Index(fields=["tenant", "study", "jurisdiction"], name="val_asset_juris_idx")], "constraints": [models.UniqueConstraint(fields=("tenant", "study", "publication"), name="val_asset_pub_uniq")]},
        ),
        migrations.CreateModel(
            name="PortfolioDiligenceSnapshot",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("asset_manifest_sha256", models.CharField(max_length=64)),
                ("methodology_version", models.CharField(default="diligence-composition-v1", max_length=60)),
                ("asset_count", models.PositiveIntegerField(default=0)),
                ("composition_score", models.DecimalField(blank=True, decimal_places=2, max_digits=5, null=True)),
                ("metrics", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="portfolio_diligence_snapshots_created", to=settings.AUTH_USER_MODEL)),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="diligence_snapshots", to="portfolio_valuation.portfoliovaluationstudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={"ordering": ("-created_at",), "indexes": [models.Index(fields=["tenant", "study", "-created_at"], name="val_diligence_study_idx")]},
        ),
        migrations.CreateModel(
            name="PortfolioScenarioRevision",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("revision_number", models.PositiveIntegerField()),
                ("currency", models.CharField(default="USD", max_length=3)),
                ("assumptions", models.JSONField(default=dict)),
                ("calculated_result", models.JSONField(default=dict)),
                ("input_sha256", models.CharField(max_length=64)),
                ("asset_manifest_sha256", models.CharField(max_length=64)),
                ("methodology_version", models.CharField(default="valuation-methods-v1", max_length=60)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="portfolio_scenario_revisions_created", to=settings.AUTH_USER_MODEL)),
                ("scenario", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="revisions", to="portfolio_valuation.portfolioscenario")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={"ordering": ("-revision_number",), "indexes": [models.Index(fields=["tenant", "scenario", "-revision_number"], name="val_scenario_revisions_idx")], "constraints": [models.UniqueConstraint(fields=("tenant", "scenario", "revision_number"), name="val_scenario_rev_uniq")]},
        ),
        migrations.CreateModel(
            name="PortfolioAnnotation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("DILIGENCE", "Diligence"), ("LEGAL_STATUS", "Legal status"), ("CITATION", "Citation evidence"), ("FINANCIAL_ASSUMPTION", "Financial assumption"), ("REVIEW_QUESTION", "Review question")], default="DILIGENCE", max_length=24)),
                ("body", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("asset", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="annotations", to="portfolio_valuation.portfolioassetsnapshot")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="portfolio_annotations_created", to=settings.AUTH_USER_MODEL)),
                ("scenario", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="annotations", to="portfolio_valuation.portfolioscenario")),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="annotations", to="portfolio_valuation.portfoliovaluationstudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={"ordering": ("-created_at",), "indexes": [models.Index(fields=["tenant", "study", "-created_at"], name="val_annotation_study_idx")]},
        ),
    ]
