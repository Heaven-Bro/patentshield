"""Persistence services for tenant-scoped PatentShield analysis runs."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from enterprise_identity.services import filter_visible_matter_queryset, matter_access_allowed
from tenants.models import Tenant
from analyses.models import AnalysisReport, ClaimAssessment, EvidenceReference
from audit.models import AuditLog
from indexing.models import TextChunk
from patents.models import Claim, Patent, PatentDocumentVersion

_WHITESPACE_RE = re.compile(r"\s+")
_ALLOWED_ASSESSMENT_STATUSES = {
    value for value, _label in ClaimAssessment.AssessmentStatus.choices
}


def persist_analysis_result(
    analysis_id: str | UUID,
    graph_state: Mapping[str, Any],
    *,
    final_status: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Persist graph assessments, canonical evidence, metrics, and final status.

    All assessment and evidence writes are committed in one transaction. Every
    foreign key is resolved through the analysis tenant, and the quote's
    canonical chunk is rechecked before it can be marked verified.
    """
    with transaction.atomic():
        analysis = (
            AnalysisReport.objects.only(
                "pk", "tenant_id", "patent_id", "target_document_version_id", "created_by_id"
            )
            .select_related("created_by")
            .select_for_update(of=("self",))
            .get(pk=analysis_id)
        )
        tenant_id = analysis.tenant_id
        tenant = Tenant.objects.get(pk=tenant_id)
        if not analysis.created_by.is_active:
            raise PermissionError("Analysis creator account is inactive.")
        if (
            not matter_access_allowed(
                tenant=tenant, user=analysis.created_by, matter_type="analysis",
                matter_id=analysis.pk, required="READ",
            )
            or not matter_access_allowed(
                tenant=tenant, user=analysis.created_by, matter_type="patent",
                matter_id=analysis.patent_id, required="READ",
            )
        ):
            raise PermissionError(
                "Analysis creator no longer has analysis and patent READ access."
            )
        # Hydrate content-bearing relations only after the creator's READ grant.
        analysis = (
            AnalysisReport.objects.select_related("patent", "target_document_version", "created_by")
            .get(pk=analysis_id)
        )
        _validate_analysis_provenance(analysis)
        visible_patent_ids = filter_visible_matter_queryset(
            Patent.objects.for_tenant(tenant_id), tenant=tenant,
            user=analysis.created_by, matter_type="patent",
        ).values_list("pk", flat=True)
        target_claim = _resolve_target_claim(analysis, graph_state)

        raw_limitations = graph_state.get("limitations", [])
        raw_assessments = graph_state.get("assessments", [])
        limitations = [
            item for item in raw_limitations if isinstance(item, Mapping)
        ] if isinstance(raw_limitations, list) else []
        assessments = [
            item for item in raw_assessments if isinstance(item, Mapping)
        ] if isinstance(raw_assessments, list) else []

        assessment_by_limitation_id = {
            str(item.get("limitation_id", "")): item
            for item in assessments
            if item.get("limitation_id")
        }
        limitation_records: list[tuple[int, str, str, Mapping[str, Any]]] = []
        for fallback_index, limitation in enumerate(limitations, start=1):
            try:
                limitation_index = int(
                    limitation.get("limitation_index", fallback_index)
                )
            except (TypeError, ValueError, OverflowError):
                limitation_index = fallback_index
            if limitation_index < 1:
                limitation_index = fallback_index
            limitation_id = str(limitation.get("limitation_id", ""))
            limitation_text = str(limitation.get("text", "")).strip()
            assessment = assessment_by_limitation_id.get(limitation_id, {})
            limitation_records.append(
                (limitation_index, limitation_id, limitation_text, assessment)
            )

        # A retried failed analysis replaces its prior partial result atomically.
        # Deleting assessments cascades to their evidence references.
        ClaimAssessment.objects.for_tenant(tenant_id).filter(
            analysis_id=analysis.pk
        ).delete()

        assessment_rows: list[ClaimAssessment] = []
        evidence_inputs: list[tuple[ClaimAssessment, list[Mapping[str, Any]]]] = []
        for (
            limitation_index,
            _limitation_id,
            limitation_text,
            result,
        ) in limitation_records:
            status_value = str(result.get("status", "UNCERTAIN")).upper()
            if status_value not in _ALLOWED_ASSESSMENT_STATUSES:
                status_value = ClaimAssessment.AssessmentStatus.UNCERTAIN

            confidence = _bounded_score(result.get("confidence", 0.0))
            retrieval_relevance = _bounded_score(
                result.get("retrieval_relevance", 0.0)
            )
            technical_correspondence = _bounded_score(
                result.get("technical_correspondence", 0.0)
            )
            result_evidence = result.get("evidence", [])
            if not isinstance(result_evidence, list):
                result_evidence = []
            stored_assessment = ClaimAssessment(
                tenant_id=tenant_id,
                analysis=analysis,
                claim=target_claim,
                limitation_index=limitation_index,
                limitation_text=limitation_text or target_claim.claim_text,
                assessment_status=status_value,
                explanation=str(result.get("explanation", ""))[:20000],
                confidence=confidence,
                metrics={
                    "retrieval_relevance": retrieval_relevance,
                    "technical_correspondence": technical_correspondence,
                    "quote_verified": result.get("quote_verified") is True,
                },
            )
            assessment_rows.append(stored_assessment)
            evidence_inputs.append(
                (
                    stored_assessment,
                    [item for item in result_evidence if isinstance(item, Mapping)],
                )
            )

        if assessment_rows:
            ClaimAssessment.objects.bulk_create(assessment_rows, batch_size=200)

        evidence_rows: list[EvidenceReference] = []
        verified_evidence_count = 0
        for assessment, evidence_items in evidence_inputs:
            for evidence in evidence_items:
                exact_quote = str(evidence.get("exact_quote", ""))
                if not exact_quote.strip():
                    continue

                chunk = _canonical_chunk(
                    evidence.get("chunk_id"), tenant_id,
                    visible_patent_ids=visible_patent_ids,
                )
                candidate_source_matches = _source_ids_match_chunk(evidence, chunk)
                canonical_quote_matches = bool(
                    chunk is not None
                    and _normalize_text(exact_quote)
                    and _normalize_text(exact_quote) in _normalize_text(chunk.text)
                )
                quote_verified = bool(
                    evidence.get("quote_verified") is True
                    and chunk is not None
                    and candidate_source_matches
                    and canonical_quote_matches
                )
                if quote_verified:
                    verified_evidence_count += 1

                page_index = _optional_nonnegative_int(evidence.get("page_index"))
                if page_index is None and chunk is not None:
                    page_index = _optional_nonnegative_int(chunk.page_start)

                start_offset: int | None = None
                end_offset: int | None = None
                if chunk is not None and candidate_source_matches:
                    quote_offset = chunk.text.find(exact_quote)
                    if quote_offset >= 0 and chunk.char_start is not None:
                        start_offset = chunk.char_start + quote_offset
                        end_offset = start_offset + len(exact_quote)

                metadata: dict[str, Any] = {
                    "quote_verified_by_guardrail": (
                        evidence.get("quote_verified") is True
                    ),
                    "dense_score": _bounded_score(
                        evidence.get("dense_score", 0.0)
                    ),
                    "lexical_score": _finite_nonnegative(
                        evidence.get("lexical_score", 0.0)
                    ),
                    "rrf_score": _finite_nonnegative(
                        evidence.get("rrf_score", 0.0)
                    ),
                    "rerank_score": _bounded_score(
                        evidence.get("rerank_score", 0.0)
                    ),
                    "retrieval_score": _bounded_score(
                        evidence.get("retrieval_score", 0.0)
                    ),
                    "provenance_status": (
                        "CANONICAL"
                        if chunk is not None and candidate_source_matches
                        else "UNVERIFIED"
                    ),
                }
                if chunk is not None and candidate_source_matches:
                    metadata["chunk_id"] = str(chunk.pk)
                source_patent = (
                    chunk.patent
                    if chunk is not None and candidate_source_matches
                    else None
                )
                source_version = (
                    chunk.document_version
                    if chunk is not None and candidate_source_matches
                    else None
                )
                evidence_rows.append(
                    EvidenceReference(
                        tenant_id=tenant_id,
                        assessment=assessment,
                        source_patent=source_patent,
                        source_document_version=source_version,
                        source_section=(
                            str(evidence.get("section_type", ""))[:100] or None
                        ),
                        source_page_index=page_index,
                        source_start_offset=start_offset,
                        source_end_offset=end_offset,
                        exact_quote=exact_quote,
                        quote_verified=quote_verified,
                        bbox=None,
                        retrieval_metadata=metadata,
                    )
                )

        if evidence_rows:
            EvidenceReference.objects.bulk_create(evidence_rows, batch_size=500)

        metrics = graph_state.get("metrics", {})
        if not isinstance(metrics, Mapping):
            metrics = {}
        normalized_metrics = {
            "retrieval_relevance": _bounded_score(
                metrics.get("retrieval_relevance", 0.0)
            ),
            "limitation_coverage": _bounded_score(
                metrics.get("limitation_coverage", 0.0)
            ),
            "evidence_confidence": _bounded_score(
                metrics.get("evidence_confidence", 0.0)
            ),
            "technical_correspondence": _bounded_score(
                metrics.get("technical_correspondence", 0.0)
            ),
            # The aggregate screening score is already expressed on 0..100.
            "screening_priority_score": min(
                100.0,
                _finite_nonnegative(
                    metrics.get("screening_priority_score", 0.0)
                ),
            ),
        }

        warnings = _safe_warning_list(graph_state.get("error_message"))
        summary = {
            "schema_version": "1.0",
            "analysis_id": str(analysis.pk),
            "target_patent_id": str(analysis.patent_id),
            "target_document_version_id": str(analysis.target_document_version_id),
            "target_claim_id": str(target_claim.pk),
            "target_claim_number": target_claim.claim_number,
            "limitation_count": len(limitation_records),
            "assessment_count": len(assessment_rows),
            "evidence_reference_count": len(evidence_rows),
            "verified_evidence_count": verified_evidence_count,
            "metrics": normalized_metrics,
            "warnings": warnings,
            "graph_status": str(graph_state.get("status", "UNKNOWN"))[:64],
        }
        analysis.target_claim = target_claim
        analysis.status = final_status
        analysis.result_summary = summary
        analysis.error_code = (
            "ANALYSIS_GRAPH_FAILED"
            if final_status == AnalysisReport.Status.FAILED
            else None
        )
        analysis.completed_at = timezone.now()
        analysis.save(
            update_fields=(
                "target_claim",
                "status",
                "result_summary",
                "error_code",
                "completed_at",
                "updated_at",
            )
        )

        AuditLog.objects.create(
            tenant_id=tenant_id,
            actor_user_id=analysis.created_by_id,
            action=(
                "PATENT_ANALYSIS_FAILED"
                if final_status == AnalysisReport.Status.FAILED
                else "PATENT_ANALYSIS_COMPLETED"
            ),
            resource_type="AnalysisReport",
            resource_id=analysis.pk,
            request_id=task_id,
            outcome=(
                AuditLog.Outcome.FAILURE
                if final_status == AnalysisReport.Status.FAILED
                else AuditLog.Outcome.SUCCESS
            ),
            metadata={
                "assessment_count": len(assessment_rows),
                "evidence_reference_count": len(evidence_rows),
                "verified_evidence_count": verified_evidence_count,
                "screening_priority_score": normalized_metrics[
                    "screening_priority_score"
                ],
            },
        )

        return {
            "analysis_id": str(analysis.pk),
            "status": analysis.status,
            "assessment_count": len(assessment_rows),
            "evidence_reference_count": len(evidence_rows),
            "verified_evidence_count": verified_evidence_count,
            "metrics": normalized_metrics,
        }


