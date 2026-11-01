"""Fail-closed verification of cited evidence against tenant-owned source chunks."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

GUARDRAIL_WARNING = (
    "Fail-Closed: Cited quote could not be verified against source document text."
)
_WHITESPACE_RE = re.compile(r"\s+")


class GroundednessGuardrail:
    """Verify every assessment quote against its canonical tenant-owned chunk.

    A cited quote is valid only if the chunk exists inside ``tenant_id``, the
    normalized quote is a substring of the stored chunk text, and any supplied
    patent/version identifiers agree with the canonical chunk. Database errors
    fail closed for all citations in the current invocation.
    """

    @classmethod
    def verify_assessments(
        cls,
        assessments: list[dict[str, Any]],
        tenant_id: str | UUID,
        actor_user_id: str | UUID,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Return copied assessments with quote verification metadata and warnings."""
        updated: list[dict[str, Any]] = []
        invalid_evidence_containers: set[int] = set()
        for assessment_index, raw_assessment in enumerate(assessments):
            if not isinstance(raw_assessment, Mapping):
                assessment: dict[str, Any] = {}
                invalid_evidence_containers.add(assessment_index)
            else:
                assessment = dict(raw_assessment)
            raw_evidence = assessment.get("evidence", [])
            if not isinstance(raw_evidence, list):
                # A malformed evidence container cannot be treated as a clean
                # no-citation assessment, so fail closed for this assessment.
                raw_evidence = []
                invalid_evidence_containers.add(assessment_index)
            assessment["evidence"] = [
                dict(item) if isinstance(item, Mapping) else item
                for item in raw_evidence
            ]
            updated.append(assessment)

        warnings: list[str] = []
        cited_ids: set[UUID] = set()
        invalid_evidence: set[tuple[int, int]] = set()

        # Identify malformed or missing references first. A malformed ID cannot
        # safely be queried as a UUID primary key and is therefore unverified.
        for assessment_index, assessment in enumerate(updated):
            evidence_items = assessment.get("evidence", [])
            if not isinstance(evidence_items, list):
                assessment["evidence"] = []
                evidence_items = []
            for evidence_index, evidence in enumerate(evidence_items):
                if not isinstance(evidence, dict):
                    invalid_evidence.add((assessment_index, evidence_index))
                    continue
                chunk_id = evidence.get("chunk_id")
                quote = evidence.get("exact_quote")
                if not chunk_id:
                    invalid_evidence.add((assessment_index, evidence_index))
                else:
                    try:
                        # Query every parseable cited chunk ID, even when its
                        # quote is missing; the citation still must be checked.
                        cited_ids.add(UUID(str(chunk_id)))
                    except (TypeError, ValueError, AttributeError):
                        invalid_evidence.add((assessment_index, evidence_index))
                if not isinstance(quote, str) or not quote.strip():
                    invalid_evidence.add((assessment_index, evidence_index))

        chunks_by_id: dict[str, dict[str, Any]] = {}
        database_failed = False
        if cited_ids:
            try:
                # Import lazily so importing graph/node modules does not access
                # Django models before the app registry is initialized.
                from django.contrib.auth import get_user_model
                from enterprise_identity.services import filter_visible_matter_queryset
                from indexing.models import TextChunk
                from patents.models import Patent
                from tenants.models import Tenant

                tenant = Tenant.objects.get(pk=tenant_id)
                actor = get_user_model().objects.get(pk=actor_user_id)
                if not actor.is_active:
                    raise PermissionError("Analysis actor account is inactive.")
                visible_patent_ids = filter_visible_matter_queryset(
                    Patent.objects.for_tenant(tenant_id), tenant=tenant, user=actor,
                    matter_type="patent",
                ).values_list("pk", flat=True)
                rows = (
                    TextChunk.objects.for_tenant(tenant_id)
                    .filter(pk__in=list(cited_ids), patent_id__in=visible_patent_ids)
                    .values(
                        "id",
                        "tenant_id",
                        "patent_id",
                        "document_version_id",
                        "text",
                    )
                )
                chunks_by_id = {str(row["id"]): row for row in rows}
            except Exception as exc:  # fail closed; never leak text into logs
                database_failed = True
                logger.error(
                    "Groundedness lookup failed; citations will fail closed "
                    "(error_type=%s).",
                    type(exc).__name__,
                )

        for assessment_index, assessment in enumerate(updated):
            evidence_items = assessment.get("evidence", [])
            has_citations = bool(evidence_items)
            assessment_failed = assessment_index in invalid_evidence_containers
            all_verified = has_citations and not assessment_failed
            if assessment_failed:
                warnings.append(_warning_for(assessment, None))

            for evidence_index, evidence in enumerate(evidence_items):
                if not isinstance(evidence, dict):
                    all_verified = False
                    assessment_failed = True
                    warnings.append(_warning_for(assessment, None))
                    continue

                key = (assessment_index, evidence_index)
                chunk_id = evidence.get("chunk_id")
                quote = evidence.get("exact_quote")
                row = None
                if not database_failed and key not in invalid_evidence and chunk_id:
                    try:
                        row = chunks_by_id.get(str(UUID(str(chunk_id))))
                    except (TypeError, ValueError, AttributeError):
                        row = None

                verified = False
                if row is not None and isinstance(quote, str) and quote.strip():
                    canonical_text = _normalize_for_match(str(row.get("text", "")))
                    normalized_quote = _normalize_for_match(quote)
                    metadata_matches = _source_metadata_matches(evidence, row)
                    verified = bool(
                        normalized_quote
                        and normalized_quote in canonical_text
                        and metadata_matches
                    )

                evidence["quote_verified"] = verified
                if not verified:
                    all_verified = False
                    assessment_failed = True
                    warnings.append(_warning_for(assessment, evidence))

            if assessment_failed:
                assessment["quote_verified"] = False
            else:
                assessment["quote_verified"] = all_verified if has_citations else None
            if assessment_failed:
                assessment["status"] = "UNCERTAIN"
                # A quote that cannot be verified cannot support the original
                # confidence estimate; fail closed before the scoring node.
                assessment["confidence"] = 0.0
                old_explanation = str(assessment.get("explanation", "")).strip()
                if GUARDRAIL_WARNING not in old_explanation:
                    assessment["explanation"] = (
                        f"{old_explanation}\n\n{GUARDRAIL_WARNING}".strip()
                    )

        # Warnings can be repeated for duplicated evidence IDs. Preserve order,
        # but avoid noisy duplicate messages in state.error_message.
        return updated, list(dict.fromkeys(warnings))


def _normalize_for_match(text: str) -> str:
    """Normalize Unicode and whitespace while preserving punctuation and words."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _source_metadata_matches(
    evidence: Mapping[str, Any],
    row: Mapping[str, Any],
) -> bool:
    """Reject a valid quote paired with mismatched source identifiers."""
    expected = (
        ("source_patent_id", "patent_id"),
        ("source_version_id", "document_version_id"),
    )
    for evidence_key, row_key in expected:
        supplied = evidence.get(evidence_key)
        if supplied is not None and str(supplied) != str(row.get(row_key)):
            return False
    return True


def _warning_for(
    assessment: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
) -> str:
    """Create a safe diagnostic without including query text or the quote."""
    limitation_id = str(assessment.get("limitation_id", "unknown"))
    evidence_id = str(evidence.get("evidence_id", "unknown")) if evidence else "unknown"
    return (
        f"{GUARDRAIL_WARNING} limitation_id={limitation_id}; "
        f"evidence_id={evidence_id}."
    )
