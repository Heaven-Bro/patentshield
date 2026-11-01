"""Tenant-authorized CJK cross-lingual search and on-demand translation APIs."""

from __future__ import annotations

import logging
import hashlib
from uuid import UUID

from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditLog
from indexing.language import detect_language
from indexing.models import TextChunk
from multilingual_search.models import TranslationRecord
from multilingual_search.serializers import (
    MultilingualSearchRequestSerializer,
    TranslateChunkRequestSerializer,
    TranslationRecordSerializer,
)
from multilingual_search.services import (
    TranslationDisabled,
    TranslationProviderError,
    TranslationTenantNotAllowed,
    translate_chunk,
)
from retrieval.services import HybridSearchPipeline
from enterprise_identity.services import filter_visible_matter_queryset, require_matter_access
from patents.models import Patent
from tenants.models import Tenant, TenantMembership
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

_TRANSLATION_ROLES = (
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
)


def _tenant(request: Request) -> Tenant:
    tenant = getattr(request, "tenant", None)
    if not isinstance(tenant, Tenant):
        raise PermissionDenied("Tenant context is unavailable.")
    return tenant


def _audit_search(
    *,
    tenant: Tenant,
    actor,
    request_id: str | None,
    query_language: dict[str, str],
    top_n: int,
    result_count: int,
) -> None:
    """Record query operational metadata but never persist the raw query text."""
    try:
        AuditLog.objects.create(
            tenant=tenant,
            actor_user_id=actor.pk,
            request_id=request_id,
            action="MULTILINGUAL_PATENT_SEARCH",
            resource_type="MultilingualSearchQuery",
            outcome=AuditLog.Outcome.SUCCESS,
            metadata={
                "query_language": query_language["language"],
                "detection_method": query_language["method"],
                "top_n": top_n,
                "result_count": result_count,
                "retrieval_model": "BAAI/bge-m3",
            },
        )
    except Exception:
        logger.exception("Could not write the multilingual search audit event.")


