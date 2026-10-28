"""Claim-chart snapshot creation, provenance validation, and revision management."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from analyses.models import AnalysisReport, ClaimAssessment, EvidenceReference
from audit.models import AuditLog
from reports.models import ClaimChart, ClaimChartRevision, ClaimChartRow

_EXPORTABLE_ANALYSIS_STATUSES = {
    AnalysisReport.Status.COMPLETED,
    AnalysisReport.Status.COMPLETED_WITH_WARNINGS,
}


def create_chart_from_analysis(
    *,
    tenant_id: Any,
    analysis_id: Any,
    user: Any,
    title: str | None = None,
    description: str = "",
    request_id: str | None = None,
) -> ClaimChart:
    """Create a chart from persisted, tenant-owned assessments transactionally."""
    with transaction.atomic():
        try:
            analysis = (
                AnalysisReport.objects.for_tenant(tenant_id)
                .select_for_update(of=("self",))
                .select_related("patent", "target_document_version", "target_claim")
                .get(pk=analysis_id)
            )
        except AnalysisReport.DoesNotExist as exc:
            raise ValidationError("Analysis was not found in this tenant.") from exc
        validate_analysis_integrity(analysis, tenant_id)
        if analysis.status not in _EXPORTABLE_ANALYSIS_STATUSES:
            raise ValidationError("Only completed analyses can create claim charts.")

        assessments = list(
            ClaimAssessment.objects.for_tenant(tenant_id)
            .filter(analysis=analysis)
            .select_related("claim", "claim__patent")
            .order_by("limitation_index", "created_at")
        )
        if not assessments:
            raise ValidationError(
                "The analysis contains no persisted claim assessments."
            )
        for assessment in assessments:
            validate_assessment_integrity(analysis, assessment, tenant_id)

        default_title = (
            "Claim chart — "
            f"{analysis.patent.publication_number or analysis.patent.title}"
        )
        if analysis.target_claim_id:
            default_title += f" — Claim {analysis.target_claim.claim_number}"
        chart = ClaimChart.objects.create(
            tenant_id=tenant_id,
            analysis=analysis,
            title=(title or default_title)[:240],
            description=description,
            status=ClaimChart.Status.DRAFT,
            revision=1,
            created_by=user,
            updated_by=user,
        )
        ClaimChartRow.objects.bulk_create(
            [
                ClaimChartRow(
                    tenant_id=tenant_id,
                    chart=chart,
                    assessment=assessment,
                    limitation_index=assessment.limitation_index,
                    limitation_text=assessment.limitation_text,
                    mapping_status=assessment.assessment_status,
                    mapping_summary=assessment.explanation,
                    attorney_decision=assessment.review_status,
                    sort_order=assessment.limitation_index,
                )
                for assessment in assessments
            ]
        )
        record_revision(
            chart=chart,
            user=user,
            change_summary="Initial chart created from completed analysis.",
            bump_revision=False,
        )
        AuditLog.objects.create(
            tenant_id=tenant_id,
            actor_user_id=user.pk,
            action="CLAIM_CHART_CREATED",
            resource_type="ClaimChart",
            resource_id=chart.pk,
            request_id=_safe_request_id(request_id),
            outcome=AuditLog.Outcome.SUCCESS,
            metadata={
                "analysis_id": str(analysis.pk),
                "row_count": len(assessments),
                "revision": chart.revision,
            },
        )
        return chart


def validate_analysis_integrity(analysis: AnalysisReport, tenant_id: Any) -> None:
    """Reject analyses whose parent patent/version/claim cross tenant or patent."""
    if analysis.tenant_id != tenant_id:
        raise ValidationError("Analysis is outside the current tenant.")
    if analysis.patent.tenant_id != tenant_id:
        raise ValidationError("Analysis patent is outside the current tenant.")
    if analysis.target_document_version.tenant_id != tenant_id:
        raise ValidationError("Analysis document is outside the current tenant.")
    if analysis.target_document_version.patent_id != analysis.patent_id:
        raise ValidationError("Analysis document does not belong to its target patent.")
    if analysis.target_claim_id and (
        analysis.target_claim.tenant_id != tenant_id
        or analysis.target_claim.patent_id != analysis.patent_id
        or analysis.target_claim.document_version_id
        != analysis.target_document_version_id
    ):
        raise ValidationError(
            "Analysis target claim does not match the target document."
        )


def validate_assessment_integrity(
    analysis: AnalysisReport,
    assessment: ClaimAssessment,
    tenant_id: Any,
) -> None:
    """Ensure each chart row is from this completed analysis and target patent."""
    if assessment.tenant_id != tenant_id or assessment.analysis_id != analysis.pk:
        raise ValidationError("Assessment is outside the current analysis or tenant.")
    if assessment.claim.tenant_id != tenant_id:
        raise ValidationError("Assessment claim is outside the current tenant.")
    if assessment.claim.patent_id != analysis.patent_id:
        raise ValidationError("Assessment claim does not belong to the target patent.")
    if assessment.claim.document_version_id != analysis.target_document_version_id:
        raise ValidationError(
            "Assessment claim does not belong to the target document."
        )
    if analysis.target_claim_id and assessment.claim_id != analysis.target_claim_id:
        raise ValidationError(
            "Assessment claim does not match the selected target claim."
        )


def build_chart_snapshot(chart: ClaimChart) -> dict[str, Any]:
    """Build a JSON-safe evidence snapshot after checking every source relation."""
    analysis = (
        AnalysisReport.objects.for_tenant(chart.tenant_id)
        .select_related("patent", "target_document_version", "target_claim")
        .get(pk=chart.analysis_id)
    )
    validate_analysis_integrity(analysis, chart.tenant_id)

    evidence_queryset = (
        EvidenceReference.objects.for_tenant(chart.tenant_id)
        .select_related("source_patent", "source_document_version")
        .order_by("source_page_index", "created_at")
    )
    rows = list(
        ClaimChartRow.objects.for_tenant(chart.tenant_id)
        .filter(chart=chart)
        .select_related("assessment", "assessment__claim", "assessment__claim__patent")
        .prefetch_related(
            Prefetch(
                "assessment__evidence_references",
                queryset=evidence_queryset,
                to_attr="claim_chart_evidence",
            )
        )
    )

    row_payloads: list[dict[str, Any]] = []
    for row in rows:
        validate_assessment_integrity(analysis, row.assessment, chart.tenant_id)
        evidence_items: list[dict[str, Any]] = []
        for evidence in getattr(row.assessment, "claim_chart_evidence", []):
            if (
                evidence.tenant_id != chart.tenant_id
                or evidence.assessment_id != row.assessment_id
            ):
                raise ValidationError(
                    "Evidence reference has inconsistent tenant provenance."
                )
            if (
                evidence.source_patent is not None
                and evidence.source_patent.tenant_id != chart.tenant_id
            ):
                raise ValidationError(
                    "Evidence patent is outside the current tenant."
                )
            if (
                evidence.source_document_version is not None
                and evidence.source_document_version.tenant_id != chart.tenant_id
            ):
                raise ValidationError(
                    "Evidence document is outside the current tenant."
                )
            if (
                evidence.source_document_version_id
                and evidence.source_patent_id
                and evidence.source_document_version.patent_id
                != evidence.source_patent_id
            ):
                raise ValidationError(
                    "Evidence document and patent references disagree."
                )
            source_patent = evidence.source_patent
            source_version = evidence.source_document_version
            retrieval_metadata = _redact_sensitive(evidence.retrieval_metadata)
            evidence_items.append(
                {
                    "evidence_id": str(evidence.pk),
                    "source_patent_id": (
                        str(evidence.source_patent_id)
                        if evidence.source_patent_id
                        else None
                    ),
                    "source_publication_number": (
                        source_patent.publication_number if source_patent else None
                    ),
                    "source_patent_title": (
                        source_patent.title if source_patent else None
                    ),
                    "source_jurisdiction": (
                        source_patent.jurisdiction if source_patent else None
                    ),
                    "source_document_version_id": (
                        str(evidence.source_document_version_id)
                        if evidence.source_document_version_id
                        else None
                    ),
                    "source_version_number": (
                        source_version.version_number if source_version else None
                    ),
                    "source_section": evidence.source_section,
                    "page_index": evidence.source_page_index,
                    "page_number": (
                        evidence.source_page_index + 1
                        if evidence.source_page_index is not None
                        else None
                    ),
                    "source_start_offset": evidence.source_start_offset,
                    "source_end_offset": evidence.source_end_offset,
                    "exact_quote": evidence.exact_quote,
                    "quote_verified": evidence.quote_verified,
                    "bbox": evidence.bbox,
                    "retrieval_metadata": retrieval_metadata,
                }
            )
        row_payloads.append(
            {
                "row_id": str(row.pk),
                "assessment_id": str(row.assessment_id),
                "claim_id": str(row.assessment.claim_id),
                "claim_number": row.assessment.claim.claim_number,
                "limitation_index": row.limitation_index,
                "limitation_text": row.limitation_text,
                "mapping_status": row.mapping_status,
                "mapping_summary": row.mapping_summary,
                "technical_metrics": _redact_sensitive(row.assessment.metrics),
                "confidence": row.assessment.confidence,
                "attorney_decision": row.attorney_decision,
                "attorney_notes": row.attorney_notes,
                "evidence": evidence_items,
            }
        )

    result_summary = (
        analysis.result_summary
        if isinstance(analysis.result_summary, dict)
        else {}
    )
    return {
        "schema_version": "patentshield-claim-chart-v1",
        "generated_at": timezone.now().isoformat(),
        "tenant_id": str(chart.tenant_id),
        "chart": {
            "chart_id": str(chart.pk),
            "title": chart.title,
            "description": chart.description,
            "status": chart.status,
            "revision": chart.revision,
            "created_at": chart.created_at.isoformat(),
            "updated_at": chart.updated_at.isoformat(),
        },
        "analysis": {
            "analysis_id": str(analysis.pk),
            "analysis_type": analysis.analysis_type,
            "status": analysis.status,
            "created_at": analysis.created_at.isoformat(),
            "score_version": analysis.score_version,
            "metrics": _redact_sensitive(result_summary.get("metrics", {})),
        },
        "target_patent": {
            "patent_id": str(analysis.patent_id),
            "publication_number": analysis.patent.publication_number,
            "application_number": analysis.patent.application_number,
            "jurisdiction": analysis.patent.jurisdiction,
            "title": analysis.patent.title,
        },
        "target_document_version": {
            "document_version_id": str(analysis.target_document_version_id),
            "version_number": analysis.target_document_version.version_number,
            "content_sha256": analysis.target_document_version.content_sha256,
        },
        "target_claim": (
            {
                "claim_id": str(analysis.target_claim_id),
                "claim_number": analysis.target_claim.claim_number,
                "claim_text": analysis.target_claim.claim_text,
            }
            if analysis.target_claim_id
            else None
        ),
        "rows": row_payloads,
        "counts": {
            "limitations": len(row_payloads),
            "evidence_references": sum(len(row["evidence"]) for row in row_payloads),
            "verified_quotes": sum(
                int(item["quote_verified"])
                for row in row_payloads
                for item in row["evidence"]
            ),
        },
        "disclaimer": (
            "AI-assisted research work product for professional review. "
            "It is not a legal opinion or a determination of infringement, "
            "validity, novelty, or freedom to operate."
        ),
    }


def record_revision(
    *,
    chart: ClaimChart,
    user: Any,
    change_summary: str,
    bump_revision: bool = True,
) -> ClaimChartRevision:
    """Persist an immutable, hash-addressed snapshot for the chart's revision."""
    if bump_revision:
        chart.revision += 1
        chart.updated_by = user
        chart.save(update_fields=("revision", "updated_by", "updated_at"))
    snapshot = build_chart_snapshot(chart)
    canonical_bytes = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return ClaimChartRevision.objects.create(
        tenant_id=chart.tenant_id,
        chart=chart,
        revision_number=chart.revision,
        snapshot=snapshot,
        snapshot_sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        change_summary=change_summary[:500],
        created_by=user,
    )


