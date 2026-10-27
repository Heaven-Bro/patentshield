"""Provider-neutral contracts and helpers for patent-data connectors."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import requests
from django.core.cache import cache


ProviderName = Literal["USPTO", "EPO"]


class PatentConnectorError(RuntimeError):
    """Sanitized exception raised by a patent-data provider connector."""

    def __init__(
        self,
        provider: str,
        code: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.provider = provider
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"{provider} connector error: {code}.")


@dataclass(frozen=True, slots=True)
class NormalizedLegalEvent:
    """A source legal event with its own stable identity and provenance."""

    source_event_id: str
    event_code: str
    event_date: date | None
    title: str
    description: str = ""
    legal_status_after: str = "UNKNOWN"
    source_url: str | None = None
    source_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NormalizedPublication:
    """Normalized, provider-neutral publication returned by an adapter."""

    provider: ProviderName
    provider_record_id: str
    publication_number: str | None
    application_number: str | None
    jurisdiction: str
    title: str
    abstract: str | None = None
    kind_code: str | None = None
    filing_date: date | None = None
    publication_date: date | None = None
    grant_date: date | None = None
    legal_status: str = "UNKNOWN"
    applicant_names: list[str] = field(default_factory=list)
    inventor_names: list[str] = field(default_factory=list)
    ipc_codes: list[str] = field(default_factory=list)
    cpc_codes: list[str] = field(default_factory=list)
    family_system: str | None = None
    family_identifier: str | None = None
    source_url: str | None = None
    source_updated_at: datetime | None = None
    source_payload: dict[str, Any] = field(default_factory=dict)
    legal_events: list[NormalizedLegalEvent] = field(default_factory=list)

    @property
    def payload_sha256(self) -> str:
        """Hash stable JSON rather than the original query or request headers."""
        encoded = json.dumps(
            self.source_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class PatentConnector(Protocol):
    """Interface implemented by every live patent-data provider."""

    provider: ProviderName

    def search(self, query: str, max_results: int = 10) -> list[NormalizedPublication]:
        """Search a provider and return normalized public patent records."""


def normalize_query(query: str, max_length: int = 1_000) -> str:
    """Normalize whitespace and remove non-printable control characters."""
    if not isinstance(query, str):
        raise TypeError("Search query must be a string.")
    clean = " ".join(query.split())
    clean = "".join(character for character in clean if character.isprintable())
    if not clean:
        raise ValueError("Search query must not be empty.")
    if len(clean) > max_length:
        raise ValueError(f"Search query exceeds {max_length} characters.")
    return clean


def validate_limit(limit: int, upper_bound: int = 25) -> int:
    """Validate a bounded page size suitable for interactive API searches."""
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("max_results must be an integer.")
    if limit < 1 or limit > upper_bound:
        raise ValueError(f"max_results must be between 1 and {upper_bound}.")
    return limit


def parse_date(value: Any) -> date | None:
    """Parse common provider date formats without guessing malformed dates."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not text:
        return None
    candidates = [text]
    if re.fullmatch(r"\d{8}", text):
        candidates.insert(0, f"{text[:4]}-{text[4:6]}-{text[6:]}")
    for candidate in candidates:
        try:
            return date.fromisoformat(candidate[:10])
        except ValueError:
            continue
    return None


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO timestamp and ensure it is timezone-aware."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed_date = parse_date(text)
        if parsed_date is None:
            return None
        parsed = datetime.combine(parsed_date, datetime.min.time())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_legal_status(value: Any) -> str:
    """Map provider status text to conservative, stable status labels."""
    text = str(value or "").strip().casefold()
    if not text:
        return "UNKNOWN"
    if any(token in text for token in ("expired",)):
        return "EXPIRED"
    if any(token in text for token in ("lapsed", "not in force", "ceased")):
        return "LAPSED"
    if any(token in text for token in ("abandoned", "withdrawn", "withdrawal")):
        return "ABANDONED"
    if any(token in text for token in ("granted", "issued", "patented")):
        return "GRANTED"
    if any(
        token in text
        for token in ("pending", "prosecution", "under examination", "published")
    ):
        return "PENDING"
    return "UNKNOWN"