class MultilingualPatentSearchView(APIView):
    """Search local tenant-owned patent chunks without translating source text."""

    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*_READ_ROLES)]

    def post(self, request: Request) -> Response:
        tenant = _tenant(request)
        payload = MultilingualSearchRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        query = values["query"]
        top_n = values["top_n"]
        detected = detect_language(query)
        visible_patent_ids = filter_visible_matter_queryset(
            Patent.objects.for_tenant(tenant.pk),
            tenant=tenant,
            user=request.user,
            matter_type="patent",
        ).values("pk")

        try:
            candidates = HybridSearchPipeline().retrieve_evidence(
                query=query,
                tenant_id=tenant.pk,
                top_n=top_n,
                visible_patent_ids=visible_patent_ids,
            )
        except ValueError as exc:
            raise serializers.ValidationError({"query": str(exc)}) from exc
        except RuntimeError as exc:
            logger.warning(
                "Multilingual retrieval unavailable (%s).", type(exc).__name__
            )
            return Response(
                {
                    "detail": (
                        "Multilingual retrieval is unavailable. Verify that the local "
                        "BAAI/bge-m3 and reranker weights are cached in the worker/API image."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        results = []
        for candidate in candidates:
            source_detection = detect_language(candidate.text)
            if candidate.language_code in {"zh", "ja", "ko"}:
                source_detection["language"] = candidate.language_code
                source_detection["confidence"] = "indexed"
            results.append(
                {
                    "chunk_id": candidate.chunk_id,
                    "patent_id": candidate.patent_id,
                    "document_version_id": candidate.document_version_id,
                    "section_type": candidate.section_type,
                    "claim_number": candidate.claim_number,
                    "source_language": source_detection["language"],
                    "source_language_confidence": source_detection["confidence"],
                    "text": candidate.text,
                    "page_start": candidate.page_start,
                    "page_end": candidate.page_end,
                    "scores": {
                        "dense": candidate.dense_score,
                        "lexical": candidate.lexical_score,
                        "rrf": candidate.rrf_score,
                        "rerank": candidate.rerank_score,
                    },
                }
            )
        _audit_search(
            tenant=tenant,
            actor=request.user,
            request_id=getattr(getattr(request, "_request", request), "request_id", None),
            query_language=detected,
            top_n=top_n,
            result_count=len(results),
        )
        return Response(
            {
                "query_language": detected,
                "retrieval": {
                    "mode": "multilingual_dense_plus_original_language_bm25",
                    "embedding_model": "BAAI/bge-m3",
                    "embedding_dimensions": 1024,
                    "query_text_translated": False,
                },
                "count": len(results),
                "results": results,
            }
        )


class TranslatePatentChunkView(APIView):
    """Translate a selected source chunk only after explicit tenant allowlisting."""

    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole(*_TRANSLATION_ROLES),
    ]

    def post(self, request: Request) -> Response:
        tenant = _tenant(request)
        payload = TranslateChunkRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        chunk_id: UUID = values["chunk_id"]
        target_language = values["target_language"]

        chunk_acl_row = get_object_or_404(
            TextChunk.objects.for_tenant(tenant.pk)
            .filter(
                patent__tenant_id=tenant.pk,
                document_version__tenant_id=tenant.pk,
                document_version__patent_id=F("patent_id"),
            )
            .only("id", "patent_id"),
            pk=chunk_id,
        )
        require_matter_access(
            request=request,
            matter_type="patent",
            matter_id=chunk_acl_row.patent_id,
            required="READ",
        )
        chunk = get_object_or_404(
            TextChunk.objects.for_tenant(tenant.pk)
            .filter(
                patent__tenant_id=tenant.pk,
                document_version__tenant_id=tenant.pk,
                document_version__patent_id=F("patent_id"),
            )
            .select_related("patent", "document_version"),
            pk=chunk_id,
        )
        request_id = getattr(getattr(request, "_request", request), "request_id", None)
        try:
            translation_audit = AuditLog.objects.create(
                tenant=tenant,
                actor_user_id=request.user.pk,
                request_id=request_id,
                action="MULTILINGUAL_TRANSLATION_REQUESTED",
                resource_type="TextChunk",
                resource_id=chunk.pk,
                outcome=AuditLog.Outcome.FAILURE,
                metadata={
                    "target_language": target_language,
                    "source_char_count": len(chunk.text),
                    "provider": "libretranslate",
                    "stage": "REQUESTED",
                },
            )
        except Exception as exc:
            logger.exception("Translation blocked because audit creation failed.")
            return Response(
                {"detail": "Required audit storage is unavailable; no translation request was sent."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        def mark_translation_audit(outcome_value: str, error_type: str | None = None, translation_id: str | None = None) -> None:
            update_metadata = {
                "target_language": target_language,
                "source_char_count": len(chunk.text),
                "provider": "libretranslate",
                "stage": "COMPLETED" if outcome_value == AuditLog.Outcome.SUCCESS else "FAILED",
                "error_type": error_type,
                "translation_id": translation_id,
            }
            updated = AuditLog.objects.filter(pk=translation_audit.pk, tenant=tenant).update(
                outcome=outcome_value, metadata=update_metadata
            )
            if updated != 1:
                raise RuntimeError("Translation audit event could not be updated.")

        try:
            outcome = translate_chunk(
                tenant=tenant,
                chunk=chunk,
                target_language=target_language,
                actor=request.user,
            )
        except TranslationTenantNotAllowed as exc:
            mark_translation_audit(AuditLog.Outcome.FAILURE, type(exc).__name__)
            raise PermissionDenied(str(exc)) from exc
        except TranslationDisabled as exc:
            mark_translation_audit(AuditLog.Outcome.FAILURE, type(exc).__name__)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except ValueError as exc:
            mark_translation_audit(AuditLog.Outcome.FAILURE, type(exc).__name__)
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except TranslationProviderError as exc:
            mark_translation_audit(AuditLog.Outcome.FAILURE, type(exc).__name__)
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except RuntimeError as exc:
            mark_translation_audit(AuditLog.Outcome.FAILURE, type(exc).__name__)
            logger.warning("Patent chunk translation failed (%s).", type(exc).__name__)
            return Response(
                {"detail": "The selected chunk could not be translated."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            mark_translation_audit(
                AuditLog.Outcome.SUCCESS,
                translation_id=str(outcome.record.pk) if outcome.record else None,
            )
        except Exception as exc:
            logger.exception("Translation result withheld because audit completion failed.")
            return Response(
                {"detail": "The translation completed but its audit state could not be finalized."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "translation_id": str(outcome.record.pk) if outcome.record else None,
                "chunk_id": str(chunk.pk),
                "source_language": outcome.source_language,
                "target_language": outcome.target_language,
                "original_text": outcome.source_text,
                "translated_text": outcome.translated_text,
                "provider": (
                    "identity" if outcome.identity_translation else "libretranslate"
                ),
                "cached": outcome.cached,
                "identity_translation": outcome.identity_translation,
                "source_preserved": True,
                "legal_notice": (
                    "Translation is an aid for navigation only. Verify terminology and "
                    "interpretation against the original-language patent source."
                ),
            }
        )


class TranslationRecordDetailView(APIView):
    """Read a cached translation only in the active tenant that owns its source."""

    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*_READ_ROLES)]

    def get(self, request: Request, translation_id: UUID) -> Response:
        tenant = _tenant(request)
        record_acl_queryset = TranslationRecord.objects.for_tenant(tenant.pk).filter(
            source_chunk__tenant_id=tenant.pk,
            source_chunk__patent__tenant_id=tenant.pk,
            source_chunk__document_version__tenant_id=tenant.pk,
            source_chunk__document_version__patent_id=F("source_chunk__patent_id"),
        )
        source_patent_id = get_object_or_404(
            record_acl_queryset.values_list("source_chunk__patent_id", flat=True),
            pk=translation_id,
        )
        require_matter_access(
            request=request,
            matter_type="patent",
            matter_id=source_patent_id,
            required="READ",
        )
        record = get_object_or_404(
            record_acl_queryset.select_related("source_chunk"),
            pk=translation_id,
        )
        current_digest = hashlib.sha256(record.source_chunk.text.encode("utf-8")).hexdigest()
        if current_digest != record.source_text_sha256:
            raise NotFound("The source chunk changed; request a fresh translation.")
        data = TranslationRecordSerializer(record).data
        data["original_text"] = record.source_chunk.text
        data["source_preserved"] = True
        data["legal_notice"] = (
            "Translation is an aid for navigation only. Verify terminology and "
            "interpretation against the original-language patent source."
        )
        return Response(data)
