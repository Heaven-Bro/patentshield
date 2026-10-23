"""Tenant-safe hybrid retrieval, rank fusion, and cross-encoder reranking."""

from __future__ import annotations

import logging
import math
import os
import re
import threading
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from django.conf import settings
from pgvector.django import CosineDistance

from indexing.models import TextChunk
from indexing.services import BGEEmbedder, BM25LexicalIndex
from retrieval.schemas import RetrievedCandidate

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"[\w]+", flags=re.UNICODE)


class DenseRetriever:
    """Retrieve the nearest embedded text chunks within one tenant."""

    @staticmethod
    def search(
        query: str,
        tenant_id: str | UUID,
        top_k: int = 20,
        document_version_id: str | None = None,
    ) -> list[RetrievedCandidate]:
        """Run cosine-distance retrieval without crossing tenant boundaries.

        ``dense_score`` stores cosine similarity (``1 - cosine distance``),
        so larger scores indicate closer vectors. Rows without embeddings are
        excluded instead of being ranked as if they had meaningful vectors.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_k < 1:
            raise ValueError("top_k must be at least 1.")

        query_embedding = BGEEmbedder.embed_texts([query.strip()])[0]
        if len(query_embedding) != BGEEmbedder.DIMENSIONS:
            raise RuntimeError(
                "Query embedding dimension does not match the TextChunk vector field."
            )

        queryset = (
            TextChunk.objects.for_tenant(tenant_id)
            .filter(
                embedding__isnull=False,
                patent__tenant_id=tenant_id,
                document_version__tenant_id=tenant_id,
            )
            .select_related("patent", "document_version")
            .annotate(distance=CosineDistance("embedding", query_embedding))
            .order_by("distance", "pk")
        )
        if document_version_id is not None:
            queryset = queryset.filter(
                document_version_id=document_version_id
            )

        results: list[RetrievedCandidate] = []
        for chunk in queryset[:top_k]:
            distance = float(chunk.distance)
            if not math.isfinite(distance):
                logger.warning(
                    "Skipping chunk %s due to a non-finite cosine distance.",
                    chunk.pk,
                )
                continue
            results.append(
                RetrievedCandidate(
                    chunk_id=str(chunk.pk),
                    tenant_id=str(chunk.tenant_id),
                    patent_id=str(chunk.patent_id),
                    document_version_id=str(chunk.document_version_id),
                    section_type=chunk.section_type,
                    claim_number=chunk.claim_number,
                    text=chunk.text,
                    dense_score=1.0 - distance,
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                )
            )
        return results


class ReciprocalRankFusionService:
    """Fuse dense and lexical rankings using 1-based reciprocal ranks."""

    @staticmethod
    def fuse(
        dense_results: list[RetrievedCandidate],
        lexical_results: list[RetrievedCandidate],
        k: int = 60,
    ) -> list[RetrievedCandidate]:
        """Merge rankings by chunk ID using reciprocal rank fusion.

        Ranks are 1-based as in the conventional RRF definition. Candidate
        metadata must agree when the same chunk occurs in both result lists;
        conflicting metadata raises rather than risking a tenant mix-up.
        """
        if k < 1:
            raise ValueError("k must be at least 1.")

        fused: dict[str, RetrievedCandidate] = {}
        identities: dict[str, tuple[str, str, str]] = {}

        def merge(
            candidates: Iterable[RetrievedCandidate],
            *,
            score_field: str,
        ) -> None:
            seen_in_list: set[str] = set()
            for rank, candidate in enumerate(candidates, start=1):
                chunk_id = candidate.chunk_id
                if chunk_id in seen_in_list:
                    continue
                seen_in_list.add(chunk_id)

                identity = (
                    candidate.tenant_id,
                    candidate.patent_id,
                    candidate.document_version_id,
                )
                previous_identity = identities.get(chunk_id)
                if (
                    previous_identity is not None
                    and previous_identity != identity
                ):
                    raise ValueError(
                        f"Conflicting tenant/resource metadata for chunk {chunk_id}."
                    )
                identities[chunk_id] = identity

                current = fused.get(chunk_id)
                if current is None:
                    current = _copy_candidate(candidate)
                    current.dense_score = 0.0
                    current.lexical_score = 0.0
                    current.rrf_score = 0.0
                    fused[chunk_id] = current

                setattr(current, score_field, getattr(candidate, score_field))
                current.rrf_score += 1.0 / (k + rank)

        merge(dense_results, score_field="dense_score")
        merge(lexical_results, score_field="lexical_score")

        return sorted(
            fused.values(),
            key=lambda candidate: (
                candidate.rrf_score,
                candidate.dense_score,
                candidate.lexical_score,
            ),
            reverse=True,
        )


class CrossEncoderReranker:
    """Rerank fused results using the locally cached BGE reranker model."""

    MODEL_NAME = "BAAI/bge-reranker-v2-m3"
    _model: Any | None = None
    _load_attempted = False
    _load_error: Exception | None = None
    _model_lock = threading.Lock()

    @classmethod
    def rerank(
        cls,
        query: str,
        candidates: list[RetrievedCandidate],
        top_n: int = 10,
    ) -> list[RetrievedCandidate]:
        """Score query/chunk pairs and return the top ``top_n`` candidates.

        Raw single-logit model outputs are converted through a sigmoid to
        normalized values in ``[0, 1]``. If model weights are missing, a
        transparent lexical-overlap mock is allowed only in DEBUG or when
        ``PATENTSHIELD_ALLOW_MOCK_EMBEDDINGS`` is enabled. Inference failures
        after successful model loading are raised, not hidden by mock scores.
        """
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_n < 1:
            raise ValueError("top_n must be at least 1.")
        if not candidates:
            return []

        model = cls._get_model()
        if model is None:
            if not cls._mock_fallback_allowed():
                raise RuntimeError(
                    "Cross-encoder model is not available locally."
                ) from cls._load_error
            raw_scores = [
                cls._mock_score(query, candidate.text)
                for candidate in candidates
            ]
            # Mock scores are already normalized, so do not sigmoid them.
            normalized_scores = [
                min(1.0, max(0.0, float(score))) for score in raw_scores
            ]
        else:
            pairs = [(query.strip(), candidate.text) for candidate in candidates]
            try:
                raw_scores = model.predict(
                    pairs,
                    batch_size=int(
                        getattr(settings, "PATENTSHIELD_RERANKER_BATCH_SIZE", 16)
                    ),
                    show_progress_bar=False,
                    convert_to_numpy=True,
                )
            except Exception:
                logger.exception("Cross-encoder inference failed.")
                raise
            flat_scores = _flatten_scalar_scores(raw_scores)
            if len(flat_scores) != len(candidates):
                raise RuntimeError(
                    "Cross-encoder score count does not match candidate count."
                )
            normalized_scores = [_sigmoid(score) for score in flat_scores]

        reranked: list[RetrievedCandidate] = []
        for candidate, score in zip(
            candidates,
            normalized_scores,
            strict=True,
        ):
            if not math.isfinite(score):
                raise RuntimeError("Reranker returned a non-finite score.")
            updated = _copy_candidate(candidate)
            updated.rerank_score = score
            reranked.append(updated)

        reranked.sort(
            key=lambda candidate: (
                candidate.rerank_score,
                candidate.rrf_score,
                candidate.dense_score,
                candidate.lexical_score,
            ),
            reverse=True,
        )
        return reranked[:top_n]

    @classmethod
    def _get_model(cls) -> Any | None:
        """Load CrossEncoder exactly once per worker process from local cache."""
        if cls._model is not None:
            return cls._model

        with cls._model_lock:
            if cls._model is not None:
                return cls._model
            if cls._load_attempted:
                if cls._mock_fallback_allowed():
                    return None
                raise RuntimeError(
                    f"Could not load local cross-encoder weights for {cls.MODEL_NAME}."
                ) from cls._load_error

            cls._load_attempted = True
            try:
                from sentence_transformers import CrossEncoder

                cls._model = CrossEncoder(
                    str(
                        getattr(
                            settings,
                            "PATENTSHIELD_RERANKER_MODEL_NAME",
                            cls.MODEL_NAME,
                        )
                    ),
                    device=os.getenv("RERANKER_DEVICE") or None,
                    local_files_only=True,
                    trust_remote_code=False,
                )
            except Exception as exc:
                cls._model = None
                cls._load_error = exc
                if cls._mock_fallback_allowed():
                    logger.warning(
                        "Local cross-encoder weights unavailable; using a "
                        "development-only lexical mock (%s).",
                        type(exc).__name__,
                    )
                    return None
                raise RuntimeError(
                    "Unable to load the cross-encoder from the local model cache. "
                    "Pre-cache the model before starting production workers."
                ) from exc

        return cls._model

    @staticmethod
    def _mock_fallback_allowed() -> bool:
        """Enable fallback only in debug mode or when explicitly configured."""
        allow_mock = _setting_enabled(
            getattr(settings, "PATENTSHIELD_ALLOW_MOCK_EMBEDDINGS", False)
        )
        debug = _setting_enabled(getattr(settings, "DEBUG", False))
        return allow_mock or debug

    @staticmethod
    def _mock_score(query: str, text: str) -> float:
        """Return deterministic token coverage for development-only ranking."""
        query_tokens = set(_TOKEN_PATTERN.findall(query.casefold()))
        if not query_tokens:
            return 0.0
        text_tokens = set(_TOKEN_PATTERN.findall(text.casefold()))
        coverage = len(query_tokens & text_tokens) / len(query_tokens)
        phrase_bonus = 0.1 if query.casefold().strip() in text.casefold() else 0.0
        return min(1.0, coverage + phrase_bonus)


class HybridSearchPipeline:
    """Run tenant-scoped dense and BM25 retrieval, RRF, and reranking.

    The default lexical index is built from tenant-owned chunks per call. This
    keeps the baseline implementation simple and isolated; for large tenants,
    move BM25 to a persistent search service or an explicitly invalidated cache.
    """

    def __init__(
        self,
        dense_retriever: type[DenseRetriever] = DenseRetriever,
        fusion_service: ReciprocalRankFusionService | None = None,
        reranker: type[CrossEncoderReranker] = CrossEncoderReranker,
    ) -> None:
        self.dense_retriever = dense_retriever
        self.fusion_service = fusion_service or ReciprocalRankFusionService()
        self.reranker = reranker

    def retrieve_evidence(
        self,
        query: str,
        tenant_id: str | UUID,
        top_n: int = 5,
    ) -> list[RetrievedCandidate]:
        """Return the highest-ranked tenant-scoped chunks as evidence candidates."""
        if not query or not query.strip():
            raise ValueError("query must be a non-empty string.")
        if top_n < 1:
            raise ValueError("top_n must be at least 1.")

        default_pool_size = max(20, top_n * 4)
        candidate_pool_size = int(
            getattr(
                settings,
                "PATENTSHIELD_RETRIEVAL_CANDIDATE_K",
                default_pool_size,
            )
        )
        candidate_pool_size = max(top_n, candidate_pool_size)

        dense_results = self.dense_retriever.search(
            query=query,
            tenant_id=tenant_id,
            top_k=candidate_pool_size,
        )
        lexical_results = self._lexical_search(
            query=query,
            tenant_id=tenant_id,
            limit=candidate_pool_size,
        )
        fused_results = self.fusion_service.fuse(
            dense_results=dense_results,
            lexical_results=lexical_results,
        )
        if not fused_results:
            return []
        return self.reranker.rerank(
            query=query,
            candidates=fused_results,
            top_n=top_n,
        )

    @staticmethod
    def _lexical_search(
        query: str,
        tenant_id: str | UUID,
        limit: int,
    ) -> list[RetrievedCandidate]:
        """Build and search the BM25 index, then restore trusted chunk metadata."""
        max_chunks_setting = getattr(
            settings,
            "PATENTSHIELD_BM25_MAX_CHUNKS",
            None,
        )
        max_chunks = (
            int(max_chunks_setting) if max_chunks_setting is not None else None
        )
        # Apply tenant predicates to the chunk and both parent relations. This
        # guards against malformed legacy rows whose foreign keys cross tenants.
        chunk_queryset = (
            TextChunk.objects.for_tenant(tenant_id)
            .filter(
                patent__tenant_id=tenant_id,
                document_version__tenant_id=tenant_id,
            )
            .exclude(text="")
            .order_by("pk")
        )
        if max_chunks is not None:
            if max_chunks < 1:
                raise ValueError(
                    "PATENTSHIELD_BM25_MAX_CHUNKS must be positive when set."
                )
            chunk_queryset = chunk_queryset[:max_chunks]
        rows = list(chunk_queryset.values_list("pk", "text"))
        lexical_index = BM25LexicalIndex(
            chunk_ids=[str(chunk_id) for chunk_id, _ in rows],
            texts=[text for _, text in rows],
        )
        lexical_hits = lexical_index.search(query, limit=limit)
        if not lexical_hits:
            return []

        chunk_ids = [hit["chunk_id"] for hit in lexical_hits]
        chunks_by_id = {
            str(chunk.pk): chunk
            for chunk in TextChunk.objects.for_tenant(tenant_id)
            .filter(
                pk__in=chunk_ids,
                patent__tenant_id=tenant_id,
                document_version__tenant_id=tenant_id,
            )
            .select_related("patent", "document_version")
        }

        candidates: list[RetrievedCandidate] = []
        for hit in lexical_hits:
            chunk = chunks_by_id.get(hit["chunk_id"])
            if chunk is None:
                # Chunk may have been deleted after the BM25 index was built.
                continue
            candidates.append(
                RetrievedCandidate(
                    chunk_id=str(chunk.pk),
                    tenant_id=str(chunk.tenant_id),
                    patent_id=str(chunk.patent_id),
                    document_version_id=str(chunk.document_version_id),
                    section_type=chunk.section_type,
                    claim_number=chunk.claim_number,
                    text=chunk.text,
                    lexical_score=float(hit["score"]),
                    page_start=chunk.page_start,
                    page_end=chunk.page_end,
                )
            )
        return candidates


def _copy_candidate(candidate: RetrievedCandidate) -> RetrievedCandidate:
    """Copy Pydantic v1 or v2 candidates without mutating upstream result sets."""
    model_copy = getattr(candidate, "model_copy", None)
    if callable(model_copy):
        return model_copy(deep=True)
    return candidate.copy(deep=True)


def _flatten_scalar_scores(raw_scores: Any) -> list[float]:
    """Convert scalar or singleton-column model output into Python floats."""
    if hasattr(raw_scores, "tolist"):
        raw_scores = raw_scores.tolist()
    if isinstance(raw_scores, (int, float)):
        return [float(raw_scores)]
    if not isinstance(raw_scores, (list, tuple)):
        try:
            return [float(raw_scores)]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Unsupported cross-encoder score format.") from exc

    scores: list[float] = []
    for value in raw_scores:
        if isinstance(value, (list, tuple)):
            if len(value) != 1:
                raise RuntimeError(
                    "Cross-encoder must return exactly one relevance score per pair."
                )
            value = value[0]
        try:
            score = float(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Cross-encoder returned a non-scalar score.") from exc
        if not math.isfinite(score):
            raise RuntimeError("Cross-encoder returned a non-finite score.")
        scores.append(score)
    return scores


def _sigmoid(value: float) -> float:
    """Map a finite logit to a numerically stable probability-like score."""
    if not math.isfinite(value):
        raise RuntimeError("Cross-encoder returned a non-finite score.")
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _setting_enabled(value: object) -> bool:
    """Parse boolean settings robustly, including common environment strings."""
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def retrieve_evidence(
    query: str,
    tenant_id: str | UUID,
    top_n: int = 5,
) -> list[RetrievedCandidate]:
    """Convenience entry point for the default hybrid retrieval pipeline."""
    return HybridSearchPipeline().retrieve_evidence(
        query=query,
        tenant_id=tenant_id,
        top_n=top_n,
    )