def _validate_analysis_provenance(analysis: AnalysisReport) -> None:
    """Reject corrupt cross-tenant or cross-version analysis relationships."""
    if (
        analysis.patent.tenant_id != analysis.tenant_id
        or analysis.target_document_version.tenant_id != analysis.tenant_id
        or analysis.target_document_version.patent_id != analysis.patent_id
    ):
        raise ValueError("Analysis target provenance does not match its tenant.")


def _resolve_target_claim(
    analysis: AnalysisReport,
    graph_state: Mapping[str, Any],
) -> Claim:
    """Resolve the selected claim through tenant, patent, and version predicates."""
    if analysis.target_claim_id:
        claim = (
            Claim.objects.for_tenant(analysis.tenant_id)
            .select_related("patent", "document_version")
            .filter(
                pk=analysis.target_claim_id,
                patent_id=analysis.patent_id,
                document_version_id=analysis.target_document_version_id,
            )
            .first()
        )
    else:
        claim_number = str(graph_state.get("target_claim_number", ""))
        claim = (
            Claim.objects.for_tenant(analysis.tenant_id)
            .select_related("patent", "document_version")
            .filter(
                patent_id=analysis.patent_id,
                document_version_id=analysis.target_document_version_id,
                claim_number=claim_number,
            )
            .first()
        )
    if claim is None:
        raise ValueError(
            "Target claim was not found in the analysis document version."
        )
    if (
        claim.tenant_id != analysis.tenant_id
        or claim.patent.tenant_id != analysis.tenant_id
        or claim.document_version.tenant_id != analysis.tenant_id
        or claim.document_version.patent_id != analysis.patent_id
    ):
        raise ValueError("Target claim provenance does not match its tenant.")
    return claim


