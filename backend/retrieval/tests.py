"""Tests for retrieval, including bounded external provider transport."""

from unittest.mock import patch

from django.test import SimpleTestCase

from retrieval.external import (
    MAX_PROVIDER_RESPONSE_BYTES,
    HTTP_TOTAL_TIMEOUT_SECONDS,
    ExternalSearchError,
    ImproperlyConfigured,
    TAVILY_SEARCH_URL,
    _post_provider_json,
)
from retrieval.services import CrossEncoderReranker


class MultilingualRerankerFallbackTests(SimpleTestCase):
    def test_development_lexical_fallback_recognizes_cjk_ngrams(self):
        score = CrossEncoderReranker._mock_score(
            "无线通信", "一种用于无线通信的电路"
        )
        self.assertGreater(score, 0.0)


class _FakeStreamingResponse:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = list(chunks)
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size):
        self.iterated = True
        yield from self.chunks

    def close(self):
        self.closed = True


class _SlowProgressRaw:
    def __init__(self, response, clock):
        self.response = response
        self.clock = clock
        self.read_count = 0

    def read1(self, _chunk_size, decode_content=True):
        self.response.iterated = True
        self.read_count += 1
        if self.read_count == 1:
            self.clock[0] = HTTP_TOTAL_TIMEOUT_SECONDS / 2
            return b'{"results":'
        self.clock[0] = HTTP_TOTAL_TIMEOUT_SECONDS + 1
        return b' []}'


class _SlowProgressResponse(_FakeStreamingResponse):
    def __init__(self, clock):
        super().__init__()
        self.raw = _SlowProgressRaw(self, clock)


class ExternalProviderTransportSecurityTests(SimpleTestCase):
    def test_redirect_is_rejected_without_following_and_response_is_closed(self):
        response = _FakeStreamingResponse(
            status_code=307,
            headers={"Location": "https://attacker.example/collect"},
            chunks=[b'{"results": []}'],
        )
        with patch("retrieval.external.requests.post", return_value=response) as post:
            with self.assertRaises(ExternalSearchError):
                _post_provider_json(
                    "TAVILY",
                    TAVILY_SEARCH_URL,
                    headers={"Accept": "application/json"},
                    json_body={"api_key": "test-only"},
                )

        self.assertFalse(response.iterated)
        self.assertTrue(response.closed)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertTrue(post.call_args.kwargs["stream"])

    def test_declared_oversized_body_is_rejected_before_reading(self):
        response = _FakeStreamingResponse(
            headers={"Content-Length": str(MAX_PROVIDER_RESPONSE_BYTES + 1)},
            chunks=[b'{"results": []}'],
        )
        with patch("retrieval.external.requests.post", return_value=response):
            with self.assertRaises(ExternalSearchError):
                _post_provider_json(
                    "TAVILY",
                    TAVILY_SEARCH_URL,
                    headers={},
                    json_body={},
                )
        self.assertFalse(response.iterated)
        self.assertTrue(response.closed)

    def test_chunked_oversized_body_is_stopped_at_the_byte_limit(self):
        response = _FakeStreamingResponse(
            chunks=[b"a" * (MAX_PROVIDER_RESPONSE_BYTES // 2),
                    b"b" * (MAX_PROVIDER_RESPONSE_BYTES // 2 + 1)],
        )
        with patch("retrieval.external.requests.post", return_value=response):
            with self.assertRaises(ExternalSearchError):
                _post_provider_json(
                    "TAVILY",
                    TAVILY_SEARCH_URL,
                    headers={},
                    json_body={},
                )
        self.assertTrue(response.closed)

    def test_only_the_official_https_endpoint_is_accepted(self):
        with patch("retrieval.external.requests.post") as post:
            with self.assertRaises(ImproperlyConfigured):
                _post_provider_json(
                    "TAVILY",
                    "https://attacker.example/search",
                    headers={},
                    json_body={},
                )
        post.assert_not_called()

    def test_valid_small_json_response_is_parsed(self):
        response = _FakeStreamingResponse(chunks=[b'{"results": []}'])
        with patch("retrieval.external.requests.post", return_value=response):
            payload = _post_provider_json(
                "TAVILY",
                TAVILY_SEARCH_URL,
                headers={"Accept": "application/json"},
                json_body={"query": "bounded"},
            )
        self.assertEqual(payload, {"results": []})
        self.assertTrue(response.closed)

    def test_slow_continuously_progressing_body_hits_total_deadline(self):
        clock = [0.0]
        response = _SlowProgressResponse(clock)
        with patch("retrieval.external.time.monotonic", side_effect=lambda: clock[0]):
            with patch("retrieval.external.requests.post", return_value=response):
                with self.assertRaisesRegex(ExternalSearchError, "request timed out"):
                    _post_provider_json(
                        "TAVILY",
                        TAVILY_SEARCH_URL,
                        headers={},
                        json_body={},
                    )
        self.assertTrue(response.iterated)
        self.assertTrue(response.closed)
