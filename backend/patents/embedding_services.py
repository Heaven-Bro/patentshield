"""1024-dimensional public-patent embeddings using BAAI/bge-large-en-v1.5 via FastEmbed.

The model is a process-wide singleton running ONNX Runtime (PyTorch-free). API workers can
preload it during Django startup, while management commands and test processes can load it on
first use. Model downloads and caching are handled internally by FastEmbed.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class PublicPatentModelUnavailable(RuntimeError):
    """Raised when the configured public-patent embedding weights are unavailable."""


# Backward-compatible exception alias for existing deployments and imports.
MiniLMModelUnavailable = PublicPatentModelUnavailable


class PublicPatentEmbedder:
    """Thread-safe cache for the 1024-d BAAI/bge-large public-patent space using FastEmbed."""

    MODEL_NAME = "BAAI/bge-large-en-v1.5"
    DIMENSIONS = 1024
    _model: Any | None = None
    _load_error: Exception | None = None
    _load_attempted = False
    _lock = threading.Lock()

    @classmethod
    def model_identity(cls) -> tuple[str, str]:
        return (
            str(getattr(settings, "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_MODEL_NAME", cls.MODEL_NAME)),
            str(getattr(settings, "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_REVISION", "main")),
        )

    @classmethod
    def preload(cls) -> bool:
        """Load model weights once and return whether the model is available."""
        try:
            cls._get_model()
        except PublicPatentModelUnavailable:
            logger.warning(
                "Embedding model preload was skipped because the configured model is unavailable."
            )
            return False
        return True

    @classmethod
    def embed_texts(cls, texts: list[str], *, batch_size: int | None = None) -> list[list[float]]:
        """Encode non-empty texts and return normalized 1024-dimensional vectors."""
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Every embedding input must be a non-empty string.")

        model = cls._get_model()
        configured_batch_size = batch_size or int(
            getattr(settings, "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_BATCH_SIZE", 16)
        )
        if configured_batch_size < 1:
            raise ValueError("batch_size must be at least 1.")

        try:
            # FastEmbed embed() returns a generator yielding numpy arrays
            embeddings_generator = model.embed(texts, batch_size=configured_batch_size)
            vectors = [vec.tolist() for vec in embeddings_generator]
        except Exception as exc:
            logger.exception("Embedding inference failed.")
            raise RuntimeError("Embedding service failed to encode the requested text.") from exc

        if len(vectors) != len(texts):
            raise RuntimeError("Model returned a different number of vectors than inputs.")

        result: list[list[float]] = []
        for vector in vectors:
            if len(vector) != cls.DIMENSIONS:
                raise RuntimeError(
                    f"Model returned {len(vector)} dimensions; expected {cls.DIMENSIONS}."
                )
            normalized = [float(value) for value in vector]
            if any(not math.isfinite(value) or not (-1.000001 <= value <= 1.000001) for value in normalized):
                raise RuntimeError("Model returned a non-finite or invalid vector value.")
            result.append(normalized)
        return result

    @classmethod
    def _get_model(cls) -> Any:
        if cls._model is not None:
            return cls._model

        with cls._lock:
            if cls._model is not None:
                return cls._model
            if cls._load_attempted:
                raise PublicPatentModelUnavailable(
                    "Embedding model is unavailable in this process; check the model cache and ML dependencies."
                ) from cls._load_error
            cls._load_attempted = True

            try:
                from fastembed import TextEmbedding

                model_name = str(
                    getattr(settings, "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_MODEL_NAME", cls.MODEL_NAME)
                ).strip() or cls.MODEL_NAME
                
                # Load ONNX FastEmbed model
                model = TextEmbedding(model_name=model_name)

                # Validate embedding dimensions using a dummy test input
                probe_vectors = list(model.embed(["probe_test"]))
                if probe_vectors:
                    dimensions = len(probe_vectors[0])
                    if dimensions != cls.DIMENSIONS:
                        raise RuntimeError(
                            f"Configured embedding model has {dimensions} dimensions; expected {cls.DIMENSIONS}."
                        )

                cls._model = model
                cls._load_error = None
            except Exception as exc:
                cls._load_error = exc
                logger.warning(
                    "Could not load embedding model (%s). Install ML dependencies and cache the model.",
                    type(exc).__name__,
                )
                raise PublicPatentModelUnavailable(
                    f"Unable to load embedding model '{cls.MODEL_NAME}'."
                ) from exc

        return cls._model


# Backward-compatible alias for older callers and deployments. New code should
# use PublicPatentEmbedder so the configured BGE-large model is never confused
# with the unrelated MiniLM family.
MiniLMEmbedder = PublicPatentEmbedder