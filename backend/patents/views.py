"""Tenant-isolated REST views for patent upload and document lifecycle APIs."""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Prefetch
from django.http import FileResponse, HttpRequest, HttpResponse
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditLog
from ingestion.tasks import EXTRACTION_QUEUE_GRACE_PERIOD, process_patent_document_version
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
    ) -> HttpResponse | Response:
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

        # Keep the existence check behind tenant and Matter authorization so a
        # caller cannot use storage responses to probe another tenant's files.
        # Local storage exposes a physical path; remote storage backends (for
        # example S3) may not implement path(), so use their exists() contract.
        try:
            storage_path = default_storage.path(version.storage_key)
        except (NotImplementedError, AttributeError):
            storage_path = None

        try:
            file_exists = (
                os.path.exists(storage_path)
                if storage_path is not None
                else default_storage.exists(version.storage_key)
            )
        except (OSError, ValueError):
            file_exists = False

        if not file_exists:
            return Response(
                {"error": "PDF file not found on storage server."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            pdf_handle = default_storage.open(version.storage_key, "rb")
        except (FileNotFoundError, OSError, ValueError):
            return Response(
                {"error": "PDF file not found on storage server."},
                status=status.HTTP_404_NOT_FOUND,
            )

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
            PatentDocumentVersion.ExtractionStatus.OCR_REQUIRED,
            PatentDocumentVersion.ExtractionStatus.EXTRACTION_FAILED,
        }:
            return Response(
                {
                    "detail": "Only pending or failed versions can be retried.",
                    "extraction_status": version.extraction_status,
                },
                status=status.HTTP_409_CONFLICT,
            )
        if (
            version.extraction_status == PatentDocumentVersion.ExtractionStatus.PENDING
            and version.extraction_queued_at
            and version.extraction_queued_at > timezone.now() - EXTRACTION_QUEUE_GRACE_PERIOD
        ):
            return Response(
                {
                    "detail": "Document processing is already queued.",
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

        version.refresh_from_db(fields=("extraction_status", "extraction_queued_at"))
        return Response(
            {
                "patent_id": str(version.patent_id),
                "document_version_id": str(version.pk),
                "extraction_status": version.extraction_status,
                "extraction_queued_at": (
                    version.extraction_queued_at.isoformat()
                    if version.extraction_queued_at
                    else None
                ),
                "queue": "ingestion",
            },
            status=status.HTTP_202_ACCEPTED,
        )


class PatentVersionIndexStageRetryView(TenantPatentAPIView):
    """Retry text or schematic indexing independently, without re-uploading."""

    def get_permissions(self) -> list[Any]:
        return [
            permission()
            for permission in (IsAuthenticated, HasTenantAccess, RequireTenantRole(*_UPLOAD_ROLES))
        ]

    def post(self, request: Request, patent_id: UUID, version_id: UUID, stage: str) -> Response:
        tenant = self.get_tenant(request)
        if stage not in {"text", "schematic"}:
            return Response({"detail": "stage must be 'text' or 'schematic'."}, status=400)
        try:
            version = (
                PatentDocumentVersion.objects.for_tenant(tenant.pk)
                .select_related("patent")
                .get(pk=version_id, patent_id=patent_id)
            )
        except PatentDocumentVersion.DoesNotExist as exc:
            raise NotFound("Patent document version was not found.") from exc
        require_matter_access(
            request=request,
            matter_type="patent",
            matter_id=version.patent_id,
            required="EDIT",
        )
        if version.extraction_status != PatentDocumentVersion.ExtractionStatus.EXTRACTED:
            return Response(
                {"detail": "Document extraction must complete before indexing can be retried.",
                 "extraction_status": version.extraction_status},
                status=status.HTTP_409_CONFLICT,
            )

        from indexing.tasks import extract_and_index_schematics, generate_document_embeddings

        field_name = "text_indexing_status" if stage == "text" else "schematic_indexing_status"
        error_field = "text_indexing_error_code" if stage == "text" else "schematic_indexing_error_code"
        task = generate_document_embeddings if stage == "text" else extract_and_index_schematics
        queue = "embedding" if stage == "text" else "ingestion"
        stage_value = getattr(version, field_name)
        task_id_field = "text_indexing_task_id" if stage == "text" else "schematic_indexing_task_id"
        started_field = "text_indexing_started_at" if stage == "text" else "schematic_indexing_started_at"
        started_at = getattr(version, started_field)
        stale_processing = (
            stage_value == PatentDocumentVersion.StageStatus.PROCESSING
            and (started_at is None or started_at < timezone.now() - timedelta(minutes=30))
        )
        if stage_value == PatentDocumentVersion.StageStatus.PROCESSING and not stale_processing:
            return Response({"detail": "The requested indexing stage is already processing."}, status=409)
        if stage_value not in {PatentDocumentVersion.StageStatus.FAILED, PatentDocumentVersion.StageStatus.PENDING} and not stale_processing:
            return Response({"detail": "Only failed, pending, or stale processing stages can be retried.", "stage_status": stage_value}, status=409)

        task_id = str(uuid.uuid4())
        updated = PatentDocumentVersion.objects.for_tenant(tenant.pk).filter(
            pk=version.pk, **{field_name: stage_value, task_id_field: getattr(version, task_id_field)}
        ).update(**{
            field_name: PatentDocumentVersion.StageStatus.PENDING,
            error_field: "", task_id_field: task_id, started_field: None,
        })
        if not updated:
            return Response({"detail": "The indexing stage changed concurrently; refresh and retry."}, status=409)
        request_id = getattr(getattr(request, "_request", request), "request_id", None)
        try:
            audit_event = AuditLog.objects.create(
                tenant=tenant,
                actor_user_id=request.user.pk,
                request_id=request_id,
                action=f"PATENT_{stage.upper()}_INDEX_RETRY_REQUESTED",
                resource_type="PatentDocumentVersion",
                resource_id=version.pk,
                outcome=AuditLog.Outcome.FAILURE,
                metadata={"stage": stage, "queue": queue, "task_id": task_id, "stage_state": "REQUESTED"},
            )
        except Exception as exc:
            logger.exception("Index retry blocked because audit creation failed for version %s.", version.pk)
            PatentDocumentVersion.objects.for_tenant(tenant.pk).filter(pk=version.pk).update(
                **{field_name: PatentDocumentVersion.StageStatus.FAILED,
                   error_field: "AUDIT_UNAVAILABLE", task_id_field: ""}
            )
            return Response({"detail": "Required audit storage is unavailable; no retry was queued."}, status=503)
        try:
            task.apply_async(args=[str(version.pk)], queue=queue, task_id=task_id)
            AuditLog.objects.filter(pk=audit_event.pk, tenant=tenant).update(
                outcome=AuditLog.Outcome.SUCCESS,
                metadata={"stage": stage, "queue": queue, "task_id": task_id, "stage_state": "QUEUED"},
            )
        except Exception:
            logger.exception("Could not queue %s indexing retry for version %s.", stage, version.pk)
            PatentDocumentVersion.objects.for_tenant(tenant.pk).filter(pk=version.pk).update(
                **{field_name: PatentDocumentVersion.StageStatus.FAILED,
                   error_field: "QUEUE_FAILED", task_id_field: ""}
            )
            return Response({"detail": "The indexing retry could not be queued."}, status=503)
        return Response(
            {"patent_id": str(patent_id), "document_version_id": str(version.pk),
             "stage": stage, "stage_status": PatentDocumentVersion.StageStatus.PENDING,
             "queue": queue},
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
    """Reserve a version queue timestamp, publish extraction, and record failures."""
    queue_time = timezone.now()
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
            PatentDocumentVersion.ExtractionStatus.OCR_REQUIRED,
            PatentDocumentVersion.ExtractionStatus.EXTRACTION_FAILED,
        }:
            return False, "VersionAlreadyProcessing"
        if (
            version.extraction_status == PatentDocumentVersion.ExtractionStatus.PENDING
            and version.extraction_queued_at
            and version.extraction_queued_at > queue_time - EXTRACTION_QUEUE_GRACE_PERIOD
        ):
            return False, "VersionAlreadyProcessing"

        version.extraction_status = PatentDocumentVersion.ExtractionStatus.PENDING
        version.extraction_started_at = None
        version.extraction_queued_at = queue_time
        version.extraction_error_code = ""
        version.save(update_fields=(
            "extraction_status", "extraction_started_at",
            "extraction_queued_at", "extraction_error_code",
        ))
        Patent.objects.filter(pk=patent_id, tenant_id=tenant_id).update(
            status=Patent.Status.PROCESSING
        )

    try:
        process_patent_document_version.apply_async(
            args=[str(version_id)],
            queue="ingestion",
        )
    except Exception as exc:
        logger.error(
            "Could not enqueue document version %s (request_id=%s, error_type=%s).",
            version_id,
            request_id,
            type(exc).__name__,
        )
        with transaction.atomic():
            changed = PatentDocumentVersion.objects.filter(
                pk=version_id,
                tenant_id=tenant_id,
                patent_id=patent_id,
                extraction_status=PatentDocumentVersion.ExtractionStatus.PENDING,
                extraction_queued_at=queue_time,
            ).update(
                extraction_status=PatentDocumentVersion.ExtractionStatus.FAILED,
                extraction_queued_at=None,
                extraction_error_code=type(exc).__name__[:64],
            )
            if changed:
                Patent.objects.filter(pk=patent_id, tenant_id=tenant_id).update(
                    status=Patent.Status.FAILED
                )
                try:
                    # Isolate audit failure so it cannot roll back the state.
                    with transaction.atomic():
                        AuditLog.objects.create(
                            tenant_id=tenant_id,
                            actor_user_id=actor_user_id,
                            action="QUEUE_PATENT_DOCUMENT_PROCESSING",
                            resource_type="PatentDocumentVersion",
                            resource_id=version_id,
                            request_id=request_id,
                            outcome=AuditLog.Outcome.FAILURE,
                            metadata={"error_type": type(exc).__name__[:64]},
                        )
                except Exception:
                    logger.error("Could not write task-enqueue failure audit for %s.", version_id)
        if not changed:
            current = PatentDocumentVersion.objects.for_tenant(tenant_id).filter(
                pk=version_id,
                patent_id=patent_id,
            ).values("extraction_status", "extraction_queued_at").first()
            if current and (
                current["extraction_status"] != PatentDocumentVersion.ExtractionStatus.PENDING
                or current["extraction_queued_at"] != queue_time
            ):
                return True, None
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
        "extraction_queued_at": (
            version.extraction_queued_at.isoformat()
            if version.extraction_queued_at
            else None
        ),
        "extraction_started_at": (
            version.extraction_started_at.isoformat()
            if version.extraction_started_at
            else None
        ),
        "extraction_error_code": version.extraction_error_code,
        "text_indexing_status": version.text_indexing_status,
        "text_indexing_attempts": version.text_indexing_attempts,
        "text_indexing_error_code": version.text_indexing_error_code,
        "schematic_indexing_status": version.schematic_indexing_status,
        "schematic_indexing_attempts": version.schematic_indexing_attempts,
        "schematic_indexing_error_code": version.schematic_indexing_error_code,
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
