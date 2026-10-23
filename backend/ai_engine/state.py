"""Typed state and evidence contracts for PatentShield claim analysis."""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, Field


class LimitationObject(BaseModel):
    """One technical limitation extracted from a target patent claim."""

    limitation_id: str
    limitation_index: int
    text: str
    evidence_requirement: str


class EvidenceItem(BaseModel):
    """A verbatim excerpt and its source-document provenance."""

    evidence_id: str
    source_patent_id: str
    source_version_id: str
    exact_quote: str
    page_index: int | None = None
    section_type: str


class LimitationAssessmentResult(BaseModel):
    """Structured, review-oriented assessment for one claim limitation."""

    limitation_id: str
    status: Literal[
        "DIRECT_SUPPORT",
        "PARTIAL_SUPPORT",
        "POSSIBLE_EQUIVALENT",
        "NOT_FOUND",
        "CONTRADICTORY",
        "UNCERTAIN",
    ]
    explanation: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class AnalysisState(TypedDict):
    """Complete state schema passed between LangGraph analysis nodes.

    Callers should initialize all keys, using empty lists and ``None`` for
    ``error_message`` when the analysis has not yet started.
    """

    analysis_id: str
    tenant_id: str
    target_patent_id: str
    target_claim_number: str
    claim_text: str
    limitations: list[dict]
    retrieved_evidence: list[dict]
    assessments: list[dict]
    status: str
    error_message: str | None
