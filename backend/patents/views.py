"""Tenant-isolated REST views for patents and authorized PDF versions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.core.files.storage import default_storage
from django.http import FileResponse, HttpRequest, HttpResponse
from django.urls import reverse
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.permissions import HasTenantAccess


class TenantPatentAPIView(APIView):
    """Base for patent API routes that require an authenticated tenant member."""

    permission_classes = [IsAuthenticated, HasTenantAccess]

    def get_tenant_id(self, request: Request) -> UUID:
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")
        return tenant.pk


class PatentListView(TenantPatentAPIView):
    """List patents visible in the currently selected tenant."""

    def get(self, request: Request) -> Response:
        tenant_id = self.get_tenant_id(request)
        patents = (
            Patent.objects.for_tenant(tenant_id)
            .order_by("-created_at")
            .prefetch_related("versions")
        )
        return Response({"results": [_serialize_patent(request, p) for p in patents]})


class PatentClaimsView(TenantPatentAPIView):
    """Return claims for one tenant-owned patent document version."""

    def get(self, request: Request, patent_id: UUID) -> Response:
        tenant_id = self.get_tenant_id(request)
        raw_version_id = request.query_params.get("document_version_id", "").strip()
        if not raw_version_id:
            return Response(
                {"detail": "document_version_id is required."},
                status=400,
            )
        try:
            version_id = UUID(raw_version_id)
        except (TypeError, ValueError):
            return Response(
                {"detail": "document_version_id must be a UUID."},
                status=400,
            )

        try:
            patent = Patent.objects.for_tenant(tenant_id).get(pk=patent_id)
            version = (
                PatentDocumentVersion.objects.for_tenant(tenant_id)
                .get(pk=version_id, patent_id=patent.pk)
            )
        except (Patent.DoesNotExist, PatentDocumentVersion.DoesNotExist) as exc:
            raise NotFound("Patent document version was not found.") from exc

        if (
            patent.tenant_id != tenant_id
            or version.tenant_id != tenant_id
            or version.patent_id != patent.pk
        ):
            raise NotFound("Patent document version was not found.")
        if (
            version.extraction_status
            != PatentDocumentVersion.ExtractionStatus.EXTRACTED
        ):
            return Response(
                {
                    "detail": "Claims are unavailable until extraction completes.",
                    "extraction_status": version.extraction_status,
                },
                status=409,
            )

        claims = list(
            Claim.objects.for_tenant(tenant_id)
            .filter(patent_id=patent.pk, document_version_id=version.pk)
        )
        claims.sort(key=_claim_sort_key)
        return Response(
            {
                "patent_id": str(patent.pk),
                "document_version_id": str(version.pk),
                "results": [
                    {
                        "id": str(claim.pk),
                        "claim_number": claim.claim_number,
                        "claim_text": claim.claim_text,
                        "claim_type": claim.claim_type,
                        "depends_on_claim_number": claim.depends_on_claim_number,
                        "source_page_index": claim.source_page_index,
                        "extraction_confidence": claim.extraction_confidence,
                    }
                    for claim in claims
                ],
            }
        )


def _claim_sort_key(claim: Claim) -> tuple[int, int, str]:
    """Sort simple numeric claim numbers numerically, then nonnumeric labels."""
    value = str(claim.claim_number).strip()
    try:
        return (0, int(value), value)
    except ValueError:
        return (1, 0, value.casefold())


class PatentDetailView(TenantPatentAPIView):
    """Return one tenant-owned patent and its authorized document versions."""

    def get(self, request: Request, patent_id: UUID) -> Response:
        tenant_id = self.get_tenant_id(request)
        try:
            patent = Patent.objects.for_tenant(tenant_id).prefetch_related(
                "versions"
            ).get(pk=patent_id)
        except Patent.DoesNotExist as exc:
            raise NotFound("Patent was not found.") from exc
        return Response(_serialize_patent(request, patent))


class PatentVersionFileView(TenantPatentAPIView):
    """Stream a tenant-owned PDF through a permission-checked endpoint."""

    def get(
        self,
        request: Request,
        patent_id: UUID,
        version_id: UUID,
    ) -> HttpResponse:
        tenant_id = self.get_tenant_id(request)
        try:
            version = (
                PatentDocumentVersion.objects.for_tenant(tenant_id)
                .select_related("patent")
                .get(pk=version_id, patent_id=patent_id)
            )
        except PatentDocumentVersion.DoesNotExist as exc:
            raise NotFound("Patent document version was not found.") from exc

        if (
            version.patent.tenant_id != tenant_id
            or version.tenant_id != tenant_id
            or version.mime_type.casefold() != "application/pdf"
        ):
            raise NotFound("Patent PDF was not found.")

        try:
            pdf_handle = default_storage.open(version.storage_key, "rb")
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise NotFound("Patent PDF was not found in storage.") from exc

        response = FileResponse(
            pdf_handle,
            content_type="application/pdf",
            as_attachment=False,
            filename=f"patent-{patent_id}-version-{version_id}.pdf",
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, no-store"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


def _serialize_patent(request: HttpRequest | Request, patent: Patent) -> dict[str, Any]:
    """Serialize only stable metadata; never expose internal storage keys."""
    versions = sorted(
        patent.versions.all(),
        key=lambda version: (version.version_number, version.created_at),
        reverse=True,
    )
    serialized_versions = []
    for version in versions:
        file_path = reverse(
            "patent-version-file",
            kwargs={"patent_id": patent.pk, "version_id": version.pk},
        )
        serialized_versions.append(
            {
                "id": str(version.pk),
                "version_number": version.version_number,
                "mime_type": version.mime_type,
                "page_count": version.page_count,
                "extraction_status": version.extraction_status,
                "created_at": version.created_at.isoformat(),
                "file_url": request.build_absolute_uri(file_path),
            }
        )

    return {
        "id": str(patent.pk),
        "title": patent.title,
        "abstract": patent.abstract,
        "publication_number": patent.publication_number,
        "application_number": patent.application_number,
        "jurisdiction": patent.jurisdiction,
        "source_type": patent.source_type,
        "status": patent.status,
        "created_at": patent.created_at.isoformat(),
        "updated_at": patent.updated_at.isoformat(),
        "versions": serialized_versions,
    }
