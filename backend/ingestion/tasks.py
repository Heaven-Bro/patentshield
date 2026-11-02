"""Celery extraction stage for tenant-owned patent document versions."""

from __future__ import annotations

import logging
import random
import uuid
from datetime import timedelta

from celery import Task, shared_task
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from indexing.pdf_extraction import (
    PDFDigestMismatchError,
    PDFExtractionError,
    PDFOCRRequiredError,
    PDFResourceLimitError,
    PermanentDocumentError,
    extract_pdf_pages,
    read_pdf_storage,
    verify_pdf_digest,
)
from ingestion.parsers import ClaimDeconstructionParser
from patents.models import Claim, Patent, PatentDocumentVersion

logger = logging.getLogger(__name__)

# Kept for compatibility with existing ingestion tests and callers.
MAX_PDF_PAGES = 2_000
MAX_EXTRACTED_CHARACTERS = 25_000_000
PROCESSING_LEASE = timedelta(minutes=45)


def _bounded_join_pages(pages, page_count: int) -> tuple[str, int]:
    """Legacy bounded helper retained for tests that exercise pypdf page mocks."""
    if page_count > MAX_PDF_PAGES:
        raise PDFResourceLimitError(
            f"PDF has {page_count} pages; the limit is {MAX_PDF_PAGES}."
        )
    extracted_pages: list[str] = []
    total_characters = 0
    for page in pages:
        page_text = page.extract_text() or ""
        total_characters += len(page_text) + (1 if extracted_pages else 0)
        if total_characters > MAX_EXTRACTED_CHARACTERS:
            raise PDFResourceLimitError(
                "PDF text exceeds the 25-million-character extraction limit."
            )
        extracted_pages.append(page_text)
    extracted_text = "\n".join(extracted_pages).strip()
    if len(extracted_text) > MAX_EXTRACTED_CHARACTERS:
        raise PDFResourceLimitError(
            "PDF text exceeds the 25-million-character extraction limit."
        )
    return extracted_text, page_count


def _extract_pdf_text(pdf_bytes: bytes) -> tuple[str, int]:
    """Use the shared bounded PyMuPDF + page-level Tesseract extraction service."""
    result = extract_pdf_pages(pdf_bytes)
    return "\n".join(result.pages).strip(), result.page_count


def _retry_countdown(retries: int) -> float:
    """Exponential retry delay with full jitter, capped at five minutes."""
    ceiling = min(300.0, 2.0 ** min(max(retries, 0), 8))
    return random.uniform(0.5, max(0.5, ceiling))




def _dispatch_pending_index_stages(document_version: PatentDocumentVersion) -> int:
    """Dispatch only unreserved pending stages, preserving partial dispatches."""
    from indexing.tasks import extract_and_index_schematics, generate_document_embeddings

    dispatches = (
        (
            "text_indexing_status", "text_indexing_task_id",
            PatentDocumentVersion.StageStatus.PENDING,
            generate_document_embeddings, "embedding",
        ),
        (
            "schematic_indexing_status", "schematic_indexing_task_id",
            PatentDocumentVersion.StageStatus.PENDING,
            extract_and_index_schematics, "ingestion",
        ),
    )
    queued = 0
    for status_field, task_id_field, pending_value, task, queue in dispatches:
        task_id = str(uuid.uuid4())
        changed = PatentDocumentVersion.objects.filter(
            pk=document_version.pk, tenant_id=document_version.tenant_id,
            **{status_field: pending_value, task_id_field: ""},
        ).update(**{task_id_field: task_id})
        if not changed:
            continue
        try:
            task.apply_async(
                args=[str(document_version.pk)], queue=queue, task_id=task_id
            )
            queued += 1
        except Exception:
            PatentDocumentVersion.objects.filter(
                pk=document_version.pk, tenant_id=document_version.tenant_id,
                **{task_id_field: task_id},
            ).update(**{task_id_field: ""})
            raise
    return queued