def serialize_chart(chart: ClaimChart, *, include_rows: bool = True) -> dict[str, Any]:
    """Serialize current chart data without exposing storage keys or user secrets."""
    payload: dict[str, Any] = {
        "chart_id": str(chart.pk),
        "analysis_id": str(chart.analysis_id),
        "title": chart.title,
        "description": chart.description,
        "status": chart.status,
        "revision": chart.revision,
        "created_by_id": str(chart.created_by_id),
        "updated_by_id": str(chart.updated_by_id) if chart.updated_by_id else None,
        "created_at": chart.created_at.isoformat(),
        "updated_at": chart.updated_at.isoformat(),
        "row_count": (
            ClaimChartRow.objects.for_tenant(chart.tenant_id)
            .filter(chart=chart)
            .count()
        ),
    }
    latest_revision = (
        ClaimChartRevision.objects.for_tenant(chart.tenant_id)
        .filter(chart=chart, revision_number=chart.revision)
        .first()
    )
    if include_rows:
        # Return the same immutable snapshot whose digest is exposed below; do
        # not regenerate a new timestamped snapshot during an ordinary GET.
        payload["snapshot"] = (
            latest_revision.snapshot if latest_revision else build_chart_snapshot(chart)
        )
    payload["latest_revision"] = (
        {
            "revision_id": str(latest_revision.pk),
            "revision_number": latest_revision.revision_number,
            "snapshot_sha256": latest_revision.snapshot_sha256,
            "created_at": latest_revision.created_at.isoformat(),
        }
        if latest_revision
        else None
    )
    return payload


def _redact_sensitive(value: Any) -> Any:
    """Redact secret-like metadata keys while preserving ordinary provenance."""
    sensitive_terms = (
        "api_key",
        "authorization",
        "access_token",
        "secret",
        "password",
        "credential",
    )
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if any(term in str(key).lower() for term in sensitive_terms)
                else _redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _safe_request_id(value: str | None) -> str | None:
    return str(value)[:100] if value else None
