"""USPTO Open Data Portal adapter."""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import quote

import requests
from django.conf import settings

from patents.connectors.base import (
    NormalizedLegalEvent,
    NormalizedPublication,
    PatentConnectorError,
    SharedProviderRateLimiter,
    first_value,
    hash_event_payload,
    normalize_legal_status,
    normalize_query,
    normalize_string_list,
    parse_date,
    parse_datetime,
    response_error_code,
    retry_after_seconds,
    safe_http_url,
    validate_limit,
)

DEFAULT_SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
TIMEOUT_SECONDS = 10

class USPTOODPConnector:
    """Search public application records through the official USPTO ODP API."""

    provider = "USPTO"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    def search(self, query: str, max_results: int = 10) -> list[NormalizedPublication]:
        clean_query = normalize_query(query)
        limit = validate_limit(max_results)
        api_key = (
            getattr(settings, "USPTO_ODP_API_KEY", "")
            or os.getenv("USPTO_ODP_API_KEY", "")
        ).strip()
        if not api_key:
            raise PatentConnectorError(self.provider, "NOT_CONFIGURED")

        interval = float(
            os.getenv("PATENTSHIELD_USPTO_MIN_INTERVAL_SECONDS", "1.0")
        )
        SharedProviderRateLimiter.acquire(self.provider, interval)
        endpoint = (
            getattr(settings, "PATENTSHIELD_USPTO_API_URL", "")
            or os.getenv("PATENTSHIELD_USPTO_API_URL", "")
            or DEFAULT_SEARCH_URL
        )
        # USPTO ODP's documented search contract exposes q/offset/limit as
        # query parameters. Using GET avoids sending an unsupported JSON shape.
        try:
            response = self.session.get(
                endpoint,
                headers={
                    "X-API-KEY": api_key,
                    "Accept": "application/json",
                },
                params={"q": clean_query, "offset": 0, "limit": limit},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise PatentConnectorError(self.provider, "TIMEOUT") from exc
        except requests.RequestException as exc:
            raise PatentConnectorError(self.provider, "NETWORK_ERROR") from exc

        if response.status_code >= 400:
            raise PatentConnectorError(
                self.provider,
                response_error_code(response),
                retry_after_seconds=retry_after_seconds(
                    response.headers.get("Retry-After")
                ),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PatentConnectorError(self.provider, "INVALID_JSON") from exc
        if not isinstance(payload, dict):
            raise PatentConnectorError(self.provider, "INVALID_RESPONSE")

        data = payload.get("patentFileWrapperDataBag")
        if not isinstance(data, list):
            data = payload.get("results")
        if not isinstance(data, list):
            data = payload.get("data")
        if not isinstance(data, list):
            raise PatentConnectorError(self.provider, "UNEXPECTED_RESULT_SHAPE")

        publications: list[NormalizedPublication] = []
        for item in data[:limit]:
            if not isinstance(item, dict):
                continue
            normalized = self._map_record(item)
            if normalized:
                publications.append(normalized)
        return publications

    def fetch_application(
        self, application_number: str
    ) -> NormalizedPublication | None:
        """Fetch one application by its official application serial number.

        The endpoint is a direct lookup. Only digits are accepted because the
        ODP application path expects the serial number without presentation
        punctuation. A 404 is an authoritative not-found result; transport,
        authentication, and rate-limit errors remain distinguishable.
        """
        supplied_number = str(application_number or "").strip()
        if not re.fullmatch(r"[0-9\s/.,-]+", supplied_number):
            raise PatentConnectorError(self.provider, "INVALID_APPLICATION_NUMBER")
        digits = re.sub(r"\D", "", supplied_number)
        if not digits or len(digits) > 20:
            raise PatentConnectorError(self.provider, "INVALID_APPLICATION_NUMBER")

        api_key = (
            getattr(settings, "USPTO_ODP_API_KEY", "")
            or os.getenv("USPTO_ODP_API_KEY", "")
        ).strip()
        if not api_key:
            raise PatentConnectorError(self.provider, "NOT_CONFIGURED")

        endpoint = (
            getattr(settings, "PATENTSHIELD_USPTO_API_URL", "")
            or os.getenv("PATENTSHIELD_USPTO_API_URL", "")
            or DEFAULT_SEARCH_URL
        ).rstrip("/")
        if not endpoint.endswith("/search"):
            raise PatentConnectorError(self.provider, "INVALID_ENDPOINT_CONFIGURATION")
        detail_url = endpoint[:-len("/search")] + "/" + quote(digits, safe="")

        interval = float(
            os.getenv("PATENTSHIELD_USPTO_MIN_INTERVAL_SECONDS", "1.0")
        )
        SharedProviderRateLimiter.acquire(self.provider, interval)
        try:
            response = self.session.get(
                detail_url,
                headers={"X-API-KEY": api_key, "Accept": "application/json"},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise PatentConnectorError(self.provider, "DETAIL_TIMEOUT") from exc
        except requests.RequestException as exc:
            raise PatentConnectorError(self.provider, "DETAIL_NETWORK_ERROR") from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise PatentConnectorError(
                self.provider,
                "DETAIL_" + response_error_code(response),
                retry_after_seconds=retry_after_seconds(
                    response.headers.get("Retry-After")
                ),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PatentConnectorError(self.provider, "DETAIL_INVALID_JSON") from exc
        if not isinstance(payload, dict):
            raise PatentConnectorError(self.provider, "DETAIL_INVALID_RESPONSE")

        candidate = payload.get("patentFileWrapperData")
        if isinstance(candidate, dict):
            payload = candidate
        bag = payload.get("patentFileWrapperDataBag")
        if isinstance(bag, list):
            match = next(
                (
                    row for row in bag
                    if isinstance(row, dict)
                    and re.sub(r"\D", "", str(
                        row.get("applicationNumberText", "")
                        or (row.get("applicationMetaData") or {}).get(
                            "applicationNumberText", ""
                        )
                    )) == digits
                ),
                None,
            )
            payload = match or {}
        record = self._map_record(payload)
        if record is None:
            return None
        returned_number = re.sub(r"\D", "", record.application_number or "")
        if returned_number and returned_number != digits:
            raise PatentConnectorError(self.provider, "DETAIL_IDENTIFIER_MISMATCH")
        return record

    @staticmethod
    def _map_record(record: dict[str, Any]) -> NormalizedPublication | None:
        metadata = record.get("applicationMetaData")
        if not isinstance(metadata, dict):
            metadata = {}

        application_number = first_value(
            record.get("applicationNumberText"),
            metadata.get("applicationNumberText"),
            metadata.get("applicationNumber"),
            record.get("applicationNumber"),
        )
        publication_number = first_value(
            metadata.get("publicationNumber"),
            record.get("publicationNumber"),
            metadata.get("publicationNumberText"),
        )
        patent_number = first_value(
            metadata.get("patentNumber"),
            record.get("patentNumber"),
            metadata.get("patentNumberText"),
        )
        if not publication_number:
            publication_number = patent_number

        # Application number is the stable identity across publication/grant updates.
        provider_record_id = application_number or publication_number
        if not provider_record_id:
            return None

        title = first_value(
            metadata.get("inventionTitle"),
            metadata.get("inventionTitleText"),
            record.get("inventionTitle"),
            record.get("title"),
        ) or "Untitled USPTO publication"
        abstract = first_value(
            metadata.get("abstractText"),
            metadata.get("abstract"),
            record.get("abstractText"),
            record.get("abstract"),
        )
        filing_date = parse_date(metadata.get("filingDate") or record.get("filingDate"))
        publication_date = parse_date(
            metadata.get("publicationDate") or record.get("publicationDate")
        )
        grant_date = parse_date(metadata.get("grantDate") or record.get("grantDate"))
        status_text = first_value(
            metadata.get("applicationStatusDescriptionText"),
            metadata.get("applicationStatus"),
            record.get("applicationStatusDescriptionText"),
            record.get("status"),
        )
        legal_status = normalize_legal_status(
            status_text or ("GRANTED" if grant_date else None)
        )

        source_url = None
        if application_number:
            source_url = safe_http_url(
                "https://patentcenter.uspto.gov/applications/"
                + quote(application_number, safe="")
            )

        selected_metadata = {
            key: value
            for key, value in {
                "application_number": application_number,
                "publication_number": publication_number,
                "patent_number": patent_number,
                "filing_date": metadata.get("filingDate"),
                "publication_date": metadata.get("publicationDate"),
                "grant_date": metadata.get("grantDate"),
                "application_status": status_text,
                "application_status_date": metadata.get("applicationStatusDate"),
                "last_modified": metadata.get("lastModifiedDateTime"),
            }.items()
            if value is not None and isinstance(value, (str, int, float, bool))
        }

        applicants = normalize_string_list(
            metadata.get(
                "applicantBag",
                record.get("applicantBag", record.get("applicants")),
            )
        )
        inventors = normalize_string_list(
            metadata.get(
                "inventorBag",
                record.get("inventorBag", record.get("inventors")),
            )
        )
        ipc_codes = _extract_codes(
            record, metadata, ("ipcCode", "ipcClassification", "ipcClassificationBag")
        )
        cpc_codes = _extract_codes(
            record, metadata, ("cpcCode", "cpcClassification", "cpcClassificationBag")
        )

        family_identifier = first_value(
            metadata.get("familyIdentifier"),
            metadata.get("familyId"),
            record.get("familyIdentifier"),
        )
        legal_events = _extract_legal_events(record, metadata, source_url)

        return NormalizedPublication(
            provider="USPTO",
            provider_record_id=str(provider_record_id).strip(),
            publication_number=(
                str(publication_number).strip() if publication_number else None
            ),
            application_number=(
                str(application_number).strip() if application_number else None
            ),
            jurisdiction="US",
            title=str(title).strip()[:4_000],
            abstract=str(abstract).strip()[:20_000] if abstract else None,
            filing_date=filing_date,
            publication_date=publication_date,
            grant_date=grant_date,
            legal_status=legal_status,
            applicant_names=applicants,
            inventor_names=inventors,
            ipc_codes=ipc_codes,
            cpc_codes=cpc_codes,
            family_system="USPTO" if family_identifier else None,
            family_identifier=str(family_identifier) if family_identifier else None,
            source_url=source_url,
            source_updated_at=parse_datetime(metadata.get("lastModifiedDateTime")),
            source_payload={
                "application_number": application_number,
                "publication_number": publication_number,
                "patent_number": patent_number,
                "title": title,
                "abstract": abstract,
                "filing_date": filing_date.isoformat() if filing_date else None,
                "publication_date": (
                    publication_date.isoformat() if publication_date else None
                ),
                "grant_date": grant_date.isoformat() if grant_date else None,
                "legal_status": legal_status,
                "applicant_names": applicants,
                "inventor_names": inventors,
                "ipc_codes": ipc_codes,
                "cpc_codes": cpc_codes,
                "family_identifier": family_identifier,
                "source_metadata": selected_metadata,
            },
            legal_events=legal_events,
        )


def _extract_legal_events(
    record: dict[str, Any],
    metadata: dict[str, Any],
    source_url: str | None,
) -> list[NormalizedLegalEvent]:
    """Normalize selected USPTO file-wrapper events without storing raw envelopes."""
    event_bag = record.get("eventDataBag")
    if not isinstance(event_bag, list):
        event_bag = metadata.get("eventDataBag")
    if not isinstance(event_bag, list):
        return []

    events: list[NormalizedLegalEvent] = []
    for index, item in enumerate(event_bag):
        if not isinstance(item, dict):
            continue
        code = first_value(
            item.get("eventCode"),
            item.get("eventCodeText"),
            item.get("eventTypeCode"),
            item.get("eventType"),
        ) or "UNKNOWN"
        event_date = parse_date(
            item.get("eventDate")
            or item.get("eventDateTime")
            or item.get("eventEffectiveDate")
        )
        title = first_value(
            item.get("eventDescriptionText"),
            item.get("eventText"),
            item.get("eventTypeDescriptionText"),
            item.get("eventName"),
            item.get("description"),
        ) or code
        status_after = first_value(
            item.get("applicationStatusDescriptionText"),
            item.get("applicationStatus"),
            item.get("statusAfter"),
        )
        selected_payload = {
            "event_code": code[:100],
            "event_date": event_date.isoformat() if event_date else None,
            "title": title[:500],
            "status_after": status_after[:200] if status_after else None,
        }
        if item.get("sequenceNumber") is not None:
            selected_payload["source_sequence"] = str(item["sequenceNumber"])[:100]
        source_event_id = first_value(
            item.get("eventIdentifier"),
            item.get("eventId"),
            item.get("eventID"),
            item.get("transactionId"),
        )
        if not source_event_id:
            source_event_id = hash_event_payload(selected_payload)
        events.append(
            NormalizedLegalEvent(
                source_event_id=str(source_event_id)[:200],
                event_code=code[:100],
                event_date=event_date,
                title=title[:500],
                description=title[:10_000],
                legal_status_after=normalize_legal_status(status_after),
                source_url=source_url,
                source_payload=selected_payload,
            )
        )
    return events


def _extract_codes(
    record: dict[str, Any],
    metadata: dict[str, Any],
    keys: tuple[str, ...],
) -> list[str]:
    """Extract classification strings without retaining arbitrary provider JSON."""
    found: list[str] = []
    candidates = [record, metadata]
    for container in candidates:
        for key in keys:
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
            elif isinstance(value, list):
                for entry in value:
                    if isinstance(entry, str) and entry.strip():
                        found.append(entry.strip())
                    elif isinstance(entry, dict):
                        for value_key in (
                            "symbol",
                            "code",
                            "cpcSymbolText",
                            "ipcSymbolText",
                            "classificationSymbol",
                        ):
                            code = entry.get(value_key)
                            if isinstance(code, str) and code.strip():
                                found.append(code.strip())
    return list(dict.fromkeys(found))