def safe_http_url(value: Any) -> str | None:
    """Accept only absolute HTTPS source URLs from provider payloads."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return candidate[:2_000]


def first_value(*values: Any) -> str | None:
    """Return the first useful scalar/text value, including provider text bags."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, dict):
                    for key in (
                        "inventionTitle",
                        "inventionTitleText",
                        "text",
                        "name",
                        "applicantNameText",
                    ):
                        inner = item.get(key)
                        if isinstance(inner, str) and inner.strip():
                            return inner.strip()
    return None


def normalize_string_list(value: Any) -> list[str]:
    """Normalize common list and name-bag structures to distinct strings."""
    if not isinstance(value, list):
        value = [value] if value is not None else []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        candidate: str | None = None
        if isinstance(item, str):
            candidate = item.strip()
        elif isinstance(item, dict):
            candidate = first_value(
                item.get("name"),
                item.get("applicantNameText"),
                item.get("inventorNameText"),
                item.get("nameLineOneText"),
                item.get("nameText"),
                item.get("fullName"),
            )
            if not candidate:
                name_parts = [
                    str(item.get(key, "")).strip()
                    for key in ("firstName", "middleName", "lastName")
                    if item.get(key)
                ]
                candidate = " ".join(name_parts) or None
        if candidate:
            candidate = " ".join(candidate.split())[:500]
            key = candidate.casefold()
            if key not in seen:
                seen.add(key)
                output.append(candidate)
    return output


def hash_event_payload(payload: dict[str, Any]) -> str:
    """Return a deterministic fingerprint for idempotent event ingestion."""
    raw = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class SharedProviderRateLimiter:
    """Coordinate request start intervals through Django's shared cache.

    The default Phase 0 cache uses Redis. If the cache cannot enforce the
    provider interval, the connector blocks the request rather than silently
    bypassing the rate limit.
    """

    lock_wait_seconds = 12.0
    lock_lease_seconds = 30

    @classmethod
    def acquire(cls, provider: str, interval_seconds: float) -> None:
        interval = max(0.0, float(interval_seconds))
        if interval == 0:
            return

        normalized_provider = provider.casefold().replace(" ", "-")
        lock_key = f"patentshield:patent-connector:{normalized_provider}:lock"
        last_key = f"patentshield:patent-connector:{normalized_provider}:last"
        token = uuid.uuid4().hex
        deadline = time.monotonic() + cls.lock_wait_seconds
        acquired = False

        while time.monotonic() < deadline:
            try:
                if cache.add(
                    lock_key,
                    token,
                    timeout=max(cls.lock_lease_seconds, int(interval) + 5),
                ):
                    acquired = True
                    break
            except Exception as exc:
                raise PatentConnectorError(
                    provider, "RATE_LIMIT_CACHE_UNAVAILABLE"
                ) from exc
            time.sleep(0.05)

        if not acquired:
            raise PatentConnectorError(provider, "RATE_LIMIT_LOCK_TIMEOUT")

        try:
            try:
                last_start = cache.get(last_key)
            except Exception as exc:
                raise PatentConnectorError(
                    provider, "RATE_LIMIT_CACHE_UNAVAILABLE"
                ) from exc
            if isinstance(last_start, (int, float)):
                sleep_seconds = interval - (time.time() - float(last_start))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            try:
                cache.set(last_key, time.time(), timeout=86_400)
            except Exception as exc:
                raise PatentConnectorError(
                    provider, "RATE_LIMIT_CACHE_UNAVAILABLE"
                ) from exc
        finally:
            try:
                if cache.get(lock_key) == token:
                    cache.delete(lock_key)
            except Exception:
                # Do not hide provider failures; an expired lock also has a TTL.
                pass


def retry_after_seconds(value: str | None) -> float | None:
    """Parse numeric or HTTP-date Retry-After values with a one-hour cap."""
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 3_600.0))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            remaining = (parsed - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, min(remaining, 3_600.0))
        except (TypeError, ValueError, OverflowError):
            return None


def response_error_code(response: requests.Response) -> str:
    """Map an HTTP response to a sanitized operational error code."""
    if response.status_code == 429:
        return "RATE_LIMITED"
    if response.status_code in {401, 403}:
        return "AUTHORIZATION_FAILED"
    if response.status_code == 404:
        return "NOT_FOUND"
    if response.status_code >= 500:
        return "PROVIDER_SERVER_ERROR"
    if response.status_code >= 400:
        return f"HTTP_{response.status_code}"
    return "UNKNOWN"
