# Generated for PatentShield Phase 7 — Patent Landscape and White-Space Analytics.
import uuid

import django.db.models.deletion
import pgvector.django.indexes
import pgvector.django.vector
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
            name="AssigneeEntity",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("canonical_name", models.CharField(max_length=400)),
                ("normalized_key", models.CharField(max_length=500)),
                ("country_code", models.CharField(blank=True, default="", max_length=2)),
                ("status", models.CharField(choices=[("UNREVIEWED", "Unreviewed"), ("CONFIRMED", "Confirmed"), ("DO_NOT_MERGE", "Do not merge")], default="UNREVIEWED", max_length=16)),
                ("notes", models.TextField(blank=True, default="")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landscape_assignee_entities_created", to=settings.AUTH_USER_MODEL)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="landscape_assignee_entities_reviewed", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("canonical_name",),
                "indexes": [models.Index(fields=["tenant", "status"], name="land_assignee_status_idx")],
                "constraints": [models.UniqueConstraint(fields=("tenant", "normalized_key"), name="land_assignee_key_uniq")],
            },
        ),
        migrations.CreateModel(
            name="LandscapeStudy",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=240)),
                ("description", models.TextField(blank=True, default="")),
                ("scope_filters", models.JSONField(blank=True, default=dict)),
                ("analysis_parameters", models.JSONField(blank=True, default=dict)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("BUILDING", "Building"), ("READY", "Ready"), ("FAILED", "Failed"), ("ARCHIVED", "Archived")], db_index=True, default="DRAFT", max_length=12)),
                ("algorithm_version", models.CharField(default="bge-m3-kmeans-rp-v1", max_length=60)),
                ("error_code", models.CharField(blank=True, default="", max_length=80)),
                ("built_by_task_id", models.CharField(blank=True, default="", max_length=100)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landscape_studies_created", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("-updated_at",),
                "indexes": [models.Index(fields=["tenant", "status", "-updated_at"], name="land_study_status_idx")],
            },
        ),
        migrations.CreateModel(
            name="TechnologyTaxonomy",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source", models.CharField(choices=[("CPC", "Cooperative Patent Classification"), ("IPC", "International Patent Classification"), ("CUSTOM", "Custom taxonomy")], max_length=8)),
                ("code", models.CharField(max_length=100)),
                ("label", models.CharField(max_length=300)),
                ("description", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="children", to="landscapes.technologytaxonomy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("source", "code"),
                "indexes": [models.Index(fields=["tenant", "source", "code"], name="land_tax_tenant_code_idx")],
                "constraints": [models.UniqueConstraint(fields=("tenant", "source", "code"), name="land_tax_source_code_uniq")],
            },
        ),
        migrations.CreateModel(
            name="AssigneeAlias",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("original_name", models.CharField(max_length=400)),
                ("normalized_key", models.CharField(max_length=500)),
                ("source", models.CharField(choices=[("AUTO", "Automatic normalization"), ("ATTORNEY", "Attorney supplied"), ("IMPORT", "Imported mapping")], default="AUTO", max_length=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landscape_assignee_aliases_created", to=settings.AUTH_USER_MODEL)),
                ("entity", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="aliases", to="landscapes.assigneeentity")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("original_name",),
                "indexes": [models.Index(fields=["tenant", "entity"], name="land_alias_entity_idx")],
                "constraints": [models.UniqueConstraint(fields=("tenant", "normalized_key"), name="land_assignee_alias_key_uniq")],
            },
        ),
        migrations.CreateModel(
            name="LandscapeCluster",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("cluster_number", models.PositiveSmallIntegerField()),
                ("label", models.CharField(max_length=300)),
                ("explanation", models.TextField(blank=True, default="")),
                ("top_terms", models.JSONField(blank=True, default=list)),
                ("top_classification_codes", models.JSONField(blank=True, default=list)),
                ("publication_count", models.PositiveIntegerField(default=0)),
                ("algorithm", models.CharField(default="deterministic-kmeans", max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="clusters", to="landscapes.landscapestudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("cluster_number",),
                "indexes": [models.Index(fields=["tenant", "study"], name="land_cluster_study_idx")],
                "constraints": [models.UniqueConstraint(fields=("tenant", "study", "cluster_number"), name="land_cluster_num_uniq")],
            },
        ),
        migrations.CreateModel(
            name="LandscapeStudyPublication",
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
                ("legal_status", models.CharField(blank=True, default="UNKNOWN", max_length=20)),
                ("applicant_names", models.JSONField(blank=True, default=list)),
                ("normalized_assignee_names", models.JSONField(blank=True, default=list)),
                ("ipc_codes", models.JSONField(blank=True, default=list)),
                ("cpc_codes", models.JSONField(blank=True, default=list)),
                ("family_system", models.CharField(blank=True, default="", max_length=20)),
                ("family_identifier", models.CharField(blank=True, default="", max_length=200)),
                ("source_url", models.URLField(blank=True, default="", max_length=2000)),
                ("source_payload_hash", models.CharField(blank=True, default="", max_length=64)),
                ("snapshot_sha256", models.CharField(max_length=64)),
                ("projection_x", models.FloatField(blank=True, null=True)),
                ("projection_y", models.FloatField(blank=True, null=True)),
                ("projection_method", models.CharField(blank=True, default="", max_length=40)),
                ("added_at", models.DateTimeField(auto_now_add=True)),
                ("cluster", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="publications", to="landscapes.landscapecluster")),
                ("publication", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landscape_snapshots", to="patents.patentpublication")),
                ("source_snapshot", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="landscape_publication_snapshots", to="patents.patentsourcesnapshot")),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="publications", to="landscapes.landscapestudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("filing_date", "publication_number", "pk"),
                "indexes": [models.Index(fields=["tenant", "study", "filing_date"], name="land_pub_timeline_idx"), models.Index(fields=["tenant", "study", "jurisdiction"], name="land_pub_geo_idx")],
                "constraints": [models.UniqueConstraint(fields=("tenant", "study", "publication"), name="land_study_pub_uniq")],
            },
        ),
        migrations.CreateModel(
            name="LandscapePublicationEmbedding",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("text_sha256", models.CharField(max_length=64)),
                ("embedding_model", models.CharField(default="BAAI/bge-m3", max_length=100)),
                ("embedding", pgvector.django.vector.VectorField(dimensions=1024)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("study_publication", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="vector_record", to="landscapes.landscapestudypublication")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={"indexes": [pgvector.django.indexes.HnswIndex(ef_construction=64, fields=["embedding"], m=16, name="land_pub_emb_hnsw", opclasses=("vector_cosine_ops",))]},
        ),
        migrations.CreateModel(
            name="LandscapePublicationAssignee",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("raw_name", models.CharField(max_length=400)),
                ("entity", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="publication_links", to="landscapes.assigneeentity")),
                ("study_publication", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assignee_links", to="landscapes.landscapestudypublication")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "indexes": [models.Index(fields=["tenant", "entity"], name="land_pub_assignee_entity_idx")],
                "constraints": [models.UniqueConstraint(fields=("tenant", "study_publication", "entity"), name="land_pub_assignee_uniq")],
            },
        ),
        migrations.CreateModel(
            name="LandscapePublicationTechnology",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_code", models.CharField(max_length=100)),
                ("study_publication", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="technology_links", to="landscapes.landscapestudypublication")),
                ("taxonomy", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="publication_links", to="landscapes.technologytaxonomy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "indexes": [models.Index(fields=["tenant", "taxonomy"], name="land_pub_taxonomy_node_idx")],
                "constraints": [models.UniqueConstraint(fields=("tenant", "study_publication", "taxonomy"), name="land_pub_taxonomy_uniq")],
            },
        ),
        migrations.CreateModel(
            name="LandscapeAnnotation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("kind", models.CharField(choices=[("OVERVIEW", "Study overview"), ("ASSIGNEE", "Assignee normalization"), ("TAXONOMY", "Technology taxonomy"), ("CLUSTER", "Cluster interpretation"), ("DATA_QUALITY", "Data quality or coverage limitation")], default="OVERVIEW", max_length=20)),
                ("body", models.TextField()),
                ("status", models.CharField(choices=[("OPEN", "Open"), ("RESOLVED", "Resolved")], default="OPEN", max_length=10)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="landscape_annotations_created", to=settings.AUTH_USER_MODEL)),
                ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="landscape_annotations_resolved", to=settings.AUTH_USER_MODEL)),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="annotations", to="landscapes.landscapestudy")),
                ("study_publication", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="annotations", to="landscapes.landscapestudypublication")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("-updated_at",),
                "indexes": [models.Index(fields=["tenant", "study", "status"], name="land_annotation_status_idx")],
            },
        ),
    ]
