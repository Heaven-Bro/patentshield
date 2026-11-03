from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

from django.test import SimpleTestCase
from rest_framework.exceptions import Throttled

from patents.ai_services import MetaLLMUpstreamError
from patents.ai_views import PatentAIAPIView


class MetaRetryAfterMappingTests(SimpleTestCase):
    now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _mapped_wait(self, header: str | None) -> tuple[int | None, str]:
        error = MetaLLMUpstreamError(
            "safe provider error",
            status_code=429,
            retry_after=header,
        )
        with patch("patents.ai_views.timezone.now", return_value=self.now):
            with self.assertRaises(Throttled) as raised:
                PatentAIAPIView._raise_provider_error(error)
        return raised.exception.wait, str(raised.exception)

    def test_delta_seconds_are_preserved(self) -> None:
        wait, _detail = self._mapped_wait("45")
        self.assertEqual(wait, 45)

    def test_http_date_is_converted_to_a_wait(self) -> None:
        header = format_datetime(self.now + timedelta(seconds=41), usegmt=True)
        wait, detail = self._mapped_wait(header)
        self.assertEqual(wait, 41)
        self.assertNotIn(header, detail)

    def test_past_http_date_does_not_create_a_negative_wait(self) -> None:
        header = format_datetime(self.now - timedelta(seconds=15), usegmt=True)
        wait, _detail = self._mapped_wait(header)
        self.assertEqual(wait, 0)

    def test_malformed_header_is_ignored(self) -> None:
        wait, detail = self._mapped_wait("not-a-retry-date")
        self.assertIsNone(wait)
        self.assertNotIn("not-a-retry-date", detail)

    def test_retry_wait_remains_capped_at_one_hour(self) -> None:
        wait, _detail = self._mapped_wait("7200")
        self.assertEqual(wait, 3600)
