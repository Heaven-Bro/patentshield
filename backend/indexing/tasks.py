"""Celery tasks for PatentShield text and schematic indexing."""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from celery import Task, shared_task
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction

from audit.models import AuditLog
from indexing.models import TextChunk
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
    """Extract selectable text by page, preferring pypdf and falling back to fitz."""
    extraction_errors: list[Exception] = []
    blank_pages: list[str] | None = None

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        extraction_errors.append(exc)
    else:
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
            pages = [page.extract_text() or "" for page in reader.pages]
            if any(page.strip() for page in pages):
                return pages
            blank_pages = pages
            extraction_errors.append(
                ValueError("pypdf found no selectable text in the PDF.")
            )
        except Exception as exc:
            extraction_errors.append(exc)

    try:
        import fitz
    except ImportError as exc:
        extraction_errors.append(exc)
    else:
        try:
            with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
                pages = [page.get_text("text") or "" for page in document]
            if any(page.strip() for page in pages):
                return pages
            if pages:
                blank_pages = pages
            extraction_errors.append(
                ValueError("PyMuPDF found no selectable text in the PDF.")
            )
        except Exception as exc:
            extraction_errors.append(exc)

    # Scanned PDFs may legitimately have no text layer. Claims and metadata can
    # still be chunked, while OCR remains a separate ingestion responsibility.
    if blank_pages is not None:
        return blank_pages
    if extraction_errors and all(
        isinstance(error, ImportError) for error in extraction_errors
    ):
        raise RuntimeError(
            "PDF text extraction requires pypdf or PyMuPDF in the worker image."
        ) from extraction_errors[-1]
    raise RuntimeError("Unable to open or extract text from the patent PDF.") from (
        extraction_errors[-1] if extraction_errors else None
    )


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


def _build_text_chunk_records(
    document_version: PatentDocumentVersion,
    raw_text: str,
    page_spans: tuple[_PageSpan, ...],
) -> list[TextChunk]:
    """Chunk abstract, specification, and extracted claims for one version."""
    patent = document_version.patent
    sources: list[_ChunkSource] = []

    if patent.abstract and patent.abstract.strip():
        sources.append(
            _ChunkSource(
                section_type=TextChunk.SectionType.ABSTRACT,
                text=patent.abstract.strip(),
            )
        )

    specification = _specification_source(raw_text, page_spans)
    if specification is not None:
        sources.append(specification)

    claims = (
        Claim.objects.for_tenant(document_version.tenant_id)
        .filter(
            patent_id=patent.pk,
            document_version_id=document_version.pk,
        )
        .order_by("claim_number", "pk")
    )
    for claim in claims.iterator(chunk_size=500):
        if not claim.claim_text.strip():
            continue
        sources.append(
            _ChunkSource(
                section_type=TextChunk.SectionType.CLAIM,
                text=claim.claim_text,
                claim_number=claim.claim_number,
                source_page_index=claim.source_page_index,
            )
        )

    records: list[TextChunk] = []
    for source in sources:
        for chunk in HybridChunker.chunk_text(source.text):
            char_start = chunk["char_start"] + source.source_char_offset
            char_end = chunk["char_end"] + source.source_char_offset

            page_start: int | None = source.source_page_index
            page_end: int | None = source.source_page_index
            if source.page_spans:
                page_start, page_end = _page_range_for_span(
                    char_start,
                    char_end,
                    source.page_spans,
                )

            records.append(
                TextChunk(
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
            )
    return records


@shared_task(bind=True, queue="embedding", max_retries=3)
def generate_document_embeddings(
    self: Task,
    document_version_id: str,
) -> dict[str, str | int]:
    """Create atomic tenant-scoped text chunks with BGE-M3 embeddings."""
    document_version: PatentDocumentVersion | None = None

    try:
        document_version = (
            PatentDocumentVersion.objects.select_related("patent")
            .get(pk=document_version_id)
        )
        if document_version.patent.tenant_id != document_version.tenant_id:
            raise ValueError(
                "Patent and document version must belong to the same tenant."
            )

        with default_storage.open(document_version.storage_key, "rb") as file_obj:
            pdf_bytes = file_obj.read()
        if not pdf_bytes:
            raise ValueError("The stored patent document is empty.")

        calculated_digest = hashlib.sha256(pdf_bytes).hexdigest()
        expected_digest = document_version.content_sha256.strip().lower()
        if not hmac.compare_digest(calculated_digest, expected_digest):
            raise ValueError(
                "Stored document SHA-256 does not match its metadata."
            )

        pages = _extract_pdf_pages(pdf_bytes)
        raw_text, page_spans = _join_pages(pages)
        records = _build_text_chunk_records(
            document_version,
            raw_text,
            page_spans,
        )
        embeddings = BGEEmbedder.embed_texts([record.text for record in records])
        if len(embeddings) != len(records):
            raise RuntimeError("Embedding count does not match the chunk count.")

        for record, embedding in zip(records, embeddings, strict=True):
            if len(embedding) != 1024:
                raise RuntimeError(
                    "An embedding has an invalid dimension; expected 1024."
                )
            record.embedding = embedding

        # Replacing all rows for this version is atomic, so a retry cannot leave
        # a mixture of old and newly generated chunk sets.
        with transaction.atomic():
            TextChunk.objects.for_tenant(document_version.tenant_id).filter(
                document_version_id=document_version.pk,
                patent_id=document_version.patent_id,
            ).delete()
            TextChunk.objects.bulk_create(records, batch_size=256)
            Patent.objects.filter(
                pk=document_version.patent_id,
                tenant_id=document_version.tenant_id,
            ).update(status=Patent.Status.INDEXED)

        logger.info(
            "Indexed document version %s with %d text chunks.",
            document_version.pk,
            len(records),
        )
        return {
            "document_version_id": str(document_version.pk),
            "chunks_created": len(records),
            "embedding_dimensions": 1024,
        }

    except Exception as exc:
        logger.exception(
            "Embedding generation failed for document version %s.",
            document_version_id,
        )
        if document_version is not None:
            Patent.objects.filter(
                pk=document_version.patent_id,
                tenant_id=document_version.tenant_id,
            ).update(status=Patent.Status.FAILED)
            _record_task_failure(
                document_version,
                self,
                exc,
                action="GENERATE_DOCUMENT_EMBEDDINGS",
            )
        raise self.retry(exc=exc, countdown=60)


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
    """Render a clipped figure as PNG and store it in Django default storage."""
    clip = fitz.Rect(*rect)
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(2.0, 2.0),
        clip=clip,
        alpha=False,
        annots=False,
    )
    png_bytes = pixmap.tobytes("png")
    if not png_bytes:
        raise RuntimeError(f"PyMuPDF produced an empty PNG for {figure_number}.")

    digest = hashlib.sha256(png_bytes).hexdigest()[:16]
    requested_key = (
        f"patents/{tenant_id}/{patent_id}/versions/{document_version_id}/"
        f"schematics/page-{page.number + 1:04d}/"
        f"{figure_number.lower()}-{digest}.png"
    )
    if default_storage.exists(requested_key):
        return requested_key

    stored_key = default_storage.save(
        requested_key,
        ContentFile(png_bytes, name=f"{figure_number.lower()}.png"),
    )
    if stored_key not in old_storage_keys:
        created_storage_keys.add(stored_key)
    return stored_key


