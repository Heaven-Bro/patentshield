"""Celery tasks for extracting text and claims from patent documents."""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
from datetime import timedelta

from celery import Task, shared_task
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from ingestion.parsers import ClaimDeconstructionParser
from patents.models import Claim, Patent, PatentDocumentVersion

logger = logging.getLogger(__name__)

MAX_PDF_PAGES = 2_000
MAX_EXTRACTED_CHARACTERS = 25_000_000
PROCESSING_LEASE = timedelta(minutes=45)


class PDFResourceLimitError(ValueError):
    """Raised when a PDF exceeds the configured extraction resource limits."""


def _bounded_join_pages(pages, page_count: int) -> tuple[str, int]:
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
    """Extract bounded text with pypdf, falling back to PyMuPDF.

    Limits are checked before iterating pages and after each page extraction to
    prevent unbounded document/page accumulation in a background worker.
    """
    extraction_errors: list[Exception] = []

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        extraction_errors.append(exc)
    else:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
            page_count = len(reader.pages)
            if page_count > MAX_PDF_PAGES:
                raise PDFResourceLimitError(
                    f"PDF has {page_count} pages; the limit is {MAX_PDF_PAGES}."
                )
            extracted_text, page_count = _bounded_join_pages(reader.pages, page_count)
            if extracted_text:
                return extracted_text, page_count
            extraction_errors.append(
                ValueError("pypdf did not extract readable text from this PDF.")
            )
        except PDFResourceLimitError:
            raise
        except Exception as exc:
            extraction_errors.append(exc)

    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError as exc:
        extraction_errors.append(exc)
    else:
        try:
            with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf_document:
                page_count = pdf_document.page_count
                if page_count > MAX_PDF_PAGES:
                    raise PDFResourceLimitError(
                        f"PDF has {page_count} pages; the limit is {MAX_PDF_PAGES}."
                    )
                # PyMuPDF pages expose get_text(), not pypdf's extract_text().
                page_texts: list[str] = []
                total_characters = 0
                for page in pdf_document:
                    page_text = page.get_text("text") or ""
                    total_characters += len(page_text) + (1 if page_texts else 0)
                    if total_characters > MAX_EXTRACTED_CHARACTERS:
                        raise PDFResourceLimitError(
                            "PDF text exceeds the 25-million-character extraction limit."
                        )
                    page_texts.append(page_text)
                extracted_text = "\n".join(page_texts).strip()
                if len(extracted_text) > MAX_EXTRACTED_CHARACTERS:
                    raise PDFResourceLimitError(
                        "PDF text exceeds the 25-million-character extraction limit."
                    )
            if extracted_text:
                return extracted_text, page_count
            extraction_errors.append(
                ValueError("PyMuPDF did not extract readable text from this PDF.")
            )
        except PDFResourceLimitError:
            raise
        except Exception as exc:
            extraction_errors.append(exc)

    if not extraction_errors:
        raise RuntimeError("No PDF extraction backend is available.")
    if all(isinstance(error, ImportError) for error in extraction_errors):
        raise RuntimeError(
            "PDF extraction requires pypdf or PyMuPDF; install one in the worker image."
        ) from extraction_errors[-1]
    raise RuntimeError("Unable to extract readable text from the PDF.") from extraction_errors[-1]

def _record_extraction_failure(
    document_version: PatentDocumentVersion,
    task: Task,
    error: Exception,
) -> None:
    """Persist failure status and an audit event without masking the cause."""
    try:
        PatentDocumentVersion.objects.filter(
            pk=document_version.pk,
            tenant_id=document_version.tenant_id,
        ).update(
            extraction_status=PatentDocumentVersion.ExtractionStatus.FAILED,
            extraction_started_at=None,
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
                "task_id": task.request.id,
                "retry_attempt": task.request.retries,
            },
        )
    except Exception:
        # Audit persistence must not prevent Celery retry behavior.
        logger.exception(
            "Could not persist extraction failure audit event for version %s.",
            document_version.pk,
        )


