"""Idempotent product-spec extraction and feature-candidate generation tasks."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import F, Q
from django.contrib.auth import get_user_model
from django.utils import timezone

from audit.models import AuditLog
from fto.models import FTOCandidateMatch, FTOStudy, LimitationAssessment, ProductFeature, ProductVersion
from fto.services import decompose_product_specification, rank_product_features_for_limitation
from enterprise_identity.services import matter_access_allowed
from tenants.models import Tenant
from indexing.pdf_extraction import (
    PDFOCRRequiredError, PermanentDocumentError, extract_pdf_pages,
    read_pdf_storage, verify_pdf_digest,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, acks_late=True, max_retries=3)
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
        pdf_bytes = read_pdf_storage(version.storage_key)
        verify_pdf_digest(pdf_bytes, version.content_sha256)
        pages = extract_pdf_pages(pdf_bytes).pages
    except PermanentDocumentError as exc:
        ocr_required = isinstance(exc, PDFOCRRequiredError)
        terminal_status = (
            ProductVersion.ExtractionStatus.OCR_REQUIRED
            if ocr_required else ProductVersion.ExtractionStatus.EXTRACTION_FAILED
        )
        error_code = "OCR_REQUIRED" if ocr_required else (
            "PDF_RESOURCE_LIMIT_EXCEEDED" if "limit" in str(exc).lower() else
            "PDF_INTEGRITY_OR_EXTRACTION_FAILED"
        )
        ProductVersion.objects.filter(pk=version.pk, tenant_id=version.tenant_id).update(
            extraction_status=terminal_status,
            decomposition_status=ProductVersion.DecompositionStatus.FAILED,
            error_code=error_code,
        )
        logger.warning("FTO PDF permanently rejected (version_id=%s, code=%s).", version.pk, error_code)
        return {"version_id": str(version.pk), "status": terminal_status, "error_code": error_code}
    except Exception as exc:
        logger.exception("Transient FTO document processing failure (version_id=%s).", version.pk)
        if self.request.retries < self.max_retries:
            import random
            base = min(300, 20 * (2 ** self.request.retries))
            raise self.retry(exc=exc, countdown=base + random.uniform(0, max(1, base * 0.25)))
        ProductVersion.objects.filter(pk=version.pk, tenant_id=version.tenant_id).update(
            extraction_status=ProductVersion.ExtractionStatus.FAILED,
            decomposition_status=ProductVersion.DecompositionStatus.FAILED,
            error_code="SOURCE_READ_OR_PROCESSING_FAILED",
        )
        return {"version_id": str(version.pk), "status": "FAILED", "error_code": "SOURCE_READ_OR_PROCESSING_FAILED"}

    if not any(page.strip() for page in pages):
        ProductVersion.objects.filter(pk=version.pk, tenant_id=version.tenant_id).update(
            extraction_status=ProductVersion.ExtractionStatus.EXTRACTION_FAILED,
            decomposition_status=ProductVersion.DecompositionStatus.FAILED,
            page_count=len(pages),
            extracted_pages=pages,
            error_code="NO_SEARCHABLE_TEXT_AFTER_OCR",
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


# Candidate matching is a review aid, never a legal or infringement conclusion.
FTO_MATCH_QUEUE_STALE_AFTER = timedelta(minutes=10)
FTO_MATCH_RUNNING_STALE_AFTER = timedelta(minutes=30)
FTO_MATCH_RECOVERY_BATCH = 100
FTO_MATCH_MAX_FEATURES = 500
FTO_MATCH_ALGORITHM_VERSION = "LEXICAL_OVERLAP_V1"


def enqueue_fto_candidate_match_generation(
    study_id: str,
    tenant_id: str,
    *,
    force: bool = False,
    requested_by_id: str | None = None,
) -> str | None:
    """Persist a fenced dispatch reservation before sending an FTO match task."""
    now = timezone.now()
    task_id = str(uuid.uuid4())
    with transaction.atomic():
        study = FTOStudy.objects.select_for_update().filter(pk=study_id, tenant_id=tenant_id).first()
        if study is None:
            return None
        if study.matching_status == FTOStudy.MatchingStatus.COMPLETED and not force:
            return None
        if (
            study.matching_status == FTOStudy.MatchingStatus.QUEUED
            and study.matching_queued_at
            and study.matching_queued_at >= now - FTO_MATCH_QUEUE_STALE_AFTER
        ):
            return None
        if (
            study.matching_status == FTOStudy.MatchingStatus.RUNNING
            and study.matching_started_at
            and study.matching_started_at >= now - FTO_MATCH_RUNNING_STALE_AFTER
        ):
            return None
        study.matching_status = FTOStudy.MatchingStatus.QUEUED
        study.matching_task_id = task_id
        study.matching_requested_by_id = requested_by_id or study.created_by_id
        study.matching_queued_at = now
        study.matching_started_at = None
        study.matching_completed_at = None
        study.matching_error_code = ""
        study.save(update_fields=(
            "matching_status", "matching_task_id", "matching_requested_by", "matching_queued_at",
            "matching_started_at", "matching_completed_at", "matching_error_code", "updated_at",
        ))

    try:
        generate_fto_candidate_matches.apply_async(
            args=(str(study_id), str(tenant_id)),
            task_id=task_id,
            queue="analysis",
        )
    except Exception as exc:
        FTOStudy.objects.filter(pk=study_id, tenant_id=tenant_id, matching_task_id=task_id).update(
            matching_status=FTOStudy.MatchingStatus.FAILED,
            matching_error_code="TASK_ENQUEUE_FAILED",
            matching_task_id="",
            updated_at=timezone.now(),
        )
        logger.warning("FTO candidate-match enqueue failed (study_id=%s, error_type=%s).", study_id, type(exc).__name__)
        return None
    return task_id


@shared_task(bind=True, acks_late=True, max_retries=2)
def generate_fto_candidate_matches(self, study_id: str, tenant_id: str) -> dict[str, str | int]:
    """Generate bounded lexical suggestions, persisting only source-linked candidates."""
    task_id = str(self.request.id or "")
    with transaction.atomic():
        study = FTOStudy.objects.select_for_update().filter(
            pk=study_id, tenant_id=tenant_id,
            product_version__tenant_id=tenant_id,
            product_version__product__tenant_id=tenant_id,
            patent__tenant_id=tenant_id,
            patent_document_version__tenant_id=tenant_id,
            patent_document_version__patent_id=F("patent_id"),
        ).first()
        if study is None:
            return {"study_id": str(study_id), "status": "NOT_FOUND"}
        if not task_id or study.matching_task_id != task_id:
            return {"study_id": str(study_id), "status": "STALE_TASK_IGNORED"}
        if study.matching_status == FTOStudy.MatchingStatus.COMPLETED:
            return {"study_id": str(study_id), "status": "COMPLETED"}
        requester_id = study.matching_requested_by_id or study.created_by_id
        requester = get_user_model().objects.filter(pk=requester_id, is_active=True).first()
        tenant = Tenant.objects.filter(pk=tenant_id, status=Tenant.Status.ACTIVE).first()
        has_access = bool(
            requester and tenant
            and matter_access_allowed(tenant=tenant, user=requester, matter_type="fto_study", matter_id=study.pk, required="READ")
            and matter_access_allowed(tenant=tenant, user=requester, matter_type="product_matter", matter_id=study.product_version.product_id, required="READ")
            and matter_access_allowed(tenant=tenant, user=requester, matter_type="patent", matter_id=study.patent_id, required="READ")
        )
        if not has_access:
            study.matching_status = FTOStudy.MatchingStatus.FAILED
            study.matching_error_code = "REQUESTER_ACCESS_REVOKED"
            study.matching_task_id = ""
            study.save(update_fields=("matching_status", "matching_error_code", "matching_task_id", "updated_at"))
            return {"study_id": str(study_id), "status": "FAILED", "error_code": "REQUESTER_ACCESS_REVOKED"}
        study.matching_status = FTOStudy.MatchingStatus.RUNNING
        study.matching_started_at = timezone.now()
        study.matching_error_code = ""
        study.save(update_fields=("matching_status", "matching_started_at", "matching_error_code", "updated_at"))

    try:
        assessments = list(
            LimitationAssessment.objects.for_tenant(tenant_id)
            .filter(study_id=study_id, study__tenant_id=tenant_id, limitation__tenant_id=tenant_id)
            .select_related("limitation")
            .order_by("limitation__claim__claim_number", "limitation__limitation_index")
        )
        feature_queryset = (
            ProductFeature.objects.for_tenant(tenant_id)
            .filter(
                product_version_id=study.product_version_id,
                product_version__tenant_id=tenant_id,
                product_version__product__tenant_id=tenant_id,
            )
            .exclude(review_status=ProductFeature.ReviewStatus.REJECTED)
            .order_by("created_at", "pk")
        )
        feature_scope_count = feature_queryset.count()
        features = list(feature_queryset[:FTO_MATCH_MAX_FEATURES])
        feature_cap_applied = feature_scope_count > FTO_MATCH_MAX_FEATURES
        candidates_to_create: list[FTOCandidateMatch] = []
        for assessment in assessments:
            ranked = rank_product_features_for_limitation(
                assessment.limitation.limitation_text,
                features,
                max_results=10,
            )
            for result in ranked:
                feature = result["feature"]
                candidates_to_create.append(FTOCandidateMatch(
                    tenant_id=tenant_id,
                    study_id=study_id,
                    assessment_id=assessment.pk,
                    feature_id=feature.pk,
                    algorithm_version=FTO_MATCH_ALGORITHM_VERSION,
                    feature_title_snapshot=feature.title,
                    feature_review_status_snapshot=feature.review_status,
                    source_quote_snapshot=feature.source_quote,
                    source_page_index_snapshot=feature.source_page_index,
                    source_start_offset_snapshot=feature.source_start_offset,
                    source_end_offset_snapshot=feature.source_end_offset,
                    score=result["score"],
                    limitation_coverage=result["limitation_coverage"],
                    matched_terms=result["matched_terms"],
                    rationale=result["rationale"],
                ))

        with transaction.atomic():
            current = FTOStudy.objects.select_for_update().filter(pk=study_id, tenant_id=tenant_id).first()
            if current is None or current.matching_task_id != task_id:
                return {"study_id": str(study_id), "status": "STALE_TASK_IGNORED"}
            FTOCandidateMatch.objects.for_tenant(tenant_id).filter(
                study_id=study_id,
                algorithm_version=FTO_MATCH_ALGORITHM_VERSION,
            ).delete()
            FTOCandidateMatch.objects.bulk_create(candidates_to_create, batch_size=500)
            current.matching_status = FTOStudy.MatchingStatus.COMPLETED
            current.matching_completed_at = timezone.now()
            current.matching_error_code = ""
            current.save(update_fields=("matching_status", "matching_completed_at", "matching_error_code", "updated_at"))

        try:
            AuditLog.objects.create(
                tenant_id=tenant_id,
                actor_user_id=study.matching_requested_by_id or study.created_by_id,
                action="FTO_CANDIDATE_MATCHES_GENERATED",
                resource_type="FTOStudy",
                resource_id=study_id,
                outcome=AuditLog.Outcome.SUCCESS,
                metadata={
                    "assessment_count": len(assessments),
                    "feature_count_examined": len(features),
                    "feature_scope_count": feature_scope_count,
                    "feature_cap": FTO_MATCH_MAX_FEATURES,
                    "feature_cap_applied": feature_cap_applied,
                    "candidate_count": len(candidates_to_create),
                    "algorithm_version": FTO_MATCH_ALGORITHM_VERSION,
                },
            )
        except Exception:
            logger.exception("Failed to audit FTO candidate matching (study_id=%s).", study_id)
        return {
            "study_id": str(study_id),
            "status": "COMPLETED",
            "candidate_count": len(candidates_to_create),
            "feature_count_examined": len(features),
            "feature_scope_count": feature_scope_count,
            "feature_cap_applied": feature_cap_applied,
        }
    except Exception as exc:
        logger.exception("FTO candidate matching failed (study_id=%s).", study_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))
        FTOStudy.objects.filter(pk=study_id, tenant_id=tenant_id, matching_task_id=task_id).update(
            matching_status=FTOStudy.MatchingStatus.FAILED,
            matching_error_code="CANDIDATE_MATCHING_FAILED",
            matching_task_id="",
            updated_at=timezone.now(),
        )
        return {"study_id": str(study_id), "status": "FAILED", "error_code": "CANDIDATE_MATCHING_FAILED"}


@shared_task
def recover_stale_fto_candidate_matches() -> dict[str, int]:
    """Re-dispatch bounded stale FTO match jobs; task IDs fence expired workers."""
    now = timezone.now()
    stale_studies = FTOStudy.objects.filter(
        Q(
            matching_status=FTOStudy.MatchingStatus.QUEUED,
            matching_queued_at__lt=now - FTO_MATCH_QUEUE_STALE_AFTER,
        )
        | Q(
            matching_status=FTOStudy.MatchingStatus.RUNNING,
            matching_started_at__lt=now - FTO_MATCH_RUNNING_STALE_AFTER,
        )
    ).order_by("matching_queued_at", "matching_started_at").values_list(
        "pk", "tenant_id", "matching_requested_by_id"
    )
    dispatched = 0
    examined = 0
    for study_id, tenant_id, requester_id in stale_studies[:FTO_MATCH_RECOVERY_BATCH]:
        examined += 1
        if enqueue_fto_candidate_match_generation(str(study_id), str(tenant_id), force=True, requested_by_id=str(requester_id) if requester_id else None):
            dispatched += 1
    return {"examined": examined, "dispatched": dispatched}
