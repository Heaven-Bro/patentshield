"""Idempotent surveillance polling, outbound notification, and recovery jobs."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import requests
from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from patents.intelligence_models import PatentPublication
from patents.intelligence_services import (
    ExternalDisclosureDenied,
    SearchAuditUnavailable,
    refresh_publication_from_provider,
    search_and_persist_publications,
)
from tenants.models import Tenant

from patents.surveillance_models import (
    AlertDelivery,
    AlertDeliveryAttempt,
    AlertDestination,
    SurveillanceWatchRule,
    WatchlistObservation,
    SurveillanceWatchlist,
)
from patents.surveillance_services import (
    SurveillanceConfigurationError,
    UnsafeWebhookDestination,
    assert_active_member,
    observe_publication_for_rule,
    record_surveillance_audit,
    rule_matches_publication,
    validate_webhook_url,
    decrypt_webhook_secret,
)

logger = logging.getLogger(__name__)
POLL_STALE_AFTER = timedelta(minutes=45)
DELIVERY_STALE_AFTER = timedelta(minutes=15)
DEFAULT_MAX_DELIVERY_ATTEMPTS = 6
# Bounded round-robin refresh prevents a long-lived watchlist from ignoring
# previously observed records that no longer appear in a provider search page.
MAX_PREVIOUS_PUBLICATIONS_TO_REFRESH_PER_RULE = 20


class _DeliveryFailure(Exception):
    def __init__(self, code: str, *, status_code: int | None = None, retryable: bool) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(code)


def _safe_provider_list(rule: SurveillanceWatchRule) -> list[str]:
    providers = [str(item).strip().upper() for item in (rule.providers or [])]
    providers = list(dict.fromkeys(item for item in providers if item in {"USPTO", "EPO"}))
    return providers or ["USPTO", "EPO"]


def _mark_poll_failed(watchlist_id: str, code: str) -> None:
    now = timezone.now()
    SurveillanceWatchlist.objects.filter(pk=watchlist_id).update(
        poll_status=SurveillanceWatchlist.PollStatus.FAILED,
        last_polled_at=now,
        next_poll_at=now + timedelta(minutes=15),
        active_task_id="",
        last_error_code=code[:80],
        updated_at=now,
    )


@shared_task(
    bind=True,
    max_retries=0,
    queue="external_search",
    name="patents.surveillance_tasks.poll_watchlist",
)
def poll_watchlist(self, watchlist_id: str) -> dict[str, Any]:
    """Search one authorized watchlist, refresh provider details, and persist alerts.

    Search goes through the existing disclosure-policy and HMAC-audited service.
    Each returned publication is then optionally refreshed through the existing
    connector so newly available legal events and status details are persisted.
    """
    now = timezone.now()
    task_id = str(getattr(self.request, "id", "") or "")
    with transaction.atomic():
        watchlist = (
            SurveillanceWatchlist.objects.select_for_update()
            .select_related("tenant", "created_by")
            .filter(pk=watchlist_id)
            .first()
        )
        if watchlist is None:
            return {"status": "NOT_FOUND", "watchlist_id": watchlist_id}
        if not watchlist.is_active:
            return {"status": "INACTIVE", "watchlist_id": watchlist_id}
        if (
            watchlist.poll_status == SurveillanceWatchlist.PollStatus.RUNNING
            and watchlist.poll_started_at is not None
            and watchlist.poll_started_at >= now - POLL_STALE_AFTER
            and watchlist.active_task_id != task_id
        ):
            return {"status": "DUPLICATE_SKIPPED", "watchlist_id": watchlist_id}
        watchlist.poll_status = SurveillanceWatchlist.PollStatus.RUNNING
        watchlist.poll_started_at = now
        watchlist.active_task_id = task_id[:100]
        watchlist.last_error_code = ""
        watchlist.save(
            update_fields=(
                "poll_status",
                "poll_started_at",
                "active_task_id",
                "last_error_code",
                "updated_at",
            )
        )
        tenant_id = str(watchlist.tenant_id)
        actor_id = watchlist.created_by_id

    if not bool(getattr(settings, "PATENTSHIELD_SURVEILLANCE_ENABLED", False)):
        _mark_poll_failed(watchlist_id, "SURVEILLANCE_DISABLED")
        return {"status": "DISABLED", "watchlist_id": watchlist_id}

    try:
        membership = assert_active_member(tenant_id=tenant_id, user_id=actor_id)
    except PermissionError:
        _mark_poll_failed(watchlist_id, "OWNER_MEMBERSHIP_INACTIVE")
        return {"status": "DENIED", "watchlist_id": watchlist_id}

    found = 0
    alert_count = 0
    rule_errors: list[str] = []
    poll_started_at = now
    rules = list(
        SurveillanceWatchRule.objects.filter(
            tenant_id=tenant_id,
            watchlist_id=watchlist_id,
            watchlist__tenant_id=tenant_id,
            is_active=True,
        ).order_by("created_at")
    )

    if not rules:
        _mark_poll_failed(watchlist_id, "NO_ACTIVE_RULES")
        return {"status": "NO_ACTIVE_RULES", "watchlist_id": watchlist_id}

    for rule in rules:
        previous_observations = list(
            WatchlistObservation.objects.filter(
                tenant_id=tenant_id,
                watchlist_id=watchlist_id,
                rule_id=rule.pk,
                publication__provider__in=_safe_provider_list(rule),
            )
            .select_related("publication")
            .order_by("last_observed_at", "pk")[:MAX_PREVIOUS_PUBLICATIONS_TO_REFRESH_PER_RULE]
        )
        processed_publication_ids: set[str] = set()
        try:
            report = search_and_persist_publications(
                tenant=membership.tenant,
                actor=membership.user,
                query=rule.value,
                providers=_safe_provider_list(rule),
                max_results=min(
                    max(1, int(getattr(settings, "PATENTSHIELD_SURVEILLANCE_MAX_RESULTS", 10))),
                    25,
                ),
            )
        except ExternalDisclosureDenied:
            rule_errors.append("EXTERNAL_DISCLOSURE_DENIED")
            continue
        except SearchAuditUnavailable:
            rule_errors.append("SEARCH_AUDIT_UNAVAILABLE")
            continue
        except ValueError:
            rule_errors.append("INVALID_PROVIDER_QUERY")
            continue
        except Exception as exc:
            # Never log criteria or provider response content.
            logger.warning("Surveillance provider search failed (%s).", type(exc).__name__)
            rule_errors.append("PROVIDER_SEARCH_FAILED")
            continue

        provider_outcomes = [getattr(item, "status", "") for item in report.providers]
        if provider_outcomes and all(str(item).upper() == "FAILED" for item in provider_outcomes):
            rule_errors.append("ALL_PROVIDERS_FAILED")
        elif any(str(item).upper() == "FAILED" for item in provider_outcomes):
            rule_errors.append("PROVIDER_PARTIAL_FAILURE")

        for result in report.records:
            # Search response results are already persisted to canonical global models.
            publication = PatentPublication.objects.filter(pk=result.pk).first()
            if publication is None:
                continue
            processed_publication_ids.add(str(publication.pk))
            matches = rule_matches_publication(
                rule,
                publication,
                trust_provider_result=(
                    rule.rule_type == SurveillanceWatchRule.RuleType.SAVED_SEARCH
                ),
            )
            if not matches:
                continue

            # Refresh canonical legal events/status from the established adapter.
            # A rate/provider error does not discard the valid search result.
            try:
                refreshed, _events, error_code = refresh_publication_from_provider(publication)
                if refreshed is not None:
                    publication = refreshed
                elif error_code:
                    rule_errors.append(error_code[:80])
            except Exception as exc:
                logger.warning(
                    "Surveillance detail refresh failed (%s).", type(exc).__name__
                )
                rule_errors.append("DETAIL_REFRESH_FAILED")

            try:
                if not rule_matches_publication(
                    rule,
                    publication,
                    trust_provider_result=(
                        rule.rule_type == SurveillanceWatchRule.RuleType.SAVED_SEARCH
                    ),
                ):
                    # If the provider record changed out of the watch criteria during
                    # this refresh, retain it only when it was observed by the rule
                    # previously; this permits an auditable change-away event.
                    known = WatchlistObservation.objects.filter(
                        tenant_id=tenant_id,
                        watchlist_id=watchlist_id,
                        rule_id=rule.pk,
                        publication_id=publication.pk,
                    ).exists()
                    if not known:
                        continue
                alert_rows = observe_publication_for_rule(
                    watchlist=watchlist,
                    rule=rule,
                    publication=publication,
                )
                found += 1
                alert_count += len(alert_rows)
            except Exception as exc:
                logger.warning(
                    "Surveillance observation persistence failed (%s).",
                    type(exc).__name__,
                )
                rule_errors.append("OBSERVATION_PERSIST_FAILED")

        # Refresh a bounded, oldest-first slice of earlier observations as well
        # as current search results. This detects legal/status changes when a
        # competitor changes assignee metadata or a result falls out of the
        # provider's current search page. Observation timestamps make the slice
        # rotate fairly on subsequent polls.
        for previous in previous_observations:
            if str(previous.publication_id) in processed_publication_ids:
                continue
            publication = previous.publication
            try:
                refreshed, _events, error_code = refresh_publication_from_provider(publication)
                if refreshed is not None:
                    publication = refreshed
                elif error_code:
                    rule_errors.append(error_code[:80])
                alert_rows = observe_publication_for_rule(
                    watchlist=watchlist,
                    rule=rule,
                    publication=publication,
                )
                found += 1
                alert_count += len(alert_rows)
            except Exception as exc:
                logger.warning(
                    "Previously observed publication refresh failed (%s).",
                    type(exc).__name__,
                )
                rule_errors.append("OBSERVED_PUBLICATION_REFRESH_FAILED")

    end = timezone.now()
    status = (
        SurveillanceWatchlist.PollStatus.SUCCEEDED
        if not rule_errors
        else SurveillanceWatchlist.PollStatus.FAILED
    )
    error_code = ",".join(dict.fromkeys(rule_errors))[:80]
    SurveillanceWatchlist.objects.filter(
        pk=watchlist_id,
        tenant_id=tenant_id,
    ).update(
        poll_status=status,
        last_polled_at=end,
        next_poll_at=end + timedelta(minutes=max(15, watchlist.poll_interval_minutes)),
        active_task_id="",
        last_error_code=error_code,
        updated_at=end,
    )
    if not rule_errors:
        SurveillanceWatchlist.objects.filter(pk=watchlist_id, tenant_id=tenant_id).update(
            last_success_at=end
        )
    try:
        record_surveillance_audit(
            tenant=membership.tenant,
            actor=membership.user,
            action="SURVEILLANCE_WATCHLIST_POLLED",
            resource_type="SurveillanceWatchlist",
            resource_id=watchlist.pk,
            outcome=(
                "SUCCESS" if not rule_errors else "FAILURE"
            ),
            metadata={
                "rules_checked": len(rules),
                "publications_observed": found,
                "alerts_created": alert_count,
                "error_codes": list(dict.fromkeys(rule_errors))[:10],
                "duration_seconds": max(0, int((end - poll_started_at).total_seconds())),
            },
        )
    except Exception as exc:
        logger.error("Surveillance poll audit write failed (%s).", type(exc).__name__)
        rule_errors.append("POLL_AUDIT_WRITE_FAILED")
        error_code = ",".join(dict.fromkeys(rule_errors))[:80]
        SurveillanceWatchlist.objects.filter(pk=watchlist_id, tenant_id=tenant_id).update(
            poll_status=SurveillanceWatchlist.PollStatus.FAILED,
            last_error_code=error_code,
            updated_at=timezone.now(),
        )
        status = SurveillanceWatchlist.PollStatus.FAILED
    return {
        "status": status,
        "watchlist_id": watchlist_id,
        "rules_checked": len(rules),
        "publications_observed": found,
        "alerts_created": alert_count,
        "error_codes": list(dict.fromkeys(rule_errors))[:10],
    }


def _notification_text(delivery: AlertDelivery) -> str:
    """Build a public-source-only notification body, never a claim chart or secret."""
    alert = delivery.alert
    publication = alert.publication
    publication_id = publication.publication_number or publication.provider_record_id
    source_url = (
        alert.evidence.get("source_url")
        or (alert.legal_event.source_url if alert.legal_event_id else "")
        or publication.source_url
    )
    lines = [
        f"PatentShield surveillance alert: {alert.title}",
        f"Type: {alert.alert_type}",
        f"Watchlist: {alert.watchlist.name}",
        f"Publication: {publication_id}",
        f"Summary: {alert.summary}",
        f"Observed at: {alert.source_observed_at.isoformat() if alert.source_observed_at else alert.created_at.isoformat()}",
    ]
    if source_url:
        lines.append(f"Source: {source_url}")
    lines.append("This is source-monitoring information, not a legal conclusion.")
    return "\n".join(lines)[:8_000]


def _send_delivery(delivery: AlertDelivery) -> tuple[int | None, str]:
    destination = delivery.destination
    text = _notification_text(delivery)
    if destination.channel == AlertDestination.Channel.EMAIL:
        if not destination.email_address:
            raise _DeliveryFailure("EMAIL_DESTINATION_MISSING", retryable=False)
        try:
            sent = send_mail(
                subject=f"[PatentShield] {delivery.alert.title[:180]}",
                message=text,
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[destination.email_address],
                fail_silently=False,
            )
        except Exception as exc:
            logger.warning("Surveillance email delivery failed (%s).", type(exc).__name__)
            raise _DeliveryFailure("EMAIL_BACKEND_ERROR", retryable=True) from exc
        if sent < 1:
            raise _DeliveryFailure("EMAIL_NOT_ACCEPTED", retryable=True)
        return None, "Email accepted by configured mail backend."

    if destination.channel not in {
        AlertDestination.Channel.SLACK,
        AlertDestination.Channel.TEAMS,
    }:
        raise _DeliveryFailure("UNSUPPORTED_DESTINATION_CHANNEL", retryable=False)

    try:
        webhook_url = decrypt_webhook_secret(destination)
        validate_webhook_url(webhook_url, resolve_dns=True)
    except UnsafeWebhookDestination as exc:
        logger.warning("Surveillance webhook blocked by destination policy (%s).", type(exc).__name__)
        raise _DeliveryFailure("UNSAFE_WEBHOOK_DESTINATION", retryable=False) from exc
    except SurveillanceConfigurationError as exc:
        logger.warning("Surveillance webhook configuration error (%s).", type(exc).__name__)
        raise _DeliveryFailure("WEBHOOK_CONFIGURATION_ERROR", retryable=False) from exc

    if destination.channel == AlertDestination.Channel.SLACK:
        payload = {"text": text}
    else:
        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "https://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {"type": "TextBlock", "text": "PatentShield surveillance alert", "weight": "Bolder", "size": "Medium", "wrap": True},
                            {"type": "TextBlock", "text": delivery.alert.title[:300], "weight": "Bolder", "wrap": True},
                            {"type": "TextBlock", "text": text[:5_000], "wrap": True},
                        ],
                    },
                }
            ],
        }

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=(3.05, 8.0),
            allow_redirects=False,
            headers={"User-Agent": "PatentShield-Surveillance/1.0"},
        )
    except requests.RequestException as exc:
        logger.warning("Surveillance webhook request failed (%s).", type(exc).__name__)
        raise _DeliveryFailure("WEBHOOK_NETWORK_ERROR", retryable=True) from exc

    code = int(response.status_code)
    if 200 <= code < 300:
        return code, "Webhook returned a successful HTTP status."
    retryable = code in {408, 425, 429} or code >= 500
    raise _DeliveryFailure(f"WEBHOOK_HTTP_{code}", status_code=code, retryable=retryable)


@shared_task(
    bind=True,
    max_retries=0,
    queue="external_search",
    name="patents.surveillance_tasks.deliver_alert",
)
def deliver_alert(self, delivery_id: str) -> dict[str, Any]:
    """Perform one persistently tracked delivery attempt with bounded retry state."""
    now = timezone.now()
    attempt = None
    with transaction.atomic():
        delivery = (
            AlertDelivery.objects.select_for_update(of=("self",))
            .select_related(
                "tenant",
                "alert",
                "alert__watchlist",
                "alert__publication",
                "alert__legal_event",
                "destination",
            )
            .filter(pk=delivery_id)
            .first()
        )
        if delivery is None:
            return {"status": "NOT_FOUND", "delivery_id": delivery_id}
        if (
            delivery.tenant_id != delivery.alert.tenant_id
            or delivery.tenant_id != delivery.alert.watchlist.tenant_id
            or delivery.tenant_id != delivery.destination.tenant_id
        ):
            delivery.status = AlertDelivery.Status.DEAD_LETTER
            delivery.last_error_code = "TENANT_BOUNDARY_MISMATCH"
            delivery.save(update_fields=("status", "last_error_code", "updated_at"))
            return {"status": "DEAD_LETTER", "error_code": "TENANT_BOUNDARY_MISMATCH"}
        if delivery.tenant.status != Tenant.Status.ACTIVE:
            delivery.status = AlertDelivery.Status.CANCELLED
            delivery.last_error_code = "TENANT_INACTIVE"
            delivery.save(update_fields=("status", "last_error_code", "updated_at"))
            return {"status": delivery.status, "delivery_id": delivery_id}
        if delivery.status in {
            AlertDelivery.Status.DELIVERED,
            AlertDelivery.Status.DEAD_LETTER,
            AlertDelivery.Status.CANCELLED,
        }:
            return {"status": delivery.status, "delivery_id": delivery_id}
        if not delivery.destination.is_active:
            delivery.status = AlertDelivery.Status.CANCELLED
            delivery.last_error_code = "DESTINATION_INACTIVE"
            delivery.save(update_fields=("status", "last_error_code", "updated_at"))
            return {"status": delivery.status, "delivery_id": delivery_id}
        if (
            delivery.status == AlertDelivery.Status.SENDING
            and delivery.last_attempt_at is not None
            and delivery.last_attempt_at >= now - DELIVERY_STALE_AFTER
        ):
            return {"status": "DUPLICATE_SKIPPED", "delivery_id": delivery_id}

        delivery.attempt_count += 1
        delivery.status = AlertDelivery.Status.SENDING
        delivery.last_attempt_at = now
        delivery.next_attempt_at = None
        delivery.last_error_code = ""
        delivery.save(
            update_fields=(
                "attempt_count",
                "status",
                "last_attempt_at",
                "next_attempt_at",
                "last_error_code",
                "updated_at",
            )
        )
        attempt = AlertDeliveryAttempt.objects.create(
            tenant_id=delivery.tenant_id,
            delivery=delivery,
            attempt_number=delivery.attempt_count,
            outcome=AlertDeliveryAttempt.Outcome.STARTED,
            started_at=now,
        )

    response_status = None
    response_summary = ""
    error_code = ""
    failed = False
    retryable = False
    try:
        response_status, response_summary = _send_delivery(delivery)
    except _DeliveryFailure as exc:
        failed = True
        retryable = exc.retryable
        error_code = exc.code
        response_status = exc.status_code
        response_summary = "Delivery failed; inspect the sanitized error code and retry history."
    except Exception as exc:
        failed = True
        retryable = True
        error_code = "UNEXPECTED_DELIVERY_ERROR"
        response_summary = "Unexpected delivery adapter failure."
        logger.warning("Surveillance delivery adapter failed (%s).", type(exc).__name__)

    finished = timezone.now()
    max_attempts = max(
        1,
        min(12, int(getattr(settings, "PATENTSHIELD_SURVEILLANCE_MAX_DELIVERY_ATTEMPTS", DEFAULT_MAX_DELIVERY_ATTEMPTS))),
    )
    if not failed:
        final_status = AlertDelivery.Status.DELIVERED
        attempt_outcome = AlertDeliveryAttempt.Outcome.DELIVERED
        next_attempt_at = None
        error_code = ""
    elif retryable and delivery.attempt_count < max_attempts:
        final_status = AlertDelivery.Status.RETRYING
        attempt_outcome = AlertDeliveryAttempt.Outcome.RETRYING
        delay_seconds = min(60 * (2 ** max(0, delivery.attempt_count - 1)), 21_600)
        next_attempt_at = finished + timedelta(seconds=delay_seconds)
    else:
        final_status = AlertDelivery.Status.DEAD_LETTER
        attempt_outcome = AlertDeliveryAttempt.Outcome.DEAD_LETTER
        next_attempt_at = None

    with transaction.atomic():
        locked_delivery = AlertDelivery.objects.select_for_update().get(pk=delivery_id)
        locked_attempt = AlertDeliveryAttempt.objects.select_for_update().get(pk=attempt.pk)
        locked_delivery.status = final_status
        locked_delivery.last_http_status = response_status
        locked_delivery.last_error_code = error_code
        locked_delivery.next_attempt_at = next_attempt_at
        if final_status == AlertDelivery.Status.DELIVERED:
            locked_delivery.delivered_at = finished
        locked_delivery.save(
            update_fields=(
                "status",
                "last_http_status",
                "last_error_code",
                "next_attempt_at",
                "delivered_at",
                "updated_at",
            )
        )
        locked_attempt.outcome = attempt_outcome
        locked_attempt.finished_at = finished
        locked_attempt.response_status = response_status
        locked_attempt.error_code = error_code
        locked_attempt.response_summary = response_summary[:500]
        locked_attempt.save(
            update_fields=(
                "outcome",
                "finished_at",
                "response_status",
                "error_code",
                "response_summary",
            )
        )
        try:
            record_surveillance_audit(
                tenant=locked_delivery.tenant,
                actor=None,
                action="SURVEILLANCE_DELIVERY_ATTEMPT",
                resource_type="AlertDelivery",
                resource_id=locked_delivery.pk,
                outcome=(
                    "SUCCESS" if final_status == AlertDelivery.Status.DELIVERED else "FAILURE"
                ),
                metadata={
                    "channel": locked_delivery.destination.channel,
                    "attempt_number": locked_delivery.attempt_count,
                    "delivery_status": final_status,
                    "http_status": response_status,
                    "error_code": error_code,
                },
            )
        except Exception as exc:  # delivery state must remain recoverable if audit DB write fails
            logger.warning("Surveillance delivery audit write failed (%s).", type(exc).__name__)

    return {
        "status": final_status,
        "delivery_id": delivery_id,
        "attempt_number": delivery.attempt_count,
        "error_code": error_code,
        "next_attempt_at": next_attempt_at.isoformat() if next_attempt_at else None,
    }


@shared_task(
    bind=True,
    max_retries=0,
    queue="external_search",
    name="patents.surveillance_tasks.enqueue_due_watchlists",
)
def enqueue_due_watchlists(self, limit: int = 50) -> dict[str, int]:
    """Enqueue due watchlists with scheduler-side locking and bounded batches."""
    if not bool(getattr(settings, "PATENTSHIELD_SURVEILLANCE_ENABLED", False)):
        return {"enqueued": 0, "disabled": 1}
    now = timezone.now()
    safe_limit = max(1, min(200, int(limit)))
    enqueued_ids: list[str] = []
    with transaction.atomic():
        due = (
            SurveillanceWatchlist.objects.select_for_update(skip_locked=True)
            .filter(is_active=True)
            .filter(Q(next_poll_at__isnull=True) | Q(next_poll_at__lte=now))
            .exclude(
                poll_status=SurveillanceWatchlist.PollStatus.RUNNING,
                poll_started_at__gte=now - POLL_STALE_AFTER,
            )
            .order_by("next_poll_at", "created_at")[:safe_limit]
        )
        for watchlist in due:
            # Moving the due marker before dispatch limits duplicate beat enqueues.
            watchlist.next_poll_at = now + timedelta(
                minutes=max(15, watchlist.poll_interval_minutes)
            )
            watchlist.save(update_fields=("next_poll_at", "updated_at"))
            enqueued_ids.append(str(watchlist.pk))

    count = 0
    for item_id in enqueued_ids:
        try:
            poll_watchlist.apply_async(args=(item_id,), queue="external_search")
            count += 1
        except Exception as exc:
            # Make this row eligible again. Do not record private rule values.
            retry_at = timezone.now() + timedelta(minutes=5)
            SurveillanceWatchlist.objects.filter(pk=item_id).update(
                next_poll_at=retry_at,
                last_error_code="TASK_ENQUEUE_FAILED",
                updated_at=timezone.now(),
            )
            logger.warning("Watchlist task enqueue failed (%s).", type(exc).__name__)
    return {"enqueued": count, "disabled": 0}


@shared_task(
    bind=True,
    max_retries=0,
    queue="external_search",
    name="patents.surveillance_tasks.recover_pending_deliveries",
)
def recover_pending_deliveries(self, limit: int = 200) -> dict[str, int]:
    """Recover queued/retryable deliveries after broker or worker interruptions."""
    if not bool(getattr(settings, "PATENTSHIELD_SURVEILLANCE_ENABLED", False)):
        return {"enqueued": 0, "disabled": 1}
    now = timezone.now()
    safe_limit = max(1, min(500, int(limit)))
    candidates = AlertDelivery.objects.filter(
        Q(status=AlertDelivery.Status.QUEUED)
        | Q(status=AlertDelivery.Status.RETRYING, next_attempt_at__lte=now)
        | Q(
            status=AlertDelivery.Status.SENDING,
            last_attempt_at__lt=now - DELIVERY_STALE_AFTER,
        )
    ).order_by("created_at").values_list("pk", flat=True)[:safe_limit]
    enqueued = 0
    for delivery_id in candidates:
        try:
            deliver_alert.apply_async(args=(str(delivery_id),), queue="external_search")
            enqueued += 1
        except Exception as exc:
            logger.warning("Delivery recovery enqueue failed (%s).", type(exc).__name__)
    return {"enqueued": enqueued, "disabled": 0}
