"""Initial tenant-scoped claim-chart schema and immutable revision snapshots."""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("analyses", "0003_report_export_lifecycle"),
        ("tenants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ClaimChart",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=240)),
                ("description", models.TextField(blank=True)),
                ("status", models.CharField(choices=[("DRAFT", "Draft"), ("IN_REVIEW", "In review"), ("APPROVED", "Approved"), ("ARCHIVED", "Archived")], db_index=True, default="DRAFT", max_length=12)),
                ("revision", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("analysis", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claim_charts", to="analyses.analysisreport")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claim_charts_created", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="claim_charts_updated", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("-updated_at",),
                "indexes": [
                    models.Index(fields=["tenant", "status", "-updated_at"], name="claimchart_tenant_status_idx"),
                    models.Index(fields=["tenant", "analysis", "-created_at"], name="claimchart_tenant_analysis_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ClaimChartRow",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("limitation_index", models.PositiveIntegerField()),
                ("limitation_text", models.TextField()),
                ("mapping_status", models.CharField(choices=[("DIRECT_SUPPORT", "Direct support"), ("PARTIAL_SUPPORT", "Partial support"), ("POSSIBLE_EQUIVALENT", "Possible equivalent"), ("NOT_FOUND", "Not found"), ("CONTRADICTORY", "Contradictory"), ("UNCERTAIN", "Uncertain")], max_length=24)),
                ("mapping_summary", models.TextField(blank=True)),
                ("attorney_notes", models.TextField(blank=True)),
                ("attorney_decision", models.CharField(choices=[("UNREVIEWED", "Unreviewed"), ("ACCEPTED", "Accepted"), ("CORRECTED", "Corrected"), ("REJECTED", "Rejected")], db_index=True, default="UNREVIEWED", max_length=12)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assessment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claim_chart_rows", to="analyses.claimassessment")),
                ("chart", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rows", to="reports.claimchart")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("sort_order", "limitation_index", "created_at"),
                "indexes": [models.Index(fields=["tenant", "chart", "sort_order"], name="claimchart_row_order_idx")],
                "constraints": [models.UniqueConstraint(fields=("chart", "assessment"), name="claimchart_row_assessment_uniq")],
            },
        ),
        migrations.CreateModel(
            name="ClaimChartRevision",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("revision_number", models.PositiveIntegerField()),
                ("snapshot", models.JSONField()),
                ("snapshot_sha256", models.CharField(max_length=64)),
                ("change_summary", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("chart", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="revisions", to="reports.claimchart")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claim_chart_revisions_created", to=settings.AUTH_USER_MODEL)),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("-revision_number",),
                "indexes": [models.Index(fields=["tenant", "chart", "-revision_number"], name="claimchart_rev_tenant_idx")],
                "constraints": [models.UniqueConstraint(fields=("chart", "revision_number"), name="claimchart_revision_number_uniq")],
            },
        ),
        migrations.CreateModel(
            name="ClaimChartExport",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("format", models.CharField(choices=[("DOCX", "Editable Word document"), ("XLSX", "Editable Excel workbook"), ("PDF", "PDF"), ("JSON", "JSON snapshot")], max_length=4)),
                ("status", models.CharField(choices=[("QUEUED", "Queued"), ("RUNNING", "Running"), ("COMPLETED", "Completed"), ("FAILED", "Failed")], db_index=True, default="QUEUED", max_length=10)),
                ("task_id", models.CharField(max_length=100, unique=True)),
                ("storage_key", models.TextField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=100, null=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("chart", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="exports", to="reports.claimchart")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="claim_chart_exports_requested", to=settings.AUTH_USER_MODEL)),
                ("revision", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="exports", to="reports.claimchartrevision")),
                ("tenant", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("-created_at",),
                "indexes": [
                    models.Index(fields=["tenant", "status", "-created_at"], name="chart_export_tenant_idx"),
                    models.Index(fields=["tenant", "chart", "-created_at"], name="chart_export_chart_idx"),
                ],
            },
        ),
    ]
