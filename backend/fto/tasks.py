"""Idempotent product-spec extraction and feature-candidate generation tasks."""
from __future__ import annotations

import logging
from pathlib import Path

from celery import shared_task
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from fto.models import ProductFeature, ProductVersion
from fto.services import decompose_product_specification, extract_pdf_pages

logger = logging.getLogger(__name__)


@shared_task(bind=True, acks_late=True, max_retries=2, default_retry_delay=45)
def process_product_version(self, version_id: str, tenant_id: str) -> dict[str, str | int]:
    """Extract a spec PDF and create idempotent, source-cited feature candidates.

    The tenant ID is part of the task payload and is revalidated against both
    the version and parent product before the task changes or reads any data.
    """
    with transaction.atomic():
        version = (
            ProductVersion.objects.select_for_update()
            .select_related("product", "created_by")
            .get(pk=version_id, tenant_id=tenant_id)
        )
        if version.product.tenant_id != version.tenant_id:
            raise ValueError("Product-version tenant validation failed.")
        if (version.extraction_status == ProductVersion.ExtractionStatus.EXTRACTED
                and version.decomposition_status == ProductVersion.DecompositionStatus.COMPLETED):
            existing_count = ProductFeature.objects.for_tenant(version.tenant_id).filter(
                product_version=version,
                product_version__product__tenant_id=version.tenant_id,
            ).count()
            return {"version_id": str(version.pk), "status": "COMPLETED", "features": existing_count}
        version.extraction_status = ProductVersion.ExtractionStatus.PROCESSING
        version.decomposition_status = ProductVersion.DecompositionStatus.PROCESSING
        version.error_code = ""
        version.save(update_fields=("extraction_status", "decomposition_status", "error_code"))

    try:
        with default_storage.open(version.storage_key, "rb") as handle:
            pdf_bytes = handle.read()
        pages = extract_pdf_pages(pdf_bytes)
    except ValueError:
        ProductVersion.objects.filter(pk=version.pk, tenant_id=version.tenant_id).update(
            extraction_status=ProductVersion.ExtractionStatus.FAILED,
            decomposition_status=ProductVersion.DecompositionStatus.FAILED,
            error_code="PDF_TEXT_EXTRACTION_FAILED",
        )
        return {"version_id": str(version.pk), "status": "FAILED", "error_code": "PDF_TEXT_EXTRACTION_FAILED"}
    except Exception as exc:
        logger.warning("Product specification read failed (version_id=%s, error_type=%s).", version.pk, type(exc).__name__)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=45 * (self.request.retries + 1))
        ProductVersion.objects.filter(pk=version.pk, tenant_id=version.tenant_id).update(
            extraction_status=ProductVersion.ExtractionStatus.FAILED,
            decomposition_status=ProductVersion.DecompositionStatus.FAILED,
            error_code="SOURCE_READ_FAILED",
        )
        return {"version_id": str(version.pk), "status": "FAILED", "error_code": "SOURCE_READ_FAILED"}

    if not any(page.strip() for page in pages):
        ProductVersion.objects.filter(pk=version.pk, tenant_id=version.tenant_id).update(
            extraction_status=ProductVersion.ExtractionStatus.FAILED,
            decomposition_status=ProductVersion.DecompositionStatus.FAILED,
            page_count=len(pages),
            extracted_pages=pages,
            error_code="NO_SELECTABLE_TEXT_OCR_REQUIRED",
        )
        return {"version_id": str(version.pk), "status": "FAILED", "error_code": "NO_SELECTABLE_TEXT_OCR_REQUIRED"}

    candidates = decompose_product_specification(pages)
    with transaction.atomic():
        current = ProductVersion.objects.select_for_update().get(pk=version.pk, tenant_id=version.tenant_id)
        current.page_count = len(pages)
        current.extracted_pages = pages
        current.extracted_text = "\n\n".join(pages)
        current.extraction_status = ProductVersion.ExtractionStatus.EXTRACTED
        current.decomposition_status = ProductVersion.DecompositionStatus.COMPLETED
        current.error_code = "" if candidates else "NO_FEATURE_CANDIDATES_FOUND"
        current.save(update_fields=("page_count", "extracted_pages", "extracted_text", "extraction_status", "decomposition_status", "error_code"))

        # get_or_create keeps task redelivery from duplicating candidates. It
        # intentionally does not delete or overwrite attorney-reviewed features.
        created_count = 0
        for candidate in candidates:
            _feature, created = ProductFeature.objects.get_or_create(
                tenant_id=current.tenant_id,
                product_version=current,
                feature_key=candidate.feature_key,
                defaults={
                    "title": candidate.title,
                    "description": candidate.source_quote,
                    "source_quote": candidate.source_quote,
                    "source_page_index": candidate.page_index,
                    "source_start_offset": candidate.start_offset,
                    "source_end_offset": candidate.end_offset,
                    "extraction_method": "RULE_BASED_CANDIDATE",
                    "extraction_confidence": candidate.confidence,
                    "review_status": ProductFeature.ReviewStatus.CANDIDATE,
                    "created_by_id": current.created_by_id,
                },
            )
            created_count += int(created)

        try:
            AuditLog.objects.create(
                tenant_id=current.tenant_id,
                actor_user_id=current.created_by_id,
                action="FTO_PRODUCT_SPEC_PROCESSED",
                resource_type="ProductVersion",
                resource_id=current.pk,
                outcome=AuditLog.Outcome.SUCCESS,
                metadata={"page_count": len(pages), "candidate_count": created_count, "content_sha256": current.content_sha256},
            )
        except Exception:
            logger.exception("Failed to audit product specification processing.")

    return {"version_id": str(version.pk), "status": "COMPLETED", "features_created": created_count}
