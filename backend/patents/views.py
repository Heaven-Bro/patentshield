"""Tenant-isolated REST views for patent upload and document lifecycle APIs."""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any
from uuid import UUID

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Prefetch
from django.http import FileResponse, HttpRequest, HttpResponse
from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditLog
from ingestion.tasks import process_patent_document_version
from patents.models import Claim, Patent, PatentDocumentVersion
from patents.serializers import PatentDocumentUploadSerializer
from tenants.models import Tenant, TenantMembership
from enterprise_identity.services import filter_visible_matter_queryset, require_matter_access
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)

_UPLOAD_ROLES = (
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
)


class TenantPatentAPIView(APIView):
    """Base for patent endpoints that require authenticated tenant membership."""

    permission_classes = [IsAuthenticated, HasTenantAccess]

    def get_tenant(self, request: Request) -> Tenant:
        """Return the tenant resolved by ``HasTenantAccess``."""
        tenant = getattr(request, "tenant", None)
        if not isinstance(tenant, Tenant):
            raise PermissionDenied("Tenant context missing.")
        return tenant

    def get_tenant_id(self, request: Request) -> UUID:
        """Return the selected tenant's primary key."""
        return self.get_tenant(request).pk


class PatentListView(TenantPatentAPIView):
    """List patents visible inside the selected tenant."""

    def get(self, request: Request) -> Response:
        tenant_id = self.get_tenant_id(request)
        version_queryset = PatentDocumentVersion.objects.for_tenant(tenant_id)
        patents = Patent.objects.for_tenant(tenant_id)
        patents = filter_visible_matter_queryset(patents, tenant=self.get_tenant(request), user=request.user, matter_type="patent")
        patents = patents.order_by("-created_at").prefetch_related(Prefetch("versions", queryset=version_queryset))
        return Response({"results": [_serialize_patent(request, p) for p in patents]})


