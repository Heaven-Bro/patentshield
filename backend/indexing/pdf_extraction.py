"""Unified bounded PDF extraction with page-scoped Tesseract OCR fallback.

All storage and processing paths use this module so byte/page/text/render/time
limits cannot silently diverge between extraction, text indexing, and schematics.
"""

from __future__ import annotations

import hashlib
import logging
import math
import hmac
import shutil
import subprocess
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)


class PermanentDocumentError(ValueError):
    """A document-specific condition that retrying cannot repair."""


class PDFResourceLimitError(PermanentDocumentError):
    """The PDF exceeded a configured resource safety bound."""


class PDFDigestMismatchError(PermanentDocumentError):
    """Stored document bytes do not match the immutable uploaded digest."""


class PDFExtractionError(PermanentDocumentError):
    """The PDF is corrupt, encrypted, unsupported, or has no readable text."""


class PDFOCRRequiredError(PermanentDocumentError):
    """The PDF needs OCR, but Tesseract or required OCR support is unavailable."""


@dataclass(frozen=True)
class PDFExtractionResult:
    pages: list[str]
    page_count: int
    ocr_pages: int
    text_characters: int
    sha256: str


def _limit(name: str, default: int) -> int:
    value = int(getattr(settings, name, default))
    if value <= 0:
        raise PDFResourceLimitError(f"Invalid non-positive PDF processing limit: {name}.")
    return value


def read_pdf_storage(storage_key: str) -> bytes:
    """Read one stored PDF using a bounded incremental read, never unbounded read()."""
    max_bytes = _limit("PATENTSHIELD_PDF_MAX_BYTES", 100 * 1024 * 1024)
    try:
        size = default_storage.size(storage_key)
    except (AttributeError, NotImplementedError, OSError):
        size = None
    if size is not None and size > max_bytes:
        raise PDFResourceLimitError(
            f"Stored PDF is {size} bytes; the limit is {max_bytes} bytes."
        )

    chunks: list[bytes] = []
    total = 0
    try:
        with default_storage.open(storage_key, "rb") as file_obj:
            while True:
                piece = file_obj.read(min(1024 * 1024, max_bytes - total + 1))
                if not piece:
                    break
                total += len(piece)
                if total > max_bytes:
                    raise PDFResourceLimitError(
                        f"Stored PDF exceeds the {max_bytes}-byte processing limit."
                    )
                chunks.append(piece)
    except FileNotFoundError as exc:
        raise PDFExtractionError("The stored PDF object is missing.") from exc
    if not total:
        raise PDFExtractionError("The stored patent document is empty.")
    return b"".join(chunks)


def verify_pdf_digest(pdf_bytes: bytes, expected_sha256: str) -> str:
    """Verify immutable content identity before parsing or indexing."""
    actual = hashlib.sha256(pdf_bytes).hexdigest()
    expected = str(expected_sha256 or "").strip().lower()
    if not expected:
        raise PDFDigestMismatchError("Stored document digest metadata is missing.")
    if not hmac.compare_digest(actual, expected):
        raise PDFDigestMismatchError("Stored document SHA-256 does not match its metadata.")
    return actual


