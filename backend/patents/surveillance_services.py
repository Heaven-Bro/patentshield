"""Security-sensitive business services for PatentShield surveillance."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from patents.intelligence_models import (
    PatentLegalEvent,
    PatentPublication,
    PatentSourceSnapshot,
)
from patents.surveillance_models import (
    AlertDelivery,
    AlertDestination,
    AlertSubscription,
    SurveillanceAlert,
    SurveillanceWatchRule,
    SurveillanceWatchlist,
    WatchlistObservation,
    WatchlistObservedLegalEvent,
)
from tenants.models import Tenant, TenantMembership

logger = logging.getLogger(__name__)

ALLOWED_PROVIDERS = frozenset(PatentPublication.Provider.values)


class SurveillanceConfigurationError(RuntimeError):
    """Raised when a required notification configuration is absent or invalid."""


class UnsafeWebhookDestination(ValueError):
    """Raised when a webhook URL does not satisfy the outbound network policy."""


def _fernet() -> Fernet:
    key = str(
        getattr(settings, "PATENTSHIELD_SURVEILLANCE_ENCRYPTION_KEY", "") or ""
    ).strip()
    if not key:
        raise SurveillanceConfigurationError(
            "Webhook destinations are disabled until the surveillance encryption key is configured."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SurveillanceConfigurationError(
            "The surveillance encryption key is invalid."
        ) from exc


def encrypt_webhook_secret(value: str) -> str:
    """Encrypt a webhook URL without placing the plaintext in persistence/logs."""
    clean_value = value.strip()
    if not clean_value:
        raise ValidationError("A webhook URL is required for this channel.")
    return _fernet().encrypt(clean_value.encode("utf-8")).decode("ascii")


def decrypt_webhook_secret(destination: AlertDestination) -> str:
    """Decrypt a configured destination URL only in the delivery worker."""
    if not destination.webhook_secret_ciphertext:
        raise SurveillanceConfigurationError("Webhook destination is not configured.")
    try:
        return _fernet().decrypt(
            destination.webhook_secret_ciphertext.encode("ascii")
        ).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError) as exc:
        raise SurveillanceConfigurationError(
            "Webhook secret could not be decrypted with the configured key."
        ) from exc


def validate_webhook_url(value: str, *, resolve_dns: bool = False) -> str:
    """Validate a destination against strict HTTPS, host allowlist, and SSRF rules.

    Deployments must explicitly allow exact webhook hostnames. Redirects are
    disabled in the delivery adapter and every dispatch re-resolves DNS so a
    host which later resolves to a private or non-routable address is rejected.
    """
    candidate = str(value or "").strip()
    if any(ord(character) < 32 for character in candidate) or "\\" in candidate:
        raise UnsafeWebhookDestination("Webhook URL contains unsafe characters.")
    if len(candidate) > 2_000:
        raise UnsafeWebhookDestination("Webhook URL is too long.")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeWebhookDestination("Webhook URL has an invalid authority or port.") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise UnsafeWebhookDestination("Webhook URL must be an absolute HTTPS URL on port 443.")

    host = parsed.hostname.rstrip(".").casefold()
    allowed_setting = getattr(
        settings, "PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS", ()
    )
    if isinstance(allowed_setting, str):
        allowed_hosts = {
            item.strip().rstrip(".").casefold()
            for item in allowed_setting.split(",")
            if item.strip()
        }
    else:
        allowed_hosts = {
            str(item).strip().rstrip(".").casefold()
            for item in (allowed_setting or ())
            if str(item).strip()
        }
    if not allowed_hosts or host not in allowed_hosts:
        raise UnsafeWebhookDestination(
            "Webhook hostname is not in PATENTSHIELD_SURVEILLANCE_WEBHOOK_ALLOWED_HOSTS."
        )

    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        ip_literal = None
    if ip_literal is not None and not ip_literal.is_global:
        raise UnsafeWebhookDestination("Private or non-routable webhook IPs are not allowed.")

    if resolve_dns:
        try:
            resolved = socket.getaddrinfo(
                host,
                443,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise UnsafeWebhookDestination("Webhook hostname did not resolve safely.") from exc
        addresses = {item[4][0].split("%", 1)[0] for item in resolved}
        if not addresses:
            raise UnsafeWebhookDestination("Webhook hostname did not resolve safely.")
        for address in addresses:
            try:
                parsed_address = ipaddress.ip_address(address)
            except ValueError as exc:
                raise UnsafeWebhookDestination("Webhook DNS returned an invalid address.") from exc
            if not parsed_address.is_global:
                raise UnsafeWebhookDestination(
                    "Webhook DNS resolved to a private or non-routable address."
                )

    # Never echo a destination URL into errors or ordinary logs.
    return candidate


def validate_watch_rule_input(
    *, rule_type: str, value: str, match_mode: str, providers: list[str]
) -> tuple[str, list[str]]:
    """Normalize and strictly validate watch criteria before persistence."""
    if rule_type not in SurveillanceWatchRule.RuleType.values:
        raise ValidationError({"rule_type": "Unsupported watch rule type."})
    clean_value = " ".join(str(value or "").split())
    if not clean_value or len(clean_value) > 500:
        raise ValidationError({"value": "A non-empty value up to 500 characters is required."})
    if match_mode not in SurveillanceWatchRule.MatchMode.values:
        raise ValidationError({"match_mode": "Unsupported match mode."})
    if not isinstance(providers, (list, tuple, set)):
        raise ValidationError({"providers": "Providers must be a list of USPTO/EPO provider names."})
    clean_providers = list(dict.fromkeys(str(item).strip().upper() for item in providers))
    if not clean_providers:
        clean_providers = list(ALLOWED_PROVIDERS)
    if any(item not in ALLOWED_PROVIDERS for item in clean_providers):
        raise ValidationError({"providers": "Providers must be USPTO and/or EPO."})
    if rule_type in {
        SurveillanceWatchRule.RuleType.PUBLICATION,
        SurveillanceWatchRule.RuleType.FAMILY,
    }:
        # Identifier rules should not accidentally become free-form sensitive queries.
        if len(clean_value) < 3:
            raise ValidationError({"value": "Identifier values must contain at least 3 characters."})
    return clean_value, clean_providers


def record_surveillance_audit(
    *,
    tenant: Tenant,
    action: str,
    resource_type: str,
    resource_id,
    actor=None,
    outcome: str = AuditLog.Outcome.SUCCESS,
    metadata: dict | None = None,
) -> None:
    """Record a bounded, non-secret audit event using the established schema."""
    safe_metadata = dict(metadata or {})
    # Keep audit metadata small and prevent accidentally persisting secret-bearing fields.
    for prohibited in ("webhook_url", "webhook_secret", "secret", "query", "raw_payload"):
        safe_metadata.pop(prohibited, None)
    AuditLog.objects.create(
        tenant=tenant,
        # Background jobs legitimately have no interactive user actor. Assigning
        # the nullable FK itself preserves that system-actor audit event without
        # dereferencing None or silently dropping the audit write.
        actor_user=actor,
        action=action[:100],
        resource_type=resource_type[:100],
        resource_id=resource_id,
        outcome=outcome,
        metadata=safe_metadata,
    )


def _normalized(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def rule_matches_publication(
    rule: SurveillanceWatchRule,
    publication: PatentPublication,
    *,
    trust_provider_result: bool = False,
) -> bool:
    """Conservatively check whether a canonical public record matches a rule."""
    if not rule.is_active:
        return False
    if trust_provider_result and rule.rule_type == SurveillanceWatchRule.RuleType.SAVED_SEARCH:
        return True
    value = _normalized(rule.value)
    if not value:
        return False

    if rule.rule_type == SurveillanceWatchRule.RuleType.ASSIGNEE:
        haystack = [_normalized(item) for item in (publication.applicant_names or [])]
    elif rule.rule_type == SurveillanceWatchRule.RuleType.PUBLICATION:
        haystack = [
            _normalized(publication.publication_number),
            _normalized(publication.application_number),
            _normalized(publication.provider_record_id),
        ]
    elif rule.rule_type == SurveillanceWatchRule.RuleType.FAMILY:
        haystack = [
            _normalized(publication.family_identifier),
            _normalized(f"{publication.family_system}:{publication.family_identifier}"),
        ]
    elif rule.rule_type == SurveillanceWatchRule.RuleType.CLASSIFICATION:
        haystack = [
            *[_normalized(item) for item in (publication.ipc_codes or [])],
            *[_normalized(item) for item in (publication.cpc_codes or [])],
        ]
    elif rule.rule_type == SurveillanceWatchRule.RuleType.SAVED_SEARCH:
        haystack = [
            _normalized(publication.title),
            _normalized(publication.abstract),
            *[_normalized(item) for item in (publication.applicant_names or [])],
            *[_normalized(item) for item in (publication.ipc_codes or [])],
            *[_normalized(item) for item in (publication.cpc_codes or [])],
        ]
    else:
        return False

    if rule.match_mode == SurveillanceWatchRule.MatchMode.EXACT:
        return any(value == candidate for candidate in haystack if candidate)
    return any(value in candidate for candidate in haystack if candidate)


def _fingerprint(*parts: object) -> str:
    encoded = json.dumps(
        [str(part) for part in parts],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _latest_source_snapshot(publication: PatentPublication) -> PatentSourceSnapshot | None:
    return publication.source_snapshots.order_by("-fetched_at", "-id").first()


def emit_surveillance_alert(
    *,
    watchlist: SurveillanceWatchlist,
    rule: SurveillanceWatchRule | None,
    publication: PatentPublication,
    alert_type: str,
    fingerprint_parts: tuple[object, ...],
    title: str,
    summary: str,
    evidence: dict,
    source_snapshot: PatentSourceSnapshot | None = None,
    legal_event: PatentLegalEvent | None = None,
) -> tuple[SurveillanceAlert, bool]:
    """Atomically create a deduplicated alert and durable delivery rows."""
    if not watchlist.tenant_id or (rule and rule.tenant_id != watchlist.tenant_id):
        raise ValidationError("Watchlist and rule tenant scopes do not match.")
    if rule and rule.watchlist_id != watchlist.pk:
        raise ValidationError("Rule does not belong to this watchlist.")
    if legal_event and legal_event.publication_id != publication.pk:
        raise ValidationError("Legal event does not belong to this publication.")
    if source_snapshot and source_snapshot.publication_id != publication.pk:
        raise ValidationError("Source snapshot does not belong to this publication.")

    alert_fingerprint = _fingerprint(
        watchlist.pk,
        rule.pk if rule else "",
        alert_type,
        publication.pk,
        *fingerprint_parts,
    )
    safe_evidence = {
        key: value
        for key, value in (evidence or {}).items()
        if key not in {"raw_payload", "webhook_url", "secret", "query"}
    }
    with transaction.atomic():
        alert, created = SurveillanceAlert.objects.get_or_create(
            tenant_id=watchlist.tenant_id,
            watchlist=watchlist,
            fingerprint=alert_fingerprint,
            defaults={
                "rule": rule,
                "publication": publication,
                "source_snapshot": source_snapshot,
                "legal_event": legal_event,
                "alert_type": alert_type,
                "title": title[:300],
                "summary": summary[:10_000],
                "evidence": safe_evidence,
                "source_observed_at": timezone.now(),
            },
        )
        if not created:
            return alert, False

        subscriptions = AlertSubscription.objects.select_related("destination").filter(
            tenant_id=watchlist.tenant_id,
            watchlist=watchlist,
            is_active=True,
            destination__tenant_id=watchlist.tenant_id,
            destination__is_active=True,
        )
        delivery_ids: list[str] = []
        for subscription in subscriptions:
            if subscription.alert_types and alert_type not in subscription.alert_types:
                continue
            key = _fingerprint(alert.pk, subscription.destination_id)
            delivery, _ = AlertDelivery.objects.get_or_create(
                tenant_id=watchlist.tenant_id,
                alert=alert,
                destination=subscription.destination,
                defaults={
                    "idempotency_key": key,
                    "next_attempt_at": timezone.now(),
                },
            )
            delivery_ids.append(str(delivery.pk))

        record_surveillance_audit(
            tenant=watchlist.tenant,
            action="SURVEILLANCE_ALERT_CREATED",
            resource_type="SurveillanceAlert",
            resource_id=alert.pk,
            actor=watchlist.created_by,
            metadata={
                "alert_type": alert_type,
                "watchlist_id": str(watchlist.pk),
                "rule_id": str(rule.pk) if rule else None,
                "publication_id": str(publication.pk),
                "source_snapshot_id": str(source_snapshot.pk) if source_snapshot else None,
                "legal_event_id": str(legal_event.pk) if legal_event else None,
                "deliveries_created": len(delivery_ids),
            },
        )
        if delivery_ids:
            # The periodic recovery task is the durable fallback if Redis is down
            # during commit; a broker enqueue never represents successful delivery.
            def _enqueue_after_commit(ids: tuple[str, ...] = tuple(delivery_ids)) -> None:
                try:
                    from patents.surveillance_tasks import enqueue_alert_delivery

                    for delivery_id in ids:
                        enqueue_alert_delivery(delivery_id)
                except Exception as exc:  # pragma: no cover - broker outage path
                    logger.warning(
                        "Alert delivery enqueue deferred to recovery task (%s).",
                        type(exc).__name__,
                    )

            transaction.on_commit(_enqueue_after_commit)
        return alert, True


def _current_evidence(publication: PatentPublication, snapshot) -> dict:
    return {
        "provider": publication.provider,
        "provider_record_id": publication.provider_record_id,
        "publication_number": publication.publication_number,
        "application_number": publication.application_number,
        "jurisdiction": publication.jurisdiction,
        "source_url": publication.source_url,
        "source_snapshot_id": str(snapshot.pk) if snapshot else None,
        "source_snapshot_sha256": snapshot.payload_sha256 if snapshot else "",
        "observed_at": timezone.now().isoformat(),
    }


def observe_publication_for_rule(
    *,
    watchlist: SurveillanceWatchlist,
    rule: SurveillanceWatchRule,
    publication: PatentPublication,
) -> list[SurveillanceAlert]:
    """Record a rule/publication observation and emit only novel changes/events."""
    if rule.tenant_id != watchlist.tenant_id or rule.watchlist_id != watchlist.pk:
        raise ValidationError("Watchlist rule tenant/resource boundary mismatch.")
    latest_snapshot = _latest_source_snapshot(publication)
    generated: list[SurveillanceAlert] = []

    with transaction.atomic():
        observation = (
            WatchlistObservation.objects.select_for_update()
            .filter(
                tenant_id=watchlist.tenant_id,
                watchlist=watchlist,
                rule=rule,
                publication=publication,
            )
            .first()
        )
        first_observation = observation is None
        if first_observation:
            observation = WatchlistObservation.objects.create(
                tenant_id=watchlist.tenant_id,
                watchlist=watchlist,
                rule=rule,
                publication=publication,
                last_snapshot=latest_snapshot,
                last_status=publication.legal_status,
                last_family_system=publication.family_system,
                last_family_identifier=publication.family_identifier,
                last_title=str(publication.title or "")[:500],
            )
            alert, created = emit_surveillance_alert(
                watchlist=watchlist,
                rule=rule,
                publication=publication,
                alert_type=SurveillanceAlert.AlertType.PUBLICATION_MATCHED,
                fingerprint_parts=(
                    latest_snapshot.payload_sha256 if latest_snapshot else publication.source_payload_hash,
                ),
                title=f"Patent matched: {publication.publication_number or publication.provider_record_id}",
                summary=(
                    f"A public patent record matched a monitoring rule in the '{watchlist.name}' watchlist."
                ),
                evidence=_current_evidence(publication, latest_snapshot),
                source_snapshot=latest_snapshot,
            )
            if created:
                generated.append(alert)
        else:
            assert observation is not None
            old_snapshot_id = observation.last_snapshot_id
            old_status = observation.last_status
            old_family_system = observation.last_family_system
            old_family_identifier = observation.last_family_identifier
            old_title = observation.last_title
            snapshot_changed = bool(
                latest_snapshot
                and str(latest_snapshot.pk) != str(old_snapshot_id or "")
            )
            current_evidence = _current_evidence(publication, latest_snapshot)
            if old_status != publication.legal_status:
                alert, created = emit_surveillance_alert(
                    watchlist=watchlist,
                    rule=rule,
                    publication=publication,
                    alert_type=SurveillanceAlert.AlertType.STATUS_CHANGED,
                    fingerprint_parts=(
                        latest_snapshot.payload_sha256 if latest_snapshot else publication.source_payload_hash,
                        old_status,
                        publication.legal_status,
                    ),
                    title=f"Patent status changed: {publication.publication_number or publication.provider_record_id}",
                    summary=f"Legal status changed from {old_status or 'unknown'} to {publication.legal_status}.",
                    evidence={**current_evidence, "previous_status": old_status, "current_status": publication.legal_status},
                    source_snapshot=latest_snapshot,
                )
                if created:
                    generated.append(alert)

            old_family = f"{old_family_system}:{old_family_identifier}".strip(":")
            new_family = f"{publication.family_system}:{publication.family_identifier}".strip(":")
            if (old_family_system, old_family_identifier) != (
                publication.family_system,
                publication.family_identifier,
            ):
                alert, created = emit_surveillance_alert(
                    watchlist=watchlist,
                    rule=rule,
                    publication=publication,
                    alert_type=SurveillanceAlert.AlertType.FAMILY_CHANGED,
                    fingerprint_parts=(
                        latest_snapshot.payload_sha256 if latest_snapshot else publication.source_payload_hash,
                        old_family,
                        new_family,
                    ),
                    title=f"Patent family changed: {publication.publication_number or publication.provider_record_id}",
                    summary=f"Family mapping changed from '{old_family or 'not provided'}' to '{new_family or 'not provided'}'.",
                    evidence={**current_evidence, "previous_family": old_family, "current_family": new_family},
                    source_snapshot=latest_snapshot,
                )
                if created:
                    generated.append(alert)

            if snapshot_changed and old_title != str(publication.title or "")[:500]:
                alert, created = emit_surveillance_alert(
                    watchlist=watchlist,
                    rule=rule,
                    publication=publication,
                    alert_type=SurveillanceAlert.AlertType.PUBLICATION_UPDATED,
                    fingerprint_parts=(latest_snapshot.payload_sha256, old_title, str(publication.title or "")[:500]),
                    title=f"Patent record updated: {publication.publication_number or publication.provider_record_id}",
                    summary="The provider's publication metadata changed; review the cited source snapshot for details.",
                    evidence={**current_evidence, "previous_title": old_title, "current_title": str(publication.title or "")[:500]},
                    source_snapshot=latest_snapshot,
                )
                if created:
                    generated.append(alert)
            elif snapshot_changed and old_status == publication.legal_status and (old_family_system, old_family_identifier) == (publication.family_system, publication.family_identifier):
                # A source snapshot may change without a title/status/family change.
                alert, created = emit_surveillance_alert(
                    watchlist=watchlist,
                    rule=rule,
                    publication=publication,
                    alert_type=SurveillanceAlert.AlertType.PUBLICATION_UPDATED,
                    fingerprint_parts=(latest_snapshot.payload_sha256, "metadata"),
                    title=f"Patent source metadata updated: {publication.publication_number or publication.provider_record_id}",
                    summary="The provider published a new source snapshot. Review its provenance before relying on the changed metadata.",
                    evidence=current_evidence,
                    source_snapshot=latest_snapshot,
                )
                if created:
                    generated.append(alert)

            observation.last_snapshot = latest_snapshot
            observation.last_status = publication.legal_status
            observation.last_family_system = publication.family_system
            observation.last_family_identifier = publication.family_identifier
            observation.last_title = str(publication.title or "")[:500]
            observation.save(
                update_fields=(
                    "last_snapshot",
                    "last_status",
                    "last_family_system",
                    "last_family_identifier",
                    "last_title",
                    "last_observed_at",
                )
            )

        assert observation is not None
        legal_events = PatentLegalEvent.objects.filter(publication=publication).order_by(
            "event_date", "observed_at", "pk"
        )
        for legal_event in legal_events:
            observed_event, created = WatchlistObservedLegalEvent.objects.get_or_create(
                tenant_id=watchlist.tenant_id,
                observation=observation,
                legal_event=legal_event,
            )
            if not created or first_observation:
                continue
            alert, alert_created = emit_surveillance_alert(
                watchlist=watchlist,
                rule=rule,
                publication=publication,
                alert_type=SurveillanceAlert.AlertType.LEGAL_EVENT,
                fingerprint_parts=(legal_event.event_fingerprint,),
                title=f"New legal event: {legal_event.title[:240]}",
                summary=(
                    f"A new provider legal event ({legal_event.event_code}) was observed for "
                    f"{publication.publication_number or publication.provider_record_id}."
                ),
                evidence={
                    **_current_evidence(publication, latest_snapshot),
                    "legal_event_id": str(legal_event.pk),
                    "event_fingerprint": legal_event.event_fingerprint,
                    "event_code": legal_event.event_code,
                    "event_date": legal_event.event_date.isoformat() if legal_event.event_date else None,
                    "event_title": legal_event.title,
                    "legal_status_after": legal_event.legal_status_after,
                    "source_url": legal_event.source_url or publication.source_url,
                },
                source_snapshot=latest_snapshot,
                legal_event=legal_event,
            )
            if alert_created:
                generated.append(alert)
    return generated


def assert_active_member(*, tenant_id, user_id) -> TenantMembership:
    """Revalidate tenant state/membership at the asynchronous job boundary."""
    membership = (
        TenantMembership.objects.select_related("tenant", "user")
        .filter(
            tenant_id=tenant_id,
            user_id=user_id,
            status=TenantMembership.Status.ACTIVE,
            tenant__status=Tenant.Status.ACTIVE,
        )
        .first()
    )
    if membership is None:
        raise PermissionError("The watchlist owner no longer has active tenant access.")
    if membership.role not in {
        TenantMembership.Role.OWNER,
        TenantMembership.Role.ADMIN,
        TenantMembership.Role.ATTORNEY,
        TenantMembership.Role.ANALYST,
    }:
        raise PermissionError("The watchlist owner no longer has a surveillance-enabled role.")
    return membership
