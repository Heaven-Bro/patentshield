"""Phase 3 surveillance regression, tenant-boundary, and delivery tests."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, Mock, patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from patents.intelligence_models import (
    PatentLegalEvent,
    PatentPublication,
    PatentSourceSnapshot,
)
from patents.surveillance_models import (
    AlertDelivery,
    AlertDeliveryAttempt,
    AlertDestination,
    SurveillanceAlert,
    SurveillanceWatchRule,
    SurveillanceWatchlist,
    WatchlistObservation,
)
from patents.surveillance_services import (
    UnsafeWebhookDestination,
    encrypt_webhook_secret,
    observe_publication_for_rule,
    validate_webhook_url,
)
from tenants.models import Tenant, TenantMembership


class SurveillanceTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="surveillance-owner", password="test-password")
        self.tenant = Tenant.objects.create(name="Acme IP", slug="acme-ip")
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.OWNER,
            status=TenantMembership.Status.ACTIVE,
        )
        self.other_tenant = Tenant.objects.create(name="Other IP", slug="other-ip")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_X_TENANT_ID": str(self.tenant.pk)}
        self.publication = PatentPublication.objects.create(
            provider=PatentPublication.Provider.USPTO,
            provider_record_id="17999999",
            publication_number="US20240123456A1",
            application_number="17999999",
            jurisdiction="US",
            title="Optical connector system",
            abstract="A public optical connector publication.",
            publication_date=date(2024, 7, 11),
            legal_status=PatentPublication.LegalStatus.PENDING,
            applicant_names=["Example Photonics Inc."],
            ipc_codes=["G02B6/00"],
            family_system="DOCDB",
            family_identifier="FAM-123",
            source_url="https://patents.example.org/publication/US20240123456A1",
        )
        self.snapshot = PatentSourceSnapshot.objects.create(
            publication=self.publication,
            provider=PatentPublication.Provider.USPTO,
            source_record_id=self.publication.provider_record_id,
            payload_sha256="a" * 64,
            source_url=self.publication.source_url,
            payload={
                "title": self.publication.title,
                "legal_status": self.publication.legal_status,
                "family_identifier": self.publication.family_identifier,
                "legal_events": [],
            },
        )
        self.watchlist = SurveillanceWatchlist.objects.create(
            tenant=self.tenant,
            name="Photonics competitors",
            created_by=self.user,
            next_poll_at=timezone.now(),
        )
        self.rule = SurveillanceWatchRule.objects.create(
            tenant=self.tenant,
            watchlist=self.watchlist,
            rule_type=SurveillanceWatchRule.RuleType.ASSIGNEE,
            value="Example Photonics",
            match_mode=SurveillanceWatchRule.MatchMode.CONTAINS,
            providers=["USPTO"],
        )

    def _legal_event(self, fingerprint: str, code: str = "PG25"):
        return PatentLegalEvent.objects.create(
            publication=self.publication,
            provider=PatentPublication.Provider.USPTO,
            source_event_id=fingerprint[:20],
            event_fingerprint=fingerprint * 64 if len(fingerprint) == 1 else fingerprint,
            event_code=code,
            event_date=date(2025, 1, 2),
            title="Patent granted",
            legal_status_after=PatentPublication.LegalStatus.GRANTED,
            source_url=self.publication.source_url,
        )

    def test_watchlist_api_is_scoped_to_selected_active_tenant(self):
        own = self.client.get("/api/v1/patents/surveillance/watchlists/", **self.headers)
        other = self.client.get(
            "/api/v1/patents/surveillance/watchlists/",
            HTTP_X_TENANT_ID=str(self.other_tenant.pk),
        )
        self.assertEqual(own.status_code, 200)
        self.assertEqual(len(own.data["results"]), 1)
        self.assertEqual(other.status_code, 403)

    def test_alert_history_is_denied_for_a_nonmember_tenant(self):
        observe_publication_for_rule(
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
        )
        response = self.client.get(
            "/api/v1/patents/surveillance/alerts/?limit=100",
            HTTP_X_TENANT_ID=str(self.other_tenant.pk),
        )
        self.assertEqual(response.status_code, 403)

    def test_watchlist_with_history_cannot_be_deleted_cascading_alert_evidence(self):
        observe_publication_for_rule(
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
        )
        response = self.client.delete(
            f"/api/v1/patents/surveillance/watchlists/{self.watchlist.pk}/",
            **self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(SurveillanceWatchlist.objects.filter(pk=self.watchlist.pk).exists())
        self.assertTrue(SurveillanceAlert.objects.filter(watchlist=self.watchlist).exists())

    def test_rule_input_is_validated_and_created_under_request_tenant(self):
        response = self.client.post(
            f"/api/v1/patents/surveillance/watchlists/{self.watchlist.pk}/rules/",
            {
                "rule_type": "CLASSIFICATION",
                "value": "G02B6/00",
                "match_mode": "CONTAINS",
                "providers": ["USPTO"],
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        created = SurveillanceWatchRule.objects.get(pk=response.data["id"])
        self.assertEqual(created.tenant_id, self.tenant.pk)
        self.assertEqual(created.watchlist_id, self.watchlist.pk)

    def test_model_rejects_cross_tenant_watchlist_rule_relationship(self):
        bad = SurveillanceWatchRule(
            tenant=self.other_tenant,
            watchlist=self.watchlist,
            rule_type=SurveillanceWatchRule.RuleType.ASSIGNEE,
            value="Other Corp",
        )
        with self.assertRaises(ValidationError):
            bad.save()

    def test_first_observation_deduplicates_and_seeds_historical_legal_events(self):
        self._legal_event("b")
        first = observe_publication_for_rule(
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
        )
        second = observe_publication_for_rule(
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
        )
        self.assertEqual([item.alert_type for item in first], [SurveillanceAlert.AlertType.PUBLICATION_MATCHED])
        self.assertEqual(second, [])
        observation = WatchlistObservation.objects.get(
            tenant=self.tenant,
            rule=self.rule,
            publication=self.publication,
        )
        self.assertEqual(observation.observed_legal_events.count(), 1)

    def test_new_legal_event_after_first_observation_emits_one_alert(self):
        observe_publication_for_rule(
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
        )
        event = self._legal_event("c", code="MAINTENANCE_FEE")
        fresh = observe_publication_for_rule(
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
        )
        self.assertEqual(sum(item.alert_type == SurveillanceAlert.AlertType.LEGAL_EVENT for item in fresh), 1)
        self.assertTrue(
            SurveillanceAlert.objects.filter(
                tenant=self.tenant,
                watchlist=self.watchlist,
                legal_event=event,
                alert_type=SurveillanceAlert.AlertType.LEGAL_EVENT,
            ).exists()
        )

    def test_status_change_creates_source_linked_alert(self):
        observe_publication_for_rule(
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
        )
        self.publication.legal_status = PatentPublication.LegalStatus.GRANTED
        self.publication.save(update_fields=("legal_status", "updated_at"))
        new_snapshot = PatentSourceSnapshot.objects.create(
            publication=self.publication,
            provider=PatentPublication.Provider.USPTO,
            source_record_id=self.publication.provider_record_id,
            payload_sha256="d" * 64,
            source_url=self.publication.source_url,
            payload={
                "title": self.publication.title,
                "legal_status": self.publication.legal_status,
                "family_identifier": self.publication.family_identifier,
                "legal_events": [],
            },
        )
        generated = observe_publication_for_rule(
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
        )
        status_alerts = [item for item in generated if item.alert_type == SurveillanceAlert.AlertType.STATUS_CHANGED]
        self.assertEqual(len(status_alerts), 1)
        self.assertEqual(status_alerts[0].source_snapshot_id, new_snapshot.pk)
        self.assertEqual(status_alerts[0].evidence["previous_status"], "PENDING")
        self.assertEqual(status_alerts[0].evidence["current_status"], "GRANTED")

    @override_settings(PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS=["hooks.slack.com"])
    def test_webhook_policy_rejects_http_unlisted_and_private_targets(self):
        with self.assertRaises(UnsafeWebhookDestination):
            validate_webhook_url("http://hooks.slack.com/services/test")
        with self.assertRaises(UnsafeWebhookDestination):
            validate_webhook_url("https://example.org/webhook")
        with self.assertRaises(UnsafeWebhookDestination):
            validate_webhook_url("https://127.0.0.1/webhook")

    @override_settings(PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS=[])
    def test_webhook_policy_rejects_empty_allowlist(self):
        with self.assertRaises(UnsafeWebhookDestination):
            validate_webhook_url("https://hooks.slack.com/services/test")

    @override_settings(PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS=["hooks.example.org"])
    @patch("patents.surveillance_services.socket.getaddrinfo")
    def test_webhook_policy_rejects_dns_resolving_to_private_ip(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with self.assertRaises(UnsafeWebhookDestination):
            validate_webhook_url("https://hooks.example.org/alerts", resolve_dns=True)

    def test_email_destination_api_accepts_an_empty_webhook_field(self):
        # The dashboard submits a single destination form containing both fields;
        # an unused blank webhook value must be accepted for email destinations.
        response = self.client.post(
            "/api/v1/patents/surveillance/destinations/",
            {
                "name": "Email legal alerts",
                "channel": "EMAIL",
                "email_address": "alerts@example.org",
                "webhook_url": "",
                "is_active": True,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["channel"], "EMAIL")
        self.assertEqual(response.data["email_address"], "alerts@example.org")

    @override_settings(
        PATENTSHIELD_SURVEILLANCE_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
        PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS=["hooks.slack.com"],
    )
    def test_webhook_destination_api_never_returns_the_plaintext_url(self):
        secret_url = "https://hooks.slack.com/services/test/secret/token"
        response = self.client.post(
            "/api/v1/patents/surveillance/destinations/",
            {
                "name": "Slack legal alerts",
                "channel": "SLACK",
                "webhook_url": secret_url,
                "is_active": True,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertNotIn("webhook_url", response.data)
        self.assertNotIn("webhook_secret_ciphertext", response.data)
        self.assertNotIn(secret_url, str(response.data))
        destination = AlertDestination.objects.get(pk=response.data["id"])
        self.assertNotEqual(destination.webhook_secret_ciphertext, secret_url)
        self.assertTrue(destination.has_webhook_secret)

    @override_settings(
        PATENTSHIELD_SURVEILLANCE_ENABLED=True,
        PATENTSHIELD_SURVEILLANCE_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
        PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS=["hooks.slack.com"],
    )
    def test_delivery_records_attempt_and_marks_success_only_after_http_2xx(self):
        from patents.surveillance_tasks import deliver_alert

        destination = AlertDestination.objects.create(
            tenant=self.tenant,
            name="Slack alerts",
            channel=AlertDestination.Channel.SLACK,
            webhook_secret_ciphertext=encrypt_webhook_secret(
                "https://hooks.slack.com/services/test/secret/token"
            ),
            created_by=self.user,
        )
        alert = SurveillanceAlert.objects.create(
            tenant=self.tenant,
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
            source_snapshot=self.snapshot,
            alert_type=SurveillanceAlert.AlertType.PUBLICATION_MATCHED,
            fingerprint="f" * 64,
            title="Patent matched",
            summary="Public provider record matched.",
            evidence={"source_url": self.publication.source_url},
        )
        delivery = AlertDelivery.objects.create(
            tenant=self.tenant,
            alert=alert,
            destination=destination,
            idempotency_key="e" * 64,
            next_attempt_at=timezone.now(),
        )
        response = Mock(status_code=200)
        session = Mock()
        session.post.return_value = response
        session_context = MagicMock()
        session_context.__enter__.return_value = session
        session_context.__exit__.return_value = False
        with patch("patents.surveillance_tasks.validate_webhook_url", return_value="https://hooks.slack.com/services/test/secret/token"), patch(
            "patents.surveillance_tasks.requests.Session", return_value=session_context
        ) as session_factory:
            result = deliver_alert.apply(args=(str(delivery.pk),), throw=True).get()
            duplicate_result = deliver_alert.apply(args=(str(delivery.pk),), throw=True).get()
        delivery.refresh_from_db()
        self.assertEqual(result["status"], AlertDelivery.Status.DELIVERED)
        self.assertEqual(delivery.status, AlertDelivery.Status.DELIVERED)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.last_http_status, 200)
        self.assertEqual(AlertDeliveryAttempt.objects.filter(delivery=delivery).count(), 1)
        self.assertEqual(duplicate_result["status"], AlertDelivery.Status.DELIVERED)
        self.assertEqual(session.post.call_count, 1)
        self.assertFalse(session.trust_env)
        self.assertNotIn("secret/token", str(session.post.call_args.kwargs.get("json", {})))

    @override_settings(
        PATENTSHIELD_SURVEILLANCE_ENABLED=True,
        PATENTSHIELD_SURVEILLANCE_ENCRYPTION_KEY=Fernet.generate_key().decode("ascii"),
        PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS=["hooks.slack.com"],
        PATENTSHIELD_SURVEILLANCE_MAX_DELIVERY_ATTEMPTS=6,
    )
    def test_retryable_webhook_failure_is_persisted_with_backoff(self):
        from patents.surveillance_tasks import deliver_alert

        destination = AlertDestination.objects.create(
            tenant=self.tenant,
            name="Slack retry alerts",
            channel=AlertDestination.Channel.SLACK,
            webhook_secret_ciphertext=encrypt_webhook_secret(
                "https://hooks.slack.com/services/test/secret/token"
            ),
            created_by=self.user,
        )
        alert = SurveillanceAlert.objects.create(
            tenant=self.tenant,
            watchlist=self.watchlist,
            rule=self.rule,
            publication=self.publication,
            source_snapshot=self.snapshot,
            alert_type=SurveillanceAlert.AlertType.PUBLICATION_MATCHED,
            fingerprint="a" * 64,
            title="Patent matched",
            summary="Public provider record matched.",
            evidence={"source_url": self.publication.source_url},
        )
        delivery = AlertDelivery.objects.create(
            tenant=self.tenant,
            alert=alert,
            destination=destination,
            idempotency_key="b" * 64,
            next_attempt_at=timezone.now(),
        )
        response = Mock(status_code=503)
        session = Mock()
        session.post.return_value = response
        session_context = MagicMock()
        session_context.__enter__.return_value = session
        session_context.__exit__.return_value = False

        with patch("patents.surveillance_tasks.validate_webhook_url", return_value="https://hooks.slack.com/services/test/secret/token"), patch(
            "patents.surveillance_tasks.requests.Session", return_value=session_context
        ):
            result = deliver_alert.apply(args=(str(delivery.pk),), throw=True).get()

        delivery.refresh_from_db()
        attempt = AlertDeliveryAttempt.objects.get(delivery=delivery)
        self.assertEqual(result["status"], AlertDelivery.Status.RETRYING)
        self.assertEqual(delivery.last_http_status, 503)
        self.assertIsNotNone(delivery.next_attempt_at)
        self.assertEqual(attempt.outcome, AlertDeliveryAttempt.Outcome.RETRYING)
        self.assertEqual(attempt.error_code, "WEBHOOK_HTTP_503")

    def test_destination_ciphertext_is_not_exposed_by_model_repr(self):
        destination = AlertDestination.objects.create(
            tenant=self.tenant,
            name="mail",
            channel=AlertDestination.Channel.EMAIL,
            email_address="alerts@example.org",
            created_by=self.user,
        )
        self.assertNotIn("webhook_secret_ciphertext", str(destination))