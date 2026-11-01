"""Authenticated tenant APIs for semantic search and patent-focused Muse Spark workflows."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db.models import Q
from pgvector.django import CosineDistance
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, Throttled
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from audit.models import AuditLog
from patents.ai_serializers import (
    InfringementRiskRequestSerializer,
    LandscapeSummaryRequestSerializer,
    PatentRAGChatRequestSerializer,
    SemanticSearchRequestSerializer,
)
from patents.ai_services import (
    MetaLLMConfigurationError,
    MetaLLMUpstreamError,
    analyze_infringement_risk,
    answer_patent_question,
    summarize_landscape,
)
from patents.embedding_services import PublicPatentEmbedder, PublicPatentModelUnavailable
from patents.intelligence_models import PatentPublication
from enterprise_identity.services import require_matter_access
from patents.models import Claim, Patent
from tenants.models import TenantExternalAIProcessingPolicy, TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)

_READ_ROLES = (
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
    TenantMembership.Role.REVIEWER,
    TenantMembership.Role.READ_ONLY,
)


class PatentAIServiceUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The requested patent AI service is not configured or available."
    default_code = "PATENT_AI_SERVICE_UNAVAILABLE"


class PatentAIProviderConfigurationFailure(APIException):
    status_code = status.HTTP_424_FAILED_DEPENDENCY
    default_detail = "The configured AI provider rejected its credentials, endpoint, or model."
    default_code = "PATENT_AI_PROVIDER_CONFIGURATION_FAILURE"


class PatentAIUpstreamFailure(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "The patent AI provider did not return a valid response."
    default_code = "PATENT_AI_UPSTREAM_FAILURE"


class PatentAIProviderBadGateway(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The patent AI provider rejected the request or returned an invalid response."
    default_code = "PATENT_AI_PROVIDER_BAD_GATEWAY"


class TenantScopedAIThrottle(SimpleRateThrottle):
    """Apply authenticated per-user and per-tenant limits to AI operations."""

    scope = "patent_ai"
    rate = "30/hour"

    def get_cache_key(self, request: Request, view) -> str | None:
        user = getattr(request, "user", None)
        tenant = getattr(request, "tenant", None)
        if not user or not user.is_authenticated or tenant is None:
            return None
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{user.pk}:{tenant.pk}",
        }


class SemanticSearchThrottle(TenantScopedAIThrottle):
    scope = "patent_semantic_search"
    rate = "120/hour"


class PatentAIAPIView(APIView):
    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole(*_READ_ROLES),
    ]
    throttle_classes = [TenantScopedAIThrottle]

    @staticmethod
    def _raise_provider_error(exc: Exception) -> None:
        if isinstance(exc, MetaLLMConfigurationError):
            raise PatentAIProviderConfigurationFailure(str(exc)) from exc
        if isinstance(exc, PublicPatentModelUnavailable):
            raise PatentAIServiceUnavailable(str(exc)) from exc
        if isinstance(exc, MetaLLMUpstreamError):
            if exc.status_code == 429:
                wait = None
                if exc.retry_after and exc.retry_after.strip().isdigit():
                    wait = min(int(exc.retry_after.strip()), 3600)
                raise Throttled(wait=wait, detail="The AI provider rate limit was reached; retry later.") from exc
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                raise PatentAIProviderBadGateway() from exc
            raise PatentAIUpstreamFailure() from exc
        logger.warning("Patent AI request failed (%s).", type(exc).__name__)
        raise PatentAIUpstreamFailure() from exc


def _run_audited_llm(
    request: Request,
    *,
    feature: str,
    metadata: dict[str, Any],
    operation: Callable[[], Any],
) -> tuple[Any, str]:
    """Audit LLM disclosure before sending it, without persisting user text."""
    policy = None
    try:
        policy = request.tenant.external_ai_policy
    except (AttributeError, TenantExternalAIProcessingPolicy.DoesNotExist):
        policy = None
    raw_request = getattr(request, "_request", request)
    request_id = getattr(raw_request, "request_id", None) or request.headers.get("X-Request-ID")
    audit_metadata = {
        "feature": feature,
        "provider": "meta",
        "provider_model": settings.META_LLM_MODEL,
        "tenant_policy_version": policy.policy_version if policy else None,
        "tenant_policy_region": policy.region if policy else None,
        "stage": "REQUESTED",
        **metadata,
    }
    try:
        event = AuditLog.objects.create(
            tenant=request.tenant,
            actor_user_id=request.user.pk,
            request_id=str(request_id)[:100] if request_id else None,
            action=f"PATENT_AI_{feature.upper()}"[:100],
            resource_type="PatentAI",
            outcome=AuditLog.Outcome.FAILURE,
            metadata=audit_metadata,
        )
    except Exception as exc:
        logger.exception("Patent AI operation blocked because its audit record could not be written.")
        raise PatentAIServiceUnavailable(
            "Required audit storage is unavailable; no external LLM request was sent."
        ) from exc

    try:
        result = operation()
    except Exception:
        try:
            AuditLog.objects.filter(pk=event.pk, tenant=request.tenant).update(
                outcome=AuditLog.Outcome.FAILURE,
                metadata={**audit_metadata, "stage": "FAILED"},
            )
        except Exception:
            logger.exception("Could not update failed patent AI audit event %s.", event.pk)
        raise

    try:
        updated = AuditLog.objects.filter(pk=event.pk, tenant=request.tenant).update(
            outcome=AuditLog.Outcome.SUCCESS,
            metadata={**audit_metadata, "stage": "COMPLETED"},
        )
        if updated != 1:
            raise RuntimeError("The required AI audit event was not updated.")
    except Exception as exc:
        # The provider was called, but no result is returned without a durable
        # completion state. The REQUESTED/FAILURE event remains available for triage.
        logger.exception("Could not complete patent AI audit event %s.", event.pk)
        raise PatentAIServiceUnavailable(
            "The AI operation completed but its audit state could not be finalized."
        ) from exc
    return result, str(event.pk)


def _publication_payload(publication: PatentPublication) -> dict:
    """Build bounded, source-backed context without exposing raw provider payloads."""
    raw_metadata = publication.raw_metadata if isinstance(publication.raw_metadata, dict) else {}
    raw_claims = raw_metadata.get("claims") or raw_metadata.get("claim_text") or []
    claims: list[str] = []
    if isinstance(raw_claims, str):
        claims = [raw_claims]
    elif isinstance(raw_claims, list):
        for item in raw_claims[:30]:
            if isinstance(item, str) and item.strip():
                claims.append(item.strip()[:6_000])
            elif isinstance(item, dict):
                text = item.get("claim_text") or item.get("text") or item.get("claim")
                if isinstance(text, str) and text.strip():
                    claims.append(text.strip()[:6_000])

    return {
        "id": str(publication.pk),
        "provider": publication.provider,
        "publication_number": publication.publication_number or "",
        "application_number": publication.application_number or "",
        "jurisdiction": publication.jurisdiction,
        "title": publication.title[:4_000],
        "abstract": (publication.abstract or "")[:20_000],
        "sector_label": publication.sector_label,
        "legal_status": publication.legal_status,
        "publication_date": publication.publication_date.isoformat() if publication.publication_date else None,
        "applicants": publication.applicant_names[:50] if isinstance(publication.applicant_names, list) else [],
        "ipc_codes": publication.ipc_codes[:100] if isinstance(publication.ipc_codes, list) else [],
        "cpc_codes": publication.cpc_codes[:100] if isinstance(publication.cpc_codes, list) else [],
        "source_url": publication.source_url,
        "claims": claims,
    }


def _resolve_publication_identifiers(identifiers: list[str]) -> list[PatentPublication]:
    """Resolve only exact canonical publication identifiers, preserving caller order."""
    cleaned = list(dict.fromkeys(str(value).strip() for value in identifiers if str(value).strip()))
    if not cleaned:
        return []
    lookup = Q(publication_number__in=cleaned) | Q(provider_record_id__in=cleaned)
    uuid_values: list[UUID] = []
    for value in cleaned:
        try:
            uuid_values.append(UUID(value))
        except (TypeError, ValueError, AttributeError):
            continue
    if uuid_values:
        lookup |= Q(pk__in=uuid_values)

    records = list(PatentPublication.objects.filter(lookup))
    by_identifier: dict[str, PatentPublication] = {}
    for record in records:
        by_identifier[str(record.pk)] = record
        if record.publication_number:
            by_identifier.setdefault(record.publication_number, record)
        by_identifier.setdefault(record.provider_record_id, record)
    ordered: list[PatentPublication] = []
    seen: set[str] = set()
    for identifier in cleaned:
        record = by_identifier.get(identifier)
        if record is not None and str(record.pk) not in seen:
            ordered.append(record)
            seen.add(str(record.pk))
    return ordered


def _require_sensitive_disclosure(request: Request, categories: set[str]) -> TenantExternalAIProcessingPolicy:
    """Require both operator approval and explicit tenant-approved data categories."""
    if not bool(getattr(settings, "META_LLM_ALLOW_SENSITIVE_DATA", False)):
        raise PermissionDenied(
            "External AI disclosure is disabled by the deployment security policy."
        )
    try:
        policy = TenantExternalAIProcessingPolicy.objects.select_related("tenant").get(
            tenant_id=request.tenant.pk
        )
    except TenantExternalAIProcessingPolicy.DoesNotExist as exc:
        raise PermissionDenied(
            "This workspace has no approved external AI processing policy."
        ) from exc
    if not policy.allows(
        categories,
        provider="meta",
        model_name=str(settings.META_LLM_MODEL),
    ):
        raise PermissionDenied(
            "The workspace policy does not authorize these data categories for the configured AI provider/model."
        )
    return policy


def _bound_context_records(records: list[dict], *, max_chars: int | None = None) -> list[dict]:
    """Apply field-level context budgets before any JSON serialization occurs."""
    configured_budget = int(max_chars or getattr(settings, "PATENTSHIELD_META_LLM_CONTEXT_MAX_CHARS", 60_000))
    # Reserve headroom for JSON escaping and structural characters. Truncation
    # happens here on source fields, never by slicing serialized JSON later.
    budget = max(256, int(configured_budget * 0.70))
    if budget < 1:
        raise ValueError("The configured LLM context budget must be positive.")
    bounded: list[dict] = []
    remaining = budget
    string_fields = (
        "id", "visibility", "provider", "publication_number", "application_number",
        "jurisdiction", "title", "abstract", "sector_label", "legal_status",
        "publication_date", "source_url",
    )
    for record in records:
        if remaining < 256:
            break
        item: dict[str, Any] = {}
        truncated = False
        for key in string_fields:
            value = record.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                allowance = max(0, min(len(value), remaining - 128))
                if allowance <= 0:
                    truncated = truncated or bool(value)
                    continue
                item[key] = value[:allowance]
                truncated = truncated or allowance < len(value)
                remaining -= allowance + len(key) + 8
            elif isinstance(value, (int, float, bool)):
                item[key] = value
                remaining -= len(key) + 16
        for key in ("applicants", "ipc_codes", "cpc_codes"):
            values = record.get(key)
            if isinstance(values, list) and remaining > 128:
                selected = []
                for value in values:
                    text_value = str(value)[:128]
                    cost = len(text_value) + len(key) + 4
                    if cost > remaining - 128:
                        truncated = True
                        break
                    selected.append(text_value)
                    remaining -= cost
                item[key] = selected
                truncated = truncated or len(selected) < len(values)
        claims = record.get("claims") if isinstance(record.get("claims"), list) else []
        selected_claims: list[Any] = []
        for claim in claims:
            if remaining < 256:
                truncated = True
                break
            if isinstance(claim, dict):
                claim_item = {}
                for claim_key in ("claim_number", "claim_type", "claim_text"):
                    claim_value = claim.get(claim_key)
                    if isinstance(claim_value, str):
                        allowance = max(0, min(len(claim_value), min(3_000, remaining - 128)))
                        if allowance:
                            claim_item[claim_key] = claim_value[:allowance]
                            remaining -= allowance + len(claim_key) + 8
                        truncated = truncated or allowance < len(claim_value)
                if claim_item:
                    selected_claims.append(claim_item)
            elif isinstance(claim, str):
                allowance = max(0, min(len(claim), min(3_000, remaining - 128)))
                if allowance:
                    selected_claims.append(claim[:allowance])
                    remaining -= allowance + 12
                truncated = truncated or allowance < len(claim)
        if claims:
            item["claims"] = selected_claims
            truncated = truncated or len(selected_claims) < len(claims)
        item["context_truncated"] = truncated
        bounded.append(item)
        remaining -= 64
    return bounded


def _resolve_target_context(request: Request, identifiers: list[str]) -> list[dict]:
    """Resolve public rows and matter-authorized tenant records before reading private text."""
    clean_ids = list(dict.fromkeys(str(value).strip() for value in identifiers if str(value).strip()))[:25]
    public_records = _resolve_publication_identifiers(clean_ids)
    public_by_key: dict[str, dict] = {}
    for record in public_records:
        payload = _publication_payload(record)
        public_by_key[str(record.pk)] = payload
        if record.publication_number:
            public_by_key.setdefault(record.publication_number, payload)
        public_by_key.setdefault(record.provider_record_id, payload)

    uuid_values: list[UUID] = []
    for identifier in clean_ids:
        try:
            uuid_values.append(UUID(identifier))
        except (TypeError, ValueError, AttributeError):
            continue

    tenant_queryset = Patent.objects.for_tenant(request.tenant).filter(
        Q(publication_number__in=clean_ids) | Q(pk__in=uuid_values)
    )
    # Resolve only PKs first; require_matter_access executes before title, abstract,
    # claims, or chunk text is selected from the tenant-private tables.
    candidate_ids = list(tenant_queryset.order_by("pk").values_list("pk", flat=True))
    for patent_id in candidate_ids:
        require_matter_access(
            request=request,
            matter_type="patent",
            matter_id=patent_id,
            required="READ",
        )

    tenant_records = list(
        tenant_queryset.filter(pk__in=candidate_ids).only(
            "id", "tenant", "publication_number", "application_number", "jurisdiction",
            "title", "abstract",
        ).order_by("pk")
    ) if candidate_ids else []
    if tenant_records:
        _require_sensitive_disclosure(request, {"private_patent_content", "claims"})

    max_chars = int(getattr(settings, "PATENTSHIELD_META_LLM_CONTEXT_MAX_CHARS", 60_000))
    remaining = max_chars
    tenant_by_key: dict[str, dict] = {}
    for record in tenant_records:
        # Bound the patent-level bibliographic fields before claims are queried.
        title = (record.title or "")[: min(2_000, max(0, remaining // 5))]
        abstract = (record.abstract or "")[: min(5_000, max(0, remaining // 3))]
        used = len(title) + len(abstract) + 512
        remaining = max(0, remaining - used)
        claims: list[dict] = []
        if remaining > 512:
            rows = Claim.objects.for_tenant(request.tenant).filter(
                patent_id=record.pk,
                patent__tenant_id=request.tenant.pk,
                document_version__tenant_id=request.tenant.pk,
            ).order_by("claim_number").values("claim_number", "claim_type", "claim_text")[:50]
            for claim in rows.iterator(chunk_size=10):
                if remaining < 256:
                    break
                claim_text = (claim["claim_text"] or "")[: min(2_500, remaining - 200)]
                if not claim_text.strip():
                    continue
                claims.append({
                    "claim_number": str(claim["claim_number"] or "")[:30],
                    "claim_type": str(claim["claim_type"] or "")[:20],
                    "claim_text": claim_text,
                })
                remaining -= len(claim_text) + 100
        payload = {
            "id": str(record.pk),
            "visibility": "tenant_private",
            "publication_number": (record.publication_number or "")[:100],
            "application_number": (record.application_number or "")[:100],
            "jurisdiction": (record.jurisdiction or "")[:10],
            "title": title,
            "abstract": abstract,
            "claims": claims,
        }
        tenant_by_key[str(record.pk)] = payload
        if record.publication_number:
            tenant_by_key.setdefault(record.publication_number, payload)

    resolved: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for identifier in clean_ids:
        tenant_payload = tenant_by_key.get(identifier)
        public_payload = public_by_key.get(identifier)
        if tenant_payload is not None:
            key = ("tenant", tenant_payload["id"])
            if key not in seen:
                resolved.append(tenant_payload)
                seen.add(key)
        elif public_payload is not None:
            key = ("public", public_payload["id"])
            if key not in seen:
                resolved.append(public_payload)
                seen.add(key)
    return _bound_context_records(resolved, max_chars=max_chars)


class SemanticPatentSearchView(PatentAIAPIView):
    """Search the public PatentPublication corpus with BAAI/bge-large-en-v1.5 cosine similarity."""

    throttle_classes = [SemanticSearchThrottle]

    def post(self, request: Request) -> Response:
        serializer = SemanticSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            query_vector = PublicPatentEmbedder.embed_texts([values["query"]])[0]
        except (PublicPatentModelUnavailable, RuntimeError) as exc:
            self._raise_provider_error(exc)

        active_model, active_revision = PublicPatentEmbedder.model_identity()
        queryset = PatentPublication.objects.filter(
            embedding__isnull=False,
            embedding_model=active_model,
            embedding_revision=active_revision,
        )
        if values.get("sector_label"):
            queryset = queryset.filter(sector_label=values["sector_label"])
        queryset = queryset.annotate(
            cosine_distance=CosineDistance("embedding", query_vector)
        ).filter(
            cosine_distance__lte=1.0 - values["similarity_threshold"]
        ).order_by("cosine_distance", "pk")[: values["top_k"]]

        results = []
        for record in queryset:
            distance = float(record.cosine_distance)
            results.append(
                {
                    "id": str(record.pk),
                    "provider": record.provider,
                    "provider_record_id": record.provider_record_id,
                    "publication_number": record.publication_number,
                    "application_number": record.application_number,
                    "jurisdiction": record.jurisdiction,
                    "title": record.title,
                    "abstract": record.abstract,
                    "sector_label": record.sector_label,
                    "legal_status": record.legal_status,
                    "publication_date": record.publication_date,
                    "applicant_names": record.applicant_names,
                    "ipc_codes": record.ipc_codes,
                    "cpc_codes": record.cpc_codes,
                    "source_url": record.source_url,
                    "cosine_distance": round(distance, 6),
                    "similarity_score": round(max(-1.0, min(1.0, 1.0 - distance)), 6),
                }
            )
        return Response(
            {
                "query": values["query"],
                "sector_label": values.get("sector_label"),
                "top_k": values["top_k"],
                "similarity_threshold": values["similarity_threshold"],
                "result_count": len(results),
                "results": results,
            }
        )


class LandscapeSummaryView(PatentAIAPIView):
    """Summarize a selected set of top-ranked canonical public patents."""

    def post(self, request: Request) -> Response:
        serializer = LandscapeSummaryRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        identifiers = [str(value) for value in serializer.validated_data["patent_ids"]]
        records_by_id = {
            str(record.pk): record
            for record in PatentPublication.objects.filter(pk__in=identifiers)
        }
        records = [records_by_id[value] for value in identifiers if value in records_by_id]
        if not records:
            raise NotFound("No matching public patent records were found.")
        context = _bound_context_records([_publication_payload(record) for record in records])
        landscape_query = serializer.validated_data.get("query", "")[:1_500]
        if landscape_query:
            _require_sensitive_disclosure(request, {"custom_queries"})
        try:
            summary, audit_event_id = _run_audited_llm(
                request,
                feature="LANDSCAPE_SUMMARY",
                metadata={
                    "patent_count": len(records),
                    "custom_query_characters": len(landscape_query),
                    "sensitive_data_included": bool(landscape_query),
                },
                operation=lambda: summarize_landscape(context, landscape_query),
            )
        except (MetaLLMConfigurationError, MetaLLMUpstreamError) as exc:
            self._raise_provider_error(exc)
        return Response(
            {
                "model": settings.META_LLM_MODEL,
                "audit_event_id": audit_event_id,
                "patent_count": len(records),
                "patent_ids": [str(record.pk) for record in records],
                "landscape": summary,
            }
        )


class InfringementRiskAnalysisView(PatentAIAPIView):
    """Compare an invention description with target publication details."""

    def post(self, request: Request) -> Response:
        serializer = InfringementRiskRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        target: dict
        identifier = values.get("target_patent_identifier")
        _require_sensitive_disclosure(request, {"invention_disclosures"})
        if identifier:
            matches = _resolve_target_context(request, [identifier])
            if not matches:
                raise NotFound("The target patent identifier was not found for this tenant or in the public corpus.")
            target = matches[0]
        else:
            target = dict(values["target_patent_details"])
            target.setdefault("claims", [])
            _require_sensitive_disclosure(request, {"user_supplied_patent_material"})

        # Explicit caller-supplied details can enrich, but not silently replace,
        # the canonical record when both are present.
        supplied = values.get("target_patent_details")
        if supplied and identifier:
            _require_sensitive_disclosure(request, {"user_supplied_patent_material"})
            for key, value in supplied.items():
                if value not in (None, "", []):
                    target[key] = value

        try:
            analysis, audit_event_id = _run_audited_llm(
                request,
                feature="INFRINGEMENT_RISK",
                metadata={
                    "invention_description_characters": min(len(values["invention_description"]), 20_000),
                    "disclosure_categories": ["invention_disclosures"],
                    "target_patent_count": 1,
                    "sensitive_data_included": True,
                },
                operation=lambda: analyze_infringement_risk(
                    values["invention_description"][:20_000], _bound_context_records([target], max_chars=35_000)[0]
                ),
            )
        except (MetaLLMConfigurationError, MetaLLMUpstreamError) as exc:
            self._raise_provider_error(exc)
        return Response(
            {
                "model": settings.META_LLM_MODEL,
                "audit_event_id": audit_event_id,
                "target_patent": {
                    "patent_identifier": target.get("patent_identifier")
                    or target.get("publication_number")
                    or target.get("id"),
                    "title": target.get("title", ""),
                },
                **analysis,
            }
        )


class PatentRAGChatView(PatentAIAPIView):
    """Answer a chat turn grounded in exact selected public patent records."""

    def post(self, request: Request) -> Response:
        serializer = PatentRAGChatRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        _require_sensitive_disclosure(request, {"chat_messages"})
        context = _resolve_target_context(request, values["target_patent_identifiers"])
        if not context:
            raise NotFound("None of the requested patent identifiers were found for this tenant or in the public corpus.")
        private_context = any(item.get("visibility") == "tenant_private" for item in context)
        try:
            answer, audit_event_id = _run_audited_llm(
                request,
                feature="RAG_CHAT",
                metadata={
                    "patent_context_count": len(context),
                    "message_characters": min(len(values["message"]), 6_000),
                    "history_message_count": min(len(values.get("conversation_history", [])), 12),
                    "disclosure_categories": ["chat_messages"],
                    "sensitive_data_included": True,
                    "private_patent_documents_included": private_context,
                },
                operation=lambda: answer_patent_question(
                    message=values["message"][:6_000],
                    history=[
                        {"role": item["role"], "content": item["content"][:1_000]}
                        for item in values.get("conversation_history", [])[-12:]
                    ],
                    patent_context=context,
                ),
            )
        except (MetaLLMConfigurationError, MetaLLMUpstreamError) as exc:
            self._raise_provider_error(exc)
        return Response(
            {
                "model": settings.META_LLM_MODEL,
                "audit_event_id": audit_event_id,
                "answer": answer,
                "used_patents": [
                    {
                        "id": item.get("id"),
                        "publication_number": item.get("publication_number"),
                        "title": item.get("title"),
                        "visibility": item.get("visibility", "public"),
                        "claims_available": bool(item.get("claims")),
                    }
                    for item in context
                ],
            }
        )