@shared_task(bind=True, queue="ingestion", max_retries=3)
def extract_and_index_schematics(
    self: Task,
    document_version_id: str,
) -> dict[str, str | int]:
    """Crop embedded images and vector drawing regions from a patent PDF."""
    document_version: PatentDocumentVersion | None = None
    created_storage_keys: set[str] = set()
    old_storage_keys: set[str] = set()

    try:
        import fitz

        document_version = (
            PatentDocumentVersion.objects.select_related("patent")
            .get(pk=document_version_id)
        )
        if document_version.patent.tenant_id != document_version.tenant_id:
            raise ValueError(
                "Patent and document version must belong to the same tenant."
            )

        with default_storage.open(document_version.storage_key, "rb") as file_obj:
            pdf_bytes = file_obj.read()
        if not pdf_bytes:
            raise ValueError("The stored patent document is empty.")
        actual_digest = hashlib.sha256(pdf_bytes).hexdigest()
        expected_digest = document_version.content_sha256.strip().lower()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise ValueError(
                "Stored document SHA-256 does not match its metadata."
            )

        existing_rows = list(
            Schematic.objects.for_tenant(document_version.tenant_id)
            .filter(document_version_id=document_version.pk)
            .values_list("image_storage_key", flat=True)
        )
        old_storage_keys = set(existing_rows)
        new_records: list[Schematic] = []
        visual_index_version = f"pymupdf-{getattr(fitz, 'VersionBind', 'unknown')}"

        with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf_document:
            for page_index, page in enumerate(pdf_document):
                page_rect = page.rect
                candidates = _figure_candidates(page, fitz)
                for candidate_index, candidate in enumerate(candidates, start=1):
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

        # Replacing the current version's schematic set is atomic. Cropped
        # files are written first so database rows never point to missing files.
        with transaction.atomic():
            Schematic.objects.for_tenant(document_version.tenant_id).filter(
                document_version_id=document_version.pk
            ).delete()
            Schematic.objects.bulk_create(new_records, batch_size=100)

        new_storage_keys = {record.image_storage_key for record in new_records}
        for old_key in old_storage_keys - new_storage_keys:
            try:
                if not Schematic.objects.filter(image_storage_key=old_key).exists():
                    default_storage.delete(old_key)
            except Exception:
                logger.exception(
                    "Could not clean up obsolete schematic file %s.", old_key
                )

        logger.info(
            "Indexed %d schematic region(s) for document version %s.",
            len(new_records),
            document_version.pk,
        )
        return {
            "document_version_id": str(document_version.pk),
            "schematics_created": len(new_records),
        }

    except Exception as exc:
        logger.exception(
            "Schematic extraction failed for document version %s.",
            document_version_id,
        )
        # Remove files created by this failed attempt unless they were already
        # referenced by prior rows. Cleanup failures do not mask task failure.
        for storage_key in created_storage_keys - old_storage_keys:
            try:
                if not Schematic.objects.filter(
                    image_storage_key=storage_key
                ).exists():
                    default_storage.delete(storage_key)
            except Exception:
                logger.exception(
                    "Could not clean up failed schematic file %s.", storage_key
                )

        if document_version is not None:
            _record_task_failure(
                document_version,
                self,
                exc,
                action="EXTRACT_AND_INDEX_SCHEMATICS",
            )
        raise self.retry(exc=exc, countdown=60)
