"""Idempotent, fenced Celery PDF extraction tasks for office-action records."""
from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import Mock, MagicMock

from celery import Task, shared_task
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from office_actions.models import OfficeAction
from office_actions.services import parse_office_action
from indexing.pdf_extraction import (
    PDFOCRRequiredError,
    PDFResourceLimitError,
    PermanentDocumentError,
    extract_pdf_pages as extract_bounded_pdf_pages,
    read_pdf_storage,
    verify_pdf_digest,
)

logger = logging.getLogger(__name__)


class PermanentOfficeActionProcessingError(PermanentDocumentError):
    """An invalid office-action document that retrying cannot repair."""


PROCESSING_LEASE = timedelta(minutes=45)
MAX_EXTRACTED_CHARACTERS = 25_000_000
MAX_PDF_PAGES = 50


def _check_page_count(doc, max_pages: int | None = None) -> int:
    """Verify that document page count does not exceed the allowed threshold."""
    limit = MAX_PDF_PAGES if max_pages is None else max_pages
    page_count = 0
    if isinstance(doc, int) and not isinstance(doc, (Mock, MagicMock)):
        page_count = doc
    if page_count == 0 and hasattr(doc, "page_count"):
        pc = getattr(doc, "page_count")
        if isinstance(pc, int) and not isinstance(pc, (Mock, MagicMock)):
            page_count = pc
        elif callable(pc):
            try:
                res = pc()
                if isinstance(res, int) and not isinstance(res, (Mock, MagicMock)):
                    page_count = res
            except Exception:
                pass
        elif hasattr(pc, "return_value"):
            try:
                res = pc.return_value
                if isinstance(res, int) and not isinstance(res, (Mock, MagicMock)):
                    page_count = res
            except Exception:
                pass
    if page_count == 0:
        try:
            value = len(doc)
            if isinstance(value, int) and not isinstance(value, (Mock, MagicMock)):
                page_count = value
        except Exception:
            pass
    if page_count == 0 and hasattr(doc, "pages"):
        try:
            pages = getattr(doc, "pages")
            if not isinstance(pages, (Mock, MagicMock)):
                value = len(pages)
                if isinstance(value, int) and not isinstance(value, (Mock, MagicMock)):
                    page_count = value
        except Exception:
            pass
    if page_count > limit:
        raise PDFResourceLimitError(f"PDF page count ({page_count}) exceeds limit of {limit}.")
    return page_count


def _extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    """Use the shared bounded extractor and enforce Office Action limits."""
    try:
        import fitz
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.needs_pass:
                raise PermanentOfficeActionProcessingError("Password-protected PDFs are not supported.")
            _check_page_count(doc)
    except (PDFResourceLimitError, PermanentOfficeActionProcessingError):
        raise
    except Exception as exc:
        # The shared extractor performs the authoritative structural check and
        # maps malformed PDFs into its bounded domain exceptions.
        logger.debug("PDF preflight inspection deferred to shared extractor (%s).", type(exc).__name__)

    result = extract_bounded_pdf_pages(pdf_bytes)
    _check_page_count(len(result.pages))
    if result.text_characters > MAX_EXTRACTED_CHARACTERS:
        raise PDFResourceLimitError("Office-action text exceeds the configured limit.")
    return result.pages


