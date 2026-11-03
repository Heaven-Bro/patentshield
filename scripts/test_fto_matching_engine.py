"""Offline, dependency-free tests for the Phase 6 lexical FTO matcher."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fto.services import rank_product_features_for_limitation  # noqa: E402


def _feature(pk: str, title: str, description: str, quote: str):
    return SimpleNamespace(pk=pk, title=title, description=description, source_quote=quote)


def test_ranks_technical_overlap_and_returns_bounded_scores():
    features = [
        _feature("sensor", "Sensor processor", "Receives sensor data and transmits readings", "Sensor processor transmits readings."),
        _feature("battery", "Battery housing", "Mechanical enclosure and connector", "A housing contains a connector."),
    ]
    results = rank_product_features_for_limitation(
        "A processor receives sensor data and transmits a control signal",
        features,
        max_results=10,
    )
    assert len(results) == 1
    assert results[0]["feature"].pk == "sensor"
    assert 0.0 < results[0]["score"] <= 1.0
    assert 0.0 < results[0]["limitation_coverage"] <= 1.0
    assert "sensor" in results[0]["matched_terms"]


def test_no_overlap_does_not_create_a_candidate():
    features = [_feature("a", "Thermal enclosure", "A housing contains an insulator", "thermal housing insulator")]
    assert rank_product_features_for_limitation("wireless signal authentication", features) == []


def test_stopword_only_input_does_not_create_a_candidate():
    features = [_feature("a", "Device controller", "A system comprising components", "The device")]
    assert rank_product_features_for_limitation("the and or comprising wherein said thereof", features) == []


def test_result_limit_is_validated():
    try:
        rank_product_features_for_limitation("sensor", [], max_results=51)
    except ValueError:
        return
    raise AssertionError("max_results above 50 must be rejected")
