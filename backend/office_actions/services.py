"""Deterministic source extraction and evidence-bound prosecution helpers.

This module does not make legal conclusions or call an LLM. Every extracted
issue is flagged for professional review and retains a verbatim source span.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any

from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from indexing.models import TextChunk
from office_actions.models import (
    AmendmentSupportCheck,
    CitedReference,
    OfficeAction,
    OfficeActionReview,
    Rejection,
    RejectionReference,
    ResponseDraft,
    ResponseDraftRevision,
)
from patents.models import Claim, PatentDocumentVersion


GROUND_PATTERNS: dict[str, tuple[re.Pattern[str], float]] = {
    "US_101": (re.compile(r"\b(?:35\s+U\.?\s*S\.?\s*C\.?\s*(?:§|section)?\s*101|(?:§|section)\s*101\b|101\s+rejection)\b", re.I), 0.96),
    "US_102": (re.compile(r"\b(?:35\s+U\.?\s*S\.?\s*C\.?\s*(?:§|section)?\s*102|(?:§|section)\s*102\b|102\s+rejection)\b", re.I), 0.96),
    "US_103": (re.compile(r"\b(?:35\s+U\.?\s*S\.?\s*C\.?\s*(?:§|section)?\s*103|(?:§|section)\s*103\b|103\s+rejection)\b", re.I), 0.96),
    "US_112": (re.compile(r"\b(?:35\s+U\.?\s*S\.?\s*C\.?\s*(?:§|section)?\s*112|(?:§|section)\s*112\b|112\s+rejection)\b", re.I), 0.96),
    "EP_52": (re.compile(r"\b(?:article|art\.?|rule)\s*52\s*(?:of\s+the\s+)?EPC\b", re.I), 0.91),
    "EP_54": (re.compile(r"\b(?:article|art\.?|rule)\s*54\s*(?:of\s+the\s+)?EPC\b", re.I), 0.91),
    "EP_56": (re.compile(r"\b(?:article|art\.?|rule)\s*56\s*(?:of\s+the\s+)?EPC\b", re.I), 0.91),
    "PCT": (re.compile(r"\b(?:PCT\s+(?:rule|article|written opinion)|international preliminary report)\b", re.I), 0.78),
    "FORMALITY": (re.compile(r"\b(?:formal(?:ity)? objection|objection as to form|formal requirement)\b", re.I), 0.72),
}

# Patent-style identifiers only. The parser never fetches metadata or claims a
# match is a valid prior-art reference; it records what appears in the source.
_REFERENCE_RE = re.compile(
    r"(?<![A-Z0-9])(?P<ref>(?:(?:US|WO|EP|JP|CN|KR)\s*)?(?:20\d{2}\s*/\s*\d{5,8}|\d{1,3}(?:,\d{3}){2}|\d{7,10})(?:\s*[A-Z]\d?)?)(?![A-Z0-9])",
    re.I,
)
_CLAIM_LABEL_RE = re.compile(
    r"\bclaims?\s+([0-9][0-9\s,;\-–—andto]{0,100})(?=\s+(?:are|is|stand|stands|were|was|have|has|under|rejected|objected|unpatentable|patentable|based|because)|[.;:\n]|$)",
    re.I,
)


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _fingerprint(*parts: str) -> str:
    payload = "\x1f".join(_normalize_space(part).casefold() for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_claim_numbers(text: str) -> list[str]:
    match = _CLAIM_LABEL_RE.search(text)
    if not match:
        return []
    fragment = match.group(1).replace("–", "-").replace("—", "-")
    fragment = re.sub(r"\band\b|\bto\b", ",", fragment, flags=re.I)
    claims: set[int] = set()
    for part in re.split(r"[,;\s]+", fragment):
        if not part:
            continue
        if "-" in part:
            limits = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
            if limits:
                low, high = int(limits.group(1)), int(limits.group(2))
                if low <= high and high - low <= 100:
                    claims.update(range(low, high + 1))
            continue
        if part.isdigit():
            claims.add(int(part))
    return [str(number) for number in sorted(claims)]


def parse_rejections(pages: Sequence[str], jurisdiction: str = "US") -> list[dict[str, Any]]:
    """Extract likely rejection passages with precise source offsets.

    Offsets are zero-based character positions within the identified PDF page.
    Quotes themselves are exact slices from the source page.
    Broad matches intentionally remain attorney-review items.
    """
    results: list[dict[str, Any]] = []
    jurisdiction = (jurisdiction or "US").upper()
    jurisdiction_keys = {
        "US": {"US_101", "US_102", "US_103", "US_112", "PCT", "FORMALITY"},
        "EP": {"EP_52", "EP_54", "EP_56", "PCT", "FORMALITY"},
        "EPO": {"EP_52", "EP_54", "EP_56", "PCT", "FORMALITY"},
        "PCT": {"PCT", "US_101", "US_102", "US_103", "US_112", "FORMALITY"},
    }.get(jurisdiction, set(GROUND_PATTERNS))
    for page_index, page in enumerate(pages):
        text = page or ""
        matches: list[tuple[int, int, str, float]] = []
        for ground, (pattern, confidence) in GROUND_PATTERNS.items():
            if ground not in jurisdiction_keys:
                continue
            for found in pattern.finditer(text):
                matches.append((found.start(), found.end(), ground, confidence))
        matches.sort(key=lambda item: item[0])
        for match_index, (start, end, ground, confidence) in enumerate(matches):
            # Capture the nearby source passage, with bounded length so the API
            # and generated draft do not expose an entire office-action page.
            quote_start = max(0, start - 350)
            quote_end = min(len(text), max(end + 850, start + 250))
            if quote_end - quote_start > 1400:
                quote_end = quote_start + 1400
            raw_quote = text[quote_start:quote_end]
            leading = len(raw_quote) - len(raw_quote.lstrip())
            trimmed = raw_quote.strip()
            quote_start += leading
            quote_end = quote_start + len(trimmed)
            quote = trimmed
            if not quote:
                continue
            claim_numbers = _parse_claim_numbers(text[max(0, start - 300):min(len(text), end + 500)])
            fingerprint = _fingerprint(ground, quote, str(page_index + 1))
            results.append({
                "ground": ground,
                "claim_numbers": claim_numbers,
                "examiner_reasoning": quote,
                "source_page": page_index + 1,
                "source_start_offset": quote_start,
                "source_end_offset": quote_end,
                "source_quote": quote,
                "parser_confidence": confidence,
                "needs_attorney_review": True,
                "fingerprint": fingerprint,
            })
    # Identical overlap windows can occur where a document repeats the same
    # ground heading on a single page; collapse only exact fingerprints.
    unique: dict[str, dict[str, Any]] = {}
    for item in results:
        unique.setdefault(item["fingerprint"], item)
    return list(unique.values())


def _candidate_reference_matches(text: str):
    for match in _REFERENCE_RE.finditer(text):
        raw = match.group("ref").strip()
        digits = re.sub(r"\D", "", raw)
        has_country = bool(re.match(r"(?i)^(US|WO|EP|JP|CN|KR)\s*", raw))
        has_publication_year = bool(re.search(r"20\d{2}\s*/", raw))
        has_commas = "," in raw
        has_kind_code = bool(re.search(r"\s+[A-Z]\d?$", raw, re.I))
        if not (has_country or has_publication_year or has_commas or has_kind_code):
            continue
        if len(digits) < 6:
            continue
        yield match


def parse_cited_references(pages: Sequence[str]) -> list[dict[str, Any]]:
    """Find patent-number-like identifiers and preserve their exact contexts."""
    results: dict[str, dict[str, Any]] = {}
    for page_index, page in enumerate(pages):
        text = page or ""
        for match in _candidate_reference_matches(text):
            raw = _normalize_space(match.group("ref"))
            identifier = re.sub(r"\s*/\s*", "/", raw.upper())
            identifier = re.sub(r"\s+", " ", identifier)
            start = max(0, match.start() - 140)
            end = min(len(text), match.end() + 180)
            quote = text[start:end].strip()
            fingerprint = _fingerprint(identifier)
            results.setdefault(identifier.casefold(), {
                "identifier": identifier,
                "title": "",
                "assignee_or_author": "",
                "source_page": page_index + 1,
                "source_start_offset": match.start(),
                "source_end_offset": match.end(),
                "source_quote": quote,
                "fingerprint": fingerprint,
                "metadata": {"extraction_method": "patent_identifier_pattern", "verified_against_external_source": False},
            })
    return list(results.values())


def parse_office_action(office_action: OfficeAction) -> dict[str, int]:
    """Persist deterministic rejection/reference extraction idempotently."""
    pages = office_action.extracted_pages or office_action.extracted_text.split("\n")
    parsed_rejections = parse_rejections(pages, office_action.jurisdiction)
    parsed_references = parse_cited_references(pages)
    with transaction.atomic():
        rejection_rows: list[Rejection] = []
        for values in parsed_rejections:
            row, _created = Rejection.objects.get_or_create(
                tenant_id=office_action.tenant_id,
                office_action=office_action,
                fingerprint=values["fingerprint"],
                defaults={key: value for key, value in values.items() if key != "fingerprint"},
            )
            rejection_rows.append(row)
        reference_rows: list[CitedReference] = []
        for values in parsed_references:
            row, _created = CitedReference.objects.get_or_create(
                tenant_id=office_action.tenant_id,
                office_action=office_action,
                identifier=values["identifier"],
                defaults={key: value for key, value in values.items()},
            )
            reference_rows.append(row)
        # Link only identifiers inside the exact retained issue window. This
        # avoids linking every citation on a page to unrelated rejections.
        for rejection in rejection_rows:
            for reference in reference_rows:
                inside_span = (
                    rejection.source_start_offset is not None
                    and rejection.source_end_offset is not None
                    and rejection.source_start_offset <= reference.source_start_offset <= rejection.source_end_offset
                )
                if not inside_span:
                    continue
                RejectionReference.objects.get_or_create(
                    tenant_id=office_action.tenant_id,
                    rejection=rejection,
                    cited_reference=reference,
                    defaults={"relationship": "EXAMINER_CITED", "source_quote": reference.source_quote, "source_page": reference.source_page},
                )
        office_action.parse_status = OfficeAction.ParseStatus.NEEDS_REVIEW if parsed_rejections or parsed_references else OfficeAction.ParseStatus.COMPLETED
        office_action.save(update_fields=("parse_status", "updated_at"))
    return {"rejections": len(parsed_rejections), "cited_references": len(parsed_references)}


def create_evidence_grounded_draft(office_action: OfficeAction, user) -> tuple[ResponseDraft, ResponseDraftRevision]:
    """Create a cautious draft framework whose factual points quote source rows."""
    if office_action.extraction_status != OfficeAction.ExtractionStatus.EXTRACTED:
        raise ValueError("The office-action document must finish text extraction before draft generation.")
    if office_action.parse_status == OfficeAction.ParseStatus.PENDING:
        parse_office_action(office_action)
        office_action.refresh_from_db()

    latest_version = (
        PatentDocumentVersion.objects.for_tenant(office_action.tenant_id)
        .filter(patent_id=office_action.patent_id)
        .order_by("-version_number")
        .first()
    )
    claim_queryset = Claim.objects.for_tenant(office_action.tenant_id).filter(patent_id=office_action.patent_id)
    if latest_version:
        claim_queryset = claim_queryset.filter(document_version_id=latest_version.pk)
    claims = list(claim_queryset.order_by("claim_number")[:200])
    rejections = list(office_action.rejections.prefetch_related("reference_links__cited_reference").order_by("source_page", "created_at"))
    refs = list(office_action.cited_references.order_by("identifier"))
    # Keep a compact view of prior communications for this same tenant-owned
    # patent. This is historical context, not proof of what claims were then pending.
    prior_actions = list(
        OfficeAction.objects.for_tenant(office_action.tenant_id)
        .filter(patent_id=office_action.patent_id)
        .exclude(pk=office_action.pk)
        .prefetch_related("rejections", "cited_references")
        .order_by("-issued_date", "-created_at")[:10]
    )

    lines = [
        "# Office Action Response — Attorney Review Draft",
        "",
        "> **CONFIDENTIAL — DRAFT ONLY. NOT APPROVED FOR FILING.** This is a deterministic planning draft, not legal advice or a legal opinion. An authorized patent professional must validate the record, cited authorities, jurisdiction, deadlines, and every proposed argument before use.",
        "",
        "## Matter details",
        f"- PatentShield matter: `{office_action.patent_id}`",
        f"- Application number as entered: {office_action.application_number or 'Not provided'}",
        f"- Jurisdiction: {office_action.jurisdiction}",
        f"- Communication type: {office_action.get_action_type_display()}",
        f"- Office action document SHA-256: `{office_action.content_sha256}`",
        f"- Issued date: {office_action.issued_date.isoformat() if office_action.issued_date else 'Not recorded'}",
        f"- Response due date (unverified): {office_action.response_due_date.isoformat() if office_action.response_due_date else 'Not recorded'}",
        "",
        "## Extraction and scope note",
        f"The parser extracted {len(rejections)} candidate issue(s) and {len(refs)} patent-style reference identifier(s). Extraction is heuristic; each item remains marked for attorney verification. Reference identifiers have not been externally verified by this draft generator.",
        "",
    ]
    source_manifest: dict[str, Any] = {
        "generator": "deterministic_evidence_template_v1",
        "office_action_id": str(office_action.pk),
        "office_action_sha256": office_action.content_sha256,
        "patent_id": str(office_action.patent_id),
        "patent_document_version_id": str(latest_version.pk) if latest_version else None,
        "rejections": [],
        "cited_references": [
            {"id": str(reference.pk), "identifier": reference.identifier, "source_page": reference.source_page, "source_quote": reference.source_quote}
            for reference in refs
        ],
        "claims": [],
        "prior_prosecution_history": [],
        "limitations": ["No legal conclusion is generated.", "Detected reference identifiers are not externally verified.", "The output must be attorney-reviewed before filing.", "Historical submitted-claim versions are not assumed to match the current tenant claim record."],
    }
    if prior_actions:
        lines.extend(["## Prior prosecution-history context", "The following prior Office Action records are attached to this same patent matter. Review the original documents and any amendment/response submissions before relying on this summary. The application data model does not prove which claim version was pending on each historical date.", ""])
        for prior in prior_actions:
            prior_rejections = list(prior.rejections.order_by("source_page", "created_at"))
            prior_refs = list(prior.cited_references.order_by("identifier"))
            source_manifest["prior_prosecution_history"].append({
                "office_action_id": str(prior.pk),
                "issued_date": prior.issued_date.isoformat() if prior.issued_date else None,
                "action_type": prior.action_type,
                "content_sha256": prior.content_sha256,
                "rejections": [{"ground": row.ground, "claim_numbers": row.claim_numbers, "source_page": row.source_page, "source_quote": row.source_quote} for row in prior_rejections],
                "cited_reference_identifiers": [row.identifier for row in prior_refs],
            })
            lines.append(f"### {prior.get_action_type_display()} — {prior.issued_date.isoformat() if prior.issued_date else 'date not recorded'} (record `{prior.pk}`)")
            if prior_rejections:
                for row in prior_rejections:
                    lines.append(f"- {row.get_ground_display()} · claims {', '.join(row.claim_numbers) if row.claim_numbers else 'not parsed'} · PDF p.{row.source_page or '?'}: “{_normalize_space(row.source_quote)[:450]}”")
            else:
                lines.append("- No structured rejection records were extracted for this earlier communication.")
            lines.append("- Cited identifiers: " + (", ".join(row.identifier for row in prior_refs) if prior_refs else "none extracted") + ".")
            lines.append("")
        lines.append("Historical context must be checked against the original response amendments, interview records, and claim versions; none are inferred from this summary alone.")
    if not rejections:
        lines.extend([
            "## No structured rejection extracted",
            "The uploaded communication did not yield a recognized statutory ground using the configured deterministic patterns. Review the original PDF manually and classify each issue before drafting a response.",
            "",
        ])
    for index, rejection in enumerate(rejections, start=1):
        source_manifest["rejections"].append({
            "id": str(rejection.pk), "ground": rejection.ground,
            "claim_numbers": rejection.claim_numbers, "source_page": rejection.source_page,
            "source_quote": rejection.source_quote,
        })
        lines.extend([
            f"## Issue {index}: {rejection.get_ground_display()}",
            f"- Claims identified by parser: {', '.join(rejection.claim_numbers) if rejection.claim_numbers else 'Not confidently identified'}",
            f"- Source location: Office Action PDF page {rejection.source_page or 'not identified'}",
            f"- Parser confidence: {rejection.parser_confidence:.2f} (heuristic; not a probability of correctness)",
            "",
            "### Examiner passage (verbatim source excerpt)",
            "> " + rejection.source_quote.replace("\n", "\n> "),
            "",
            "### Response-planning framework — not a finding",
        ])
        framework = {
            "US_101": ["Identify the exact eligibility rationale stated by the examiner.", "Map each challenged claim limitation to the claim as actually pending and the specification disclosure.", "Develop any response only after verifying the examiner's reasoning and relevant controlling authority."],
            "US_102": ["Compare each limitation of each identified claim with the exact disclosure attributed to the cited reference.", "Do not assert novelty or lack of anticipation without reviewing the cited reference itself and the examiner's mapping.", "Record any missing, differently arranged, or unsupported limitation only with pinpoint source citations."],
            "US_103": ["Map the examiner's stated combination of references and rationale to the limitations of each claim.", "Verify the actual teachings of each cited document and the stated reason for combination before developing arguments.", "Do not infer motivation, reasonable expectation, or technical effect from citation metadata alone."],
            "US_112": ["Identify whether the cited passage concerns definiteness, written description, enablement, or another stated §112 issue.", "Quote the exact claim language and compare it against the relevant specification disclosure.", "Any proposed amendment requires a separate textual support check and attorney assessment."],
            "EP_52": ["Confirm the examiner's precise EPC Article 52 objection and affected claim language.", "Tie any response to verified technical features and the examiner's stated reasoning."],
            "EP_54": ["For each affected claim, compare the full claim feature set with the examiner-identified disclosure in each cited document.", "Do not state a novelty conclusion until the original reference and pinpoint passages are reviewed."],
            "EP_56": ["Identify the closest prior art and the problem/rationale as actually stated in the communication.", "Evaluate claimed differences and the examiner's reasoning using verified source passages; do not infer facts from a publication number alone."],
            "PCT": ["Confirm the specific PCT rule/article and whether this is an international search opinion or another communication.", "Separate non-binding search observations from any later jurisdiction-specific examination requirements."],
            "FORMALITY": ["Identify the exact formality requested by the examiner and the deadline/instructions in the original communication.", "Prepare a procedural response only after confirming the current filing requirements."],
            "OTHER": ["Classify the issue from the exact communication text and identify the applicable jurisdiction-specific rule before proposing substantive arguments."],
        }.get(rejection.ground, ["Classify and verify the issue before proposing arguments."])
        lines.extend([f"- {item}" for item in framework])
        lines.extend(["", "### Examiner-cited references detected near this issue"])
        links = list(rejection.reference_links.all())
        if links:
            for link in links:
                reference = link.cited_reference
                lines.append(f"- `{reference.identifier}` — source page {reference.source_page or 'not identified'}; extracted context: “{_normalize_space(reference.source_quote)[:700]}” (not externally verified).")
        else:
            lines.append("- No patent-style identifier was linked to this issue by the parser. Review the original examiner citation list.")
        matched_claims = [claim for claim in claims if str(claim.claim_number).strip() in {str(value).strip() for value in rejection.claim_numbers}]
        lines.extend(["", "### Current tenant-record claim text"])
        if matched_claims:
            for claim in matched_claims:
                source_manifest["claims"].append({"id": str(claim.pk), "claim_number": claim.claim_number, "document_version_id": str(claim.document_version_id), "claim_text_sha256": hashlib.sha256(claim.claim_text.encode("utf-8")).hexdigest()})
                lines.append(f"**Claim {claim.claim_number}** (patent document version `{claim.document_version_id}`): {claim.claim_text}")
        else:
            lines.append("No matching extracted claim record was found for the parser's claim list. Import/verify the pending claims before drafting a claim-specific argument.")
        lines.extend(["", "### Evidence gaps before any argument is finalized", "- The cited-reference identifiers and excerpts come from this office-action PDF; full prior-art documents were not retrieved or verified by this workflow.", "- Check whether prior amendments, interview summaries, declarations, and earlier office actions affect the response.", "- Confirm the response deadline and any extension/payment requirements from authoritative matter records.", ""])

    lines.extend([
        "## Proposed response structure",
        "1. Confirm applicant, application number, jurisdiction, communication date, response period, and the set of claims currently pending.",
        "2. Address each identified rejection/objection separately, using verified claim language and pinpoint citations to the actual references and specification.",
        "3. State any amendment only after documenting literal disclosure support and reviewing added-matter/new-matter constraints for the relevant jurisdiction.",
        "4. Reconcile the proposed response with the complete prosecution history and have an authorized practitioner approve the final filing text.",
        "",
        "## Filing safeguard",
        "This system does not file, transmit, or submit responses to patent offices. The text above is an issue map and drafting framework; it is not a final response. No argument about what a cited reference teaches is asserted without the reference's actual text.",
    ])
    content = "\n".join(lines)

    with transaction.atomic():
        draft = ResponseDraft.objects.create(
            tenant_id=office_action.tenant_id,
            office_action=office_action,
            title=f"Response draft — {office_action.application_number or office_action.patent.title[:100]}",
            created_by=user,
            current_revision_number=1,
            status=ResponseDraft.Status.DRAFT,
        )
        revision = ResponseDraftRevision.objects.create(
            tenant_id=office_action.tenant_id,
            draft=draft,
            revision_number=1,
            content_markdown=content,
            source_manifest=source_manifest,
            generation_method="evidence_template",
            generated_by=user,
        )
        AuditLog.objects.create(
            tenant_id=office_action.tenant_id,
            actor_user_id=user.pk,
            action="OFFICE_ACTION_DRAFT_GENERATED",
            resource_type="ResponseDraft",
            resource_id=draft.pk,
            outcome=AuditLog.Outcome.SUCCESS,
            metadata={"office_action_id": str(office_action.pk), "revision_id": str(revision.pk), "source_count": len(source_manifest["rejections"]) + len(source_manifest["cited_references"])},
        )
    return draft, revision


def check_amendment_support(office_action: OfficeAction, claim_number: str, proposed_text: str, user) -> AmendmentSupportCheck:
    """Check literal phrase presence in indexed specification chunks only.

    A positive result means text was found, not that an amendment is legally
    supported, enabled, or permissible in the relevant jurisdiction.
    """
    proposed_text = proposed_text.strip()
    latest_version = PatentDocumentVersion.objects.for_tenant(office_action.tenant_id).filter(
        patent_id=office_action.patent_id,
    ).order_by("-version_number").first()
    manifest: dict[str, Any] = {"method": "literal_specification_chunk_search_v1", "matches": [], "disclaimer": "Literal text overlap is not a legal written-description, added-matter, or enablement determination."}
    status = AmendmentSupportCheck.Status.NOT_CHECKED
    explanation = "No indexed SPECIFICATION chunks were available; no support conclusion was made."
    matched_chunk = None
    if latest_version:
        chunks = TextChunk.objects.for_tenant(office_action.tenant_id).filter(
            patent_id=office_action.patent_id,
            patent__tenant_id=office_action.tenant_id,
            document_version_id=latest_version.pk,
            document_version__tenant_id=office_action.tenant_id,
            document_version__patent_id=office_action.patent_id,
            section_type=TextChunk.SectionType.SPECIFICATION,
        ).only("id", "text", "page_start", "page_end", "document_version_id")[:1000]
        chunks = list(chunks)
        if chunks and proposed_text:
            def normalize(value: str) -> str:
                return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
            normalized_proposed = normalize(proposed_text)
            candidate_phrases = [part.strip() for part in re.split(r"[;,\n]+", proposed_text) if len(part.strip()) >= 18]
            if not candidate_phrases:
                candidate_phrases = [proposed_text]
            match_records = []
            for chunk in chunks:
                normalized_chunk = normalize(chunk.text)
                exact = normalized_proposed and normalized_proposed in normalized_chunk
                hit_phrases = [phrase for phrase in candidate_phrases if normalize(phrase) in normalized_chunk]
                if exact:
                    status = AmendmentSupportCheck.Status.SUPPORTED
                    matched_chunk = chunk
                    match_records.append({"chunk_id": str(chunk.pk), "page_start": chunk.page_start, "page_end": chunk.page_end, "matching_text": proposed_text[:500], "match_type": "full_text"})
                    break
                if hit_phrases:
                    matched_chunk = chunk
                    match_records.append({"chunk_id": str(chunk.pk), "page_start": chunk.page_start, "page_end": chunk.page_end, "matching_text": hit_phrases[0][:500], "match_type": "phrase"})
            if status != AmendmentSupportCheck.Status.SUPPORTED:
                status = AmendmentSupportCheck.Status.PARTIAL if match_records else AmendmentSupportCheck.Status.NOT_FOUND
            manifest["matches"] = match_records[:10]
            manifest["document_version_id"] = str(latest_version.pk)
            if status == AmendmentSupportCheck.Status.SUPPORTED:
                explanation = "The proposed text was found verbatim (after punctuation/whitespace normalization) in an indexed specification chunk. This is not a legal sufficiency determination."
            elif status == AmendmentSupportCheck.Status.PARTIAL:
                explanation = "Some proposed phrase text was found in specification chunks, but the full proposed text was not found verbatim. A practitioner must assess the underlying disclosure."
            else:
                explanation = "The proposed text was not found verbatim in the available indexed specification chunks. This is not proof that the disclosure lacks support; review the full source document."
    return AmendmentSupportCheck.objects.create(
        tenant_id=office_action.tenant_id,
        office_action=office_action,
        checked_document_version=latest_version,
        claim_number=str(claim_number).strip(),
        proposed_claim_text=proposed_text,
        status=status,
        source_manifest=manifest,
        explanation=explanation,
        requested_by=user,
    )


def create_revision(draft: ResponseDraft, user, content_markdown: str) -> ResponseDraftRevision:
    """Append an attorney-edited revision; existing revision rows are immutable."""
    content_markdown = content_markdown.strip()
    if not content_markdown:
        raise ValueError("Draft content cannot be blank.")
    with transaction.atomic():
        current = ResponseDraft.objects.select_for_update().get(pk=draft.pk, tenant_id=draft.tenant_id)
        number = current.current_revision_number + 1
        previous = current.revisions.filter(revision_number=current.current_revision_number).first()
        manifest = previous.source_manifest if previous else {}
        revision = ResponseDraftRevision.objects.create(
            tenant_id=current.tenant_id,
            draft=current,
            revision_number=number,
            content_markdown=content_markdown,
            source_manifest=manifest,
            generation_method="attorney_edit",
            generated_by=user,
        )
        current.current_revision_number = number
        current.status = ResponseDraft.Status.DRAFT
        current.save(update_fields=("current_revision_number", "status", "updated_at"))
        AuditLog.objects.create(
            tenant_id=current.tenant_id,
            actor_user_id=user.pk,
            action="OFFICE_ACTION_DRAFT_REVISION_CREATED",
            resource_type="ResponseDraftRevision",
            resource_id=revision.pk,
            metadata={"draft_id": str(current.pk), "revision_number": number},
        )
        return revision


def record_review(revision: ResponseDraftRevision, user, decision: str, comments: str) -> OfficeActionReview:
    with transaction.atomic():
        review = OfficeActionReview.objects.create(
            tenant_id=revision.tenant_id,
            revision=revision,
            reviewer=user,
            decision=decision,
            comments=comments.strip(),
        )
        draft = revision.draft
        if decision == OfficeActionReview.Decision.APPROVED:
            # Approval is only applied to the current revision, never a stale one.
            if draft.current_revision_number == revision.revision_number:
                draft.status = ResponseDraft.Status.APPROVED
            else:
                draft.status = ResponseDraft.Status.IN_REVIEW
        elif decision == OfficeActionReview.Decision.CHANGES_REQUESTED:
            draft.status = ResponseDraft.Status.CHANGES_REQUESTED
        else:
            draft.status = ResponseDraft.Status.DRAFT
        draft.save(update_fields=("status", "updated_at"))
        AuditLog.objects.create(
            tenant_id=revision.tenant_id,
            actor_user_id=user.pk,
            action="OFFICE_ACTION_DRAFT_REVIEWED",
            resource_type="OfficeActionReview",
            resource_id=review.pk,
            metadata={"draft_id": str(draft.pk), "revision_id": str(revision.pk), "decision": decision},
        )
        return review
