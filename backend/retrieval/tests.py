"""Tests for multilingual retrieval behavior."""

from django.test import SimpleTestCase

from retrieval.services import CrossEncoderReranker


class MultilingualRerankerFallbackTests(SimpleTestCase):
    def test_development_lexical_fallback_recognizes_cjk_ngrams(self):
        score = CrossEncoderReranker._mock_score(
            "无线通信", "一种用于无线通信的电路"
        )
        self.assertGreater(score, 0.0)
