"""European Patent Office Open Patent Services (OPS) connector.

OPS uses OAuth 2.0 and returns XML. The adapter preserves source identifiers
and deliberately stores original-source evidence rather than translated text.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.cache import cache

from patents.connectors.base import (
    NormalizedLegalEvent,
    NormalizedPublication,
    PatentConnectorError,
    SharedProviderRateLimiter,
    first_value,
    hash_event_payload,
    normalize_legal_status,
    normalize_query,
    parse_date,
    response_error_code,
    retry_after_seconds,
    safe_http_url,
    validate_limit,
)

OPS_BASE_URL = "https://ops.epo.org/3.2/rest-services"
TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
TIMEOUT_SECONDS = 10
_TOKEN_SAFETY_MARGIN = 60


class EPOOPSConnector:
    """Search EPO worldwide bibliographic records and fetch legal events."""

    provider = "EPO"

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.base_url = (
            getattr(settings, "PATENTSHIELD_EPO_OPS_BASE_URL", "")
            or os.getenv("PATENTSHIELD_EPO_OPS_BASE_URL", "")
            or OPS_BASE_URL
        ).rstrip("/")
        self.token_url = (
            getattr(settings, "PATENTSHIELD_EPO_OPS_TOKEN_URL", "")
            or os.getenv("PATENTSHIELD_EPO_OPS_TOKEN_URL", "")
            or TOKEN_URL
        )

    def _credentials(self) -> tuple[str, str]:
        client_id = (
            getattr(settings, "EPO_OPS_CLIENT_ID", "")
            or os.getenv("EPO_OPS_CLIENT_ID", "")
        ).strip()
        client_secret = (
            getattr(settings, "EPO_OPS_CLIENT_SECRET", "")
            or os.getenv("EPO_OPS_CLIENT_SECRET", "")
        ).strip()
        if not client_id or not client_secret:
            raise PatentConnectorError(self.provider, "NOT_CONFIGURED")
        return client_id, client_secret

    def _access_token(self) -> str:
        client_id, client_secret = self._credentials()
        token_fingerprint = hash_event_payload(
            {"client_id": client_id, "client_secret": client_secret}
        )
        cache_key = f"patentshield:epo-ops:token:{token_fingerprint}"
        cached = cache.get(cache_key)
        if isinstance(cached, str) and cached:
            return cached

        try:
            response = self.session.post(
                self.token_url,
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise PatentConnectorError(self.provider, "TOKEN_TIMEOUT") from exc
        except requests.RequestException as exc:
            raise PatentConnectorError(self.provider, "TOKEN_NETWORK_ERROR") from exc

        if response.status_code >= 400:
            raise PatentConnectorError(
                self.provider,
                "TOKEN_" + response_error_code(response),
                retry_after_seconds=retry_after_seconds(
                    response.headers.get("Retry-After")
                ),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PatentConnectorError(self.provider, "TOKEN_INVALID_JSON") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise PatentConnectorError(self.provider, "TOKEN_MISSING")
        try:
            expires_in = int(payload.get("expires_in", 300))
        except (TypeError, ValueError):
            expires_in = 300
        cache.set(
            cache_key,
            token,
            timeout=max(30, expires_in - _TOKEN_SAFETY_MARGIN),
        )
        return token

    def search(self, query: str, max_results: int = 10) -> list[NormalizedPublication]:
        clean_query = normalize_query(query)
        limit = validate_limit(max_results)
        token = self._access_token()
        interval = float(os.getenv("PATENTSHIELD_EPO_MIN_INTERVAL_SECONDS", "6.0"))
        SharedProviderRateLimiter.acquire(self.provider, interval)

        # The user query is confined to a quoted title expression to keep it
        # from being interpreted as arbitrary OPS query syntax.
        safe_terms = clean_query.replace("\\", " ").replace('"', " ")
        epo_query = f'ta all "{safe_terms}"'
        try:
            response = self.session.get(
                f"{self.base_url}/published-data/search",
                params={"q": epo_query},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/xml",
                    "Range": f"1-{limit}",
                },
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
        return self._parse_search_response(response.content)[:limit]

    def fetch_publication(
        self, publication_number: str
    ) -> NormalizedPublication | None:
        """Fetch current bibliographic data by an exact EPO publication identifier.

        This endpoint is used by scheduled refresh jobs; unlike ``search``, it
        does not reinterpret the identifier as a title query.
        """
        normalized = re.sub(r"\s+", "", publication_number).upper()
        match = re.match(r"^([A-Z]{2}\d+)", normalized)
        if match is None:
            raise PatentConnectorError(self.provider, "INVALID_PUBLICATION_ID")
        epodoc_identifier = match.group(1)
        token = self._access_token()
        interval = float(os.getenv("PATENTSHIELD_EPO_MIN_INTERVAL_SECONDS", "6.0"))
        SharedProviderRateLimiter.acquire(self.provider, interval)

        url = (
            f"{self.base_url}/published-data/publication/epodoc/"
            f"{quote(epodoc_identifier, safe='')}/biblio"
        )
        try:
            response = self.session.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/xml",
                },
                timeout=TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise PatentConnectorError(self.provider, "PUBLICATION_TIMEOUT") from exc
        except requests.RequestException as exc:
            raise PatentConnectorError(
                self.provider, "PUBLICATION_NETWORK_ERROR"
            ) from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise PatentConnectorError(
                self.provider,
                "PUBLICATION_" + response_error_code(response),
                retry_after_seconds=retry_after_seconds(
                    response.headers.get("Retry-After")
                ),
            )
        records = self._parse_search_response(response.content)
        if not records:
            return None
        requested = "".join(char for char in normalized if char.isalnum())
        return next(
            (
                record
                for record in records
                if requested in {
                    "".join(
                        char
                        for char in (record.publication_number or "").upper()
                        if char.isalnum()
                    ),
                    "".join(
                        char
                        for char in (record.provider_record_id or "").upper()
                        if char.isalnum()
                    ),
                }
                or requested.startswith(
                    "".join(
                        char
                        for char in (record.publication_number or "").upper()
                        if char.isalnum()
                    )
                )
            ),
            None,
        )

    def fetch_legal_events(
        self,
        publication_number: str,
    ) -> list[NormalizedLegalEvent]:
        """Retrieve EPO OPS legal-event history for one publication identifier."""
        normalized = re.sub(r"\s+", "", publication_number).upper()
        match = re.match(r"^([A-Z]{2}\d+)", normalized)
        if match is None:
            raise PatentConnectorError(self.provider, "INVALID_PUBLICATION_ID")
        epodoc_identifier = match.group(1)
        token = self._access_token()
        interval = float(os.getenv("PATENTSHIELD_EPO_MIN_INTERVAL_SECONDS", "6.0"))
        SharedProviderRateLimiter.acquire(self.provider, interval)

        url = (
            f"{self.base_url}/published-data/publication/epodoc/"
            f"{quote(epodoc_identifier, safe='')}/legal"
        )
        try:
            response = self.session.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/xml",
                },
                timeout=TIMEOUT_SECONDS,
            )
        except requests.Timeout as exc:
            raise PatentConnectorError(self.provider, "LEGAL_EVENTS_TIMEOUT") from exc
        except requests.RequestException as exc:
            raise PatentConnectorError(
                self.provider, "LEGAL_EVENTS_NETWORK_ERROR"
            ) from exc

        if response.status_code == 404:
            return []
        if response.status_code >= 400:
            raise PatentConnectorError(
                self.provider,
                "LEGAL_" + response_error_code(response),
                retry_after_seconds=retry_after_seconds(
                    response.headers.get("Retry-After")
                ),
            )
        return self._parse_legal_events(response.content)

    @classmethod
    def _parse_search_response(
        cls,
        content: bytes,
    ) -> list[NormalizedPublication]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise PatentConnectorError(cls.provider, "INVALID_XML") from exc

        documents = [
            node for node in root.iter() if _local_name(node.tag) == "exchange-document"
        ]
        output: list[NormalizedPublication] = []
        for document in documents:
            publication_ref = _find_descendant(document, "publication-reference")
            publication_id = _read_document_id(publication_ref)
            country = (
                document.attrib.get("country")
                or publication_id.get("country")
                or "EP"
            ).upper()
            doc_number = (
                document.attrib.get("doc-number")
                or publication_id.get("doc_number")
            )
            kind_code = (
                document.attrib.get("kind")
                or publication_id.get("kind_code")
            )
            if not doc_number:
                continue

            publication_number = f"{country}{doc_number}{kind_code or ''}"
            application_ref = _find_descendant(document, "application-reference")
            application_id = _read_document_id(application_ref)
            application_number = application_id.get("doc_number")

            title = _preferred_text(document, "invention-title")
            if not title:
                title = f"{publication_number} (title unavailable)"
            abstract = _joined_text(document, "abstract")
            filing_date = parse_date(application_id.get("date"))
            publication_date = parse_date(publication_id.get("date"))
            grant_date = parse_date(
                _first_descendant_text(document, ("grant-date", "grant-date-text"))
            )
            family_identifier = _first_descendant_text(
                document, ("docdb-family-id", "family-id", "family-identifier")
            )
            applicant_names = _extract_people(document, "applicant")
            inventor_names = _extract_people(document, "inventor")

            source_url = (
                "https://worldwide.espacenet.com/publicationDetails/biblio"
                f"?CC={quote(country, safe='')}&NR={quote(doc_number, safe='')}"
                f"&KC={quote(kind_code or '', safe='')}"
            )
            safe_source_url = safe_http_url(source_url)
            source_payload: dict[str, Any] = {
                "publication_number": publication_number,
                "application_number": application_number,
                "jurisdiction": country,
                "kind_code": kind_code,
                "title": title,
                "abstract": abstract,
                "filing_date": filing_date.isoformat() if filing_date else None,
                "publication_date": (
                    publication_date.isoformat() if publication_date else None
                ),
                "grant_date": grant_date.isoformat() if grant_date else None,
                "applicant_names": applicant_names,
                "inventor_names": inventor_names,
                "family_identifier": family_identifier,
            }
            output.append(
                NormalizedPublication(
                    provider="EPO",
                    provider_record_id=publication_number,
                    publication_number=publication_number,
                    application_number=application_number,
                    jurisdiction=country,
                    title=title[:4_000],
                    abstract=abstract[:20_000] if abstract else None,
                    kind_code=kind_code,
                    filing_date=filing_date,
                    publication_date=publication_date,
                    grant_date=grant_date,
                    legal_status=normalize_legal_status(
                        _first_descendant_text(
                            document,
                            ("legal-status", "application-status", "status-text"),
                        )
                    ),
                    applicant_names=applicant_names,
                    inventor_names=inventor_names,
                    family_system="DOCDB" if family_identifier else None,
                    family_identifier=family_identifier,
                    source_url=safe_source_url,
                    source_payload=source_payload,
                )
            )
        return output

    @classmethod
    def _parse_legal_events(cls, content: bytes) -> list[NormalizedLegalEvent]:
        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise PatentConnectorError(
                cls.provider, "LEGAL_EVENTS_INVALID_XML"
            ) from exc

        event_nodes = [
            node
            for node in root.iter()
            if _local_name(node.tag) in {"legal-event", "legal-event-data"}
        ]
        output: list[NormalizedLegalEvent] = []
        for node in event_nodes:
            code = _first_descendant_text(
                node, ("event-code", "legal-event-code", "code")
            ) or "UNKNOWN"
            event_date = parse_date(
                _first_descendant_text(node, ("event-date", "date"))
            )
            title = _first_descendant_text(
                node, ("event-text", "event-description", "description", "title")
            ) or code
            payload = {
                "event_code": code,
                "event_date": event_date.isoformat() if event_date else None,
                "title": title[:500],
            }
            source_event_id = _first_descendant_text(
                node, ("event-id", "event-identifier", "id")
            ) or (
                f"{code}:{event_date.isoformat() if event_date else 'undated'}:"
                f"{hash_event_payload(payload)[:20]}"
            )
            payload["source_event_id"] = source_event_id
            output.append(
                NormalizedLegalEvent(
                    source_event_id=source_event_id[:200],
                    event_code=code[:100],
                    event_date=event_date,
                    title=title[:500],
                    description=_joined_text(node, "event-text")[:10_000],
                    legal_status_after=normalize_legal_status(
                        _first_descendant_text(
                            node, ("legal-status", "status-after", "status-text")
                        )
                    ),
                    source_payload=payload,
                )
            )
        return output


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold()


def _find_descendant(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    expected = name.casefold()
    return next(
        (child for child in node.iter() if _local_name(child.tag) == expected),
        None,
    )


def _read_document_id(reference: ET.Element | None) -> dict[str, str]:
    if reference is None:
        return {}
    ids = [
        node for node in reference.iter() if _local_name(node.tag) == "document-id"
    ]
    # Prefer a complete identifier with a country and document number.
    for document_id in ids:
        values = {
            _local_name(child.tag): (child.text or "").strip()
            for child in document_id.iter()
            if child is not document_id and (child.text or "").strip()
        }
        if values.get("country") and values.get("doc-number"):
            return {
                "country": values["country"],
                "doc_number": values["doc-number"],
                "kind_code": values.get("kind", ""),
                "date": values.get("date", ""),
            }
    return {}


def _first_descendant_text(
    node: ET.Element,
    names: tuple[str, ...],
) -> str | None:
    accepted = {name.casefold() for name in names}
    for child in node.iter():
        if _local_name(child.tag) in accepted:
            text = " ".join("".join(child.itertext()).split())
            if text:
                return text
    return None


def _preferred_text(node: ET.Element, name: str) -> str | None:
    matches = [
        child for child in node.iter() if _local_name(child.tag) == name.casefold()
    ]
    if not matches:
        return None
    english = [
        child
        for child in matches
        if (
            child.attrib.get("{http://www.w3.org/XML/1998/namespace}lang") or ""
        ).lower()
        in {"en", "eng"}
    ]
    selected = english[0] if english else matches[0]
    return " ".join("".join(selected.itertext()).split()) or None


def _joined_text(node: ET.Element, name: str) -> str | None:
    matches = [
        child for child in node.iter() if _local_name(child.tag) == name.casefold()
    ]
    if not matches:
        return None
    parts = [" ".join("".join(item.itertext()).split()) for item in matches]
    text = " ".join(part for part in parts if part).strip()
    return text or None


def _extract_people(node: ET.Element, role: str) -> list[str]:
    names: list[str] = []
    for person in node.iter():
        if _local_name(person.tag) != role.casefold():
            continue
        name_element = _find_descendant(person, f"{role}-name")
        if name_element is None:
            name_element = _find_descendant(person, "name")
        if name_element is not None:
            text = " ".join("".join(name_element.itertext()).split())
            if text and text not in names:
                names.append(text[:500])
    return names