def _record_extraction_failure(
    document_version: PatentDocumentVersion,
    task: Task,
    error: Exception,
    *,
    status_value: str = PatentDocumentVersion.ExtractionStatus.EXTRACTION_FAILED,
) -> None:
    """Persist terminal extraction state and a correlated audit event."""
    error_code = type(error).__name__[:64]
    try:
        PatentDocumentVersion.objects.filter(
            pk=document_version.pk,
            tenant_id=document_version.tenant_id,
        ).update(
            extraction_status=status_value,
            extraction_started_at=None,
            extraction_error_code=error_code,
        )
        Patent.objects.filter(
            pk=document_version.patent_id,
            tenant_id=document_version.tenant_id,
        ).update(status=Patent.Status.FAILED)
    except Exception:
        logger.exception(
            "Could not mark patent document version %s as failed.",
            document_version.pk,
        )

    try:
        AuditLog.objects.create(
            tenant_id=document_version.tenant_id,
            actor_user_id=document_version.patent.created_by_id,
            action="PROCESS_PATENT_DOCUMENT_VERSION",
            resource_type="PatentDocumentVersion",
            resource_id=document_version.pk,
            request_id=task.request.id,
            outcome=AuditLog.Outcome.FAILURE,
            metadata={
                "error_type": type(error).__name__,
                "error_code": error_code,
                "task_id": task.request.id,
                "retry_attempt": task.request.retries,
                "extraction_status": status_value,
            },
        )
    except Exception:
        logger.exception(
            "Could not persist extraction failure audit event for version %s.",
            document_version.pk,
        )


