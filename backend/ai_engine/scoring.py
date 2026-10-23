"""Transparent, deterministic screening-priority metrics for PatentShield."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


class MultiMetricScorer:
    """Calculate normalized search-screening metrics, not legal probabilities.

    ``R`` is average retrieval relevance, ``C`` is the fraction of limitations
    with verified positive-support evidence, ``E`` is the average confidence of
    assessments with verified citations, and ``T`` is average technical-term
    correspondence among supported limitations. The score is a prioritization
    heuristic for review queues; it is not a probability of infringement,
    novelty, validity, or legal equivalence.
    """

    DEFAULT_WEIGHTS: dict[str, float] = {
        "wr": 0.3,
        "wc": 0.4,
        "we": 0.2,
        "wt": 0.1,
    }
    COMPONENT_KEYS = {
        "wr": "retrieval_relevance",
        "wc": "limitation_coverage",
        "we": "evidence_confidence",
        "wt": "technical_correspondence",
    }
    SUPPORT_STATUSES = frozenset(
        {"DIRECT_SUPPORT", "PARTIAL_SUPPORT", "POSSIBLE_EQUIVALENT"}
    )

    @classmethod
    def calculate_screening_priority(
        cls,
        assessments: list[dict[str, Any]],
        weights: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """Calculate four [0, 1] metrics and the weighted [0, 100] score.

        Custom weights override individual defaults. Weight values must be
        finite, non-negative numbers and the resulting weight sum must be
        positive. The supplied formula is applied as given, then the final
        score is clamped to [0, 100].
        """
        effective_weights = cls._validate_weights(weights)
        if not assessments:
            return {
                "retrieval_relevance": 0.0,
                "limitation_coverage": 0.0,
                "evidence_confidence": 0.0,
                "technical_correspondence": 0.0,
                "screening_priority_score": 0.0,
            }

        count = len(assessments)
        retrieval_values: list[float] = []
        evidence_confidence_values: list[float] = []
        supported_count = 0
        correspondence_values: list[float] = []

        for assessment in assessments:
            status = str(assessment.get("status", "UNCERTAIN")).upper()
            verified_evidence = cls._verified_evidence(assessment)
            is_uncertain = status == "UNCERTAIN"

            if is_uncertain:
                relevance = 0.0
            else:
                relevance = _bounded_score(assessment.get("retrieval_relevance", 0.0))
                evidence_relevance = [
                    _bounded_score(
                        item.get(
                            "retrieval_score",
                            item.get("rerank_score", item.get("dense_score", 0.0)),
                        )
                    )
                    for item in verified_evidence
                ]
                if evidence_relevance:
                    relevance = max([relevance, *evidence_relevance])
            retrieval_values.append(relevance)

            has_verified_support = (
                status in cls.SUPPORT_STATUSES and bool(verified_evidence)
            )
            if has_verified_support:
                supported_count += 1
                evidence_confidence_values.append(
                    _bounded_score(assessment.get("confidence", 0.0))
                )
                correspondence_values.append(
                    _bounded_score(
                        assessment.get("technical_correspondence", 0.0)
                    )
                )

        retrieval_relevance = _mean(retrieval_values)
        limitation_coverage = supported_count / count
        evidence_confidence = _mean(evidence_confidence_values)
        technical_correspondence = _mean(correspondence_values)

        weighted_sum = (
            effective_weights["wr"] * retrieval_relevance
            + effective_weights["wc"] * limitation_coverage
            + effective_weights["we"] * evidence_confidence
            + effective_weights["wt"] * technical_correspondence
        )
        screening_priority_score = min(100.0, max(0.0, 100.0 * weighted_sum))

        return {
            "retrieval_relevance": _round_metric(retrieval_relevance),
            "limitation_coverage": _round_metric(limitation_coverage),
            "evidence_confidence": _round_metric(evidence_confidence),
            "technical_correspondence": _round_metric(technical_correspondence),
            "screening_priority_score": _round_metric(screening_priority_score),
        }

    @staticmethod
    def _verified_evidence(
        assessment: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        """Return evidence citations positively verified by the guardrail."""
        evidence = assessment.get("evidence", [])
        if not isinstance(evidence, list):
            return []
        return [
            item
            for item in evidence
            if isinstance(item, Mapping)
            and item.get("quote_verified") is True
        ]

    @classmethod
    def _validate_weights(
        cls,
        weights: dict[str, float] | None,
    ) -> dict[str, float]:
        """Merge overrides with defaults and reject nonsensical weights."""
        unknown = set(weights or {}) - set(cls.DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"Unknown screening weight key(s): {sorted(unknown)}")

        merged = dict(cls.DEFAULT_WEIGHTS)
        for key, raw_value in (weights or {}).items():
            try:
                value = float(raw_value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"Weight {key!r} must be a finite number.") from exc
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"Weight {key!r} must be finite and non-negative.")
            merged[key] = value

        if sum(merged.values()) <= 0.0:
            raise ValueError("At least one screening weight must be greater than zero.")
        return merged


def _bounded_score(value: Any) -> float:
    """Convert a value to a finite score in [0, 1], defaulting to zero."""
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(numeric):
        return 0.0
    return min(1.0, max(0.0, numeric))


def _mean(values: list[float]) -> float:
    """Calculate a finite arithmetic mean, returning zero for empty data."""
    return sum(values) / len(values) if values else 0.0


def _round_metric(value: float) -> float:
    """Return a stable, finite value for API/UI serialization."""
    if not math.isfinite(value):
        return 0.0
    return round(min(100.0, max(0.0, value)), 6)
