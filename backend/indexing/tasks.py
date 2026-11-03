"""Celery tasks for PatentShield text and schematic indexing."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any

from celery import Task, shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from audit.models import AuditLog
from indexing.models import TextChunk
from indexing.pdf_extraction import (
    PDFExtractionError,
    PDFResourceLimitError,
    PermanentDocumentError,
    extract_pdf_pages,
    read_pdf_storage,
    validate_pdf_structure,
    verify_pdf_digest,
)
from indexing.language import detect_language_code
from indexing.services import BGEEmbedder, HybridChunker
from patents.models import Claim, Patent, PatentDocumentVersion, Schematic

logger = logging.getLogger(__name__)

_CLAIMS_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:what\s+is\s+claimed\s+is|"
    r"claims(?:[ \t]*\(?\d+\)?)?|we\s+claim|i\s+claim)"
    r"[ \t:]*$"
)

RectTuple = tuple[float, float, float, float]


@dataclass(frozen=True)
class _PageSpan:
    """Character span for one page in the joined PDF text."""

    start: int
    end: int
    page_index: int


@dataclass(frozen=True)
class _ChunkSource:
    """Source metadata used to create TextChunk rows."""

    section_type: str
    text: str
    claim_number: str | None = None
    source_char_offset: int = 0
    source_page_index: int | None = None
    page_spans: tuple[_PageSpan, ...] = ()


@dataclass(frozen=True)
class _FigureCandidate:
    """PDF region that may contain a patent drawing or schematic."""

    rect: RectTuple
    source: str


def _record_task_failure(
    document_version: PatentDocumentVersion,
    task: Task,
    error: Exception,
    action: str,
) -> None:
    """Attempt to persist a tenant-scoped failure event without masking error."""
    try:
        AuditLog.objects.create(
            tenant_id=document_version.tenant_id,
            actor_user_id=document_version.patent.created_by_id,
            action=action,
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
        logger.exception(
            "Could not persist %s failure audit for document version %s.",
            action,
            document_version.pk,
        )


def _extract_pdf_pages(pdf_bytes: bytes) -> list[str]:
    """Compatibility wrapper over the shared OCR-aware bounded extractor."""
    return extract_pdf_pages(pdf_bytes).pages


def _join_pages(pages: list[str]) -> tuple[str, tuple[_PageSpan, ...]]:
    """Join page text and retain each page's offsets in the joined string."""
    parts: list[str] = []
    spans: list[_PageSpan] = []
    cursor = 0

    for page_index, page_text in enumerate(pages):
        page_text = page_text or ""
        start = cursor
        parts.append(page_text)
        cursor += len(page_text)
        spans.append(_PageSpan(start=start, end=cursor, page_index=page_index))
        if page_index < len(pages) - 1:
            parts.append("\n\n")
            cursor += 2

    return "".join(parts), tuple(spans)


def _specification_source(
    raw_text: str,
    page_spans: tuple[_PageSpan, ...],
) -> _ChunkSource | None:
    """Return a best-effort description/specification section before claims."""
    match = _CLAIMS_HEADING_RE.search(raw_text)
    section_end = match.start() if match else len(raw_text)
    prefix = raw_text[:section_end]
    leading_trim = len(prefix) - len(prefix.lstrip())
    section_text = prefix.strip()
    if not section_text:
        return None

    return _ChunkSource(
        section_type=TextChunk.SectionType.SPECIFICATION,
        text=section_text,
        source_char_offset=leading_trim,
        page_spans=page_spans,
    )


def _page_range_for_span(
    char_start: int,
    char_end: int,
    page_spans: tuple[_PageSpan, ...],
) -> tuple[int | None, int | None]:
    """Map a global character range to the first and last overlapping PDF page."""
    matching_pages = [
        span.page_index
        for span in page_spans
        if span.end > char_start and span.start < char_end
    ]
    if not matching_pages:
        return None, None
    return matching_pages[0], matching_pages[-1]