class PatentClaimsView(TenantPatentAPIView):
    """Return extracted claims from one tenant-owned document version."""

    def get(self, request: Request, patent_id: UUID) -> Response:
        tenant_id = self.get_tenant_id(request)
        raw_version_id = request.query_params.get("document_version_id", "").strip()
        if not raw_version_id:
            return Response({"detail": "document_version_id is required."}, status=400)
        try:
            version_id = UUID(raw_version_id)
        except (TypeError, ValueError):
            return Response({"detail": "document_version_id must be a UUID."}, status=400)

        try:
            patent = Patent.objects.for_tenant(tenant_id).get(pk=patent_id)
            version = (
                PatentDocumentVersion.objects.for_tenant(tenant_id)
                .get(pk=version_id, patent_id=patent.pk)
            )
        except (Patent.DoesNotExist, PatentDocumentVersion.DoesNotExist) as exc:
            raise NotFound("Patent document version was not found.") from exc

        require_matter_access(request=request, matter_type="patent", matter_id=patent.pk, required="READ")
        if (
            patent.tenant_id != tenant_id
            or version.tenant_id != tenant_id
            or version.patent_id != patent.pk
        ):
            raise NotFound("Patent document version was not found.")
        if version.extraction_status != PatentDocumentVersion.ExtractionStatus.EXTRACTED:
            return Response(
                {
                    "detail": "Claims are unavailable until extraction completes.",
                    "extraction_status": version.extraction_status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        claims = list(
            Claim.objects.for_tenant(tenant_id).filter(
                patent_id=patent.pk,
                document_version_id=version.pk,
            )
        )
        claims.sort(key=_claim_sort_key)
        return Response({
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
        })


def _claim_sort_key(claim: Claim) -> tuple[int, int, str]:
    """Sort simple numeric claim numbers numerically before other labels."""
    value = str(claim.claim_number).strip()
    try:
        return (0, int(value), value)
    except ValueError:
        return (1, 0, value.casefold())


class PatentDocumentUploadView(TenantPatentAPIView):
    """Create a tenant-owned patent and first PDF version, then enqueue ingest."""

    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self) -> list[Any]:
        """Require an upload-capable tenant role for this mutating endpoint."""
        permission_classes = [
            IsAuthenticated,
            HasTenantAccess,
            RequireTenantRole(*_UPLOAD_ROLES),
        ]
        return [permission() for permission in permission_classes]

    def post(self, request: Request) -> Response:
        serializer = PatentDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        uploaded_file = data.pop("file")

        tenant = self.get_tenant(request)
        patent_id = uuid.uuid4()
        version_id = uuid.uuid4()
        content_sha256 = _sha256_uploaded_file(uploaded_file)
        storage_key = (
            f"patents/{tenant.pk}/{patent_id}/versions/{version_id}.pdf"
        )

        stored_key: str | None = None
        try:
            # Upload bytes before committing rows so no document row references
            # a missing object. UUID-only paths avoid user-controlled filenames.
            stored_key = default_storage.save(storage_key, uploaded_file)
            with transaction.atomic():
                patent = Patent.objects.create(
                    id=patent_id,
                    tenant=tenant,
                    title=data["title"],
                    abstract=data.get("abstract"),
                    publication_number=data.get("publication_number"),
                    application_number=data.get("application_number"),
                    jurisdiction=data.get("jurisdiction"),
                    source_type="UPLOAD",
                    status=Patent.Status.UPLOADED,
                    created_by=request.user,
                )
                version = PatentDocumentVersion.objects.create(
                    id=version_id,
                    tenant=tenant,
                    patent=patent,
                    version_number=1,
                    storage_key=stored_key,
                    content_sha256=content_sha256,
                    mime_type="application/pdf",
                    extraction_status=(
                        PatentDocumentVersion.ExtractionStatus.PENDING
                    ),
                )
                AuditLog.objects.create(
                    tenant=tenant,
                    actor_user_id=request.user.pk,
                    action="UPLOAD_PATENT_DOCUMENT",
                    resource_type="PatentDocumentVersion",
                    resource_id=version.pk,
                    request_id=getattr(request._request, "request_id", None),
                    outcome=AuditLog.Outcome.SUCCESS,
                    metadata={
                        "patent_id": str(patent.pk),
                        "document_version_id": str(version.pk),
                        "file_size_bytes": getattr(uploaded_file, "size", None),
                        "content_sha256": content_sha256,
                        "mime_type": "application/pdf",
                    },
                )
        except Exception:
            if stored_key:
                _delete_storage_object_safely(stored_key)
            raise

        queued, queue_error_type = _queue_document_processing(
            tenant_id=tenant.pk,
            patent_id=patent.pk,
            version_id=version.pk,
            actor_user_id=request.user.pk,
            request_id=getattr(request._request, "request_id", None),
        )
        if not queued:
            response_status = (
                status.HTTP_409_CONFLICT
                if queue_error_type == "VersionAlreadyProcessing"
                else status.HTTP_503_SERVICE_UNAVAILABLE
            )
            return Response(
                {
                    "detail": (
                        "The PDF was stored, but document processing could not "
                        "be queued. Use the process endpoint to retry."
                    ),
                    "patent_id": str(patent.pk),
                    "document_version_id": str(version.pk),
                    "retry_url": reverse(
                        "patents:patent-version-process",
                        kwargs={"patent_id": patent.pk, "version_id": version.pk},
                    ),
                    "error_type": queue_error_type,
                },
                status=response_status,
            )

        patent.refresh_from_db()
        version.refresh_from_db()
        response_payload = {
            "patent": _serialize_patent(request, patent),
            "document_version": _serialize_version(version),
            "processing": {"state": "QUEUED", "queue": "ingestion"},
        }
        return Response(response_payload, status=status.HTTP_202_ACCEPTED)


class PatentDetailView(TenantPatentAPIView):
    """Return one tenant-owned patent and its document-version metadata."""

    def get(self, request: Request, patent_id: UUID) -> Response:
        tenant_id = self.get_tenant_id(request)
        try:
            patent = (
                Patent.objects.for_tenant(tenant_id)
                .prefetch_related(
                    Prefetch(
                        "versions",
                        queryset=PatentDocumentVersion.objects.for_tenant(tenant_id),
                    )
                )
                .get(pk=patent_id)
            )
        except Patent.DoesNotExist as exc:
            raise NotFound("Patent was not found.") from exc
        require_matter_access(request=request, matter_type="patent", matter_id=patent.pk, required="READ")
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

        require_matter_access(request=request, matter_type="patent", matter_id=version.patent_id, required="READ")
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


class PatentVersionProcessView(TenantPatentAPIView):
    """Retry extraction for a failed or unqueued version in the selected tenant."""

    def get_permissions(self) -> list[Any]:
        permission_classes = [
            IsAuthenticated,
            HasTenantAccess,
            RequireTenantRole(*_UPLOAD_ROLES),
        ]
        return [permission() for permission in permission_classes]

    def post(
        self,
        request: Request,
        patent_id: UUID,
        version_id: UUID,
    ) -> Response:
        tenant = self.get_tenant(request)
        try:
            version = (
                PatentDocumentVersion.objects.for_tenant(tenant.pk)
                .select_related("patent")
                .get(pk=version_id, patent_id=patent_id)
            )
        except PatentDocumentVersion.DoesNotExist as exc:
            raise NotFound("Patent document version was not found.") from exc

        if version.patent.tenant_id != tenant.pk:
            raise NotFound("Patent document version was not found.")
        require_matter_access(request=request, matter_type="patent", matter_id=version.patent_id, required="EDIT")
        if version.extraction_status not in {
            PatentDocumentVersion.ExtractionStatus.PENDING,
            PatentDocumentVersion.ExtractionStatus.FAILED,
        }:
            return Response(
                {
                    "detail": "Only pending or failed versions can be retried.",
                    "extraction_status": version.extraction_status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        queued, queue_error_type = _queue_document_processing(
            tenant_id=tenant.pk,
            patent_id=version.patent_id,
            version_id=version.pk,
            actor_user_id=request.user.pk,
            request_id=getattr(request._request, "request_id", None),
        )
        if not queued:
            response_status = (
                status.HTTP_409_CONFLICT
                if queue_error_type == "VersionAlreadyProcessing"
                else status.HTTP_503_SERVICE_UNAVAILABLE
            )
            return Response(
                {
                    "detail": "Document processing could not be queued.",
                    "error_type": queue_error_type,
                },
                status=response_status,
            )

        return Response(
            {
                "patent_id": str(version.patent_id),
                "document_version_id": str(version.pk),
                "extraction_status": PatentDocumentVersion.ExtractionStatus.EXTRACTING,
                "queue": "ingestion",
            },
            status=status.HTTP_202_ACCEPTED,
        )


def _sha256_uploaded_file(uploaded_file: Any) -> str:
    """Calculate SHA-256 in bounded chunks and rewind the upload."""
    digest = hashlib.sha256()
    try:
        uploaded_file.seek(0)
        for chunk in uploaded_file.chunks():
            digest.update(chunk)
        uploaded_file.seek(0)
    except (AttributeError, OSError, ValueError) as exc:
        raise APIException("The uploaded PDF could not be hashed.") from exc
    return digest.hexdigest()


def _queue_document_processing(
    *,
    tenant_id: UUID,
    patent_id: UUID,
    version_id: UUID,
    actor_user_id: Any,
    request_id: str | None,
) -> tuple[bool, str | None]:
    """Reserve a pending version, enqueue extraction, and record queue failures."""
    with transaction.atomic():
        try:
            version = (
                PatentDocumentVersion.objects.select_for_update()
                .select_related("patent")
                .get(
                    pk=version_id,
                    tenant_id=tenant_id,
                    patent_id=patent_id,
                )
            )
        except PatentDocumentVersion.DoesNotExist:
            raise NotFound("Patent document version was not found.")

        if version.tenant_id != version.patent.tenant_id:
            raise NotFound("Patent document version was not found.")
        if version.extraction_status not in {
            PatentDocumentVersion.ExtractionStatus.PENDING,
            PatentDocumentVersion.ExtractionStatus.FAILED,
        }:
            return False, "VersionAlreadyProcessing"

        version.extraction_status = (
            PatentDocumentVersion.ExtractionStatus.EXTRACTING
        )
        version.save(update_fields=("extraction_status",))
        Patent.objects.filter(pk=patent_id, tenant_id=tenant_id).update(
            status=Patent.Status.PROCESSING
        )

    try:
        process_patent_document_version.apply_async(
            args=[str(version_id)],
            queue="ingestion",
        )
    except Exception as exc:
        logger.exception(
            "Could not enqueue document version %s (request_id=%s).",
            version_id,
            request_id,
        )
        with transaction.atomic():
            PatentDocumentVersion.objects.filter(
                pk=version_id,
                tenant_id=tenant_id,
            ).update(
                extraction_status=PatentDocumentVersion.ExtractionStatus.FAILED
            )
            Patent.objects.filter(pk=patent_id, tenant_id=tenant_id).update(
                status=Patent.Status.FAILED
            )
            try:
                # Isolate an audit-only database error with a savepoint so it
                # cannot roll back the failure state recorded above.
                with transaction.atomic():
                    AuditLog.objects.create(
                        tenant_id=tenant_id,
                        actor_user_id=actor_user_id,
                        action="QUEUE_PATENT_DOCUMENT_PROCESSING",
                        resource_type="PatentDocumentVersion",
                        resource_id=version_id,
                        request_id=request_id,
                        outcome=AuditLog.Outcome.FAILURE,
                        metadata={"error_type": type(exc).__name__},
                    )
            except Exception:
                logger.exception(
                    "Could not write task-enqueue failure audit for %s.",
                    version_id,
                )
        return False, type(exc).__name__

    return True, None


def _serialize_version(version: PatentDocumentVersion) -> dict[str, Any]:
    """Serialize public version fields without exposing storage internals."""
    return {
        "id": str(version.pk),
        "version_number": version.version_number,
        "mime_type": version.mime_type,
        "page_count": version.page_count,
        "extraction_status": version.extraction_status,
        "created_at": version.created_at.isoformat(),
    }


def _serialize_patent(
    request: HttpRequest | Request,
    patent: Patent,
) -> dict[str, Any]:
    """Serialize stable metadata and authenticated API links only."""
    cached_versions = getattr(patent, "_prefetched_objects_cache", {}).get(
        "versions"
    )
    if cached_versions is None:
        cached_versions = PatentDocumentVersion.objects.for_tenant(
            patent.tenant_id
        ).filter(patent_id=patent.pk)
    tenant_versions = (
        version
        for version in cached_versions
        if version.tenant_id == patent.tenant_id
    )
    versions = sorted(
        tenant_versions,
        key=lambda version: (version.version_number, version.created_at),
        reverse=True,
    )
    serialized_versions = []
    for version in versions:
        file_path = reverse(
            "patents:patent-version-file",
            kwargs={"patent_id": patent.pk, "version_id": version.pk},
        )
        serialized_versions.append(
            {
                **_serialize_version(version),
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


def _delete_storage_object_safely(storage_key: str) -> None:
    """Delete an orphaned upload without hiding the original persistence error."""
    try:
        default_storage.delete(storage_key)
    except Exception:
        logger.exception("Could not clean up orphaned upload %s.", storage_key)
