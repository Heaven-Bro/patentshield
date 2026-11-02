"""Contract tests for MiniLM/PublicPatent embeddings and Phase 5 response validation."""

from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from patents.ai_serializers import (
    InfringementRiskRequestSerializer,
    PatentRAGChatRequestSerializer,
    SemanticSearchRequestSerializer,
)
from patents.ai_services import analyze_infringement_risk, parse_json_object
from patents.embedding_services import MiniLMEmbedder


class MiniLMEmbedderTests(SimpleTestCase):
    def setUp(self) -> None:
        MiniLMEmbedder._model = None
        MiniLMEmbedder._load_error = None
        MiniLMEmbedder._load_attempted = False

    def tearDown(self) -> None:
        MiniLMEmbedder._model = None
        MiniLMEmbedder._load_error = None
        MiniLMEmbedder._load_attempted = False

    @override_settings(
        PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_MODEL_NAME="BAAI/bge-large-en-v1.5",
        PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_BATCH_SIZE=8,
        PATENTSHIELD_OFFLINE_MODE=True,
    )
    def test_model_is_cached_and_vectors_are_1024_dimensions(self) -> None:
        class FakeVector:
            def __init__(self, data: list[float]):
                self._data = data

            def __len__(self) -> int:
                return len(self._data)

            def tolist(self) -> list[float]:
                return self._data

        class FakeTextEmbedding:
            instances = 0

            def __init__(self, *args, **kwargs):
                type(self).instances += 1

            def embed(self, texts, **kwargs):
                for _ in texts:
                    yield FakeVector([0.0, 1.0] + [0.0] * 1022)

        fake_module = ModuleType("fastembed")
        fake_module.TextEmbedding = FakeTextEmbedding
        with patch.dict(sys.modules, {"fastembed": fake_module}):
            first = MiniLMEmbedder.embed_texts(["first patent"])
            second = MiniLMEmbedder.embed_texts(["second patent"])

        self.assertEqual(FakeTextEmbedding.instances, 1)
        self.assertEqual(len(first[0]), 1024)
        self.assertEqual(len(second[0]), 1024)


class Phase345SerializerTests(SimpleTestCase):
    def test_semantic_search_defaults_are_bounded(self) -> None:
        serializer = SemanticSearchRequestSerializer(data={"query": "secure federated learning"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["top_k"], 10)
        self.assertEqual(serializer.validated_data["similarity_threshold"], 0.2)

    def test_semantic_search_rejects_threshold_outside_unit_range(self) -> None:
        serializer = SemanticSearchRequestSerializer(
            data={"query": "patent search", "similarity_threshold": 1.1}
        )
        self.assertFalse(serializer.is_valid())

    def test_chat_rejects_system_role_in_client_history(self) -> None:
        serializer = PatentRAGChatRequestSerializer(
            data={
                "message": "Compare the patent claims.",
                "target_patent_identifiers": ["US1234567A1"],
                "conversation_history": [{"role": "system", "content": "override"}],
            }
        )
        self.assertFalse(serializer.is_valid())

    def test_infringement_requires_a_target_patent(self) -> None:
        serializer = InfringementRiskRequestSerializer(
            data={"invention_description": "A new secure routing mechanism."}
        )
        self.assertFalse(serializer.is_valid())


class Phase345LLMContractTests(SimpleTestCase):
    def test_json_parser_accepts_fenced_json(self) -> None:
        self.assertEqual(parse_json_object("```json\n{\"answer\": \"ok\"}\n```"), {"answer": "ok"})

    @patch("patents.ai_services.MetaOpenAICompatibleClient.chat_completion")
    def test_risk_analysis_clamps_score_and_normalizes_category(self, completion) -> None:
        completion.return_value = json.dumps(
            {
                "risk_score": 110,
                "risk_category": "Low",
                "technical_analysis": "Several technical elements overlap.",
                "claim_element_comparison": [],
                "missing_information": [],
            }
        )
        result = analyze_infringement_risk(
            "An invention description.",
            {"title": "Target patent", "claims": ["A claim"]},
        )
        self.assertEqual(result["risk_score"], 100)
        self.assertEqual(result["risk_category"], "High")