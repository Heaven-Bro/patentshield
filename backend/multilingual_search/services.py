"""Cross-lingual retrieval orchestration and opt-in translation adapter."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from audit.models import AuditLog
from enterprise_identity.services import matter_access_allowed
from indexing.language import detect_language
from indexing.models import TextChunk
from multilingual_search.models import TranslationRecord
from tenants.models import Tenant, TenantExternalAIProcessingPolicy

logger = logging.getLogger(__name__)

SUPPORTED_TARGETS = frozenset({"en", "zh", "ja", "ko"})
PROVIDER_NAME = "libretranslate"


class TranslationDisabled(RuntimeError):
    """Raised when deployment-level translation is not enabled/configured."""


class TranslationTenantNotAllowed(PermissionError):
    """Raised when a tenant has not approved outbound text translation."""


class TranslationProviderError(RuntimeError):
    """Raised when the configured translation service fails or misbehaves."""


@dataclass(frozen=True)
class TranslationOutcome:
    record: TranslationRecord | None
    source_text: str
    source_language: str
    target_language: str
    translated_text: str
    cached: bool
    identity_translation: bool = False


def _tenant_translation_allowed(tenant_id: Any) -> None:
    if not bool(getattr(settings, "PATENTSHIELD_TRANSLATION_ENABLED", False)):
        raise TranslationDisabled("On-demand translation is disabled by deployment policy.")
    allowed = {
        str(value).strip().lower()
        for value in getattr(settings, "PATENTSHIELD_TRANSLATION_ALLOWED_TENANTS", [])
        if str(value).strip()
    }
    if str(tenant_id).lower() not in allowed:
        raise TranslationTenantNotAllowed(
            "This tenant has not been authorized to disclose text to the "
            "translation service."
        )

    try:
        valid_uuid = uuid.UUID(str(tenant_id))
        policy = TenantExternalAIProcessingPolicy.objects.filter(tenant_id=valid_uuid).first()
    except (ValueError, TypeError, AttributeError, ValidationError):
        policy = None

    if policy is not None and not policy.allows(
        {"private_patent_content", "translation"},
        provider="libretranslate",
        model_name=str(getattr(settings, "PATENTSHIELD_TRANSLATION_ENGINE_VERSION", "configured-v1")),
    ):
        raise TranslationTenantNotAllowed(
            "This tenant has no approved external-processing policy for patent translation."
        )
    if not str(getattr(settings, "PATENTSHIELD_TRANSLATION_URL", "")).strip():
        raise TranslationDisabled("No translation service endpoint is configured.")


def _translate_with_libretranslate(
    text: str,
    source_language: str,
    target_language: str,
) -> str:
    """Call a server-configured LibreTranslate-compatible endpoint securely."""
    base_url = str(settings.PATENTSHIELD_TRANSLATION_URL).rstrip("/") + "/"
    endpoint = urljoin(base_url, "translate")
    payload: dict[str, str] = {
        "q": text,
        "source": source_language if source_language in SUPPORTED_TARGETS else "auto",
        "target": target_language,
        "format": "text",
    }
    api_key = str(getattr(settings, "PATENTSHIELD_TRANSLATION_API_KEY", "")).strip()
    if api_key:
        payload["api_key"] = api_key

    max_response_bytes = int(getattr(settings, "PATENTSHIELD_TRANSLATION_MAX_RESPONSE_BYTES", 1_048_576))
    timeout_val = getattr(settings, "PATENTSHIELD_TRANSLATION_TIMEOUT_SECONDS", 15)
    try:
        timeout_val = float(timeout_val) if not isinstance(timeout_val, (int, float)) else timeout_val
    except (ValueError, TypeError):
        timeout_val = 15.0

    try:
        with requests.post(
            endpoint,
            json=payload,
            headers={"Accept": "application/json"},
            timeout=timeout_val,
            allow_redirects=False,
            stream=True,
        ) as response:
            if response.status_code < 200 or response.status_code >= 300:
                logger.warning("Configured translation provider returned HTTP %s.", response.status_code)
                raise TranslationProviderError(
                    "The configured translation provider returned an unsuccessful response."
                )
            response_body = bytearray()
            if hasattr(response, "iter_content"):
                try:
                    for piece in response.iter_content(chunk_size=65_536):
                        if not piece:
                            continue
                        if isinstance(piece, (bytes, bytearray)):
                            response_body.extend(piece)
                            if len(response_body) > max_response_bytes:
                                raise TranslationProviderError(
                                    "The translation provider response exceeded the configured size limit."
                                )
                except TranslationProviderError:
                    raise
                except Exception:
                    pass

            if not response_body and hasattr(response, "content") and response.content:
                if isinstance(response.content, (bytes, bytearray)):
                    response_body = bytearray(response.content)
                elif isinstance(response.content, str):
                    response_body = bytearray(response.content.encode("utf-8"))

        if response_body:
            result = json.loads(response_body.decode("utf-8"))
        elif hasattr(response, "json") and callable(response.json):
            try:
                result = response.json()
            except Exception:
                result = {}
        else:
            result = {}
    except TranslationProviderError:
        raise
    except (requests.RequestException, ValueError, UnicodeDecodeError) as exc:
        logger.warning("Configured translation provider request failed (%s).", type(exc).__name__)
        raise TranslationProviderError(
            "The configured translation provider could not be reached or returned invalid JSON."
        ) from exc

    translated = result.get("translatedText") if isinstance(result, dict) else None
    if not isinstance(translated, str) or not translated.strip():
        raise TranslationProviderError("The translation provider returned no translated text.")
    if len(translated) > int(getattr(settings, "PATENTSHIELD_TRANSLATION_MAX_OUTPUT_CHARS", 30000)):
        raise TranslationProviderError(
            "The translated text exceeded the configured output limit."
        )
    return translated


def translate_chunk(
    *,
    tenant: Tenant,
    chunk: TextChunk,
    target_language: str,
    actor,
) -> TranslationOutcome:
    """Translate one tenant-owned chunk on demand and cache by exact source hash."""
    target_language = str(target_language).strip().lower()
    if target_language not in SUPPORTED_TARGETS:
        raise ValueError("target_language must be one of en, zh, ja, or ko.")
    if (
        str(chunk.tenant_id) != str(tenant.pk)
        or str(chunk.patent.tenant_id) != str(tenant.pk)
        or str(chunk.document_version.tenant_id) != str(tenant.pk)
        or str(chunk.document_version.patent_id) != str(chunk.patent_id)
    ):
        raise TranslationTenantNotAllowed("The requested source is not available in this tenant.")
    if not matter_access_allowed(
        tenant=tenant, user=actor, matter_type="patent", matter_id=chunk.patent_id, required="READ"
    ):
        raise TranslationTenantNotAllowed("You do not have READ access to this patent matter.")

    source_text = chunk.text
    if not source_text or not source_text.strip():
        raise ValueError("The selected source chunk contains no text.")
    max_input_chars = int(getattr(settings, "PATENTSHIELD_TRANSLATION_MAX_INPUT_CHARS", 8000))
    if len(source_text) > max_input_chars:
        raise ValueError(
            f"This chunk exceeds the {max_input_chars}-character translation limit; select a smaller chunk."
        )

    detected = detect_language(source_text)
    stored_language = getattr(chunk, "language_code", "und")
    source_language = stored_language if stored_language in SUPPORTED_TARGETS else detected["language"]
    if source_language == target_language:
        return TranslationOutcome(
            record=None,
            source_text=source_text,
            source_language=source_language,
            target_language=target_language,
            translated_text=source_text,
            cached=False,
            identity_translation=True,
        )

    _tenant_translation_allowed(tenant.pk)
    source_digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    provider_version = str(
        getattr(settings, "PATENTSHIELD_TRANSLATION_ENGINE_VERSION", "configured-v1")
    )
    cache_filters = {
        "tenant": tenant,
        "source_chunk": chunk,
        "source_text_sha256": source_digest,
        "source_language": source_language,
        "target_language": target_language,
        "provider": PROVIDER_NAME,
        "provider_version": provider_version,
    }
    cached_record = (
        TranslationRecord.objects.for_tenant(tenant.pk)
        .filter(**cache_filters)
        .first()
    )
    if cached_record is not None:
        return TranslationOutcome(
            record=cached_record,
            source_text=source_text,
            source_language=source_language,
            target_language=target_language,
            translated_text=cached_record.translated_text,
            cached=True,
        )

    translated_text = _translate_with_libretranslate(
        source_text,
        source_language,
        target_language,
    )
    translated_digest = hashlib.sha256(translated_text.encode("utf-8")).hexdigest()

    with transaction.atomic():
        record, created = TranslationRecord.objects.get_or_create(
            **cache_filters,
            defaults={
                "translated_text": translated_text,
                "translation_sha256": translated_digest,
                "created_by": actor,
            },
        )
    return TranslationOutcome(
        record=record,
        source_text=source_text,
        source_language=source_language,
        target_language=target_language,
        translated_text=record.translated_text,
        cached=not created,
    )


def record_translation_audit(
    *,
    tenant: Tenant,
    actor,
    chunk: TextChunk,
    outcome: TranslationOutcome,
) -> None:
    """Write operational metadata without putting source or translated text in logs."""
    AuditLog.objects.create(
        tenant=tenant,
        actor_user_id=actor.pk,
        action="MULTILINGUAL_TRANSLATION_REQUESTED",
        resource_type="TextChunk",
        resource_id=chunk.pk,
        outcome=AuditLog.Outcome.SUCCESS,
        metadata={
            "source_language": outcome.source_language,
            "target_language": outcome.target_language,
            "provider": PROVIDER_NAME if not outcome.identity_translation else "identity",
            "cache_hit": outcome.cached,
            "identity_translation": outcome.identity_translation,
            "translation_id": str(outcome.record.pk) if outcome.record else None,
            "source_char_count": len(outcome.source_text),
        },
    )