@shared_task(bind=True, max_retries=3)
def process_patent_document_version(
    self: Task,
    document_version_id: str,
) -> dict[str, str | int]:
    """Extract PDF text, deconstruct claims, and store results idempotently."""
    document_version: PatentDocumentVersion | None = None

    try:
        with transaction.atomic():
            document_version = (
                PatentDocumentVersion.objects.select_for_update()
                .select_related("patent")
                .get(pk=document_version_id)
            )
            if document_version.extraction_status == PatentDocumentVersion.ExtractionStatus.EXTRACTED:
                return {
                    "document_version_id": str(document_version.pk),
                    "extraction_status": document_version.extraction_status,
                    "idempotent": 1,
                }
            if (
                document_version.extraction_status == PatentDocumentVersion.ExtractionStatus.EXTRACTING
                and document_version.extraction_started_at
                and document_version.extraction_started_at > timezone.now() - PROCESSING_LEASE
            ):
                return {
                    "document_version_id": str(document_version.pk),
                    "extraction_status": document_version.extraction_status,
                    "already_processing": 1,
                }
            document_version.extraction_status = PatentDocumentVersion.ExtractionStatus.EXTRACTING
            document_version.extraction_started_at = timezone.now()
            document_version.save(update_fields=("extraction_status", "extraction_started_at"))
            Patent.objects.filter(
                pk=document_version.patent_id,
                tenant_id=document_version.tenant_id,
            ).update(status=Patent.Status.PROCESSING)

        if document_version.patent.tenant_id != document_version.tenant_id:
            raise ValueError(
                "Patent and document version must belong to the same tenant."
            )

        with default_storage.open(
            document_version.storage_key, "rb"
        ) as file_obj:
            pdf_bytes = file_obj.read()
        if not pdf_bytes:
            raise ValueError("The stored patent document is empty.")

        calculated_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        expected_sha256 = document_version.content_sha256.strip().lower()
        if not hmac.compare_digest(calculated_sha256, expected_sha256):
            raise ValueError(
                "Stored document SHA-256 does not match its metadata."
            )

        raw_text, page_count = _extract_pdf_text(pdf_bytes)
        parsed_claims = (
            ClaimDeconstructionParser.parse_claims_from_text(raw_text)
        )

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
        ]

        # Replace claims and mark the version extracted in one transaction so
        # retries cannot leave duplicate or partially written claim rows.
        with transaction.atomic():
            Claim.objects.filter(
                tenant_id=document_version.tenant_id,
                patent_id=document_version.patent_id,
                document_version_id=document_version.pk,
            ).delete()
            Claim.objects.bulk_create(claim_records, batch_size=500)
            PatentDocumentVersion.objects.filter(
                pk=document_version.pk,
                tenant_id=document_version.tenant_id,
            ).update(
                extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
                extraction_started_at=None,
                page_count=page_count,
            )

        # Extraction is the first stage of the document pipeline. Dispatch text
        # embeddings and schematic crops only after claims/status are committed.
        # Both downstream tasks are idempotent and can safely be retried.
        from indexing.tasks import (
            extract_and_index_schematics,
            generate_document_embeddings,
        )

        generate_document_embeddings.apply_async(
            args=[str(document_version.pk)],
            queue="embedding",
        )
        extract_and_index_schematics.apply_async(
            args=[str(document_version.pk)],
            queue="ingestion",
        )

        logger.info(
            "Extracted patent document version %s: %d page(s), %d claim(s).",
            document_version.pk,
            page_count,
            len(claim_records),
        )
        return {
            "document_version_id": str(document_version.pk),
            "extraction_status": (
                PatentDocumentVersion.ExtractionStatus.EXTRACTED
            ),
            "page_count": page_count,
            "claims_created": len(claim_records),
            "indexing_tasks_dispatched": 2,
        }

    except Exception as exc:
        logger.exception(
            "Patent document extraction failed for version ID %s.",
            document_version_id,
        )
        if document_version is not None:
            _record_extraction_failure(document_version, self, exc)
        else:
            # A tenant-scoped AuditLog row cannot safely be created when the
            # document version (and therefore its tenant) cannot be resolved.
            logger.error(
                "No tenant context is available to audit failed version %s.",
                document_version_id,
            )
        if isinstance(exc, PDFResourceLimitError):
            # Retrying an over-limit artifact cannot succeed and amplifies CPU cost.
            raise
        raise self.retry(exc=exc, countdown=60)
