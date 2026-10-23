"""Typed data contracts for PatentShield retrieval results."""

from __future__ import annotations

from pydantic import BaseModel


class RetrievedCandidate(BaseModel):
    """A tenant-scoped text chunk and its retrieval/ranking scores."""

    chunk_id: str
    tenant_id: str
    patent_id: str
    document_version_id: str
    section_type: str
    claim_number: str | None = None
    text: str
    dense_score: float = 0.0
    lexical_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float = 0.0
    page_start: int | None = None
    page_end: int | None = None
