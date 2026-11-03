"""Tests for Unicode language detection and original-language lexical tokenization."""

from django.test import SimpleTestCase

from indexing.language import detect_language, detect_language_code, tokenize_original_language
from indexing.services import BGEEmbedder, BM25LexicalIndex, HybridChunker


class CJKLanguageDetectionTests(SimpleTestCase):
    def test_detects_simplified_chinese(self):
        result = detect_language("一种用于无线通信的电路")
        self.assertEqual(result["language"], "zh")
        self.assertEqual(result["method"], "unicode_script")

    def test_detects_japanese_from_kana(self):
        self.assertEqual(detect_language_code("無線通信システムを提供する"), "ja")

    def test_detects_korean_hangul(self):
        self.assertEqual(detect_language_code("무선 통신 시스템을 제공한다"), "ko")

    def test_does_not_mislabel_non_cjk_text_as_english(self):
        self.assertEqual(detect_language_code("A patent for wireless communication"), "und")

    def test_empty_input_is_undetermined(self):
        self.assertEqual(detect_language_code("  "), "und")

    def test_cjk_lexical_tokenizer_emits_overlapping_ngrams(self):
        tokens = tokenize_original_language("一种用于无线通信的电路")
        self.assertIn("无线", tokens)
        self.assertIn("线通", tokens)
        self.assertIn("通信", tokens)
        self.assertIn("无线通", tokens)

    def test_original_language_bm25_query_uses_same_cjk_tokenization(self):
        query_tokens = BM25LexicalIndex._tokenize("无线通信")
        document_tokens = BM25LexicalIndex._tokenize("一种用于无线通信的电路")
        self.assertTrue(set(query_tokens).intersection(document_tokens))

    def test_bm25_returns_matching_document_when_single_corpus_scores_are_negative(self):
        index = BM25LexicalIndex(
            chunk_ids=["matching"],
            texts=["A wireless sensor transmits measurements to a processor."],
        )

        results = index.search("wireless sensor measurements processor")

        self.assertEqual([result["chunk_id"] for result in results], ["matching"])
        self.assertGreater(results[0]["score"], 0)

    def test_bm25_small_corpus_fallback_excludes_documents_without_query_terms(self):
        index = BM25LexicalIndex(
            chunk_ids=["matching", "unrelated"],
            texts=[
                "A wireless sensor transmits measurements to a processor.",
                "A ceramic glaze is fired in a kiln.",
            ],
        )

        results = index.search("wireless sensor measurements processor")

        self.assertEqual([result["chunk_id"] for result in results], ["matching"])

    def test_cjk_chunker_bounds_text_without_spaces_and_preserves_offsets(self):
        source = "无线通信系统" * 120
        chunks = HybridChunker.chunk_text(source, max_tokens=100, overlap=20)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0]["char_start"], 0)
        self.assertEqual(chunks[0]["text"], source[:chunks[0]["char_end"]])
        self.assertLessEqual(len(chunks[0]["text"]), 100)
        self.assertEqual(chunks[1]["char_start"], chunks[0]["char_end"] - 20)

    def test_existing_embedder_is_the_multilingual_bge_m3_model(self):
        self.assertEqual(BGEEmbedder.MODEL_NAME, "BAAI/bge-m3")
        self.assertEqual(BGEEmbedder.DIMENSIONS, 1024)
