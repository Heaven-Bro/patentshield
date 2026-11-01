"""Idempotent Celery PDF extraction tasks for office-action records."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import Task, shared_task
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from office_actions.models import OfficeAction
from office_actions.services import parse_office_action
from indexing.pdf_extraction import (
    PDFOCRRequiredError, PDFResourceLimitError, PermanentDocumentError,
    extract_pdf_pages as extract_bounded_pdf_pages, read_pdf_storage, verify_pdf_digest,
)

logger = logging.getLogger(__name__)


class PermanentOfficeActionProcessingError(PermanentDocumentError):
    """An invalid office-action document that retrying cannot repair."""


PROCESSING_LEASE = timedelta(minutes=45)
MAX_EXTRACTED_CHARACTERS = 25_000_000


def _extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    """Compatibility wrapper around the shared bounded OCR-capable extractor."""
    result = extract_bounded_pdf_pages(pdf_bytes)
    if result.text_characters > MAX_EXTRACTED_CHARACTERS:
        raise PDFResourceLimitError("Office-action text exceeds the configured limit.")
    return result.pages


@shared_task(bind=True, max_retries=3, acks_late=True)
def process_office_action(self: Task, office_action_id: str) -> dict[str, object]:
    """Extract, classify, and map citations with row-lock single-flight semantics."""
    action = None
    try:
        with transaction.atomic():
            action = (
                OfficeAction.objects.select_for_update()
                .select_related("patent", "created_by")
                .get(pk=office_action_id)
            )
            if (
                action.extraction_status == OfficeAction.ExtractionStatus.EXTRACTED
                and action.parse_status in {
                    OfficeAction.ParseStatus.COMPLETED,
                    OfficeAction.ParseStatus.NEEDS_REVIEW,
                }
            ):
                return {"office_action_id": str(action.pk), "status": action.extraction_status, "idempotent": True}
            now = timezone.now()
            if (
                action.extraction_status == OfficeAction.ExtractionStatus.PROCESSING
                and action.updated_at > now - PROCESSING_LEASE
            ):
                return {"office_action_id": str(action.pk), "status": action.extraction_status, "already_processing": True}
            if action.patent.tenant_id != action.tenant_id:
                raise PermanentOfficeActionProcessingError("Office action and patent tenant boundaries do not match.")
            action.extraction_status = OfficeAction.ExtractionStatus.PROCESSING
            action.extraction_error_code = ""
            action.save(update_fields=("extraction_status", "extraction_error_code", "updated_at"))

        try:
            pdf_bytes = read_pdf_storage(action.storage_key)
        except FileNotFoundError as exc:
            raise PermanentOfficeActionProcessingError("Stored office-action PDF is missing.") from exc
        verify_pdf_digest(pdf_bytes, action.content_sha256)
        try:
            pages = _extract_pdf_pages(pdf_bytes)
        except PDFResourceLimitError:
            raise
        except PermanentDocumentError:
            raise
        if not pages or not any(page.strip() for page in pages):
            raise PermanentOfficeActionProcessingError("The office-action document has no searchable text after OCR fallback.")
        joined = "\n".join(pages)
        if len(joined) > MAX_EXTRACTED_CHARACTERS:
            raise PDFResourceLimitError("PDF text exceeds the 25-million-character extraction limit.")
        with transaction.atomic():
            locked = OfficeAction.objects.select_for_update().get(pk=action.pk, tenant_id=action.tenant_id)
            # Keep PROCESSING set through parsing; duplicate Celery deliveries see
            # the active lease and return without repeating expensive extraction.
            locked.extracted_pages = pages
            locked.extracted_text = joined
            locked.page_count = len(pages)
            locked.extraction_error_code = ""
            locked.save(update_fields=("extracted_pages", "extracted_text", "page_count", "extraction_error_code", "updated_at"))
        counts = parse_office_action(locked)
        with transaction.atomic():
            locked = OfficeAction.objects.select_for_update().get(pk=action.pk, tenant_id=action.tenant_id)
            locked.extraction_status = OfficeAction.ExtractionStatus.EXTRACTED
            locked.extraction_error_code = ""
            locked.save(update_fields=("extraction_status", "extraction_error_code", "updated_at"))
        AuditLog.objects.create(
            tenant_id=action.tenant_id,
            actor_user_id=action.created_by.pk,
            action="OFFICE_ACTION_DOCUMENT_PROCESSED",
            resource_type="OfficeAction",
            resource_id=action.pk,
            request_id=getattr(self.request, "id", None),
            metadata={"page_count": len(pages), "rejection_count": counts["rejections"], "cited_reference_count": counts["cited_references"], "extraction_method": "pymupdf_with_tesseract_fallback"},
        )
        return {"office_action_id": str(action.pk), "status": OfficeAction.ExtractionStatus.EXTRACTED, **counts}
    except Exception as exc:
        retries = getattr(self.request, "retries", 0)
        is_permanent = (
            isinstance(exc, PermanentDocumentError)
            or action is None
            or isinstance(exc, OfficeAction.DoesNotExist)
        )
        will_retry = not is_permanent and retries < self.max_retries
        if action is not None:
            code = (
                "OCR_REQUIRED" if isinstance(exc, PDFOCRRequiredError) else
                "RESOURCE_LIMIT_EXCEEDED" if isinstance(exc, PDFResourceLimitError) else
                "INTEGRITY_CHECK_FAILED" if "SHA-256" in str(exc) or "digest" in str(exc).lower() else
                "PDF_EXTRACTION_FAILED"
            )
            terminal_status = (
                OfficeAction.ExtractionStatus.OCR_REQUIRED
                if isinstance(exc, PDFOCRRequiredError)
                else OfficeAction.ExtractionStatus.EXTRACTION_FAILED
            )
            OfficeAction.objects.filter(pk=action.pk, tenant_id=action.tenant_id).update(
                extraction_status=(OfficeAction.ExtractionStatus.RETRYING if will_retry else terminal_status),
                extraction_error_code=code,
                updated_at=timezone.now(),
            )
            try:
                AuditLog.objects.create(
                    tenant_id=action.tenant_id,
                    actor_user_id=action.created_by.pk,
                    action="OFFICE_ACTION_DOCUMENT_PROCESSING_FAILED",
                    resource_type="OfficeAction",
                    resource_id=action.pk,
                    request_id=getattr(self.request, "id", None),
                    outcome=AuditLog.Outcome.FAILURE,
                    metadata={"error_type": type(exc).__name__, "error_code": code, "retry_attempt": retries, "retry_scheduled": will_retry},
                )
            except Exception:
                logger.exception("Could not write the office-action failure audit record.")
        logger.warning("Office-action task failed for %s (%s).", office_action_id, type(exc).__name__)
        if will_retry:
            import random
            base = min(300, 15 * (2 ** retries))
            raise self.retry(exc=exc, countdown=base + random.uniform(0, max(1, base * 0.25)))
        raise