@shared_task(
    bind=True,
    max_retries=5,
    soft_time_limit=960,
    time_limit=990,
    acks_late=True,
)
def process_patent_document_version(
    self: Task,
    document_version_id: str,
) -> dict[str, str | int]:
    """Extract searchable text/claims, then dispatch independent indexing stages."""
    document_version: PatentDocumentVersion | None = None

    try:
        already_extracted = False
        with transaction.atomic():
            document_version = (
                PatentDocumentVersion.objects.select_for_update()
                .select_related("patent")
                .get(pk=document_version_id)
            )
            if document_version.extraction_status == PatentDocumentVersion.ExtractionStatus.EXTRACTED:
                already_extracted = True
            elif (
                document_version.extraction_status == PatentDocumentVersion.ExtractionStatus.EXTRACTING
                and document_version.extraction_started_at
                and document_version.extraction_started_at > timezone.now() - PROCESSING_LEASE
            ):
                return {
                    "document_version_id": str(document_version.pk),
                    "extraction_status": document_version.extraction_status,
                    "already_processing": 1,
                }
            if not already_extracted:
                document_version.extraction_status = PatentDocumentVersion.ExtractionStatus.EXTRACTING
                document_version.extraction_started_at = timezone.now()
                document_version.text_indexing_task_id = ""
                document_version.schematic_indexing_task_id = ""
                document_version.save(update_fields=(
                    "extraction_status", "extraction_started_at",
                    "text_indexing_task_id", "schematic_indexing_task_id",
                ))
                Patent.objects.filter(
                    pk=document_version.patent_id,
                    tenant_id=document_version.tenant_id,
                ).update(status=Patent.Status.PROCESSING)

        if already_extracted:
            queued = _dispatch_pending_index_stages(document_version)
            return {
                "document_version_id": str(document_version.pk),
                "extraction_status": document_version.extraction_status,
                "indexing_tasks_dispatched": queued,
                "idempotent": 1,
            }

        if document_version.patent.tenant_id != document_version.tenant_id:
            raise PDFExtractionError("Patent and document version tenant ownership do not match.")

        pdf_bytes = read_pdf_storage(document_version.storage_key)
        verify_pdf_digest(pdf_bytes, document_version.content_sha256)
        extracted = extract_pdf_pages(pdf_bytes)
        raw_text = "\n".join(extracted.pages).strip()
        if not raw_text:
            raise PDFExtractionError("No searchable text chunks can be built from this document.")

        parsed_claims = ClaimDeconstructionParser.parse_claims_from_text(raw_text)
        claim_records = [
            Claim(
                tenant_id=document_version.tenant_id,
                patent_id=document_version.patent_id,
                document_version_id=document_version.pk,
                claim_number=claim["claim_number"],
                claim_text=claim["claim_text"],
                claim_type=claim["claim_type"],
                depends_on_claim_number=claim["depends_on_claim_number"],
            )
            for claim in parsed_claims
            if str(claim.get("claim_text") or "").strip()
        ]

        with transaction.atomic():
            Claim.objects.filter(
                tenant_id=document_version.tenant_id,
                patent_id=document_version.patent_id,
                document_version_id=document_version.pk,
            ).delete()
            Claim.objects.filter(document_version=document_version).delete()
            Claim.objects.bulk_create(claim_records, batch_size=200, ignore_conflicts=True)
            PatentDocumentVersion.objects.filter(
                pk=document_version.pk,
                tenant_id=document_version.tenant_id,
            ).update(
                extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
                extraction_started_at=None,
                page_count=extracted.page_count,
                text_indexing_status=PatentDocumentVersion.StageStatus.PENDING,
                schematic_indexing_status=PatentDocumentVersion.StageStatus.PENDING,
                extraction_error_code="",
                schematic_indexing_error_code="",
                text_indexing_task_id="",
                schematic_indexing_task_id="",
            )

        # Refresh the in-memory state after the queryset update above. The
        # exception handler uses EXTRACTED to distinguish a broker-dispatch
        # failure from a true extraction failure.
        document_version.refresh_from_db(fields=(
            "extraction_status", "text_indexing_status", "schematic_indexing_status",
            "text_indexing_task_id", "schematic_indexing_task_id",
        ))

        # Independent stages are durably reserved before dispatch. If one
        # broker publish fails, the extraction task retries and dispatches only
        # the stage whose task ID is still empty.
        dispatched = _dispatch_pending_index_stages(document_version)
        logger.info(
            "Extracted document version %s: %s pages, %s OCR pages, %s claims.",
            document_version.pk,
            extracted.page_count,
            extracted.ocr_pages,
            len(claim_records),
        )
        return {
            "document_version_id": str(document_version.pk),
            "extraction_status": PatentDocumentVersion.ExtractionStatus.EXTRACTED,
            "page_count": extracted.page_count,
            "ocr_pages": extracted.ocr_pages,
            "claims_created": len(claim_records),
            "indexing_tasks_dispatched": dispatched,
        }

    except Exception as exc:
        logger.exception(
            "Patent document extraction failed for version ID %s (task=%s, retry=%s).",
            document_version_id,
            self.request.id,
            self.request.retries,
        )
        if (
            document_version is not None
            and document_version.extraction_status == PatentDocumentVersion.ExtractionStatus.EXTRACTED
        ):
            if self.request.retries >= self.max_retries:
                PatentDocumentVersion.objects.filter(
                    pk=document_version.pk, tenant_id=document_version.tenant_id,
                    text_indexing_status=PatentDocumentVersion.StageStatus.PENDING,
                    text_indexing_task_id="",
                ).update(
                    text_indexing_status=PatentDocumentVersion.StageStatus.FAILED,
                    text_indexing_error_code="QUEUE_FAILED",
                )
                PatentDocumentVersion.objects.filter(
                    pk=document_version.pk, tenant_id=document_version.tenant_id,
                    schematic_indexing_status=PatentDocumentVersion.StageStatus.PENDING,
                    schematic_indexing_task_id="",
                ).update(
                    schematic_indexing_status=PatentDocumentVersion.StageStatus.FAILED,
                    schematic_indexing_error_code="QUEUE_FAILED",
                )
                raise
            raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))

        if document_version is not None and isinstance(exc, PermanentDocumentError):
            final_status = (
                PatentDocumentVersion.ExtractionStatus.OCR_REQUIRED
                if isinstance(exc, PDFOCRRequiredError)
                else PatentDocumentVersion.ExtractionStatus.EXTRACTION_FAILED
            )
            _record_extraction_failure(
                document_version, self, exc, status_value=final_status
            )
            raise

        if self.request.retries >= self.max_retries:
            if document_version is not None:
                _record_extraction_failure(document_version, self, exc)
            raise

        if document_version is not None:
            PatentDocumentVersion.objects.filter(
                pk=document_version.pk,
                tenant_id=document_version.tenant_id,
            ).update(
                extraction_status=PatentDocumentVersion.ExtractionStatus.PENDING,
                extraction_started_at=None,
            )
            try:
                AuditLog.objects.create(
                    tenant_id=document_version.tenant_id,
                    actor_user_id=document_version.patent.created_by_id,
                    action="PROCESS_PATENT_DOCUMENT_VERSION_RETRY",
                    resource_type="PatentDocumentVersion",
                    resource_id=document_version.pk,
                    request_id=self.request.id,
                    outcome=AuditLog.Outcome.FAILURE,
                    metadata={
                        "error_type": type(exc).__name__,
                        "task_id": self.request.id,
                        "retry_attempt": self.request.retries,
                    },
                )
            except Exception:
                logger.exception("Could not persist extraction retry audit for %s.", document_version.pk)
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))
