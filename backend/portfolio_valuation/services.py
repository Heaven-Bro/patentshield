"""Deterministic, versioned diligence indicators and scenario calculations.

All values are driven by explicitly supplied assumptions or the pinned provider
records. No method estimates a legal conclusion or manufactures market data.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from rest_framework.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.db.models import F

from audit.models import AuditLog
from patents.intelligence_models import PatentLegalEvent, PatentPublication, PatentSourceSnapshot
from portfolio_valuation.financial import FinancialAssumptionError, calculate_scenario as _calculate_scenario, validate_assumptions as _validate_assumptions
from portfolio_valuation.models import (
    PortfolioAssetSnapshot,
    PortfolioDiligenceSnapshot,
    PortfolioScenario,
    PortfolioScenarioRevision,
    PortfolioValuationStudy,
)

MONEY_QUANTUM = Decimal("0.01")
DILIGENCE_METHOD_VERSION = "diligence-composition-v1"
VALUATION_METHOD_VERSION = "valuation-methods-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _money(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def _provider_citations(publication: PatentPublication, source_snapshot: PatentSourceSnapshot | None) -> tuple[int | None, int | None, dict[str, Any]]:
    """Read only explicitly named provider citation counts; absence is unknown, not zero."""
    sources = []
    if source_snapshot and isinstance(source_snapshot.payload, dict):
        sources.append((source_snapshot.payload, "source_snapshot.payload"))
    if isinstance(publication.raw_metadata, dict):
        sources.append((publication.raw_metadata, "publication.raw_metadata"))
    aliases = {
        "forward_citation_count": ("forward_citation_count", "forward_citations", "cited_by_count"),
        "backward_citation_count": ("backward_citation_count", "backward_citations", "references_count"),
    }
    result: dict[str, int | None] = {"forward_citation_count": None, "backward_citation_count": None}
    evidence: dict[str, Any] = {"availability": "unknown", "source_snapshot_sha256": source_snapshot.payload_sha256 if source_snapshot else ""}
    for canonical, keys in aliases.items():
        for source, source_name in sources:
            found = False
            for key in keys:
                value = source.get(key)
                if key in source and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    result[canonical] = value
                    evidence[canonical] = {"field": key, "source": source_name}
                    found = True
                    break
            if found:
                break
    if result["forward_citation_count"] is not None or result["backward_citation_count"] is not None:
        evidence["availability"] = "provider-reported"
    return result["forward_citation_count"], result["backward_citation_count"], evidence


def _latest_legal_event(publication: PatentPublication) -> tuple[int, dict[str, Any]]:
    qs = PatentLegalEvent.objects.filter(publication=publication).order_by(F("event_date").desc(nulls_last=True), "-observed_at")
    event = qs.first()
    payload: dict[str, Any] = {}
    if event:
        payload = {
            "event_id": str(event.pk),
            "event_code": event.event_code,
            "title": event.title,
            "event_date": event.event_date.isoformat() if event.event_date else None,
            "provider": event.provider,
            "source_url": event.source_url,
            "source_payload_hash": event.source_payload_hash,
            "legal_status_after": event.legal_status_after,
        }
    return qs.count(), payload


def add_publication_to_study(study: PortfolioValuationStudy, publication: PatentPublication) -> tuple[PortfolioAssetSnapshot, bool]:
    """Pin canonical bibliographic, status, citation, and event evidence atomically."""
    source_snapshot = PatentSourceSnapshot.objects.filter(publication=publication).order_by("-fetched_at").first()
    forward, backward, citation_provenance = _provider_citations(publication, source_snapshot)
    event_count, latest_event = _latest_legal_event(publication)
    snapshot = {
        "publication_id": str(publication.pk),
        "provider": publication.provider,
        "provider_record_id": publication.provider_record_id,
        "publication_number": publication.publication_number or "",
        "application_number": publication.application_number or "",
        "jurisdiction": publication.jurisdiction or "",
        "title": publication.title,
        "abstract": publication.abstract or "",
        "filing_date": publication.filing_date.isoformat() if publication.filing_date else None,
        "publication_date": publication.publication_date.isoformat() if publication.publication_date else None,
        "grant_date": publication.grant_date.isoformat() if publication.grant_date else None,
        "legal_status": publication.legal_status,
        "applicant_names": publication.applicant_names if isinstance(publication.applicant_names, list) else [],
        "ipc_codes": publication.ipc_codes if isinstance(publication.ipc_codes, list) else [],
        "cpc_codes": publication.cpc_codes if isinstance(publication.cpc_codes, list) else [],
        "family_system": publication.family_system or "",
        "family_identifier": publication.family_identifier or "",
        "source_url": publication.source_url or "",
        "source_payload_hash": source_snapshot.payload_sha256 if source_snapshot else (publication.source_payload_hash or ""),
        "source_last_synced_at": publication.last_synced_at.isoformat() if publication.last_synced_at else None,
        "forward_citation_count": forward,
        "backward_citation_count": backward,
        "citation_provenance": citation_provenance,
        "legal_event_count": event_count,
        "latest_legal_event": latest_event,
    }
    row, created = PortfolioAssetSnapshot.objects.get_or_create(
        tenant_id=study.tenant_id, study=study, publication=publication,
        defaults={
            "source_snapshot": source_snapshot,
            "publication_number": snapshot["publication_number"],
            "application_number": snapshot["application_number"],
            "jurisdiction": snapshot["jurisdiction"],
            "title": snapshot["title"],
            "abstract": snapshot["abstract"],
            "filing_date": publication.filing_date,
            "publication_date": publication.publication_date,
            "grant_date": publication.grant_date,
            "legal_status": snapshot["legal_status"],
            "applicant_names": snapshot["applicant_names"],
            "ipc_codes": snapshot["ipc_codes"],
            "cpc_codes": snapshot["cpc_codes"],
            "family_system": snapshot["family_system"],
            "family_identifier": snapshot["family_identifier"],
            "source_url": snapshot["source_url"],
            "source_payload_hash": snapshot["source_payload_hash"],
            "snapshot_sha256": sha256_json(snapshot),
            "source_last_synced_at": publication.last_synced_at,
            "forward_citation_count": forward,
            "backward_citation_count": backward,
            "citation_provenance": citation_provenance,
            "legal_event_count": event_count,
            "latest_legal_event": latest_event,
        },
    )
    return row, created


def calculate_composition_metrics(assets: Iterable[Any]) -> dict[str, Any]:
    """Create transparent, methodology-versioned portfolio composition indicators."""
    rows = list(assets)
    total = len(rows)
    if not total:
        return {
            "asset_count": 0,
            "composition_score": None,
            "composition_score_interpretation": "No assets selected; no score is calculated.",
            "methodology_version": DILIGENCE_METHOD_VERSION,
            "score_components": {},
            "weights": {"data_completeness": "0.50", "status_mix": "0.35", "family_diversity": "0.15"},
            "legal_status_counts": {}, "jurisdiction_counts": {}, "filing_year_counts": {},
            "ipc_code_counts": {}, "cpc_code_counts": {}, "family_metrics": {}, "citation_metrics": {},
            "freshness_metrics": {},
        }

    def has_value(value: Any) -> bool:
        return value not in (None, "", [], {})

    completeness_items = []
    statuses = Counter()
    jurisdictions = Counter()
    years = Counter()
    ipc = Counter()
    cpc = Counter()
    families: set[str] = set()
    known_family_assets = 0
    known_forward: list[int] = []
    known_backward: list[int] = []
    stale_assets = 0
    synced_ages: list[int] = []
    status_scores = {
        "GRANTED": Decimal("100"), "PENDING": Decimal("70"),
        "EXPIRED": Decimal("10"), "LAPSED": Decimal("10"),
        "ABANDONED": Decimal("0"), "UNKNOWN": Decimal("25"),
    }
    status_score_items: list[Decimal] = []
    for row in rows:
        completeness_flags = [
            has_value(row.title), has_value(row.jurisdiction), has_value(row.filing_date),
            has_value(row.legal_status) and row.legal_status != "UNKNOWN", has_value(row.source_payload_hash),
        ]
        completeness_items.append(Decimal(sum(completeness_flags)) / Decimal(len(completeness_flags)) * 100)
        status = str(row.legal_status or "UNKNOWN").upper()
        statuses[status] += 1
        status_score_items.append(status_scores.get(status, Decimal("25")))
        if has_value(row.jurisdiction):
            jurisdictions[str(row.jurisdiction).upper()] += 1
        if row.filing_date:
            years[str(row.filing_date.year)] += 1
        for code in (row.ipc_codes or []):
            if isinstance(code, str) and code.strip():
                ipc[code.strip().upper()] += 1
        for code in (row.cpc_codes or []):
            if isinstance(code, str) and code.strip():
                cpc[code.strip().upper()] += 1
        if row.family_system and row.family_identifier:
            known_family_assets += 1
            families.add(f"{row.family_system}:{row.family_identifier}")
        if row.forward_citation_count is not None:
            known_forward.append(int(row.forward_citation_count))
        if row.backward_citation_count is not None:
            known_backward.append(int(row.backward_citation_count))
        if row.source_last_synced_at:
            age = max(0, (timezone.now() - row.source_last_synced_at).days)
            synced_ages.append(age)
            if age > 180:
                stale_assets += 1

    completeness = sum(completeness_items, Decimal("0")) / Decimal(total)
    status_mix = sum(status_score_items, Decimal("0")) / Decimal(total)
    family_diversity = (Decimal(len(families)) / Decimal(known_family_assets) * 100) if known_family_assets else Decimal("0")
    weights = {"data_completeness": Decimal("0.50"), "status_mix": Decimal("0.35"), "family_diversity": Decimal("0.15")}
    composite = completeness * weights["data_completeness"] + status_mix * weights["status_mix"] + family_diversity * weights["family_diversity"]
    latest_event_rows = [row for row in rows if row.latest_legal_event]
    latest_event_rows.sort(key=lambda r: str(r.latest_legal_event.get("event_date") or ""), reverse=True)
    return {
        "asset_count": total,
        "composition_score": _money(composite),
        "composition_score_interpretation": "A data-completeness, reported-status mix, and known-family-diversity indicator. Not a value, patent-strength score, legal opinion, or probability.",
        "methodology_version": DILIGENCE_METHOD_VERSION,
        "weights": {key: str(value) for key, value in weights.items()},
        "score_components": {
            "data_completeness_score": _money(completeness),
            "reported_status_mix_score": _money(status_mix),
            "known_family_diversity_score": _money(family_diversity),
            "computation": "0.50 × data completeness + 0.35 × reported-status mix + 0.15 × known-family diversity",
            "status_points": {key: str(value) for key, value in status_scores.items()},
        },
        "legal_status_counts": dict(sorted(statuses.items())),
        "jurisdiction_counts": dict(sorted(jurisdictions.items())),
        "filing_year_counts": dict(sorted(years.items())),
        "ipc_code_counts": dict(ipc.most_common(50)),
        "cpc_code_counts": dict(cpc.most_common(50)),
        "family_metrics": {
            "distinct_known_families": len(families),
            "assets_with_known_family": known_family_assets,
            "assets_without_known_family": total - known_family_assets,
            "family_identifiers_are_provider_scoped": True,
        },
        "citation_metrics": {
            "assets_with_forward_citation_counts": len(known_forward),
            "assets_with_backward_citation_counts": len(known_backward),
            "forward_citation_count_total_known_only": sum(known_forward) if known_forward else None,
            "backward_citation_count_total_known_only": sum(known_backward) if known_backward else None,
            "forward_citation_mean_known_only": _money(Decimal(sum(known_forward)) / Decimal(len(known_forward))) if known_forward else None,
            "backward_citation_mean_known_only": _money(Decimal(sum(known_backward)) / Decimal(len(known_backward))) if known_backward else None,
            "citation_coverage_pct_forward": _money(Decimal(len(known_forward)) / Decimal(total) * 100),
            "citation_coverage_pct_backward": _money(Decimal(len(known_backward)) / Decimal(total) * 100),
            "missing_means_unknown_not_zero": True,
        },
        "freshness_metrics": {
            "assets_with_source_sync_timestamp": len(synced_ages),
            "assets_with_source_older_than_180_days": stale_assets,
            "oldest_known_sync_age_days": max(synced_ages) if synced_ages else None,
            "freshness_threshold_days": 180,
        },
        "latest_observed_legal_event": latest_event_rows[0].latest_legal_event if latest_event_rows else None,
        "coverage_caveat": "Indicators reflect only selected records and fields reported by their provider snapshots. They may be incomplete, stale, jurisdiction-specific, or incomparable across sources.",
    }


def create_diligence_snapshot(study: PortfolioValuationStudy, actor) -> PortfolioDiligenceSnapshot:
    rows = list(PortfolioAssetSnapshot.objects.filter(tenant_id=study.tenant_id, study=study).order_by("pk"))
    manifest = sha256_json([{"id": str(row.pk), "snapshot_sha256": row.snapshot_sha256} for row in rows])
    metrics = calculate_composition_metrics(rows)
    score = Decimal(metrics["composition_score"]) if metrics.get("composition_score") is not None else None
    snapshot = PortfolioDiligenceSnapshot.objects.create(
        tenant_id=study.tenant_id, study=study, asset_manifest_sha256=manifest,
        methodology_version=DILIGENCE_METHOD_VERSION, asset_count=len(rows),
        composition_score=score, metrics=metrics, created_by=actor,
    )
    return snapshot


def validate_assumptions(method: str, assumptions: Any) -> dict[str, Any]:
    try:
        return _validate_assumptions(method, assumptions)
    except FinancialAssumptionError as exc:
        raise ValidationError({exc.field: exc.detail}) from None


def calculate_scenario(method: str, currency: str, assumptions: dict[str, Any]) -> dict[str, Any]:
    try:
        validated = _validate_assumptions(method, assumptions)
        return _calculate_scenario(method, currency, validated)
    except FinancialAssumptionError as exc:
        raise ValidationError({exc.field: exc.detail}) from None


def create_scenario_revision(scenario: PortfolioScenario, actor, assumptions: Any, currency: str | None = None) -> PortfolioScenarioRevision:
    """Append one immutable revision under a scenario row lock to prevent duplicate numbering."""
    with transaction.atomic():
        locked = PortfolioScenario.objects.select_for_update().select_related("study").get(pk=scenario.pk, tenant_id=scenario.tenant_id)
        normalized = validate_assumptions(locked.method, assumptions)
        normalized_currency = (currency or locked.study.currency).upper()
        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise ValidationError({"currency": "Use an uppercase three-letter currency code."})
        if normalized_currency != locked.study.currency:
            raise ValidationError({"currency": "Scenario revision currency must match the study currency."})
        previous = PortfolioScenarioRevision.objects.filter(tenant_id=locked.tenant_id, scenario=locked).order_by("-revision_number").first()
        revision_number = previous.revision_number + 1 if previous else 1
        assets = list(PortfolioAssetSnapshot.objects.filter(tenant_id=locked.tenant_id, study_id=locked.study_id).order_by("pk").values("id", "snapshot_sha256"))
        asset_manifest = sha256_json([{"id": str(item["id"]), "snapshot_sha256": item["snapshot_sha256"]} for item in assets])
        result = calculate_scenario(locked.method, normalized_currency, normalized)
        result["portfolio_asset_count"] = len(assets)
        result["portfolio_asset_manifest_sha256"] = asset_manifest
        envelope = {"scenario_id": str(locked.pk), "method": locked.method, "currency": normalized_currency, "assumptions": normalized, "result": result, "asset_manifest_sha256": asset_manifest, "methodology_version": VALUATION_METHOD_VERSION}
        return PortfolioScenarioRevision.objects.create(
            tenant_id=locked.tenant_id, scenario=locked, revision_number=revision_number,
            currency=normalized_currency, assumptions=normalized, calculated_result=result,
            input_sha256=sha256_json(envelope), asset_manifest_sha256=asset_manifest,
            methodology_version=VALUATION_METHOD_VERSION, created_by=actor,
        )


def audit_valuation_event(*, tenant_id: Any, actor_id: Any, action: str, resource_type: str, resource_id: Any, metadata: dict[str, Any] | None = None) -> None:
    """Store compact audit metadata; do not copy financial assumptions into logs."""
    try:
        AuditLog.objects.create(
            tenant_id=tenant_id, actor_user_id=actor_id, action=action[:100],
            resource_type=resource_type[:100], resource_id=resource_id,
            outcome=AuditLog.Outcome.SUCCESS, metadata=metadata or {},
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Portfolio valuation audit write failed (%s).", resource_type)