def _ocr_page(page, fitz, *, language: str, page_timeout: int, max_pixels: int) -> str:
    """Render and OCR one page with a strict pixel and subprocess timeout bound."""
    executable = shutil.which(str(getattr(settings, "PATENTSHIELD_TESSERACT_BINARY", "tesseract")))
    if not executable:
        raise PDFOCRRequiredError(
            "This PDF page requires OCR, but the Tesseract executable is unavailable."
        )

    rect = page.rect
    area_points = max(1.0, float(rect.width) * float(rect.height))
    # Leave headroom for integer pixel rounding and unusual page coordinate boxes.
    scale = min(1.5, math.sqrt(max_pixels / area_points) * 0.95)
    if not math.isfinite(scale) or scale <= 0:
        raise PDFResourceLimitError("OCR page geometry cannot be rendered within the pixel budget.")
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale),
        alpha=False,
        colorspace=fitz.csRGB,
        annots=False,
    )
    if pixmap.width * pixmap.height > max_pixels:
        raise PDFResourceLimitError("Rendered OCR page exceeds the pixel limit.")

    try:
        completed = subprocess.run(
            [executable, "stdin", "stdout", "-l", language, "--dpi", "180"],
            input=pixmap.tobytes("png"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=page_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PDFResourceLimitError("Tesseract exceeded the per-page OCR timeout.") from exc
    except OSError as exc:
        raise PDFOCRRequiredError("Tesseract could not be started for OCR.") from exc

    if completed.returncode != 0:
        logger.warning("Tesseract OCR failed for page %s (exit=%s).", page.number + 1, completed.returncode)
        raise PDFOCRRequiredError("Tesseract could not OCR one or more scanned PDF pages.")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def validate_pdf_structure(pdf_bytes: bytes) -> int:
    """Validate bytes, encryption, parser structure, and page count without OCR.

    Schematic extraction uses this lightweight validation because the text
    extraction stage already performs OCR. It still shares the same byte/page
    limits and rejects encrypted or malformed files.
    """
    max_bytes = _limit("PATENTSHIELD_PDF_MAX_BYTES", 100 * 1024 * 1024)
    max_pages = _limit("PATENTSHIELD_PDF_MAX_PAGES", 2_000)
    if len(pdf_bytes) > max_bytes:
        raise PDFResourceLimitError(f"PDF exceeds the {max_bytes}-byte processing limit.")
    if not pdf_bytes.startswith(b"%PDF-") and b"%PDF-" not in pdf_bytes[:1024]:
        raise PDFExtractionError("Stored document is not a valid PDF header.")
    try:
        import fitz
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            if document.needs_pass:
                raise PDFExtractionError("Password-protected PDF documents are not supported.")
            page_count = int(document.page_count)
            if page_count < 1:
                raise PDFExtractionError("The PDF contains no pages.")
            if page_count > max_pages:
                raise PDFResourceLimitError(
                    f"PDF has {page_count} pages; the limit is {max_pages}."
                )
            return page_count
    except (PDFResourceLimitError, PDFExtractionError):
        raise
    except Exception as exc:
        raise PDFExtractionError("The PDF is corrupt or could not be parsed safely.") from exc


def extract_pdf_pages(pdf_bytes: bytes) -> PDFExtractionResult:
    """Extract selectable text, OCR sparse pages, and enforce shared limits."""
    max_bytes = _limit("PATENTSHIELD_PDF_MAX_BYTES", 100 * 1024 * 1024)
    max_pages = _limit("PATENTSHIELD_PDF_MAX_PAGES", 2_000)
    max_chars = _limit("PATENTSHIELD_PDF_MAX_TEXT_CHARS", 25_000_000)
    min_page_chars = _limit("PATENTSHIELD_PDF_OCR_MIN_PAGE_CHARS", 40)
    max_ocr_pages = _limit("PATENTSHIELD_PDF_MAX_OCR_PAGES", 250)
    max_pixels = _limit("PATENTSHIELD_PDF_MAX_RENDER_PIXELS", 24_000_000)
    page_timeout = _limit("PATENTSHIELD_PDF_OCR_PAGE_TIMEOUT_SECONDS", 30)
    total_timeout = _limit("PATENTSHIELD_PDF_TOTAL_TIMEOUT_SECONDS", 900)

    if len(pdf_bytes) > max_bytes:
        raise PDFResourceLimitError(f"PDF exceeds the {max_bytes}-byte processing limit.")
    if not pdf_bytes.startswith(b"%PDF-") and b"%PDF-" not in pdf_bytes[:1024]:
        raise PDFExtractionError("Stored document is not a valid PDF header.")

    started = time.monotonic()
    try:
        import fitz
    except ImportError as exc:
        raise PDFExtractionError("PyMuPDF is required for bounded PDF extraction.") from exc

    pages: list[str] = []
    ocr_pages = 0
    total_chars = 0
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            if document.needs_pass:
                raise PDFExtractionError("Password-protected PDF documents are not supported.")
            page_count = int(document.page_count)
            if page_count < 1:
                raise PDFExtractionError("The PDF contains no pages.")
            if page_count > max_pages:
                raise PDFResourceLimitError(
                    f"PDF has {page_count} pages; the limit is {max_pages}."
                )

            for page_index in range(page_count):
                if time.monotonic() - started > total_timeout:
                    raise PDFResourceLimitError("PDF extraction exceeded its total time budget.")
                page = document.load_page(page_index)
                page_text = (page.get_text("text") or "").strip()
                if len(page_text) < min_page_chars:
                    if ocr_pages >= max_ocr_pages:
                        raise PDFResourceLimitError(
                            f"PDF requires OCR on more than {max_ocr_pages} pages."
                        )
                    language = str(getattr(settings, "PATENTSHIELD_PDF_OCR_LANGUAGE", "eng"))
                    page_text = _ocr_page(
                        page,
                        fitz,
                        language=language,
                        page_timeout=page_timeout,
                        max_pixels=max_pixels,
                    )
                    ocr_pages += 1
                total_chars += len(page_text) + (1 if pages else 0)
                if total_chars > max_chars:
                    raise PDFResourceLimitError(
                        f"Extracted PDF text exceeds the {max_chars}-character limit."
                    )
                pages.append(page_text)
    except (PDFResourceLimitError, PDFOCRRequiredError, PDFExtractionError):
        raise
    except Exception as exc:
        raise PDFExtractionError("The PDF is corrupt or could not be parsed safely.") from exc

    if not any(page.strip() for page in pages):
        raise PDFExtractionError("No searchable text was extracted after OCR fallback.")
    return PDFExtractionResult(
        pages=pages,
        page_count=page_count,
        ocr_pages=ocr_pages,
        text_characters=total_chars,
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )
