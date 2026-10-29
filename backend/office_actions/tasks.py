"""Idempotent Celery PDF extraction tasks for office-action records."""
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
from office_actions.models import OfficeAction
from office_actions.services import parse_office_action

logger = logging.getLogger(__name__)


class PermanentOfficeActionProcessingError(Exception):
    """An invalid document or configuration issue that retry cannot fix."""


class PDFResourceLimitError(PermanentOfficeActionProcessingError):
    """The supplied PDF exceeds page or extracted-text limits."""


MAX_PDF_PAGES = 2_000
MAX_EXTRACTED_CHARACTERS = 25_000_000
PROCESSING_LEASE = timedelta(minutes=45)


def _check_page_count(page_count: int) -> None:
    if page_count > MAX_PDF_PAGES:
        raise PDFResourceLimitError(
            f"PDF has {page_count} pages; the limit is {MAX_PDF_PAGES}."
        )


def _extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    """Extract text with page and total-character limits before accumulation."""
    errors: list[Exception] = []
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        errors.append(exc)
    else:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
            _check_page_count(len(reader.pages))
            pages: list[str] = []
            total_characters = 0
            for page in reader.pages:
                text = page.extract_text() or ""
                total_characters += len(text) + (1 if pages else 0)
                if total_characters > MAX_EXTRACTED_CHARACTERS:
                    raise PDFResourceLimitError(
                        "PDF text exceeds the 25-million-character extraction limit."
                    )
                pages.append(text)
            if any(page.strip() for page in pages):
                return pages
            errors.append(ValueError("No readable text was extracted by pypdf."))
        except PDFResourceLimitError:
            raise
        except Exception as exc:
            errors.append(exc)
    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError as exc:
        errors.append(exc)
    else:
        try:
            with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
                _check_page_count(document.page_count)
                pages: list[str] = []
                total_characters = 0
                for page in document:
                    text = page.get_text("text") or ""
                    total_characters += len(text) + (1 if pages else 0)
                    if total_characters > MAX_EXTRACTED_CHARACTERS:
                        raise PDFResourceLimitError(
                            "PDF text exceeds the 25-million-character extraction limit."
                        )
                    pages.append(text)
            if any(page.strip() for page in pages):
                return pages
            errors.append(ValueError("No readable text was extracted by PyMuPDF."))
        except PDFResourceLimitError:
            raise
        except Exception as exc:
            errors.append(exc)
    if errors and all(isinstance(error, ImportError) for error in errors):
        raise RuntimeError("Office-action PDF extraction requires pypdf or PyMuPDF.") from errors[-1]
    raise RuntimeError("Could not extract readable text from the office-action PDF.") from (errors[-1] if errors else None)


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
            with default_storage.open(action.storage_key, "rb") as source:
                pdf_bytes = source.read()
        except FileNotFoundError as exc:
            raise PermanentOfficeActionProcessingError("Stored office-action PDF is missing.") from exc
        if not pdf_bytes:
            raise PermanentOfficeActionProcessingError("Stored office-action PDF is empty.")
        actual_digest = hashlib.sha256(pdf_bytes).hexdigest()
        if not hmac.compare_digest(actual_digest, action.content_sha256.lower()):
            raise PermanentOfficeActionProcessingError("Stored office-action PDF failed its SHA-256 integrity check.")
        try:
            pages = _extract_pdf_pages(pdf_bytes)
        except PDFResourceLimitError:
            raise
        except RuntimeError as exc:
            raise PermanentOfficeActionProcessingError(str(exc)) from exc
        if not pages or not any(page.strip() for page in pages):
            raise PermanentOfficeActionProcessingError("The office-action document has no extractable text; OCR is not configured.")
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
            metadata={"page_count": len(pages), "rejection_count": counts["rejections"], "cited_reference_count": counts["cited_references"], "extraction_method": "pypdf_or_pymupdf"},
        )
        return {"office_action_id": str(action.pk), "status": OfficeAction.ExtractionStatus.EXTRACTED, **counts}
    except Exception as exc:
        retries = getattr(self.request, "retries", 0)
        is_permanent = isinstance(exc, PermanentOfficeActionProcessingError) or action is None or isinstance(exc, OfficeAction.DoesNotExist)
        will_retry = not is_permanent and retries < self.max_retries
        if action is not None:
            code = "RESOURCE_LIMIT_EXCEEDED" if isinstance(exc, PDFResourceLimitError) else ("INTEGRITY_CHECK_FAILED" if "SHA-256" in str(exc) else "PDF_EXTRACTION_FAILED")
            OfficeAction.objects.filter(pk=action.pk, tenant_id=action.tenant_id).update(
                extraction_status=(OfficeAction.ExtractionStatus.RETRYING if will_retry else OfficeAction.ExtractionStatus.FAILED),
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
            raise self.retry(exc=exc, countdown=min(300, 15 * (2 ** retries)))
        raise
