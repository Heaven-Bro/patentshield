# Generated for PatentShield Phase 6 — explainable FTO candidate matching.
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("fto", "0002_productversion_ocr_extraction_statuses"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="ftostudy",
            name="matching_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("QUEUED", "Queued"),
                    ("RUNNING", "Running"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="PENDING",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="ftostudy",
            name="matching_task_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="ftostudy",
            name="matching_requested_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fto_match_jobs_requested", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name="ftostudy",
            name="matching_queued_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ftostudy",
            name="matching_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ftostudy",
            name="matching_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="ftostudy",
            name="matching_error_code",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AddIndex(
            model_name="ftostudy",
            index=models.Index(fields=["tenant", "matching_status", "matching_queued_at"], name="fto_match_state_queued_idx"),
        ),
        migrations.CreateModel(
            name="FTOCandidateMatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("algorithm_version", models.CharField(default="LEXICAL_OVERLAP_V1", max_length=40)),
                ("feature_title_snapshot", models.CharField(max_length=240)),
                ("feature_review_status_snapshot", models.CharField(max_length=12)),
                ("source_quote_snapshot", models.TextField()),
                ("source_page_index_snapshot", models.PositiveIntegerField(blank=True, null=True)),
                ("source_start_offset_snapshot", models.PositiveIntegerField(blank=True, null=True)),
                ("source_end_offset_snapshot", models.PositiveIntegerField(blank=True, null=True)),
                ("score", models.FloatField()),
                ("limitation_coverage", models.FloatField()),
                ("matched_terms", models.JSONField(blank=True, default=list)),
                ("rationale", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("assessment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="candidate_matches", to="fto.limitationassessment")),
                ("feature", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="fto_candidate_matches", to="fto.productfeature")),
                ("study", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="candidate_matches", to="fto.ftostudy")),
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="%(app_label)s_%(class)s_records", to="tenants.tenant")),
            ],
            options={
                "ordering": ("assessment_id", "-score", "feature_id"),
                "indexes": [
                    models.Index(fields=["tenant", "study", "assessment"], name="fto_candidate_study_idx"),
                    models.Index(fields=["tenant", "study", "-score"], name="fto_candidate_score_idx"),
                ],
                "constraints": [
                    models.UniqueConstraint(fields=("assessment", "feature", "algorithm_version"), name="fto_candidate_match_uniq"),
                    models.CheckConstraint(condition=Q(score__gte=0.0) & Q(score__lte=1.0), name="fto_candidate_score_ck"),
                    models.CheckConstraint(condition=Q(limitation_coverage__gte=0.0) & Q(limitation_coverage__lte=1.0), name="fto_candidate_coverage_ck"),
                ],
            },
        ),
    ]