def _iter_text_chunk_records(
    document_version: PatentDocumentVersion,
    raw_text: str,
    page_spans: tuple[_PageSpan, ...],
):
    """Yield chunk rows lazily without materializing every claim in memory."""
    patent = document_version.patent

    def emit_source(source: _ChunkSource):
        for chunk in HybridChunker.iter_chunks(source.text):
            char_start = chunk["char_start"] + source.source_char_offset
            char_end = chunk["char_end"] + source.source_char_offset
            page_start: int | None = source.source_page_index
            page_end: int | None = source.source_page_index
            if source.page_spans:
                page_start, page_end = _page_range_for_span(
                    char_start, char_end, source.page_spans
                )
            yield TextChunk(
                tenant_id=document_version.tenant_id,
                patent_id=patent.pk,
                document_version_id=document_version.pk,
                section_type=source.section_type,
                claim_number=source.claim_number,
                text=chunk["text"],
                language_code=detect_language_code(chunk["text"]),
                page_start=page_start,
                page_end=page_end,
                char_start=char_start,
                char_end=char_end,
            )

    if patent.abstract and patent.abstract.strip():
        yield from emit_source(
            _ChunkSource(
                section_type=TextChunk.SectionType.ABSTRACT,
                text=patent.abstract.strip()[:5_000_000],
            )
        )

    specification = _specification_source(raw_text, page_spans)
    if specification is not None:
        yield from emit_source(specification)

    claims = (
        Claim.objects.for_tenant(document_version.tenant_id)
        .filter(
            patent_id=patent.pk,
            document_version_id=document_version.pk,
        )
        .order_by("claim_number", "pk")
    )
    for claim in claims.iterator(chunk_size=100):
        if claim.claim_text and claim.claim_text.strip():
            yield from emit_source(
                _ChunkSource(
                    section_type=TextChunk.SectionType.CLAIM,
                    text=claim.claim_text[:5_000_000],
                    claim_number=claim.claim_number,
                    source_page_index=claim.source_page_index,
                )
            )


def _build_text_chunk_records(
    document_version: PatentDocumentVersion,
    raw_text: str,
    page_spans: tuple[_PageSpan, ...],
) -> list[TextChunk]:
    """Compatibility wrapper for tests and small internal callers."""
    return list(_iter_text_chunk_records(document_version, raw_text, page_spans))


def _retry_countdown(retries: int) -> float:
    ceiling = min(300.0, 2.0 ** min(max(retries, 0), 8))
    return random.uniform(0.5, max(0.5, ceiling))


def _set_text_stage_failure(
    document_version: PatentDocumentVersion,
    error: Exception,
    *,
    terminal: bool,
) -> None:
    PatentDocumentVersion.objects.filter(
        pk=document_version.pk, tenant_id=document_version.tenant_id
    ).update(
        text_indexing_status=(
            PatentDocumentVersion.StageStatus.FAILED
            if terminal else PatentDocumentVersion.StageStatus.PENDING
        ),
        text_indexing_error_code=type(error).__name__[:64],
        **({"text_indexing_task_id": "", "text_indexing_started_at": None} if terminal else {"text_indexing_started_at": None}),
    )
    if terminal:
        Patent.objects.filter(
            pk=document_version.patent_id, tenant_id=document_version.tenant_id
        ).update(status=Patent.Status.FAILED)


