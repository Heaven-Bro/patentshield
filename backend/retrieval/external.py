"""Confidentiality-gated external prior-art search adapters for PatentShield.

The external providers receive the search text. Calls are therefore disabled by
_default_ and require explicit deployment-level opt-in plus a tenant allowlist.
Only HMAC query hashes and operational metadata are persisted to AuditLog.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import quote, urlparse, urlunparse

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, ValidationError
from pydantic import BaseModel, Field

from audit.models import AuditLog
from tenants.models import Tenant

logger = logging.getLogger(__name__)

USPTO_SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
HTTP_TIMEOUT_SECONDS = 10
MAX_QUERY_LENGTH = 1_000
MAX_PROVIDER_RESULTS = 50
_RATE_LIMIT_LOCK_WAIT_SECONDS = 10.0
_RATE_LIMIT_LOCK_LEASE_SECONDS = 30
_LAST_REQUEST_TTL_SECONDS = 86_400

USPTO_RESULT_FIELDS = ",".join(
    (
        "applicationNumberText",
        "applicationMetaData.inventionTitle",
        "applicationMetaData.publicationNumber",
        "applicationMetaData.patentNumber",
        "applicationMetaData.publicationDate",
        "applicationMetaData.filingDate",
        "applicationMetaData.grantDate",
        "applicationMetaData.abstractText",
    )
)

_PATENT_DOMAIN_MARKERS = (
    "patents.google.com",
    "patentscope.wipo.int",
    "worldwide.espacenet.com",
    "espacenet.com",
    "patentcenter.uspto.gov",
    "ppubs.uspto.gov",
    "uspto.gov",
    "patents.justia.com",
)
_PATENT_ID_PATTERN = re.compile(
    r"\b(?:US|WO|EP|CN|JP|KR|DE|GB)\s*\d{6,14}\s*[A-Z]\d?\b",
    flags=re.IGNORECASE,
)


class ExternalSearchError(RuntimeError):
    """Base error for an external search provider failure."""


class ExternalSearchPolicyError(ExternalSearchError):
    """Raised when outbound query disclosure is not explicitly authorized."""


class ExternalSearchRateLimited(ExternalSearchError):
    """Raised when a provider returns HTTP 429."""

    def __init__(self, provider: str, retry_after_seconds: float | None = None) -> None:
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds
        retry_text = (
            f" Retry after {retry_after_seconds:.1f} seconds."
            if retry_after_seconds is not None
            else ""
        )
        super().__init__(f"{provider} rate limit reached.{retry_text}")


class ExternalSearchResult(BaseModel):
    """A provenance-preserving result returned by an external search provider."""

    source_provider: Literal["USPTO", "TAVILY"]
    publication_id: str | None = None
    title: str
    snippet: str
    source_url: str | None = None
    retrieval_timestamp: str
    query_hash: str
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class DistributedProviderRateLimiter:
    """Apply a provider-wide request start interval through Django's cache.

    Configure Django's default cache to use Redis (or another shared cache with
    atomic ``add``) in multi-worker deployments. Local-memory caches only rate
    limit within a process and are not sufficient for production clustering.
    """

    @classmethod
    def acquire(
        cls,
        provider: str,
        min_interval_seconds: float,
    ) -> None:
        """Wait for the provider's next permitted request slot."""
        interval = max(0.0, float(min_interval_seconds))
        if interval == 0:
            return

        lock_key = f"patentshield:external-search:{provider.lower()}:rate-lock"
        last_key = f"patentshield:external-search:{provider.lower()}:last-start"
        lock_token = uuid.uuid4().hex
        deadline = time.monotonic() + _RATE_LIMIT_LOCK_WAIT_SECONDS

        try:
            acquired = False
            while time.monotonic() < deadline:
                if cache.add(
                    lock_key,
                    lock_token,
                    timeout=max(
                        _RATE_LIMIT_LOCK_LEASE_SECONDS,
                        int(interval) + 5,
                    ),
                ):
                    acquired = True
                    break
                time.sleep(0.05)
            if not acquired:
                raise ExternalSearchError(
                    f"Could not acquire the {provider} rate-limit lock."
                )

            try:
                last_start = cache.get(last_key)
                if isinstance(last_start, (int, float)):
                    wait_seconds = interval - (time.time() - float(last_start))
                    if wait_seconds > 0:
                        time.sleep(wait_seconds)
                cache.set(
                    last_key,
                    time.time(),
                    timeout=_LAST_REQUEST_TTL_SECONDS,
                )
            finally:
                # Cache.delete is only performed if this process still owns the
                # lock; this avoids deleting a lock acquired after lease expiry.
                if cache.get(lock_key) == lock_token:
                    cache.delete(lock_key)
        except ExternalSearchError:
            raise
        except Exception as exc:
            logger.warning(
                "External-search rate limiter unavailable for %s (%s).",
                provider,
                type(exc).__name__,
            )
            raise ExternalSearchError(
                "External-search rate limiter is unavailable; request blocked."
            ) from exc