def _canonical_chunk(
    chunk_id: Any, tenant_id: Any, *, visible_patent_ids=None,
) -> TextChunk | None:
    """Resolve a citation only inside the tenant and creator's READ-visible matters."""
    try:
        chunk_uuid = UUID(str(chunk_id))
    except (AttributeError, TypeError, ValueError):
        return None
    return (
        TextChunk.objects.for_tenant(tenant_id)
        .filter(
            pk=chunk_uuid,
            patent__tenant_id=tenant_id,
            document_version__tenant_id=tenant_id,
            patent_id__in=visible_patent_ids if visible_patent_ids is not None else [],
        )
        .select_related("patent", "document_version")
        .first()
    )


def _source_ids_match_chunk(
    evidence: Mapping[str, Any],
    chunk: TextChunk | None,
) -> bool:
    """Validate source identifiers supplied by retrieval against the real chunk."""
    if chunk is None:
        return False
    supplied_patent_id = evidence.get("source_patent_id")
    supplied_version_id = evidence.get("source_version_id")
    return bool(
        (
            supplied_patent_id is None
            or str(supplied_patent_id) == str(chunk.patent_id)
        )
        and (
            supplied_version_id is None
            or str(supplied_version_id) == str(chunk.document_version_id)
        )
        and chunk.document_version.patent_id == chunk.patent_id
    )


def _normalize_text(value: str) -> str:
    """Normalize Unicode and whitespace for fail-closed quote containment."""
    return _WHITESPACE_RE.sub(
        " ", unicodedata.normalize("NFKC", value).casefold()
    ).strip()


def _bounded_score(value: Any) -> float:
    """Return a finite float bounded to 0..1."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(1.0, max(0.0, number))


def _finite_nonnegative(value: Any) -> float:
    """Convert an optional score into a finite non-negative float."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, number)


def _optional_nonnegative_int(value: Any) -> int | None:
    """Parse a nonnegative integer or return None."""
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _safe_warning_list(value: Any) -> list[str]:
    """Normalize a warning string/list without persisting non-string objects."""
    if isinstance(value, str):
        parts = [part.strip() for part in value.splitlines() if part.strip()]
    elif isinstance(value, list):
        parts = [
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
    else:
        parts = []
    return list(dict.fromkeys(part[:500] for part in parts))[:100]
