"""Deterministic feature extraction and source-grounded FTO helpers.

These routines identify review candidates and preserve source spans. They do not
make legal conclusions, infer that a feature is absent, or determine infringement.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureCandidate:
    title: str
    source_quote: str
    page_index: int
    start_offset: int
    end_offset: int
    feature_key: str
    confidence: float


_FEATURE_CUES = re.compile(
    r"\b(?:compris(?:e|es|ing)|include(?:s|d)?|configured to|adapted to|"
    r"module|processor|sensor|controller|circuit|memory|interface|"
    r"transmit(?:s|ting)?|receiv(?:e|es|ing)|generate(?:s|d)?|"
    r"detect(?:s|ing)?|measure(?:s|ment)?|display(?:s|ing)?|"
    r"connect(?:s|ed|ing)?|communicat(?:e|es|ion)|mechanism|assembly|"
    r"subsystem|component|system|device|apparatus|housing|power supply|"
    r"input|output|data|feature|requirement|shall|must|wherein)\b",
    re.IGNORECASE,
)
_STRUCTURED_PREFIX = re.compile(r"^\s*(?:[-*•▪]|\(?\d{1,3}[.)]|[A-Z][.)])\s+")


def extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    """Compatibility wrapper around the shared bounded OCR-capable extractor."""
    from indexing.pdf_extraction import extract_pdf_pages as extract_bounded_pdf_pages

    return extract_bounded_pdf_pages(pdf_bytes).pages


def _candidate_spans(page_text: str) -> Iterable[tuple[int, int, str]]:
    """Split page text into bounded source-preserving sentence/list candidates."""
    # Keep page-relative offsets stable. Sentence splitting is only a candidate
    # generator; every resulting item is explicitly marked for attorney review.
    paragraph_spans = list(re.finditer(r"[^\n]+", page_text))
    for paragraph in paragraph_spans:
        raw = paragraph.group(0)
        if not raw.strip():
            continue
        # Splitting at semicolons first preserves claim-like item boundaries;
        # otherwise split conservatively on sentence-ending punctuation.
        boundaries = [m.end() for m in re.finditer(r";|(?<=[.!?])\s+(?=[A-Z0-9•*\-(])", raw)]
        cursor = 0
        ranges: list[tuple[int, int]] = []
        for boundary in boundaries:
            if boundary > cursor:
                ranges.append((cursor, boundary))
                cursor = boundary
        if cursor < len(raw):
            ranges.append((cursor, len(raw)))
        for local_start, local_end in ranges:
            raw_piece = raw[local_start:local_end]
            leading = len(raw_piece) - len(raw_piece.lstrip())
            trailing = len(raw_piece.rstrip())
            start = paragraph.start() + local_start + leading
            end = paragraph.start() + local_start + trailing
            quote = page_text[start:end]
            if len(quote) < 28 or len(quote) > 900:
                continue
            # Prefer explicit engineering/requirement terms and list entries.
            if not (_FEATURE_CUES.search(quote) or _STRUCTURED_PREFIX.search(quote)):
                continue
            yield start, end, quote


def decompose_product_specification(pages: list[str], max_candidates: int = 300) -> list[FeatureCandidate]:
    """Return heuristic feature candidates tied to exact page quotations."""
    candidates: list[FeatureCandidate] = []
    seen: set[str] = set()
    for page_index, page_text in enumerate(pages):
        for start, end, quote in _candidate_spans(page_text or ""):
            normalized = re.sub(r"\s+", " ", quote).strip()
            fingerprint = hashlib.sha256(
                f"{page_index}:{start}:{end}:{normalized}".encode("utf-8")
            ).hexdigest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            title = normalized
            if len(title) > 236:
                title = title[:233].rstrip() + "..."
            cue_hits = len(_FEATURE_CUES.findall(quote))
            confidence = min(0.90, 0.45 + (0.08 * min(cue_hits, 5)))
            candidates.append(
                FeatureCandidate(
                    title=title,
                    source_quote=quote,
                    page_index=page_index,
                    start_offset=start,
                    end_offset=end,
                    feature_key=fingerprint,
                    confidence=confidence,
                )
            )
            if len(candidates) >= max_candidates:
                return candidates
    return candidates


def split_claim_limitations(claim_text: str) -> list[tuple[int, int, str, str]]:
    """Split claim text on clear clause boundaries and retain claim-relative offsets.

    The output is a suggested segmentation only. When a claim has no clear
    delimiter, it remains one limitation rather than fabricating finer structure.
    """
    if not claim_text or not claim_text.strip():
        return []
    raw = claim_text
    candidates: list[tuple[int, int]] = []
    semicolons = list(re.finditer(r";", raw))
    if semicolons:
        cursor = 0
        for match in semicolons:
            candidates.append((cursor, match.end()))
            cursor = match.end()
        if cursor < len(raw):
            candidates.append((cursor, len(raw)))
        method = "SEMICOLON_CLAUSES"
    else:
        # A second level is used only for explicit transition clauses.
        transitions = list(re.finditer(r",\s*(?=(?:wherein|whereby|such that|in which|configured to)\b)", raw, re.IGNORECASE))
        if transitions:
            cursor = 0
            for match in transitions:
                candidates.append((cursor, match.start()))
                cursor = match.start() + match.group(0).find(",") + 1
            if cursor < len(raw):
                candidates.append((cursor, len(raw)))
            method = "TRANSITION_CLAUSES"
        else:
            candidates = [(0, len(raw))]
            method = "WHOLE_CLAIM_UNSPLIT"

    output: list[tuple[int, int, str, str]] = []
    for start, end in candidates:
        segment = raw[start:end]
        leading = len(segment) - len(segment.lstrip())
        trailing = len(segment.rstrip())
        source_start = start + leading
        source_end = start + trailing
        text = raw[source_start:source_end]
        if text:
            output.append((source_start, source_end, text, method))
    return output or [(0, len(raw), raw, "WHOLE_CLAIM_UNSPLIT")]


def audit_fto_event(*, tenant, actor, action: str, resource_type: str, resource_id, metadata: dict | None = None, outcome: str = "SUCCESS") -> None:
    """Write a minimal tenant audit event; never log source text or file content."""
    from audit.models import AuditLog

    try:
        AuditLog.objects.create(
            tenant=tenant,
            actor_user_id=actor.pk,
            action=action[:100],
            resource_type=resource_type[:100],
            resource_id=resource_id,
            outcome=outcome,
            metadata=metadata or {},
        )
    except Exception:
        # Audit failure must not make an already-committed workflow look failed.
        logger.exception("FTO audit write failed (action=%s, resource_type=%s).", action, resource_type)


# Kept deliberately small and versioned: this is a transparent lexical candidate
# generator, not semantic claim construction or a legal opinion.
_MATCH_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "by", "with",
    "from", "at", "as", "is", "are", "was", "were", "be", "being", "been",
    "comprising", "comprise", "comprises", "wherein", "whereby", "thereof", "therein",
    "said", "such", "each", "one", "more", "at", "least", "plurality", "configured",
    "adapted", "according", "claim", "claims", "feature", "features",
    # Generic claim categories are not evidence of a product-feature match.
    # Keep specific technical nouns (e.g. sensor, processor, circuit) searchable.
    "device", "system", "apparatus", "method",
})
_MATCH_TOKEN = re.compile(r"[^\W_]{2,}", re.UNICODE)


def _match_tokens(value: str) -> set[str]:
    return {
        token for token in _MATCH_TOKEN.findall((value or "").casefold())
        if token not in _MATCH_STOP_WORDS and len(token) <= 48
    }


def rank_product_features_for_limitation(
    limitation_text: str,
    features: Iterable[object],
    *,
    max_results: int = 10,
) -> list[dict]:
    """Rank source features by weighted lexical overlap with a claim limitation.

    Scores are a deterministic retrieval aid (weighted F1), not a probability,
    claim construction, element-by-element comparison, infringement opinion, or
    absence-of-feature conclusion. Product feature source quotations are retained
    by the caller and must be reviewed before they can be linked as evidence.
    """
    if not isinstance(max_results, int) or max_results < 1 or max_results > 50:
        raise ValueError("max_results must be between 1 and 50")
    limitation_terms = _match_tokens(limitation_text)
    if not limitation_terms:
        return []

    feature_rows = []
    document_frequency: dict[str, int] = {}
    for feature in features:
        text = " ".join((
            str(getattr(feature, "title", "") or ""),
            str(getattr(feature, "description", "") or ""),
            str(getattr(feature, "source_quote", "") or ""),
        ))
        terms = _match_tokens(text)
        if not terms:
            continue
        feature_rows.append((feature, terms))
        for token in terms:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    if not feature_rows:
        return []

    from math import log

    total_docs = len(feature_rows)
    def weight(token: str) -> float:
        # Smoothed inverse feature frequency makes rare technical terms more
        # informative without allowing a token to have an unbounded weight.
        return 1.0 + log((total_docs + 1.0) / (document_frequency.get(token, 0) + 1.0))

    query_weight = sum(weight(token) for token in sorted(limitation_terms))
    ranked: list[dict] = []
    for feature, feature_terms in feature_rows:
        overlap = limitation_terms & feature_terms
        if not overlap:
            continue
        overlap_weight = sum(weight(token) for token in sorted(overlap))
        feature_weight = sum(weight(token) for token in sorted(feature_terms))
        precision = overlap_weight / feature_weight if feature_weight else 0.0
        coverage = overlap_weight / query_weight if query_weight else 0.0
        score = (2.0 * precision * coverage / (precision + coverage)) if (precision + coverage) else 0.0
        matched_terms = sorted(overlap, key=lambda token: (-weight(token), token))[:20]
        if score <= 0.0:
            continue
        ranked.append({
            "feature": feature,
            "score": round(min(1.0, max(0.0, score)), 6),
            "limitation_coverage": round(min(1.0, max(0.0, coverage)), 6),
            "matched_terms": matched_terms,
            "rationale": (
                "Lexical retrieval candidate based on shared terms: "
                + ", ".join(matched_terms)
                + ". This ranking is not an element-by-element claim analysis or legal conclusion."
            ),
        })
    ranked.sort(key=lambda row: (-row["score"], -row["limitation_coverage"], str(getattr(row["feature"], "pk", ""))))
    return ranked[:max_results]
