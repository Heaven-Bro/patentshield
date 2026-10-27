"""Search, provenance persistence, and disclosure controls for patent connectors."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from patents.connectors.base import (
    NormalizedLegalEvent,
    NormalizedPublication,
    PatentConnectorError,
    normalize_query,
    validate_limit,
)
from patents.connectors.epo import EPOOPSConnector
from patents.connectors.uspto import USPTOODPConnector
from patents.intelligence_models import (
    ConnectorSyncCursor,
    PatentFamily,
    PatentFamilyMember,
    PatentLegalEvent,
    PatentPublication,
    PatentSourceSnapshot,
)
from tenants.models import Tenant, TenantMembership

logger = logging.getLogger(__name__)

PROVIDER_NAMES = ("USPTO", "EPO")
CONNECTOR_TIMEOUT_SECONDS = 40


class ExternalDisclosureDenied(PermissionError):
    """Raised when tenant policy does not authorize external query disclosure."""


class SearchAuditUnavailable(RuntimeError):
    """Raised when the query disclosure audit event cannot be written."""


@dataclass(frozen=True, slots=True)
class ProviderSearchStatus:
    """Safe provider status returned to the UI without secrets or raw errors."""

    provider: str
    status: str
    result_count: int
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class PatentSearchReport:
    """Canonical search result set with per-provider operational status."""

    records: list[PatentPublication]
    providers: list[ProviderSearchStatus]
    query_hmac: str
    audit_event_id: str


def get_connector(provider: str) -> Any:
    """Return the configured provider adapter, rejecting unknown providers."""
    normalized = provider.strip().upper()
    if normalized == "USPTO":
        return USPTOODPConnector()
    if normalized == "EPO":
        return EPOOPSConnector()
    raise ValueError("Unsupported patent data provider.")


def _require_query_disclosure(tenant: Tenant) -> None:
    """Fail closed until global and tenant-level outbound disclosure is allowed."""
    if not bool(getattr(settings, "PATENTSHIELD_EXTERNAL_SEARCH_ENABLED", False)):
        raise ExternalDisclosureDenied("External patent search is disabled by policy.")
    if not bool(
        getattr(settings, "PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE", False)
    ):
        raise ExternalDisclosureDenied(
            "External query disclosure is not authorized by deployment policy."
        )
    if tenant.status != Tenant.Status.ACTIVE:
        raise ExternalDisclosureDenied("An active tenant is required.")
    if bool(
        getattr(settings, "PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_ALL_TENANTS", False)
    ):
        return
    allowed = getattr(settings, "PATENTSHIELD_EXTERNAL_SEARCH_ALLOWED_TENANTS", ())
    if isinstance(allowed, str):
        allowed = [part.strip() for part in allowed.split(",") if part.strip()]
    authorized_ids = {str(value).strip() for value in allowed or ()}
    if str(tenant.pk) not in authorized_ids:
        raise ExternalDisclosureDenied(
            "This tenant is not authorized to disclose queries externally."
        )


def _require_actor_membership(tenant: Tenant, actor: Any) -> None:
    """Revalidate disclosure authorization at the service boundary.

    API permissions are the first control, but this service may also be called
    from management commands or another internal entry point. Require an
    authenticated actor with an active, authorized tenant membership here too.
    """
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise ExternalDisclosureDenied("An authenticated tenant actor is required.")
    allowed_roles = (
        TenantMembership.Role.OWNER,
        TenantMembership.Role.ADMIN,
        TenantMembership.Role.ATTORNEY,
        TenantMembership.Role.ANALYST,
    )
    authorized = TenantMembership.objects.filter(
        tenant=tenant,
        tenant__status=Tenant.Status.ACTIVE,
        user=actor,
        status=TenantMembership.Status.ACTIVE,
        role__in=allowed_roles,
    ).exists()
    if not authorized:
        raise ExternalDisclosureDenied(
            "The actor does not have an active role that permits external search."
        )


def _query_hmac(query: str, tenant: Tenant) -> str:
    key = str(
        getattr(settings, "PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY", "") or ""
    ).strip()
    if len(key) < 32 or key == str(getattr(settings, "SECRET_KEY", "")):
        raise ExternalDisclosureDenied(
            "An independent query HMAC key must be configured."
        )
    scope = f"{tenant.pk}\0{normalize_query(query).casefold()}"
    return hmac.new(
        key.encode("utf-8"), scope.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _snapshot_payload(record: NormalizedPublication) -> dict[str, Any]:
    """Retain selected source fields, not the full provider response envelope."""
    return {
        "provider": record.provider,
        "provider_record_id": record.provider_record_id,
        "publication_number": record.publication_number,
        "application_number": record.application_number,
        "jurisdiction": record.jurisdiction,
        "kind_code": record.kind_code,
        "title": record.title,
        "abstract": record.abstract,
        "filing_date": record.filing_date.isoformat() if record.filing_date else None,
        "publication_date": (
            record.publication_date.isoformat() if record.publication_date else None
        ),
        "grant_date": record.grant_date.isoformat() if record.grant_date else None,
        "legal_status": record.legal_status,
        "applicant_names": record.applicant_names,
        "inventor_names": record.inventor_names,
        "ipc_codes": record.ipc_codes,
        "cpc_codes": record.cpc_codes,
        "family_system": record.family_system,
        "family_identifier": record.family_identifier,
        "source_url": record.source_url,
        "source_updated_at": (
            record.source_updated_at.isoformat() if record.source_updated_at else None
        ),
        "provider_metadata": record.source_payload,
        "legal_events": [
            {
                "source_event_id": event.source_event_id,
                "event_code": event.event_code,
                "event_date": (
                    event.event_date.isoformat() if event.event_date else None
                ),
                "title": event.title,
                "description": event.description,
                "legal_status_after": event.legal_status_after,
                "source_url": event.source_url,
                "source_payload": event.source_payload,
            }
            for event in record.legal_events
        ],
    }


@transaction.atomic
def persist_publication(
    record: NormalizedPublication,
) -> tuple[PatentPublication, bool]:
    """Upsert a normalized publication and append immutable snapshot provenance."""
    if record.provider not in PROVIDER_NAMES:
        raise ValueError("Unsupported provider.")
    provider_record_id = record.provider_record_id.strip()
    title = record.title.strip()
    if not provider_record_id or not title:
        raise ValueError("Provider record ID and title are required.")
    if len(provider_record_id) > 200:
        raise ValueError("Provider record ID is too long.")

    snapshot_payload = _snapshot_payload(record)
    snapshot_bytes = json.dumps(
        snapshot_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    snapshot_hash = hashlib.sha256(snapshot_bytes).hexdigest()
    existing = PatentPublication.objects.filter(
        provider=record.provider,
        provider_record_id=provider_record_id,
    ).first()

    # Provider search responses are sometimes less complete than detail
    # responses. Preserve known non-empty metadata when a new payload omits it.
    legal_status = (
        record.legal_status
        if record.legal_status in PatentPublication.LegalStatus.values
        else PatentPublication.LegalStatus.UNKNOWN
    )
    if (
        legal_status == PatentPublication.LegalStatus.UNKNOWN
        and existing is not None
    ):
        legal_status = existing.legal_status

    defaults = {
        "publication_number": (
            (record.publication_number or "").strip()
            or (existing.publication_number if existing else None)
        ),
        "application_number": (
            (record.application_number or "").strip()
            or (existing.application_number if existing else None)
        ),
        "jurisdiction": (
            record.jurisdiction or (existing.jurisdiction if existing else "XX")
        ).strip().upper()[:10],
        "kind_code": (
            (record.kind_code or "").strip()
            or (existing.kind_code if existing else None)
        ),
        "title": title[:4_000],
        "abstract": (
            (record.abstract or "")[:20_000]
            or (existing.abstract if existing else None)
        ),
        "filing_date": record.filing_date
        or (existing.filing_date if existing else None),
        "publication_date": (
            record.publication_date
            or (existing.publication_date if existing else None)
        ),
        "grant_date": record.grant_date or (existing.grant_date if existing else None),
        "legal_status": legal_status,
        "applicant_names": record.applicant_names or (
            existing.applicant_names if existing else []
        ),
        "inventor_names": record.inventor_names or (
            existing.inventor_names if existing else []
        ),
        "ipc_codes": record.ipc_codes or (existing.ipc_codes if existing else []),
        "cpc_codes": record.cpc_codes or (existing.cpc_codes if existing else []),
        "family_system": record.family_system or (
            existing.family_system if existing else ""
        ),
        "family_identifier": record.family_identifier or (
            existing.family_identifier if existing else ""
        ),
        "source_url": record.source_url or (existing.source_url if existing else ""),
        "source_updated_at": record.source_updated_at or (
            existing.source_updated_at if existing else None
        ),
        # Hash the provider-derived normalized payload separately from the
        # broader canonical snapshot (which also includes normalization fields).
        "source_payload_hash": record.payload_sha256,
        "last_synced_at": timezone.now(),
        "raw_metadata": {
            "normalized_source_payload_sha256": record.payload_sha256,
            "normalized_snapshot_sha256": snapshot_hash,
            "source_updated_at": (
                record.source_updated_at.isoformat()
                if record.source_updated_at
                else None
            ),
        },
    }
    publication, created = PatentPublication.objects.update_or_create(
        provider=record.provider,
        provider_record_id=provider_record_id,
        defaults=defaults,
    )

    PatentSourceSnapshot.objects.get_or_create(
        publication=publication,
        payload_sha256=snapshot_hash,
        defaults={
            "provider": record.provider,
            "source_record_id": provider_record_id,
            "source_url": record.source_url or "",
            "source_license": (
                "USPTO ODP terms apply"
                if record.provider == "USPTO"
                else "EPO OPS terms and fair-use charter apply"
            ),
            "payload": snapshot_payload,
        },
    )

    if record.family_system and record.family_identifier:
        family, _ = PatentFamily.objects.update_or_create(
            family_system=record.family_system[:20],
            family_identifier=record.family_identifier[:200],
            defaults={
                "display_name": record.family_identifier[:300],
                "source_url": record.source_url or "",
                "raw_metadata": {
                    "provider": record.provider,
                    "provider_record_id": provider_record_id,
                },
            },
        )
        PatentFamilyMember.objects.get_or_create(
            family=family,
            publication=publication,
            defaults={
                "relation_type": PatentFamilyMember.RelationType.MEMBER,
                "source_metadata": {
                    "provider": record.provider,
                    "source_payload_hash": snapshot_hash,
                },
            },
        )

    for event in record.legal_events:
        _persist_legal_event(publication, record.provider, event)

    return publication, created


def _persist_legal_event(
    publication: PatentPublication,
    provider: str,
    event: NormalizedLegalEvent,
) -> PatentLegalEvent:
    payload = dict(event.source_payload)
    payload.setdefault("source_event_id", event.source_event_id)
    payload.setdefault("event_code", event.event_code)
    payload.setdefault(
        "event_date",
        event.event_date.isoformat() if event.event_date else None,
    )
    payload.setdefault("title", event.title)
    fingerprint = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return PatentLegalEvent.objects.update_or_create(
        publication=publication,
        provider=provider,
        event_fingerprint=fingerprint,
        defaults={
            "source_event_id": event.source_event_id[:200],
            "event_code": event.event_code[:100],
            "event_date": event.event_date,
            "title": event.title[:500],
            "description": event.description[:10_000],
            "legal_status_after": (
                event.legal_status_after
                if event.legal_status_after in PatentPublication.LegalStatus.values
                else PatentPublication.LegalStatus.UNKNOWN
            ),
            "source_url": event.source_url or publication.source_url or "",
            "source_payload_hash": fingerprint,
            "raw_metadata": payload,
        },
    )[0]


def search_and_persist_publications(
    *,
    tenant: Tenant,
    actor: Any,
    query: str,
    providers: list[str],
    max_results: int,
) -> PatentSearchReport:
    """Authorize, audit, call live sources in parallel, and persist canonical results.

    The raw query is sent to a provider only after tenant-level disclosure policy
    checks and a durable audit record are created. The raw text is never stored
    in AuditLog or printed to logs.
    """
    clean_query = normalize_query(query)
    limit = validate_limit(max_results)
    normalized_providers = list(dict.fromkeys(p.strip().upper() for p in providers))
    if not normalized_providers or any(
        provider not in PROVIDER_NAMES for provider in normalized_providers
    ):
        raise ValueError("Choose USPTO, EPO, or both providers.")

    _require_query_disclosure(tenant)
    _require_actor_membership(tenant, actor)
    query_hmac = _query_hmac(clean_query, tenant)

    try:
        audit_event = AuditLog.objects.create(
            tenant=tenant,
            actor_user_id=actor.pk,
            action="PATENT_DATA_SEARCH",
            resource_type="PATENT_DATA_SEARCH",
            outcome=AuditLog.Outcome.SUCCESS,
            metadata={
                "stage": "REQUESTED",
                "query_hmac": query_hmac,
                "providers_requested": normalized_providers,
                "max_results_per_provider": limit,
            },
        )
    except Exception as exc:
        logger.exception(
            "Patent connector search blocked because audit could not be written."
        )
        raise SearchAuditUnavailable(
            "Required audit record unavailable; external search was not sent."
        ) from exc

    provider_records: dict[str, list[NormalizedPublication]] = {}
    provider_errors: dict[str, str] = {}
    executor = ThreadPoolExecutor(max_workers=len(normalized_providers))
    futures = {
        provider: executor.submit(
            get_connector(provider).search,
            clean_query,
            limit,
        )
        for provider in normalized_providers
    }
    deadline = time.monotonic() + CONNECTOR_TIMEOUT_SECONDS + 2
    try:
        for provider, future in futures.items():
            try:
                remaining = max(0.0, deadline - time.monotonic())
                provider_records[provider] = future.result(timeout=remaining)
            except FutureTimeout:
                future.cancel()
                provider_errors[provider] = "TIMEOUT"
            except PatentConnectorError as exc:
                provider_errors[provider] = exc.code
            except Exception as exc:
                logger.warning(
                    "Patent provider %s failed (%s).",
                    provider,
                    type(exc).__name__,
                )
                provider_errors[provider] = "INTERNAL_CONNECTOR_ERROR"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    canonical_by_id: dict[str, PatentPublication] = {}
    status_records: list[ProviderSearchStatus] = []
    for provider in normalized_providers:
        error_code = provider_errors.get(provider, "")
        records = provider_records.get(provider, [])
        saved_count = 0
        if not error_code:
            try:
                for record in records:
                    canonical, _ = persist_publication(record)
                    canonical_by_id[str(canonical.pk)] = canonical
                    saved_count += 1
            except Exception as exc:
                logger.exception(
                    "Failed to persist normalized patent source record (%s).",
                    type(exc).__name__,
                )
                error_code = "PERSISTENCE_ERROR"
        provider_status = (
            "SUCCEEDED"
            if not error_code
            else "PARTIAL"
            if saved_count > 0
            else "FAILED"
        )
        status_records.append(
            ProviderSearchStatus(
                provider=provider,
                status=provider_status,
                result_count=saved_count,
                error_code=error_code,
            )
        )

    all_failed = bool(status_records) and all(
        item.status == "FAILED" for item in status_records
    )
    has_warnings = any(item.status != "SUCCEEDED" for item in status_records)
    audit_event.outcome = (
        AuditLog.Outcome.FAILURE if all_failed else AuditLog.Outcome.SUCCESS
    )
    audit_event.metadata = {
        **audit_event.metadata,
        "stage": (
            "FAILED" if all_failed else "COMPLETED_WITH_WARNINGS"
            if has_warnings else "COMPLETED"
        ),
        "query_hmac": query_hmac,
        "providers": [
            {
                "provider": item.provider,
                "status": item.status,
                "result_count": item.result_count,
                "error_code": item.error_code,
            }
            for item in status_records
        ],
        "result_count": len(canonical_by_id),
    }
    audit_event.save(update_fields=("outcome", "metadata"))

    ordered = sorted(
        canonical_by_id.values(),
        key=lambda item: (
            item.publication_date is None,
            item.publication_date or date.min,
            item.publication_number or "",
        ),
        reverse=True,
    )
    return PatentSearchReport(
        records=ordered,
        providers=status_records,
        query_hmac=query_hmac,
        audit_event_id=str(audit_event.pk),
    )


def refresh_publication_from_provider(
    publication: PatentPublication,
) -> tuple[PatentPublication | None, list[NormalizedLegalEvent], str]:
    """Refresh one stored publication by its public identifier, preserving history."""
    connector = get_connector(publication.provider)
    lookup = publication.publication_number or publication.application_number
    if not lookup:
        raise PatentConnectorError(
            publication.provider, "PUBLICATION_IDENTIFIER_MISSING"
        )
    if publication.provider == "EPO" and hasattr(connector, "fetch_publication"):
        match = connector.fetch_publication(lookup)
    elif publication.provider == "USPTO" and publication.application_number and hasattr(
        connector, "fetch_application"
    ):
        match = connector.fetch_application(publication.application_number)
    else:
        lookup = (
            publication.application_number
            or publication.publication_number
            or lookup
        )
        results = connector.search(lookup, max_results=10)
        requested = _normalize_identifier(lookup)
        match = next(
            (
                record
                for record in results
                if requested
                in {
                    _normalize_identifier(record.publication_number or ""),
                    _normalize_identifier(record.application_number or ""),
                    _normalize_identifier(record.provider_record_id),
                }
            ),
            None,
        )
    if match is None:
        return None, [], "SOURCE_RECORD_NOT_FOUND"

    if publication.provider == "EPO" and hasattr(connector, "fetch_legal_events"):
        try:
            legal_events = connector.fetch_legal_events(
                match.publication_number or match.provider_record_id
            )
        except PatentConnectorError:
            return persist_publication(match)[0], [], "LEGAL_EVENTS_UNAVAILABLE"
        from dataclasses import replace

        match = replace(match, legal_events=legal_events)

    refreshed, _ = persist_publication(match)
    return refreshed, match.legal_events, ""


def _normalize_identifier(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())
