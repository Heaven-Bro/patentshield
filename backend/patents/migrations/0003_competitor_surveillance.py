# Generated for PatentShield Phase 3: competitor surveillance and webhook alerts.
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    dependencies = [
        ("patents", "0002_connectorsynccursor_patentfamily_patentpublication_and_more"),
        ("tenants", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SurveillanceWatchlist",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_surveillancewatchlist_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=180)),
                ("description", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("poll_interval_minutes", models.PositiveSmallIntegerField(default=360)),
                ("poll_status", models.CharField(choices=[("IDLE", "Idle"), ("RUNNING", "Running"), ("SUCCEEDED", "Succeeded"), ("FAILED", "Failed")], db_index=True, default="IDLE", max_length=12)),
                ("next_poll_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("poll_started_at", models.DateTimeField(blank=True, null=True)),
                ("last_polled_at", models.DateTimeField(blank=True, null=True)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("active_task_id", models.CharField(blank=True, default="", max_length=100)),
                ("last_error_code", models.CharField(blank=True, default="", max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="surveillance_watchlists_created", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="surveillance_watchlists_updated", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("name", "created_at")},
        ),
        migrations.CreateModel(
            name="SurveillanceWatchRule",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_surveillancewatchrule_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("rule_type", models.CharField(choices=[("ASSIGNEE", "Competitor / assignee"), ("PUBLICATION", "Publication or application number"), ("FAMILY", "Patent family"), ("CLASSIFICATION", "IPC/CPC classification"), ("SAVED_SEARCH", "Saved provider search")], max_length=16)),
                ("value", models.CharField(max_length=500)),
                ("match_mode", models.CharField(choices=[("EXACT", "Exact"), ("CONTAINS", "Contains")], default="CONTAINS", max_length=8)),
                ("providers", models.JSONField(blank=True, default=list)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("watchlist", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rules", to="patents.surveillancewatchlist")),
            ],
            options={"ordering": ("rule_type", "value", "created_at")},
        ),
        migrations.CreateModel(
            name="AlertDestination",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_alertdestination_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=160)),
                ("channel", models.CharField(choices=[("EMAIL", "Email"), ("SLACK", "Slack incoming webhook"), ("TEAMS", "Microsoft Teams webhook")], max_length=8)),
                ("email_address", models.EmailField(blank=True, default="", max_length=254)),
                ("webhook_secret_ciphertext", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="surveillance_destinations_created", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("name", "created_at")},
        ),
        migrations.CreateModel(
            name="AlertSubscription",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_alertsubscription_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("alert_types", models.JSONField(blank=True, default=list)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("destination", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="subscriptions", to="patents.alertdestination")),
                ("watchlist", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="subscriptions", to="patents.surveillancewatchlist")),
            ],
        ),
        migrations.CreateModel(
            name="WatchlistObservation",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_watchlistobservation_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("last_status", models.CharField(blank=True, default="", max_length=20)),
                ("last_family_system", models.CharField(blank=True, default="", max_length=20)),
                ("last_family_identifier", models.CharField(blank=True, default="", max_length=200)),
                ("last_title", models.CharField(blank=True, default="", max_length=500)),
                ("first_observed_at", models.DateTimeField(auto_now_add=True)),
                ("last_observed_at", models.DateTimeField(auto_now=True)),
                ("last_snapshot", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="surveillance_observations", to="patents.patentsourcesnapshot")),
                ("publication", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="surveillance_observations", to="patents.patentpublication")),
                ("rule", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="observations", to="patents.surveillancewatchrule")),
                ("watchlist", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="observations", to="patents.surveillancewatchlist")),
            ],
        ),
        migrations.CreateModel(
            name="WatchlistObservedLegalEvent",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_watchlistobservedlegalevent_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("first_observed_at", models.DateTimeField(auto_now_add=True)),
                ("legal_event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="surveillance_observations", to="patents.patentlegalevent")),
                ("observation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="observed_legal_events", to="patents.watchlistobservation")),
            ],
        ),
        migrations.CreateModel(
            name="SurveillanceAlert",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_surveillancealert_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("alert_type", models.CharField(choices=[("PUBLICATION_MATCHED", "Publication matched"), ("PUBLICATION_UPDATED", "Publication metadata changed"), ("FAMILY_CHANGED", "Patent family changed"), ("STATUS_CHANGED", "Legal status changed"), ("LEGAL_EVENT", "New legal event")], max_length=24)),
                ("fingerprint", models.CharField(max_length=64)),
                ("title", models.CharField(max_length=300)),
                ("summary", models.TextField(blank=True, default="")),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("source_observed_at", models.DateTimeField(blank=True, null=True)),
                ("state", models.CharField(choices=[("OPEN", "Open"), ("ACKNOWLEDGED", "Acknowledged"), ("RESOLVED", "Resolved")], db_index=True, default="OPEN", max_length=14)),
                ("acknowledged_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("acknowledged_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="surveillance_alerts_acknowledged", to=settings.AUTH_USER_MODEL)),
                ("legal_event", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="surveillance_alerts", to="patents.patentlegalevent")),
                ("publication", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="surveillance_alerts", to="patents.patentpublication")),
                ("rule", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="alerts", to="patents.surveillancewatchrule")),
                ("source_snapshot", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="surveillance_alerts", to="patents.patentsourcesnapshot")),
                ("watchlist", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="alerts", to="patents.surveillancewatchlist")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="AlertDelivery",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_alertdelivery_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("QUEUED", "Queued"), ("SENDING", "Sending"), ("DELIVERED", "Delivered"), ("RETRYING", "Retrying"), ("DEAD_LETTER", "Dead letter"), ("CANCELLED", "Cancelled")], db_index=True, default="QUEUED", max_length=12)),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("idempotency_key", models.CharField(max_length=64, unique=True)),
                ("next_attempt_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("last_http_status", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("last_error_code", models.CharField(blank=True, default="", max_length=80)),
                ("provider_message_id", models.CharField(blank=True, default="", max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("alert", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deliveries", to="patents.surveillancealert")),
                ("destination", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="deliveries", to="patents.alertdestination")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="AlertDeliveryAttempt",
            fields=[
                ("tenant", models.ForeignKey(db_index=True, on_delete=django.db.models.deletion.CASCADE, related_name="patents_alertdeliveryattempt_records", to="tenants.tenant")),
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("attempt_number", models.PositiveSmallIntegerField()),
                ("outcome", models.CharField(choices=[("STARTED", "Started"), ("DELIVERED", "Delivered"), ("FAILED", "Failed"), ("RETRYING", "Retry scheduled"), ("DEAD_LETTER", "Dead letter")], max_length=12)),
                ("started_at", models.DateTimeField()),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("response_status", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, default="", max_length=80)),
                ("response_summary", models.CharField(blank=True, default="", max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("delivery", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="attempts", to="patents.alertdelivery")),
            ],
            options={"ordering": ("attempt_number",)},
        ),
        migrations.AddConstraint(
            model_name="surveillancewatchlist",
            constraint=models.UniqueConstraint(fields=("tenant", "name"), name="surv_watch_tenant_name_uniq"),
        ),
        migrations.AddConstraint(
            model_name="surveillancewatchlist",
            constraint=models.CheckConstraint(condition=models.Q(("poll_interval_minutes__gte", 15), ("poll_interval_minutes__lte", 10080)), name="surv_watch_interval_range"),
        ),
        migrations.AddConstraint(
            model_name="surveillancewatchrule",
            constraint=models.UniqueConstraint(fields=("tenant", "watchlist", "rule_type", "value"), name="surv_rule_value_uniq"),
        ),
        migrations.AddConstraint(
            model_name="alertdestination",
            constraint=models.UniqueConstraint(fields=("tenant", "name"), name="surv_dest_tenant_name_uniq"),
        ),
        migrations.AddConstraint(
            model_name="alertsubscription",
            constraint=models.UniqueConstraint(fields=("tenant", "watchlist", "destination"), name="surv_sub_watch_dest_uniq"),
        ),
        migrations.AddConstraint(
            model_name="watchlistobservation",
            constraint=models.UniqueConstraint(fields=("tenant", "rule", "publication"), name="surv_observe_rule_pub_uniq"),
        ),
        migrations.AddConstraint(
            model_name="watchlistobservedlegalevent",
            constraint=models.UniqueConstraint(fields=("tenant", "observation", "legal_event"), name="surv_observe_event_uniq"),
        ),
        migrations.AddConstraint(
            model_name="surveillancealert",
            constraint=models.UniqueConstraint(fields=("tenant", "watchlist", "fingerprint"), name="surv_alert_fingerprint_uniq"),
        ),
        migrations.AddConstraint(
            model_name="alertdelivery",
            constraint=models.UniqueConstraint(fields=("tenant", "alert", "destination"), name="surv_delivery_alert_dest_uniq"),
        ),
        migrations.AddConstraint(
            model_name="alertdeliveryattempt",
            constraint=models.UniqueConstraint(fields=("tenant", "delivery", "attempt_number"), name="surv_attempt_no_uniq"),
        ),
        migrations.AddIndex(
            model_name="surveillancewatchlist",
            index=models.Index(fields=["tenant", "is_active", "next_poll_at"], name="surv_watch_due_idx"),
        ),
        migrations.AddIndex(
            model_name="surveillancewatchrule",
            index=models.Index(fields=["tenant", "watchlist", "is_active"], name="surv_rule_active_idx"),
        ),
        migrations.AddIndex(
            model_name="alertdestination",
            index=models.Index(fields=["tenant", "channel", "is_active"], name="surv_dest_active_idx"),
        ),
        migrations.AddIndex(
            model_name="alertsubscription",
            index=models.Index(fields=["tenant", "watchlist", "is_active"], name="surv_sub_active_idx"),
        ),
        migrations.AddIndex(
            model_name="watchlistobservation",
            index=models.Index(fields=["tenant", "watchlist", "last_observed_at"], name="surv_observe_recent_idx"),
        ),
        migrations.AddIndex(
            model_name="watchlistobservedlegalevent",
            index=models.Index(fields=["tenant", "observation", "first_observed_at"], name="surv_obs_event_recent_idx"),
        ),
        migrations.AddIndex(
            model_name="surveillancealert",
            index=models.Index(fields=["tenant", "state", "-created_at"], name="surv_alert_state_idx"),
        ),
        migrations.AddIndex(
            model_name="surveillancealert",
            index=models.Index(fields=["tenant", "watchlist", "-created_at"], name="surv_alert_watch_idx"),
        ),
        migrations.AddIndex(
            model_name="alertdelivery",
            index=models.Index(fields=["tenant", "status", "next_attempt_at"], name="surv_delivery_due_idx"),
        ),
        migrations.AddIndex(
            model_name="alertdeliveryattempt",
            index=models.Index(fields=["tenant", "delivery", "started_at"], name="surv_attempt_recent_idx"),
        ),
    ]
