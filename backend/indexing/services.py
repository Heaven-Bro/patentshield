"""Text chunking, dense embedding, and lexical BM25 search services."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import random
import re
import threading
from typing import Any, TypedDict
from uuid import UUID

from django.conf import settings

from indexing.models import TextChunk
from indexing.language import has_substantial_cjk, tokenize_original_language
from tenants.models import Tenant

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"\S+", flags=re.UNICODE)


class ChunkData(TypedDict):
    """A chunk with half-open character offsets into its source string."""

    text: str
    char_start: int
    char_end: int


class BM25SearchResult(TypedDict):
    """A lexical-search result with stable chunk identity and score."""

    chunk_id: str
    score: float
    text: str


class HybridChunker:
    """Create overlapping text windows while preserving source character spans.

    The default tokenizer treats each whitespace-delimited span as one token.
    For CJK text, it uses visible Unicode characters as chunking units so text
    without spaces does not become one oversized chunk. Both modes preserve
    exact source character offsets and avoid downloading tokenizers in workers.
    """

    @staticmethod
    def iter_chunks(
        text: str,
        max_tokens: int = 512,
        overlap: int = 64,
    ):
        """Yield bounded overlapping windows without materializing all tokens."""
        if max_tokens < 1:
            raise ValueError("max_tokens must be at least 1.")
        if overlap < 0 or overlap >= max_tokens:
            raise ValueError("overlap must be >= 0 and less than max_tokens.")
        if not text or not text.strip():
            return

        token_iterator = iter(
            re.finditer(r"\S", text, flags=re.UNICODE)
            if has_substantial_cjk(text)
            else _TOKEN_PATTERN.finditer(text)
        )
        window = []
        for _ in range(max_tokens):
            try:
                window.append(next(token_iterator))
            except StopIteration:
                break

        step = max_tokens - overlap
        while window:
            char_start = window[0].start()
            char_end = window[-1].end()
            yield {
                "text": text[char_start:char_end],
                "char_start": char_start,
                "char_end": char_end,
            }
            if len(window) < max_tokens:
                break

            next_window = window[-overlap:] if overlap else []
            added = 0
            while len(next_window) < max_tokens:
                try:
                    next_window.append(next(token_iterator))
                    added += 1
                except StopIteration:
                    break
            if not added:
                break
            window = next_window

    @staticmethod
    def chunk_text(
        text: str,
        max_tokens: int = 512,
        overlap: int = 64,
    ) -> list[ChunkData]:
        """Compatibility wrapper that collects chunks from :meth:`iter_chunks`."""
        return list(HybridChunker.iter_chunks(text, max_tokens=max_tokens, overlap=overlap))


class BGEEmbedder:
    """Lazy wrapper for local BAAI/bge-m3 dense embeddings via FastEmbed (ONNX Runtime).

    Model inference runs lightweight via FastEmbed without requiring PyTorch/CUDA.
    A deterministic mock embedding is permitted in development when
    ``PATENTSHIELD_ALLOW_MOCK_EMBEDDINGS`` is enabled (or ``DEBUG`` is true and
    the setting is omitted). Production defaults to failing closed rather than
    silently persisting non-semantic vectors.
    """

    MODEL_NAME = "BAAI/bge-m3"
    DIMENSIONS = 1024
    _model: Any | None = None
    _load_attempted = False
    _load_error: Exception | None = None
    _model_lock = threading.Lock()

    @classmethod
    def model_identity(cls) -> tuple[str, str]:
        return (
            str(getattr(settings, "BGE_MODEL_NAME", cls.MODEL_NAME)),
            str(getattr(settings, "BGE_MODEL_REVISION", "main")),
        )

    @classmethod
    def embed_texts(cls, texts: list[str]) -> list[list[float]]:
        """Embed text with FastEmbed BGE-M3, returning normalized 1024-d vectors."""
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Every text passed to BGEEmbedder must be non-empty.")

        model = cls._get_model()
        if model is None:
            if not cls._mock_fallback_allowed():
                error = cls._load_error
                raise RuntimeError(
                    "BGE-M3 weights are unavailable locally for FastEmbed. Install/cache the "
                    "model or explicitly enable the development-only mock."
                ) from error
            logger.warning(
                "Using deterministic mock embeddings. These vectors are not "
                "suitable for semantic retrieval or production data."
            )
            return [cls._mock_embedding(text) for text in texts]

        try:
            batch_size = int(getattr(settings, "BGE_BATCH_SIZE", 32))
            # FastEmbed's embed method returns a generator yielding NumPy arrays
            embeddings_generator = model.embed(texts, batch_size=batch_size)
            vectors = [vec.tolist() for vec in embeddings_generator]
        except Exception:
            # An inference error must not silently replace real embeddings
            # with mock vectors; Celery should retry the failed operation.
            logger.exception("FastEmbed BGE-M3 inference failed.")
            raise

        if len(vectors) != len(texts):
            raise RuntimeError(
                "FastEmbed returned a different number of vectors than inputs."
            )

        normalized_vectors: list[list[float]] = []
        for vector in vectors:
            if len(vector) != cls.DIMENSIONS:
                raise RuntimeError(
                    f"Expected {cls.DIMENSIONS}-dimensional BGE vectors, "
                    f"received {len(vector)} dimensions."
                )
            normalized = [float(value) for value in vector]
            if any(not math.isfinite(value) for value in normalized):
                raise RuntimeError("FastEmbed returned a non-finite vector component.")
            
            # Ensure vectors are normalized (L2 norm)
            magnitude = math.sqrt(sum(v * v for v in normalized))
            if magnitude > 0 and not math.isclose(magnitude, 1.0, abs_tol=1e-3):
                normalized = [v / magnitude for v in normalized]

            normalized_vectors.append(normalized)
        return normalized_vectors

    @classmethod
    def _get_model(cls) -> Any | None:
        """Load FastEmbed TextEmbedding model once without PyTorch dependency."""
        if cls._model is not None:
            return cls._model

        with cls._model_lock:
            if cls._model is not None:
                return cls._model
            if cls._load_attempted:
                return None
            cls._load_attempted = True

            try:
                from fastembed import TextEmbedding

                model_name = str(getattr(settings, "BGE_MODEL_NAME", cls.MODEL_NAME))
                cls._model = TextEmbedding(model_name=model_name)

                # Validate embedding dimensions using a dummy test input
                probe_vectors = list(cls._model.embed(["probe_test"]))
                if probe_vectors:
                    dimensions = len(probe_vectors[0])
                    if dimensions != cls.DIMENSIONS:
                        raise RuntimeError(
                            f"Configured FastEmbed model has {dimensions} dimensions; "
                            f"PatentShield requires {cls.DIMENSIONS}."
                        )
            except Exception as exc:
                cls._model = None
                cls._load_error = exc
                logger.warning(
                    "Could not load local FastEmbed BGE-M3 model: %s",
                    type(exc).__name__,
                )
                if not cls._mock_fallback_allowed():
                    raise RuntimeError(
                        "Unable to load local FastEmbed BGE weights."
                    ) from exc
                return None

        return cls._model

    @staticmethod
    def _mock_fallback_allowed() -> bool:
        """Return whether deterministic mock vectors are explicitly allowed."""
        configured_value = getattr(
            settings,
            "PATENTSHIELD_ALLOW_MOCK_EMBEDDINGS",
            getattr(settings, "DEBUG", False),
        )
        return bool(configured_value)

    @classmethod
    def _mock_embedding(cls, text: str) -> list[float]:
        """Return a deterministic normalized pseudo-vector for development."""
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        generator = random.Random(int.from_bytes(digest, byteorder="big"))
        vector = [generator.uniform(-1.0, 1.0) for _ in range(cls.DIMENSIONS)]
        magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / magnitude for value in vector]


class BM25LexicalIndex:
    """In-memory BM25 index over tenant-scoped chunks using original-script tokens.

    The index is deliberately an explicit service object: build it once per
    worker/request batch and reuse it for multiple queries. It is not a shared
    persistent index, so large multi-worker deployments should use a durable
    search service or a separately managed lexical index.
    """

    def __init__(
        self,
        chunk_ids: list[str],
        texts: list[str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if len(chunk_ids) != len(texts):
            raise ValueError("chunk_ids and texts must have equal lengths.")
        if k1 <= 0 or not 0 <= b <= 1:
            raise ValueError("BM25 parameters require k1 > 0 and 0 <= b <= 1.")

        self._chunk_ids = chunk_ids
        self._texts = texts
        self._engine: Any | None = None

        tokenized_corpus = [self._tokenize(text) for text in texts]
        if tokenized_corpus and any(tokenized_corpus):
            try:
                from rank_bm25 import BM25Okapi
            except ImportError as exc:
                raise RuntimeError(
                    "BM25 indexing requires the 'rank-bm25' package."
                ) from exc
            self._engine = BM25Okapi(tokenized_corpus, k1=k1, b=b)

    @classmethod
    def from_tenant(
        cls,
        tenant: Tenant | UUID | str,
        *,
        document_version_id: UUID | str | None = None,
        max_chunks: int | None = None,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> BM25LexicalIndex:
        """Build a lexical index from one tenant's chunks only."""
        queryset = (
            TextChunk.objects.for_tenant(tenant)
            .exclude(text="")
            .order_by("pk")
        )
        if document_version_id is not None:
            queryset = queryset.filter(
                document_version_id=document_version_id
            )
        if max_chunks is not None:
            if max_chunks < 1:
                raise ValueError("max_chunks must be positive when provided.")
            queryset = queryset[:max_chunks]

        rows = list(
            queryset.values_list("pk", "text").iterator(chunk_size=1000)
        )
        return cls(
            chunk_ids=[str(chunk_id) for chunk_id, _ in rows],
            texts=[text for _, text in rows],
            k1=k1,
            b=b,
        )

    def search(self, query: str, *, limit: int = 10) -> list[BM25SearchResult]:
        """Return the highest-scoring chunks for a lexical query."""
        if limit < 1:
            raise ValueError("limit must be positive.")
        query_tokens = self._tokenize(query)
        if not query_tokens or self._engine is None:
            return []

        scores = self._engine.get_scores(query_tokens)
        query_terms = set(query_tokens)
        tokenized_documents = [self._tokenize(text) for text in self._texts]
        matched_indices = [
            index
            for index, document_tokens in enumerate(tokenized_documents)
            if query_terms.intersection(document_tokens)
        ]
        if not matched_indices:
            return []

        # BM25Okapi's IDF can be negative when a term occurs in every document,
        # especially in one-document or otherwise tiny corpora. In that case,
        # discard its misleading negative ordering and rank only genuine token
        # matches by query coverage. The ordinary positive-score BM25 path is
        # unchanged for corpora with usable positive scores.
        if any(float(scores[index]) <= 0 for index in matched_indices):
            denominator = max(len(query_terms), 1)
            ranked_indices = sorted(
                matched_indices,
                key=lambda index: (
                    -len(query_terms.intersection(tokenized_documents[index])) / denominator,
                    -sum(tokenized_documents[index].count(term) for term in query_terms),
                    index,
                ),
            )
            ranked_results = [
                (
                    index,
                    len(query_terms.intersection(tokenized_documents[index])) / denominator,
                )
                for index in ranked_indices
            ]
        else:
            ranked_results = [
                (index, float(scores[index]))
                for index in sorted(
                    matched_indices,
                    key=lambda index: float(scores[index]),
                    reverse=True,
                )
            ]

        results: list[BM25SearchResult] = []
        for index, score in ranked_results[:limit]:
            results.append(
                {
                    "chunk_id": self._chunk_ids[index],
                    "score": score,
                    "text": self._texts[index],
                }
            )
            if len(results) >= limit:
                break
        return results

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Normalize text for Unicode-aware, original-language BM25 retrieval."""
        return tokenize_original_language(text)
