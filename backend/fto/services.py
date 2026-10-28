"""Deterministic feature extraction and source-grounded FTO helpers.

These routines identify review candidates and preserve source spans. They do not
make legal conclusions, infer that a feature is absent, or determine infringement.
"""
from __future__ import annotations

import hashlib
import io
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
    """Extract selectable text using installed PDF libraries, without OCR."""
    failures: list[Exception] = []
    blank_pages: list[str] | None = None
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        pages = [(page.extract_text() or "") for page in reader.pages]
        if any(page.strip() for page in pages):
            return pages
        if pages:
            blank_pages = pages
        failures.append(ValueError("pypdf found no selectable text."))
    except Exception as exc:  # pypdf may reject malformed or encrypted documents
        failures.append(exc)

    try:
        import fitz  # PyMuPDF
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            pages = [(page.get_text("text") or "") for page in document]
        if any(page.strip() for page in pages):
            return pages
        if pages:
            blank_pages = pages
        failures.append(ValueError("PyMuPDF found no selectable text."))
    except Exception as exc:
        failures.append(exc)

    if blank_pages is not None:
        return blank_pages
    raise ValueError("The PDF could not be opened for text extraction.") from (failures[-1] if failures else None)


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