@shared_task(
    bind=True,
    queue="embedding",
    max_retries=5,
    soft_time_limit=960,
    time_limit=990,
    acks_late=True,
)
def generate_document_embeddings(
    self: Task,
    document_version_id: str,
) -> dict[str, str | int]:
    """Build bounded inactive chunk generations and atomically activate on success."""
    document_version: PatentDocumentVersion | None = None
    generation_id = uuid.uuid4()
    total_chunks = 0
    staged_batch_size = max(1, min(64, int(getattr(settings, "PATENTSHIELD_TEXT_EMBEDDING_BATCH_SIZE", 32))))

    try:
        task_id = str(self.request.id or "")
        with transaction.atomic():
            document_version = (
                PatentDocumentVersion.objects.select_for_update()
                .select_related("patent")
                .get(pk=document_version_id)
            )
            reserved_task_id = document_version.text_indexing_task_id
            if reserved_task_id and reserved_task_id != task_id:
                return {"document_version_id": str(document_version.pk), "skipped": 1, "reason": "another_text_task_is_reserved"}
            if document_version.text_indexing_status == PatentDocumentVersion.StageStatus.INDEXED:
                return {"document_version_id": str(document_version.pk), "skipped": 1, "reason": "text_stage_already_indexed"}
            if document_version.extraction_status != PatentDocumentVersion.ExtractionStatus.EXTRACTED:
                raise PDFExtractionError("Document text extraction must complete before vector indexing.")
            document_version.text_indexing_task_id = task_id
            document_version.text_indexing_status = PatentDocumentVersion.StageStatus.PROCESSING
            document_version.text_indexing_attempts = F("text_indexing_attempts") + 1
            document_version.text_indexing_error_code = ""
            document_version.text_indexing_started_at = timezone.now()
            document_version.save(update_fields=(
                "text_indexing_task_id", "text_indexing_status",
                "text_indexing_attempts", "text_indexing_error_code",
                "text_indexing_started_at",
            ))
        if document_version.patent.tenant_id != document_version.tenant_id:
            raise PDFExtractionError("Patent and document version tenant ownership do not match.")
        if document_version.extraction_status != PatentDocumentVersion.ExtractionStatus.EXTRACTED:
            raise PDFExtractionError("Document text extraction must complete before vector indexing.")

        # Staging chunks left by a hard-killed previous delivery are always
        # inactive and safe to remove before rebuilding this generation.
        TextChunk.objects.for_tenant(document_version.tenant_id).filter(
            document_version_id=document_version.pk, is_active=False,
        ).delete()

        pdf_bytes = read_pdf_storage(document_version.storage_key)
        verify_pdf_digest(pdf_bytes, document_version.content_sha256)
        extracted = extract_pdf_pages(pdf_bytes)
        raw_text, page_spans = _join_pages(extracted.pages)
        if not raw_text.strip():
            raise PDFExtractionError("No searchable text is available for vector indexing.")

        model_name, model_revision = BGEEmbedder.model_identity()
        batch: list[TextChunk] = []

        def flush_batch() -> None:
            nonlocal total_chunks, batch
            if not batch:
                return
            embeddings = BGEEmbedder.embed_texts([record.text for record in batch])
            if len(embeddings) != len(batch):
                raise RuntimeError("Embedding count does not match the chunk batch.")
            for record, embedding in zip(batch, embeddings, strict=True):
                if len(embedding) != BGEEmbedder.DIMENSIONS:
                    raise PermanentDocumentError("Embedding dimension mismatch; refusing to store vectors.")
                record.embedding = embedding
                record.embedding_model = model_name
                record.embedding_revision = model_revision
                record.index_generation = generation_id
                record.is_active = False
            # Persist each small batch as inactive staging rows. The previous
            # generation remains queryable until the final activation transaction.
            TextChunk.objects.bulk_create(batch, batch_size=staged_batch_size)
            total_chunks += len(batch)
            logger.info(
                "Staged %s chunks for document version %s (generation=%s).",
                total_chunks, document_version.pk, generation_id,
            )
            batch = []

        for record in _iter_text_chunk_records(document_version, raw_text, page_spans):
            batch.append(record)
            if len(batch) >= staged_batch_size:
                flush_batch()
        flush_batch()
        if total_chunks < 1:
            raise PDFExtractionError("Zero searchable text chunks were extracted; document cannot be indexed.")

        with transaction.atomic():
            locked_version = PatentDocumentVersion.objects.select_for_update().get(
                pk=document_version.pk, tenant_id=document_version.tenant_id
            )
            TextChunk.objects.for_tenant(document_version.tenant_id).filter(
                document_version_id=document_version.pk,
                patent_id=document_version.patent_id,
            ).exclude(index_generation=generation_id).update(is_active=False)
            TextChunk.objects.for_tenant(document_version.tenant_id).filter(
                index_generation=generation_id,
                document_version_id=document_version.pk,
            ).update(is_active=True)
            PatentDocumentVersion.objects.filter(pk=locked_version.pk).update(
                text_indexing_status=PatentDocumentVersion.StageStatus.INDEXED,
                text_indexing_error_code="",
                text_indexing_task_id="",
                text_indexing_started_at=None,
            )
            Patent.objects.filter(
                pk=document_version.patent_id, tenant_id=document_version.tenant_id
            ).update(status=Patent.Status.INDEXED)

        logger.info(
            "Activated %s text chunks for document version %s (generation=%s).",
            total_chunks, document_version.pk, generation_id,
        )
        return {
            "document_version_id": str(document_version.pk),
            "chunks_created": total_chunks,
            "embedding_dimensions": BGEEmbedder.DIMENSIONS,
            "index_generation": str(generation_id),
        }

    except Exception as exc:
        logger.exception(
            "Embedding generation failed for document version %s (task=%s, retry=%s).",
            document_version_id, self.request.id, self.request.retries,
        )
        if document_version is not None:
            # An incomplete generation is never visible to retrieval. Remove its
            # staged rows so retries do not grow the tenant's vector table.
            try:
                TextChunk.objects.for_tenant(document_version.tenant_id).filter(
                    index_generation=generation_id,
                    document_version_id=document_version.pk,
                ).delete()
            except Exception:
                logger.exception("Could not clean up inactive chunk generation %s.", generation_id)
            terminal = isinstance(exc, PermanentDocumentError) or self.request.retries >= self.max_retries
            _set_text_stage_failure(document_version, exc, terminal=terminal)
            _record_task_failure(
                document_version, self, exc, action="GENERATE_DOCUMENT_EMBEDDINGS"
            )
        if isinstance(exc, PermanentDocumentError) or self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))


