"""Tenant-authorized REST endpoint for live patent-provider search."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from patents.intelligence_models import PatentPublication
from patents.intelligence_serializers import LivePatentSearchRequestSerializer
from patents.intelligence_services import (
    ExternalDisclosureDenied,
    SearchAuditUnavailable,
    search_and_persist_publications,
)
from tenants.models import TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)


class PatentConnectorSearchThrottle(SimpleRateThrottle):
    """Apply per-user, per-tenant throttling to outbound provider queries."""

    scope = "patent_live_search"
    rate = "20/hour"

    def get_cache_key(self, request: Request, view: Any) -> str | None:
        """Do not use IP-only throttling for authenticated tenant traffic."""
        user = getattr(request, "user", None)
        tenant_id = request.headers.get("X-Tenant-ID", "")
        if not user or not user.is_authenticated or not tenant_id:
            return None
        try:
            canonical_tenant_id = str(UUID(tenant_id))
        except (AttributeError, TypeError, ValueError):
            return None
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{user.pk}:{canonical_tenant_id}",
        }


class PatentPublicationResultSerializer(serializers.ModelSerializer):
    """Serialize source-backed records without exposing raw provider payloads."""

    class Meta:
        model = PatentPublication
        fields = (
            "id",
            "provider",
            "provider_record_id",
            "publication_number",
            "application_number",
            "jurisdiction",
            "kind_code",
            "title",
            "abstract",
            "filing_date",
            "publication_date",
            "grant_date",
            "legal_status",
            "applicant_names",
            "inventor_names",
            "ipc_codes",
            "cpc_codes",
            "family_system",
            "family_identifier",
            "source_url",
            "source_updated_at",
            "last_synced_at",
            "source_payload_hash",
        )
        read_only_fields = fields


class LivePatentSearchView(APIView):
    """Search live USPTO/EPO records after tenant-authorized disclosure."""

    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole(
            TenantMembership.Role.OWNER,
            TenantMembership.Role.ADMIN,
            TenantMembership.Role.ATTORNEY,
            TenantMembership.Role.ANALYST,
        ),
    ]
    throttle_classes = [PatentConnectorSearchThrottle]

    def post(self, request: Request) -> Response:
        """Search providers, persist canonical records, and return provenance."""
        serializer = LivePatentSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tenant = getattr(request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")

        try:
            report = search_and_persist_publications(
                tenant=tenant,
                actor=request.user,
                query=serializer.validated_data["query"],
                providers=serializer.validated_data["providers"],
                max_results=serializer.validated_data["max_results"],
            )
        except ExternalDisclosureDenied as exc:
            raise PermissionDenied(str(exc)) from exc
        except SearchAuditUnavailable as exc:
            return Response(
                {"detail": str(exc), "code": "SEARCH_AUDIT_UNAVAILABLE"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc), "code": "INVALID_SEARCH_REQUEST"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:
            logger.warning(
                "Live patent search failed before a safe result was returned (%s).",
                type(exc).__name__,
            )
            return Response(
                {"detail": "Live patent search failed.", "code": "SEARCH_FAILED"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        provider_statuses = [
            {
                "provider": item.provider,
                "status": item.status,
                "result_count": item.result_count,
                "error_code": item.error_code,
            }
            for item in report.providers
        ]
        all_failed = bool(provider_statuses) and all(
            item["status"] == "FAILED" for item in provider_statuses
        )
        response_status = (
            status.HTTP_502_BAD_GATEWAY
            if all_failed
            else status.HTTP_200_OK
        )
        serialized_records = PatentPublicationResultSerializer(
            report.records,
            many=True,
            context={"request": request},
        ).data
        return Response(
            {
                "results": list(serialized_records),
                "providers": provider_statuses,
                "query_hmac": report.query_hmac,
                "audit_event_id": report.audit_event_id,
                "result_count": len(report.records),
            },
            status=response_status,
        )