class USPTOSearchAdapter:
    """Search the official USPTO Open Data Portal Patent File Wrapper API."""

    provider = "USPTO"

    def search(
        self,
        keywords: list[str],
        max_results: int = 10,
        *,
        query_hash: str | None = None,
    ) -> list[ExternalSearchResult]:
        """Search public patent application records by the supplied keywords.

        Uses the official ``/api/v1/patent/applications/search`` endpoint. The
        API key is read from server-side configuration and is never returned in
        result metadata or application logs. HTTP requests have a hard
        10-second timeout.
        """
        query = _normalise_query(" ".join(_clean_query_part(k) for k in keywords))
        if not query:
            return []
        _require_provider_disclosure_opt_in()
        api_key = _configured_secret(
            setting_names=("USPTO_ODP_API_KEY", "USPTO_API_KEY"),
            env_names=("USPTO_ODP_API_KEY", "USPTO_API_KEY"),
        )
        if not api_key:
            raise ImproperlyConfigured(
                "Configure USPTO_ODP_API_KEY to use the official USPTO API."
            )

        result_limit = _validate_max_results(max_results)
        request_hash = query_hash or _hash_query(query)
        rate_interval = float(
            getattr(settings, "PATENTSHIELD_USPTO_MIN_INTERVAL_SECONDS", 1.0)
        )
        DistributedProviderRateLimiter.acquire("USPTO", rate_interval)

        try:
            response = requests.post(
                getattr(settings, "PATENTSHIELD_USPTO_API_URL", USPTO_SEARCH_URL),
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
                json={
                    "q": query,
                    "limit": result_limit,
                    "offset": 0,
                    "fields": USPTO_RESULT_FIELDS,
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise ExternalSearchError("USPTO request timed out.") from exc
        except requests.RequestException as exc:
            raise ExternalSearchError("USPTO request failed.") from exc

        _raise_for_provider_response(response, self.provider)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalSearchError("USPTO returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ExternalSearchError("USPTO returned an unexpected response shape.")

        records = payload.get("patentFileWrapperDataBag", [])
        if not isinstance(records, list):
            raise ExternalSearchError("USPTO returned an invalid result collection.")

        results: list[ExternalSearchResult] = []
        timestamp = _utc_timestamp()
        for record in records[:result_limit]:
            if not isinstance(record, dict):
                continue
            result = _map_uspto_record(record, request_hash, timestamp)
            if result is not None:
                results.append(result)
        return results


class TavilySearchAdapter:
    """Search the Tavily API and prioritize results from patent repositories."""

    provider = "TAVILY"

    def search(
        self,
        query: str,
        max_results: int = 5,
        *,
        query_hash: str | None = None,
    ) -> list[ExternalSearchResult]:
        """Run a structured Tavily web search with raw-page capture disabled."""
        clean_query = _normalise_query(query)
        if not clean_query:
            return []
        _require_provider_disclosure_opt_in()
        api_key = _configured_secret(
            setting_names=("TAVILY_API_KEY",),
            env_names=("TAVILY_API_KEY",),
        )
        if not api_key:
            raise ImproperlyConfigured(
                "Configure TAVILY_API_KEY to use Tavily search."
            )

        result_limit = _validate_max_results(max_results)
        request_hash = query_hash or _hash_query(clean_query)
        rate_interval = float(
            getattr(settings, "PATENTSHIELD_TAVILY_MIN_INTERVAL_SECONDS", 0.25)
        )
        DistributedProviderRateLimiter.acquire("TAVILY", rate_interval)

        try:
            response = requests.post(
                getattr(settings, "PATENTSHIELD_TAVILY_API_URL", TAVILY_SEARCH_URL),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json={
                    "api_key": api_key,
                    "query": clean_query,
                    "topic": "general",
                    "search_depth": "basic",
                    "max_results": result_limit,
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                },
                timeout=HTTP_TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise ExternalSearchError("Tavily request timed out.") from exc
        except requests.RequestException as exc:
            raise ExternalSearchError("Tavily request failed.") from exc

        _raise_for_provider_response(response, self.provider)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalSearchError("Tavily returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise ExternalSearchError("Tavily returned an unexpected response shape.")

        raw_results = payload.get("results", [])
        if not isinstance(raw_results, list):
            raise ExternalSearchError("Tavily returned an invalid result collection.")

        timestamp = _utc_timestamp()
        mapped: list[tuple[bool, ExternalSearchResult]] = []
        for item in raw_results[:result_limit]:
            if not isinstance(item, dict):
                continue
            result = _map_tavily_result(item, request_hash, timestamp)
            if result is not None:
                mapped.append(
                    (_is_patent_source(result.source_url, result.title), result)
                )

        # Keep general web prior-art sources, but rank known patent repositories
        # first. This avoids losing useful standards, papers, or product manuals.
        mapped.sort(key=lambda pair: pair[0], reverse=True)
        return [result for _, result in mapped]


class ExternalSearchAggregator:
    """Run authorized provider searches, deduplicate results, and audit safely."""

    def __init__(
        self,
        uspto_adapter: USPTOSearchAdapter | None = None,
        tavily_adapter: TavilySearchAdapter | None = None,
    ) -> None:
        self.uspto_adapter = uspto_adapter or USPTOSearchAdapter()
        self.tavily_adapter = tavily_adapter or TavilySearchAdapter()

    def search_prior_art(
        self,
        query: str,
        tenant_id: str | uuid.UUID,
        enable_uspto: bool = True,
        enable_tavily: bool = True,
    ) -> list[ExternalSearchResult]:
        """Search selected external providers concurrently and audit metadata.

        Patent queries can contain unpublished invention details. This method
        requires an active tenant, two explicit global disclosure switches, and
        tenant allowlisting unless the deployment explicitly opts into all
        tenants. Audit entries contain no raw query, API key, headers, or raw
        provider response bodies.
        """
        clean_query = _normalise_query(query)
        if not clean_query:
            raise ValueError("query must be a non-empty string.")
        tenant = _get_active_tenant(tenant_id)
        query_hash = _hash_query(clean_query, tenant_id=str(tenant.pk))
        request_id = str(uuid.uuid4())
        enabled = {
            "USPTO": bool(enable_uspto),
            "TAVILY": bool(enable_tavily),
        }
        provider_meta: dict[str, dict[str, Any]] = {
            provider: {
                "enabled": is_enabled,
                "status": "skipped" if not is_enabled else "pending",
                "result_count": 0,
                "elapsed_ms": 0,
            }
            for provider, is_enabled in enabled.items()
        }

        if not any(enabled.values()):
            self._write_audit(
                tenant=tenant,
                query_hash=query_hash,
                request_id=request_id,
                outcome=AuditLog.Outcome.SUCCESS,
                provider_metadata=provider_meta,
                result_count=0,
            )
            return []

        try:
            _require_tenant_disclosure_opt_in(str(tenant.pk))
        except ExternalSearchPolicyError:
            for provider, is_enabled in enabled.items():
                if is_enabled:
                    provider_meta[provider]["status"] = "denied"
            self._write_audit(
                tenant=tenant,
                query_hash=query_hash,
                request_id=request_id,
                outcome=AuditLog.Outcome.DENIED,
                provider_metadata=provider_meta,
                result_count=0,
            )
            raise

        started_at = time.monotonic()
        call_specs: dict[str, tuple[Any, tuple[Any, ...], dict[str, Any]]] = {}
        if enable_uspto:
            keywords = _keywords_for_uspto(clean_query)
            call_specs["USPTO"] = (
                self.uspto_adapter.search,
                (keywords,),
                {"max_results": _provider_limit("USPTO", 10), "query_hash": query_hash},
            )
        if enable_tavily:
            call_specs["TAVILY"] = (
                self.tavily_adapter.search,
                (clean_query,),
                {"max_results": _provider_limit("TAVILY", 5), "query_hash": query_hash},
            )

        provider_results: dict[str, list[ExternalSearchResult]] = {}
        with ThreadPoolExecutor(max_workers=max(1, len(call_specs))) as executor:
            futures: dict[Any, str] = {}
            future_started_at: dict[Any, float] = {}
            for provider, (function, args, kwargs) in call_specs.items():
                future = executor.submit(function, *args, **kwargs)
                futures[future] = provider
                future_started_at[future] = time.monotonic()

            for future, provider in futures.items():
                try:
                    records = future.result()
                    provider_results[provider] = records
                    provider_meta[provider]["status"] = "success"
                    provider_meta[provider]["result_count"] = len(records)
                except Exception as exc:
                    provider_results[provider] = []
                    provider_meta[provider]["status"] = "error"
                    provider_meta[provider]["error_code"] = _safe_error_code(exc)
                    if isinstance(exc, ExternalSearchRateLimited):
                        provider_meta[provider]["retry_after_seconds"] = (
                            exc.retry_after_seconds
                        )
                    logger.warning(
                        "External prior-art provider failed (provider=%s, type=%s).",
                        provider,
                        type(exc).__name__,
                    )
                finally:
                    provider_meta[provider]["elapsed_ms"] = int(
                        (time.monotonic() - future_started_at[future]) * 1000
                    )

        combined: list[ExternalSearchResult] = []
        for provider in ("USPTO", "TAVILY"):
            for result in provider_results.get(provider, []):
                # All records from this invocation share a tenant-scoped,
                # keyed hash that can be correlated safely with the audit event.
                result.query_hash = query_hash
                combined.append(result)

        deduplicated = _deduplicate_results(combined)
        provider_failures = any(
            meta["status"] == "error" for meta in provider_meta.values()
        )
        provider_successes = any(
            meta["status"] == "success" for meta in provider_meta.values()
        )
        if not provider_successes:
            audit_outcome = AuditLog.Outcome.FAILURE
        elif provider_failures:
            audit_outcome = AuditLog.Outcome.FAILURE
        else:
            audit_outcome = AuditLog.Outcome.SUCCESS

        self._write_audit(
            tenant=tenant,
            query_hash=query_hash,
            request_id=request_id,
            outcome=audit_outcome,
            provider_metadata=provider_meta,
            result_count=len(deduplicated),
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
        )

        if not provider_successes:
            raise ExternalSearchError(
                "All enabled external prior-art providers failed. See audit metadata."
            )
        return deduplicated

    @staticmethod
    def _write_audit(
        *,
        tenant: Tenant,
        query_hash: str,
        request_id: str,
        outcome: str,
        provider_metadata: dict[str, dict[str, Any]],
        result_count: int,
        elapsed_ms: int | None = None,
    ) -> None:
        """Persist only safe search telemetry; never persist raw query text."""
        metadata: dict[str, Any] = {
            "query_hash": query_hash,
            "provider_results": provider_metadata,
            "result_count": result_count,
            "request_id": request_id,
            "query_disclosure_authorized": any(
                meta.get("status") in {"success", "error", "pending"}
                for meta in provider_metadata.values()
            ),
        }
        if elapsed_ms is not None:
            metadata["elapsed_ms"] = elapsed_ms
        try:
            AuditLog.objects.create(
                tenant=tenant,
                action="EXTERNAL_PATENT_SEARCH",
                resource_type="ExternalSearch",
                request_id=request_id,
                outcome=outcome,
                metadata=metadata,
            )
        except Exception:
            # Audit storage failures are non-blocking for search availability;
            # alerting/metrics should monitor this log event in production.
            logger.exception(
                "Could not persist external-search audit event (request_id=%s).",
                request_id,
            )


def _require_provider_disclosure_opt_in() -> None:
    """Fail closed unless outbound search and query disclosure are enabled."""
    if not _setting_bool("PATENTSHIELD_EXTERNAL_SEARCH_ENABLED", False):
        raise ExternalSearchPolicyError(
            "External search is disabled by deployment policy."
        )
    if not _setting_bool(
        "PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE",
        False,
    ):
        raise ExternalSearchPolicyError(
            "External query disclosure is not authorized by deployment policy."
        )


def _require_tenant_disclosure_opt_in(tenant_id: str) -> None:
    """Enforce provider-wide opt-in and per-tenant disclosure authorization."""
    _require_provider_disclosure_opt_in()
    if _setting_bool("PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_ALL_TENANTS", False):
        return
    allowed_tenants = getattr(
        settings,
        "PATENTSHIELD_EXTERNAL_SEARCH_ALLOWED_TENANTS",
        (),
    )
    if allowed_tenants is None:
        allowed_tenants = ()
    elif isinstance(allowed_tenants, str):
        allowed_tenants = (allowed_tenants,)
    allowed_ids = {str(value).strip() for value in allowed_tenants}
    if tenant_id not in allowed_ids:
        raise ExternalSearchPolicyError(
            "This tenant is not authorized to disclose queries to external providers."
        )


def _configured_secret(
    *,
    setting_names: tuple[str, ...],
    env_names: tuple[str, ...],
) -> str | None:
    """Read the first configured secret without logging its value."""
    for name in setting_names:
        value = getattr(settings, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for name in env_names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _normalise_query(query: str) -> str:
    """Normalize whitespace and reject unreasonable outbound query lengths."""
    if not isinstance(query, str):
        raise TypeError("query must be a string.")
    clean_query = " ".join(query.split()).strip()
    if len(clean_query) > MAX_QUERY_LENGTH:
        raise ValueError(
            f"Search query exceeds the {MAX_QUERY_LENGTH}-character limit."
        )
    # Drop control characters; they have no useful search function and can
    # complicate provider-side audit and log parsing.
    clean_query = "".join(char for char in clean_query if char.isprintable())
    return clean_query


def _clean_query_part(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("USPTO keywords must be strings.")
    return " ".join(value.split()).strip()


def _validate_max_results(max_results: int) -> int:
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise TypeError("max_results must be an integer.")
    if max_results < 1:
        raise ValueError("max_results must be at least 1.")
    return min(max_results, MAX_PROVIDER_RESULTS)


def _hash_query(query: str, *, tenant_id: str | None = None) -> str:
    """Return a keyed SHA-256 HMAC instead of a guessable plain query hash."""
    secret = getattr(settings, "PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY", None)
    if not isinstance(secret, str) or not secret:
        secret = getattr(settings, "SECRET_KEY", None)
    if not isinstance(secret, str) or not secret:
        raise ImproperlyConfigured(
            "Django SECRET_KEY or PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY is required."
        )
    scoped_query = f"{tenant_id or ''}\0{_normalise_query(query).casefold()}"
    return hmac.new(
        secret.encode("utf-8"),
        scoped_query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _get_active_tenant(tenant_id: str | uuid.UUID) -> Tenant:
    try:
        return Tenant.objects.get(pk=tenant_id, status=Tenant.Status.ACTIVE)
    except (Tenant.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
        raise ExternalSearchPolicyError(
            "External search requires a valid active tenant."
        ) from exc


def _raise_for_provider_response(
    response: requests.Response,
    provider: str,
) -> None:
    """Translate provider status into sanitized integration errors."""
    if response.status_code == 429:
        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        logger.warning("External provider rate limited the request (%s).", provider)
        raise ExternalSearchRateLimited(provider, retry_after)
    if response.status_code in {401, 403}:
        raise ExternalSearchError(f"{provider} rejected API credentials or access.")
    if response.status_code >= 500:
        raise ExternalSearchError(f"{provider} returned a server error.")
    if response.status_code >= 400:
        raise ExternalSearchError(
            f"{provider} rejected the request (HTTP {response.status_code})."
        )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(float(value), 3600.0))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
            return max(0.0, min(seconds, 3600.0))
        except (TypeError, ValueError, OverflowError):
            return None


def _map_uspto_record(
    record: dict[str, Any],
    query_hash: str,
    timestamp: str,
) -> ExternalSearchResult | None:
    metadata = record.get("applicationMetaData")
    metadata = metadata if isinstance(metadata, dict) else {}
    application_number = _first_non_empty(
        record.get("applicationNumberText"),
        metadata.get("applicationNumberText"),
    )
    publication_number = _first_non_empty(
        metadata.get("publicationNumber"),
        record.get("publicationNumber"),
    )
    patent_number = _first_non_empty(
        metadata.get("patentNumber"),
        record.get("patentNumber"),
    )
    publication_id = publication_number or patent_number or application_number
    title = _first_non_empty(
        metadata.get("inventionTitle"),
        metadata.get("inventionTitleText"),
        record.get("title"),
    ) or "Untitled USPTO patent record"
    snippet = _first_non_empty(
        metadata.get("abstractText"),
        metadata.get("abstract"),
        title,
    ) or title
    if not publication_id and title == "Untitled USPTO patent record":
        return None

    if application_number:
        source_url = (
            "https://patentcenter.uspto.gov/applications/"
            f"{quote(application_number, safe='')}"
        )
    else:
        source_url = "https://ppubs.uspto.gov/pubwebapp/"

    safe_metadata = {
        key: value
        for key, value in {
            "application_number": application_number,
            "publication_number": publication_number,
            "patent_number": patent_number,
            "publication_date": metadata.get("publicationDate"),
            "filing_date": metadata.get("filingDate"),
            "grant_date": metadata.get("grantDate"),
        }.items()
        if isinstance(value, (str, int, float, bool))
    }
    return ExternalSearchResult(
        source_provider="USPTO",
        publication_id=str(publication_id) if publication_id else None,
        title=_truncate(str(title), 500),
        snippet=_truncate(str(snippet), 2_000),
        source_url=source_url,
        retrieval_timestamp=timestamp,
        query_hash=query_hash,
        raw_metadata=safe_metadata,
    )


def _map_tavily_result(
    item: dict[str, Any],
    query_hash: str,
    timestamp: str,
) -> ExternalSearchResult | None:
    title = _first_non_empty(item.get("title"))
    snippet = _first_non_empty(item.get("content"), item.get("snippet"))
    source_url = _safe_http_url(_first_non_empty(item.get("url")))
    if not title or not source_url:
        return None
    score = item.get("score")
    safe_metadata: dict[str, Any] = {"domain": urlparse(source_url).hostname or ""}
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        safe_metadata["provider_score"] = float(score)
    published_date = item.get("published_date")
    if isinstance(published_date, str) and len(published_date) <= 40:
        safe_metadata["published_date"] = published_date
    is_patent_source = _is_patent_source(source_url, title)
    safe_metadata["patent_source_match"] = is_patent_source
    publication_id = _extract_publication_id(f"{title} {source_url}")
    return ExternalSearchResult(
        source_provider="TAVILY",
        publication_id=publication_id,
        title=_truncate(title, 500),
        snippet=_truncate(snippet or title, 2_000),
        source_url=source_url,
        retrieval_timestamp=timestamp,
        query_hash=query_hash,
        raw_metadata=safe_metadata,
    )


def _first_non_empty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return None


def _truncate(value: str, max_length: int) -> str:
    clean_value = " ".join(value.split())
    return clean_value[:max_length]


def _safe_http_url(value: str | None) -> str | None:
    if not value or len(value) > 2_048:
        return None
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    # Drop query strings/fragments because they may contain tracking IDs,
    # search terms, or signed tokens; provenance remains at the source page URL.
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _is_patent_source(source_url: str | None, title: str) -> bool:
    host = (urlparse(source_url).hostname or "").lower() if source_url else ""
    if any(
        host == marker or host.endswith(f".{marker}")
        for marker in _PATENT_DOMAIN_MARKERS
    ):
        return True
    return bool(re.search(r"\bpatent(?:s)?\b|prior art", title, flags=re.IGNORECASE))


def _extract_publication_id(text: str) -> str | None:
    match = _PATENT_ID_PATTERN.search(text)
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(0)).upper()


def _deduplicate_results(
    results: list[ExternalSearchResult],
) -> list[ExternalSearchResult]:
    """Deduplicate on publication identifier or normalized source URL."""
    seen_publications: set[str] = set()
    seen_urls: set[str] = set()
    deduplicated: list[ExternalSearchResult] = []
    for result in results:
        publication_key = _normalise_publication_id(result.publication_id)
        url_key = _normalise_source_url(result.source_url)
        if (
            publication_key and publication_key in seen_publications
        ) or (url_key and url_key in seen_urls):
            continue
        deduplicated.append(result)
        if publication_key:
            seen_publications.add(publication_key)
        if url_key:
            seen_urls.add(url_key)
    return deduplicated


def _normalise_publication_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return normalized or None


def _normalise_source_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if not parsed.hostname:
        return value.casefold().rstrip("/")
    path = parsed.path.rstrip("/")
    if path.casefold() in {"", "/", "/pubwebapp", "/search"}:
        return None
    return urlunparse(
        (parsed.scheme.casefold(), parsed.hostname.casefold(), path, "", "", "")
    )


def _keywords_for_uspto(query: str) -> list[str]:
    """Reduce an authorized search string to distinct, bounded USPTO terms."""
    terms = [term for term in re.findall(r"[\w-]+", query) if len(term) > 1]
    unique_terms = list(dict.fromkeys(terms))[:12]
    return unique_terms or [query]


def _provider_limit(provider: str, default: int) -> int:
    setting_name = f"PATENTSHIELD_{provider}_MAX_RESULTS"
    configured = getattr(settings, setting_name, default)
    try:
        configured_int = int(configured)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"{setting_name} must be an integer.") from exc
    return _validate_max_results(configured_int)


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, ExternalSearchRateLimited):
        return "RATE_LIMITED"
    if isinstance(exc, requests.Timeout):
        return "TIMEOUT"
    if isinstance(exc, ImproperlyConfigured):
        return "CONFIGURATION_ERROR"
    if isinstance(exc, ExternalSearchPolicyError):
        return "POLICY_DENIED"
    if isinstance(exc, ExternalSearchError):
        return "PROVIDER_ERROR"
    return "UNEXPECTED_ERROR"


def _setting_bool(name: str, default: bool) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
