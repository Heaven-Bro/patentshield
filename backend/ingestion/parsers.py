"""Text parsers used by PatentShield's document ingestion pipeline."""

from __future__ import annotations

import re
from typing import TypedDict


class ParsedClaim(TypedDict):
    """Normalized fields extracted from a single numbered patent claim."""

    claim_number: str
    claim_text: str
    claim_type: str
    depends_on_claim_number: str | None


class ClaimDeconstructionParser:
    """Extract numbered claims and their first explicit claim dependency.

    This parser deliberately uses deterministic text rules. It is not an OCR
    engine and does not infer dependencies that are not explicitly numbered.
    """

    _CLAIM_START_RE = re.compile(
        r"(?m)^[ \t]*(?P<number>\d{1,3})[.)][ \t]+(?=\S)"
    )
    _DEPENDENCY_RE = re.compile(
        r"\bclaims?\s+(?:(?:no\.?|number)\s*)?(?P<number>\d+)\b",
        flags=re.IGNORECASE,
    )

    @classmethod
    def parse_claims_from_text(cls, raw_text: str) -> list[ParsedClaim]:
        """Parse numbered claim blocks from extracted document text.

        Claim markers must begin a line, as is usual in text extracted from
        patent documents. The stored claim text excludes the leading marker.
        For a claim referencing multiple claims, the schema's scalar
        ``depends_on_claim_number`` field stores the first explicit number.
        """
        if not raw_text or not raw_text.strip():
            return []

        normalized_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        markers = list(cls._CLAIM_START_RE.finditer(normalized_text))
        parsed_claims: list[ParsedClaim] = []

        for index, marker in enumerate(markers):
            block_end = (
                markers[index + 1].start()
                if index + 1 < len(markers)
                else len(normalized_text)
            )
            claim_text = cls._normalize_claim_text(
                normalized_text[marker.end() : block_end]
            )
            if not claim_text:
                continue

            dependency_match = cls._DEPENDENCY_RE.search(claim_text)
            dependency_number = (
                dependency_match.group("number")
                if dependency_match is not None
                else None
            )
            parsed_claims.append(
                {
                    "claim_number": marker.group("number"),
                    "claim_text": claim_text,
                    "claim_type": (
                        "DEPENDENT"
                        if dependency_number is not None
                        else "INDEPENDENT"
                    ),
                    "depends_on_claim_number": dependency_number,
                }
            )

        return parsed_claims

    @staticmethod
    def _normalize_claim_text(claim_text: str) -> str:
        """Collapse PDF line wrapping while preserving sentence punctuation."""
        return re.sub(r"\s+", " ", claim_text).strip()