def _clamp_rect(rect: RectTuple, page_rect: Any) -> RectTuple | None:
    """Clamp a rectangle to page bounds and reject an empty result."""
    x0 = max(float(page_rect.x0), rect[0])
    y0 = max(float(page_rect.y0), rect[1])
    x1 = min(float(page_rect.x1), rect[2])
    y1 = min(float(page_rect.y1), rect[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _rect_from_object(rect: Any) -> RectTuple:
    """Convert a PyMuPDF rectangle to a primitive tuple."""
    return (
        float(rect.x0),
        float(rect.y0),
        float(rect.x1),
        float(rect.y1),
    )


def _rect_area(rect: RectTuple) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _near_rect(first: RectTuple, second: RectTuple, gap: float) -> bool:
    """Return whether two rectangles overlap or are within a point gap."""
    return not (
        first[2] + gap < second[0]
        or second[2] + gap < first[0]
        or first[3] + gap < second[1]
        or second[3] + gap < first[1]
    )


def _union_rect(first: RectTuple, second: RectTuple) -> RectTuple:
    return (
        min(first[0], second[0]),
        min(first[1], second[1]),
        max(first[2], second[2]),
        max(first[3], second[3]),
    )


def _cluster_drawing_rects(
    rectangles: list[RectTuple],
    page_area: float,
) -> list[RectTuple]:
    """Merge nearby vector paths into likely figure regions."""
    clusters: list[RectTuple] = []
    for rect in sorted(rectangles, key=lambda item: (item[1], item[0])):
        current = rect
        index = 0
        while index < len(clusters):
            if _near_rect(current, clusters[index], gap=14.0):
                current = _union_rect(current, clusters.pop(index))
                index = 0
            else:
                index += 1
        clusters.append(current)

    valid: list[RectTuple] = []
    for rect in clusters:
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        area = _rect_area(rect)
        if width < 15.0 or height < 15.0 or area < 250.0:
            continue
        if page_area > 0 and area / page_area >= 0.90:
            continue
        valid.append(rect)
    return valid


def _overlap_ratio(first: RectTuple, second: RectTuple) -> float:
    """Return intersection-over-smaller-area for duplicate-region detection."""
    x0 = max(first[0], second[0])
    y0 = max(first[1], second[1])
    x1 = min(first[2], second[2])
    y1 = min(first[3], second[3])
    intersection = _rect_area((x0, y0, x1, y1)) if x1 > x0 and y1 > y0 else 0.0
    smaller_area = min(_rect_area(first), _rect_area(second))
    return intersection / smaller_area if smaller_area else 0.0


def _figure_candidates(page: Any, fitz: Any) -> list[_FigureCandidate]:
    """Find embedded-image placements and clusters of vector drawing paths."""
    page_rect = page.rect
    page_area = float(page_rect.width) * float(page_rect.height)
    candidates: list[_FigureCandidate] = []
    seen_xrefs: set[int] = set()

    for image_info in page.get_images(full=True):
        xref = int(image_info[0])
        if xref <= 0 or xref in seen_xrefs:
            continue
        seen_xrefs.add(xref)
        try:
            placements = page.get_image_rects(xref)
        except Exception:
            logger.debug("Unable to resolve image placements for xref %s.", xref)
            continue

        for placement in placements:
            rect = _clamp_rect(_rect_from_object(placement), page_rect)
            if rect is None:
                continue
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            area = _rect_area(rect)
            if width < 10.0 or height < 10.0 or area < 150.0:
                continue
            # Avoid storing a full-page scan as if it were an individual figure.
            if page_area > 0 and area / page_area >= 0.97:
                continue
            candidates.append(_FigureCandidate(rect=rect, source="image"))

    drawing_rectangles: list[RectTuple] = []
    for drawing in page.get_drawings():
        drawing_rect = drawing.get("rect")
        if drawing_rect is None:
            continue
        rect = _clamp_rect(_rect_from_object(drawing_rect), page_rect)
        if rect is None:
            continue
        # Expand zero-width/height line bounds slightly so they can join a
        # neighboring path when clustered into a larger vector figure.
        x0, y0, x1, y1 = rect
        if x1 - x0 < 1.0:
            x0 -= 0.5
            x1 += 0.5
        if y1 - y0 < 1.0:
            y0 -= 0.5
            y1 += 0.5
        rect = _clamp_rect((x0, y0, x1, y1), page_rect)
        if rect is not None:
            drawing_rectangles.append(rect)

    candidates.extend(
        _FigureCandidate(rect=rect, source="drawing")
        for rect in _cluster_drawing_rects(drawing_rectangles, page_area)
    )

    # Prefer embedded-image crops when a vector region duplicates the same
    # location; this avoids double-indexing a figure represented in both forms.
    candidates.sort(
        key=lambda candidate: (
            0 if candidate.source == "image" else 1,
            candidate.rect[1],
            candidate.rect[0],
        )
    )
    deduplicated: list[_FigureCandidate] = []
    for candidate in candidates:
        if any(
            _overlap_ratio(candidate.rect, existing.rect) >= 0.80
            for existing in deduplicated
        ):
            continue
        deduplicated.append(candidate)

    return sorted(
        deduplicated,
        key=lambda candidate: (candidate.rect[1], candidate.rect[0]),
    )


def _normalized_bbox(rect: RectTuple, page_rect: Any) -> dict[str, float]:
    """Convert a PDF-coordinate rectangle to normalized 0..1 coordinates."""
    page_width = float(page_rect.width)
    page_height = float(page_rect.height)
    if page_width <= 0 or page_height <= 0:
        raise ValueError("PDF page has invalid dimensions.")

    x = min(1.0, max(0.0, (rect[0] - float(page_rect.x0)) / page_width))
    y = min(1.0, max(0.0, (rect[1] - float(page_rect.y0)) / page_height))
    right = min(1.0, max(x, (rect[2] - float(page_rect.x0)) / page_width))
    bottom = min(1.0, max(y, (rect[3] - float(page_rect.y0)) / page_height))
    return {
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(max(0.0, right - x), 6),
        "height": round(max(0.0, bottom - y), 6),
    }


def _store_figure_png(
    page: Any,
    fitz: Any,
    rect: RectTuple,
    *,
    tenant_id: uuid.UUID,
    patent_id: uuid.UUID,
    document_version_id: uuid.UUID,
    figure_number: str,
    created_storage_keys: set[str],
    old_storage_keys: set[str],
) -> str:
    """Render one bounded crop, enforcing pixel and output-byte limits."""
    clip = fitz.Rect(*rect)
    width_points = max(1.0, float(clip.width))
    height_points = max(1.0, float(clip.height))
    max_pixels = int(getattr(settings, "PATENTSHIELD_PDF_SCHEMATIC_MAX_PIXELS", 12_000_000))
    scale = min(2.0, (max_pixels / (width_points * height_points)) ** 0.5 * 0.95)
    if not math.isfinite(scale) or scale <= 0:
        raise PDFResourceLimitError("Schematic geometry cannot be rendered within the pixel budget.")
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        clip=clip,
        alpha=False,
        annots=False,
    )
    if pixmap.width * pixmap.height > max_pixels:
        raise PDFResourceLimitError("Schematic render exceeded the configured pixel limit.")
    png_bytes = pixmap.tobytes("png")
    if not png_bytes:
        raise PDFExtractionError(f"PyMuPDF produced an empty PNG for {figure_number}.")
    max_bytes = int(getattr(settings, "PATENTSHIELD_PDF_SCHEMATIC_MAX_BYTES", 20 * 1024 * 1024))
    if len(png_bytes) > max_bytes:
        raise PDFResourceLimitError("Schematic PNG exceeded the configured output-byte limit.")

    digest = hashlib.sha256(png_bytes).hexdigest()[:16]
    requested_key = (
        f"patents/{tenant_id}/{patent_id}/versions/{document_version_id}/"
        f"schematics/page-{page.number + 1:04d}/"
        f"{figure_number.lower()}-{digest}.png"
    )
    if default_storage.exists(requested_key):
        if requested_key not in old_storage_keys:
            created_storage_keys.add(requested_key)
        return requested_key

    stored_key = default_storage.save(
        requested_key,
        ContentFile(png_bytes, name=f"{figure_number.lower()}.png"),
    )
    if stored_key not in old_storage_keys:
        created_storage_keys.add(stored_key)
    return stored_key


def _set_schematic_stage_failure(
    document_version: PatentDocumentVersion,
    error: Exception,
    *,
    terminal: bool,
) -> None:
    PatentDocumentVersion.objects.filter(
        pk=document_version.pk, tenant_id=document_version.tenant_id
    ).update(
        schematic_indexing_status=(
            PatentDocumentVersion.StageStatus.FAILED
            if terminal else PatentDocumentVersion.StageStatus.PENDING
        ),
        schematic_indexing_error_code=type(error).__name__[:64],
        **({"schematic_indexing_task_id": "", "schematic_indexing_started_at": None} if terminal else {"schematic_indexing_started_at": None}),
    )


@shared_task(
    bind=True,
    queue="ingestion",
    max_retries=5,
    soft_time_limit=900,
    time_limit=930,
    acks_late=True,
)
def extract_and_index_schematics(
    self: Task,
    document_version_id: str,
) -> dict[str, str | int]:
    """Index at most the configured schematic count under bounded PDF limits."""
    document_version: PatentDocumentVersion | None = None
    created_storage_keys: set[str] = set()
    old_storage_keys: set[str] = set()
    stage_started = time.monotonic()

    try:
        import fitz

        task_id = str(self.request.id or "")
        with transaction.atomic():
            document_version = (
                PatentDocumentVersion.objects.select_for_update()
                .select_related("patent")
                .get(pk=document_version_id)
            )
            reserved_task_id = document_version.schematic_indexing_task_id
            if reserved_task_id and reserved_task_id != task_id:
                return {"document_version_id": str(document_version.pk), "skipped": 1, "reason": "another_schematic_task_is_reserved"}
            if document_version.schematic_indexing_status == PatentDocumentVersion.StageStatus.INDEXED:
                return {"document_version_id": str(document_version.pk), "skipped": 1, "reason": "schematic_stage_already_indexed"}
            if document_version.extraction_status != PatentDocumentVersion.ExtractionStatus.EXTRACTED:
                raise PDFExtractionError("Document extraction must complete before schematic indexing.")
            document_version.schematic_indexing_task_id = task_id
            document_version.schematic_indexing_status = PatentDocumentVersion.StageStatus.PROCESSING
            document_version.schematic_indexing_attempts = F("schematic_indexing_attempts") + 1
            document_version.schematic_indexing_error_code = ""
            document_version.schematic_indexing_started_at = timezone.now()
            document_version.save(update_fields=(
                "schematic_indexing_task_id", "schematic_indexing_status",
                "schematic_indexing_attempts", "schematic_indexing_error_code",
                "schematic_indexing_started_at",
            ))
        if document_version.patent.tenant_id != document_version.tenant_id:
            raise PDFExtractionError("Patent and document version tenant ownership do not match.")
        if document_version.extraction_status != PatentDocumentVersion.ExtractionStatus.EXTRACTED:
            raise PDFExtractionError("Document extraction must complete before schematic indexing.")

        pdf_bytes = read_pdf_storage(document_version.storage_key)
        verify_pdf_digest(pdf_bytes, document_version.content_sha256)
        validate_pdf_structure(pdf_bytes)

        existing_rows = list(
            Schematic.objects.for_tenant(document_version.tenant_id)
            .filter(document_version_id=document_version.pk)
            .values_list("image_storage_key", flat=True)
        )
        old_storage_keys = set(existing_rows)
        new_records: list[Schematic] = []
        max_schematics = int(getattr(settings, "PATENTSHIELD_PDF_MAX_SCHEMATICS", 200))
        total_timeout = int(getattr(settings, "PATENTSHIELD_PDF_SCHEMATIC_TIMEOUT_SECONDS", 600))
        visual_index_version = f"pymupdf-{getattr(fitz, 'VersionBind', 'unknown')}"

        with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf_document:
            if pdf_document.needs_pass:
                raise PDFExtractionError("Password-protected PDFs are not supported.")
            if pdf_document.page_count > int(getattr(settings, "PATENTSHIELD_PDF_MAX_PAGES", 2_000)):
                raise PDFResourceLimitError("PDF page count exceeds the schematic worker limit.")
            for page_index in range(pdf_document.page_count):
                if time.monotonic() - stage_started > total_timeout:
                    raise PDFResourceLimitError("Schematic extraction exceeded its total time budget.")
                page = pdf_document.load_page(page_index)
                page_rect = page.rect
                candidates = _figure_candidates(page, fitz)
                for candidate_index, candidate in enumerate(candidates, start=1):
                    if len(new_records) >= max_schematics:
                        break
                    if time.monotonic() - stage_started > total_timeout:
                        raise PDFResourceLimitError("Schematic extraction exceeded its total time budget.")
                    figure_number = f"P{page_index + 1}-F{candidate_index}"
                    rect = _clamp_rect(candidate.rect, page_rect)
                    if rect is None:
                        continue
                    stored_key = _store_figure_png(
                        page,
                        fitz,
                        rect,
                        tenant_id=document_version.tenant_id,
                        patent_id=document_version.patent_id,
                        document_version_id=document_version.pk,
                        figure_number=figure_number,
                        created_storage_keys=created_storage_keys,
                        old_storage_keys=old_storage_keys,
                    )
                    new_records.append(
                        Schematic(
                            tenant_id=document_version.tenant_id,
                            patent_id=document_version.patent_id,
                            document_version_id=document_version.pk,
                            figure_number=figure_number,
                            page_index=page_index,
                            image_storage_key=stored_key,
                            bbox=_normalized_bbox(rect, page_rect),
                            caption=None,
                            visual_index_version=visual_index_version,
                        )
                    )
                if len(new_records) >= max_schematics:
                    logger.warning(
                        "Schematic cap reached for document version %s (limit=%s).",
                        document_version.pk, max_schematics,
                    )
                    break

        # A zero-figure patent is a successful schematic stage; text indexing
        # determines whether the document itself is searchable/indexed.
        with transaction.atomic():
            Schematic.objects.for_tenant(document_version.tenant_id).filter(
                document_version_id=document_version.pk
            ).delete()
            Schematic.objects.bulk_create(new_records, batch_size=32)
            PatentDocumentVersion.objects.filter(
                pk=document_version.pk, tenant_id=document_version.tenant_id
            ).update(
                schematic_indexing_status=PatentDocumentVersion.StageStatus.INDEXED,
                schematic_indexing_error_code="",
                schematic_indexing_task_id="",
                schematic_indexing_started_at=None,
            )

        new_storage_keys = {record.image_storage_key for record in new_records}
        for old_key in old_storage_keys - new_storage_keys:
            try:
                if not Schematic.objects.filter(image_storage_key=old_key).exists():
                    default_storage.delete(old_key)
            except Exception:
                logger.exception("Could not clean up obsolete schematic file %s.", old_key)

        logger.info(
            "Indexed %d schematic region(s) for document version %s.",
            len(new_records), document_version.pk,
        )
        return {
            "document_version_id": str(document_version.pk),
            "schematics_created": len(new_records),
            "schematic_indexing_status": PatentDocumentVersion.StageStatus.INDEXED,
        }

    except Exception as exc:
        logger.exception(
            "Schematic extraction failed for version %s (task=%s, retry=%s).",
            document_version_id, self.request.id, self.request.retries,
        )
        for storage_key in created_storage_keys - old_storage_keys:
            try:
                if not Schematic.objects.filter(image_storage_key=storage_key).exists():
                    default_storage.delete(storage_key)
            except Exception:
                logger.exception("Could not clean up failed schematic file %s.", storage_key)

        if document_version is not None:
            terminal = isinstance(exc, PermanentDocumentError) or self.request.retries >= self.max_retries
            _set_schematic_stage_failure(document_version, exc, terminal=terminal)
            _record_task_failure(
                document_version, self, exc, action="EXTRACT_AND_INDEX_SCHEMATICS"
            )
        if isinstance(exc, PermanentDocumentError) or self.request.retries >= self.max_retries:
            raise
        raise self.retry(exc=exc, countdown=_retry_countdown(self.request.retries))