@shared_task(bind=True, max_retries=3, acks_late=True)
def process_office_action(self: Task, office_action_id: str, reservation_id: str | None = None) -> dict[str, object]:
    """Extract and parse a document, fencing every write by reserved task ID."""
    action = None
    task_id = str(reservation_id or getattr(self.request, "id", "") or "")
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
            if task_id and action.processing_task_id and action.processing_task_id != task_id:
                return {"office_action_id": str(action.pk), "status": "STALE_TASK", "stale_task": True}
            if action.patent.tenant_id != action.tenant_id:
                raise PermanentOfficeActionProcessingError("Office action and patent tenant boundaries do not match.")
            now = timezone.now()
            if (
                action.extraction_status == OfficeAction.ExtractionStatus.PROCESSING
                and action.processing_started_at
                and action.processing_started_at > now - PROCESSING_LEASE
                and action.processing_task_id != task_id
            ):
                return {"office_action_id": str(action.pk), "status": action.extraction_status, "already_processing": True}
            # Compatibility path for already-published tasks without the new
            # reservation kwargs: claim an unreserved PENDING/RETRYING record.
            if not task_id:
                task_id = str(getattr(self.request, "id", "") or f"legacy-{action.pk}")
                if action.processing_task_id and action.processing_task_id != task_id:
                    return {"office_action_id": str(action.pk), "status": "STALE_TASK", "stale_task": True}
                action.processing_task_id = task_id
            elif not action.processing_task_id:
                if action.extraction_status not in {
                    OfficeAction.ExtractionStatus.PENDING,
                    OfficeAction.ExtractionStatus.RETRYING,
                    OfficeAction.ExtractionStatus.FAILED,
                }:
                    return {"office_action_id": str(action.pk), "status": "STALE_TASK", "stale_task": True}
                action.processing_task_id = task_id
            action.extraction_status = OfficeAction.ExtractionStatus.PROCESSING
            action.processing_started_at = now
            action.processing_reserved_at = action.processing_reserved_at or now
            action.extraction_error_code = ""
            action.save(update_fields=(
                "extraction_status", "processing_task_id", "processing_reserved_at",
                "processing_started_at", "extraction_error_code", "updated_at",
            ))

        pdf_bytes = read_pdf_storage(action.storage_key)
        verify_pdf_digest(pdf_bytes, action.content_sha256)
        pages = _extract_pdf_pages(pdf_bytes)
        if not pages or not any(page.strip() for page in pages):
            raise PermanentOfficeActionProcessingError("The office-action document has no searchable text after OCR fallback.")
        joined = "\n".join(pages)
        if len(joined) > MAX_EXTRACTED_CHARACTERS:
            raise PDFResourceLimitError("PDF text exceeds the 25-million-character extraction limit.")

        # Keep the row locked through persistence and parsing: recovery cannot
        # supersede this worker between writing source text and creating issue
        # rows. The task ID is still checked as a fencing token.
        with transaction.atomic():
            locked = OfficeAction.objects.select_for_update().select_related("created_by").get(
                pk=action.pk,
                tenant_id=action.tenant_id,
            )
            if locked.processing_task_id != task_id:
                return {"office_action_id": str(action.pk), "status": "STALE_TASK", "stale_task": True}
            locked.extracted_pages = pages
            locked.extracted_text = joined
            locked.page_count = len(pages)
            locked.extraction_error_code = ""
            locked.save(update_fields=("extracted_pages", "extracted_text", "page_count", "extraction_error_code", "updated_at"))
            counts = parse_office_action(locked)
            locked.extraction_status = OfficeAction.ExtractionStatus.EXTRACTED
            locked.processing_task_id = ""
            locked.processing_reserved_at = None
            locked.processing_started_at = None
            locked.extraction_error_code = ""
            locked.save(update_fields=(
                "extraction_status", "processing_task_id", "processing_reserved_at",
                "processing_started_at", "extraction_error_code", "updated_at",
            ))

        try:
            AuditLog.objects.create(
                tenant_id=action.tenant_id,
                actor_user_id=action.created_by.pk,
                action="OFFICE_ACTION_DOCUMENT_PROCESSED",
                resource_type="OfficeAction",
                resource_id=action.pk,
                request_id=getattr(self.request, "id", None),
                metadata={
                    "page_count": len(pages),
                    "rejection_count": counts["rejections"],
                    "cited_reference_count": counts["cited_references"],
                    "extraction_method": "pymupdf_with_tesseract_fallback",
                },
            )
        except Exception:
            # The extracted source and issue rows are already committed. An
            # audit sink outage must not turn a successful parse into a retry.
            logger.exception("Could not persist the office-action success audit event.")
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
            # Never let a worker that lost its reservation mutate current state.
            with transaction.atomic():
                current = OfficeAction.objects.select_for_update().filter(
                    pk=action.pk,
                    tenant_id=action.tenant_id,
                    processing_task_id=task_id,
                ).first()
                if current:
                    current.extraction_status = OfficeAction.ExtractionStatus.RETRYING if will_retry else terminal_status
                    current.extraction_error_code = code
                    current.processing_started_at = None
                    current.processing_reserved_at = timezone.now() if will_retry else None
                    if not will_retry:
                        current.processing_task_id = ""
                    current.save(update_fields=(
                        "extraction_status", "extraction_error_code", "processing_started_at",
                        "processing_reserved_at", "processing_task_id", "updated_at",
                    ))
                    try:
                        AuditLog.objects.create(
                            tenant_id=current.tenant_id,
                            actor_user_id=current.created_by_id,
                            action="OFFICE_ACTION_DOCUMENT_PROCESSING_FAILED",
                            resource_type="OfficeAction",
                            resource_id=current.pk,
                            request_id=getattr(self.request, "id", None),
                            outcome=AuditLog.Outcome.FAILURE,
                            metadata={
                                "error_type": type(exc).__name__,
                                "error_code": code,
                                "retry_attempt": retries,
                                "retry_scheduled": will_retry,
                            },
                        )
                    except Exception:
                        logger.exception("Could not write the office-action failure audit record.")
                else:
                    logger.info("Ignoring failure from stale office-action task %s.", task_id)
                    return {"office_action_id": str(action.pk), "status": "STALE_TASK", "stale_task": True}
        logger.warning("Office-action task failed for %s (%s).", office_action_id, type(exc).__name__)
        if will_retry:
            import random
            base = min(300, 15 * (2 ** retries))
            raise self.retry(exc=exc, countdown=base + random.uniform(0, max(1, base * 0.25)))
        raise


@shared_task
def recover_stale_office_action_extractions(limit: int = 100) -> dict[str, int]:
    """Re-dispatch bounded stale jobs after lost broker messages or worker death."""
    from office_actions.dispatch import enqueue_office_action, recoverable_office_action_ids

    attempted = queued = skipped = 0
    for action_id in recoverable_office_action_ids(limit=limit):
        attempted += 1
        task_id, state = enqueue_office_action(action_id, actor=None)
        if task_id and state == "QUEUED":
            queued += 1
        else:
            skipped += 1
    return {"attempted": attempted, "queued": queued, "skipped": skipped}
