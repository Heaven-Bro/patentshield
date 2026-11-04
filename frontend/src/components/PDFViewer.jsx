import { Component, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/TextLayer.css";

import { useAuthStore } from "../stores/authStore";
import { useDocumentStore } from "../stores/documentStore";

pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

const MIN_ZOOM = 0.25;
const MAX_ZOOM = 4;

function DocumentUnavailable({
  message = "The requested patent document could not be loaded.",
  onRetry,
}) {
  return (
    <div
      className="flex min-h-64 flex-col items-center justify-center rounded-lg border border-rose-200 bg-white p-6 text-center"
      role="alert"
      data-testid="pdf-unavailable"
    >
      <span className="flex h-11 w-11 items-center justify-center rounded-full bg-rose-50 text-rose-700" aria-hidden="true">
        <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path d="M7 3.75h7l4 4v12.5H7a2 2 0 0 1-2-2v-12.5a2 2 0 0 1 2-2Z" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M14 3.75v4h4M9 12h6M9 15.5h6" strokeLinecap="round" />
        </svg>
      </span>
      <h2 className="mt-3 text-base font-semibold text-slate-900">Document Unavailable</h2>
      <p className="mt-1 max-w-md text-sm leading-6 text-slate-600">{message}</p>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-4 rounded-lg bg-indigo-700 px-4 py-2 text-sm font-semibold text-white transition hover:bg-indigo-800 focus:outline-none focus:ring-2 focus:ring-indigo-300 focus:ring-offset-2"
        >
          Retry document
        </button>
      )}
    </div>
  );
}

class PdfViewerErrorBoundary extends Component {
  state = { error: null, retryCount: 0 };

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error) {
    if (import.meta.env?.DEV) {
      console.error("PatentShield PDF viewer crashed:", error);
    }
  }

  retry = () => {
    this.setState((state) => ({ error: null, retryCount: state.retryCount + 1 }));
  };

  render() {
    if (this.state.error) {
      return (
        <DocumentUnavailable
          message="The document viewer encountered an unexpected rendering error. Try loading the document again."
          onRetry={this.retry}
        />
      );
    }
    return <div key={this.state.retryCount}>{this.props.children}</div>;
  }
}

/** @typedef {{x:number, y:number, width:number, height:number}} NormalizedBoundingBox */
/** @typedef {{left:number, top:number, width:number, height:number}} PixelRect */

/**
 * Resolve an authorized PDF source and always attach the selected tenant header
 * to API URLs. The tenant header selects a workspace; the Django endpoint still
 * enforces authentication and membership authorization.
 *
 * @param {string | File | Blob | object | null | undefined} file
 * @param {object | null} currentVersion
 * @param {string | null} tenantId
 * @returns {string | File | Blob | object | null}
 */
function resolvePdfFile(file, currentVersion, tenantId) {
  const source = file ?? currentVersion?.file_url ?? currentVersion?.pdf_url
    ?? currentVersion?.download_url ?? currentVersion?.url ?? currentVersion?.file ?? null;
  if (typeof source === "string") {
    return {
      url: source,
      withCredentials: true,
      httpHeaders: tenantId ? { "X-Tenant-ID": tenantId } : {},
    };
  }
  if (source && typeof source === "object" && typeof source.url === "string") {
    return {
      ...source,
      withCredentials: source.withCredentials ?? true,
      httpHeaders: {
        ...(source.httpHeaders || {}),
        ...(tenantId ? { "X-Tenant-ID": tenantId } : {}),
      },
    };
  }
  return source;
}

/**
 * Scale a normalized page-relative box to displayed page CSS pixels.
 * @param {NormalizedBoundingBox} bbox
 * @param {number} pageWidth
 * @param {number} pageHeight
 * @returns {PixelRect}
 */
function getPixelBounds(bbox, pageWidth, pageHeight) {
  const finite = (value) => (Number.isFinite(value) ? value : 0);
  const x = Math.min(1, Math.max(0, finite(bbox.x)));
  const y = Math.min(1, Math.max(0, finite(bbox.y)));
  const width = Math.min(1 - x, Math.max(0, finite(bbox.width)));
  const height = Math.min(1 - y, Math.max(0, finite(bbox.height)));
  return {
    left: x * pageWidth,
    top: y * pageHeight,
    width: width * pageWidth,
    height: height * pageHeight,
  };
}

/**
 * Find quote fragments in PDF.js's text layer and return their client rectangles.
 * Each matching text span is highlighted; this is a visual navigation aid, not
 * a substitute for the backend's canonical quote-verification guardrail.
 * @param {HTMLElement | null} pageElement
 * @param {string} quote
 * @returns {PixelRect[]}
 */
function findQuoteRects(pageElement, quote) {
  if (!pageElement || !quote.trim()) return [];
  const textLayer = pageElement.querySelector(
    ".react-pdf__Page__textContent, .textLayer",
  );
  if (!textLayer) return [];

  const spans = Array.from(textLayer.querySelectorAll("span"))
    .filter((span) => Boolean(span.textContent?.trim()));
  if (spans.length === 0) return [];

  const normalizedCharacters = [];
  const characterSpans = [];
  const appendText = (text, span) => {
    const normalizedText = text.normalize("NFKC").toLocaleLowerCase();
    for (let index = 0; index < normalizedText.length; index += 1) {
      const character = normalizedText[index];
      if (/\s/u.test(character)) {
        if (normalizedCharacters.length && normalizedCharacters.at(-1) !== " ") {
          normalizedCharacters.push(" ");
          characterSpans.push(span);
        }
      } else {
        normalizedCharacters.push(character);
        characterSpans.push(span);
      }
    }
  };

  for (const span of spans) {
    const text = span.textContent || "";
    if (normalizedCharacters.length && normalizedCharacters.at(-1) !== " ") {
      normalizedCharacters.push(" ");
      characterSpans.push(span);
    }
    appendText(text, span);
  }

  const searchableText = normalizedCharacters.join("");
  const normalizedQuote = quote
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/\s+/gu, " ")
    .trim();
  if (!normalizedQuote) return [];

  const matchIndex = searchableText.indexOf(normalizedQuote);
  if (matchIndex < 0) return [];
  const matchingSpans = new Set(
    characterSpans.slice(matchIndex, matchIndex + normalizedQuote.length),
  );
  const pageBounds = pageElement.getBoundingClientRect();
  return Array.from(matchingSpans)
    .map((span) => span.getBoundingClientRect())
    .filter((rect) => rect.width > 0 && rect.height > 0)
    .map((rect) => {
      const left = Math.max(0, rect.left - pageBounds.left);
      const top = Math.max(0, rect.top - pageBounds.top);
      const right = Math.min(pageBounds.width, rect.right - pageBounds.left);
      const bottom = Math.min(pageBounds.height, rect.bottom - pageBounds.top);
      return {
        left,
        top,
        width: Math.max(0, right - left),
        height: Math.max(0, bottom - top),
      };
    })
    .filter((rect) => rect.width > 0 && rect.height > 0);
}

/**
 * PDF.js viewer with bounding-box overlays and text-layer quote highlighting.
 * Page numbers in the UI are one-based; evidence page_index is zero-based.
 * @param {{file?: string | File | Blob | object | null, className?: string}} props
 */
function PDFViewerContent({ file = null, className = "" }) {
  const currentPatent = useDocumentStore((state) => state.currentPatent);
  const currentVersion = useDocumentStore((state) => state.currentVersion);
  const currentPage = useDocumentStore((state) => state.currentPage);
  const totalPages = useDocumentStore((state) => state.totalPages);
  const zoom = useDocumentStore((state) => state.zoom);
  const activeHighlight = useDocumentStore((state) => state.activeHighlight);
  const activeTenantId = useAuthStore((state) => state.activeTenantId);
  const setPage = useDocumentStore((state) => state.setPage);
  const setZoom = useDocumentStore((state) => state.setZoom);
  const setTotalPages = useDocumentStore((state) => state.setTotalPages);
  const clearHighlight = useDocumentStore((state) => state.clearHighlight);

  const [loadError, setLoadError] = useState("");
  const [retryCount, setRetryCount] = useState(0);
  const [viewportWidth, setViewportWidth] = useState(720);
  const [pageSize, setPageSize] = useState({ width: 0, height: 0 });
  const [quoteRects, setQuoteRects] = useState(/** @type {PixelRect[]} */ ([]));
  const pageInputRef = useRef(null);
  const viewportRef = useRef(null);
  const pageContainerRef = useRef(null);
  const resolvedFile = useMemo(
    () => resolvePdfFile(file, currentVersion, activeTenantId),
    [file, currentVersion, activeTenantId],
  );

  useEffect(() => {
    setLoadError("");
    setTotalPages(0);
  }, [resolvedFile, setTotalPages]);

  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return undefined;
    const measureViewport = () => {
      const width = element.getBoundingClientRect().width;
      if (width > 0) setViewportWidth(width);
    };
    measureViewport();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measureViewport);
      return () => window.removeEventListener("resize", measureViewport);
    }
    const observer = new ResizeObserver(measureViewport);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const measurePage = useCallback(() => {
    const element = pageContainerRef.current;
    if (!element) return;
    const { width, height } = element.getBoundingClientRect();
    if (width > 0 && height > 0) {
      setPageSize((previous) => (
        previous.width === width && previous.height === height
          ? previous
          : { width, height }
      ));
    }
  }, []);

  useEffect(() => {
    const element = pageContainerRef.current;
    if (!element) return undefined;
    measurePage();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measurePage);
    observer.observe(element);
    return () => observer.disconnect();
  }, [measurePage, resolvedFile, currentPage, zoom]);

  const isHighlightPage = activeHighlight?.page_index === currentPage - 1;
  const hasValidBBox = Boolean(
    isHighlightPage
    && activeHighlight?.bbox
    && Number.isFinite(activeHighlight.bbox.x)
    && Number.isFinite(activeHighlight.bbox.y)
    && Number.isFinite(activeHighlight.bbox.width)
    && Number.isFinite(activeHighlight.bbox.height)
    && pageSize.width > 0
    && pageSize.height > 0,
  );
  const pixelBounds = hasValidBBox
    ? getPixelBounds(activeHighlight.bbox, pageSize.width, pageSize.height)
    : null;

  useEffect(() => {
    let frameId = null;
    let timeoutId = null;
    let cancelled = false;
    let attempts = 0;

    if (
      !isHighlightPage
      || activeHighlight?.bbox
      || !activeHighlight?.exact_quote
    ) {
      frameId = window.requestAnimationFrame(() => {
        if (!cancelled) setQuoteRects([]);
      });
      return () => {
        cancelled = true;
        if (frameId !== null) window.cancelAnimationFrame(frameId);
      };
    }

    const search = () => {
      frameId = window.requestAnimationFrame(() => {
        if (cancelled) return;
        const rects = findQuoteRects(pageContainerRef.current, activeHighlight.exact_quote);
        if (rects.length > 0 || attempts >= 5) {
          setQuoteRects(rects);
          return;
        }
        attempts += 1;
        timeoutId = window.setTimeout(search, 80);
      });
    };
    search();
    return () => {
      cancelled = true;
      if (frameId !== null) window.cancelAnimationFrame(frameId);
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    };
  }, [activeHighlight, currentPage, isHighlightPage, pageSize.width, pageSize.height, zoom, resolvedFile]);

  const handleDocumentLoad = ({ numPages }) => {
    setLoadError("");
    setTotalPages(numPages);
  };

  const handlePageError = (error) => {
    const responseStatus = Number(error?.status ?? error?.response?.status ?? 0);
    const errorMessage = String(error?.message || error || "");
    const isNotFound = responseStatus === 404 || /\b404\b/u.test(errorMessage);
    setLoadError(
      isNotFound
        ? "This patent PDF could not be found in document storage. It may have been removed or moved."
        : "The patent PDF could not be loaded. Check your connection and document access, then try again.",
    );
    if (import.meta.env?.DEV) console.error("PatentShield PDF rendering failed:", error);
  };

  const retryDocument = () => {
    setLoadError("");
    setTotalPages(0);
    setRetryCount((count) => count + 1);
  };

  const clearHighlightIfPageChanges = (nextPage) => {
    if (activeHighlight && activeHighlight.page_index !== Math.trunc(nextPage) - 1) {
      clearHighlight();
    }
  };

  const commitPageInput = () => {
    const nextPage = Number.parseInt(pageInputRef.current?.value ?? "", 10);
    if (!Number.isFinite(nextPage)) {
      if (pageInputRef.current) pageInputRef.current.value = String(currentPage);
      return;
    }
    const normalizedPage = totalPages > 0
      ? Math.min(totalPages, Math.max(1, nextPage))
      : Math.max(1, nextPage);
    setPage(normalizedPage);
    clearHighlightIfPageChanges(normalizedPage);
  };

  const goToPreviousPage = () => {
    const nextPage = Math.max(1, currentPage - 1);
    setPage(nextPage);
    clearHighlightIfPageChanges(nextPage);
  };

  const goToNextPage = () => {
    const nextPage = totalPages > 0 ? Math.min(totalPages, currentPage + 1) : currentPage + 1;
    setPage(nextPage);
    clearHighlightIfPageChanges(nextPage);
  };

  const updateZoom = (delta) => setZoom(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom + delta)));

  return (
    <section
      className={`flex h-full min-h-0 flex-col overflow-hidden rounded-xl border border-slate-200 bg-slate-100 ${className}`}
      aria-label="Patent PDF viewer"
    >
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-3 py-2">
        <div className="min-w-0">
          <p className="truncate text-xs font-semibold text-slate-800" title={currentPatent?.title || "Patent document"}>
            {currentPatent?.title || "Patent document"}
          </p>
          <p className="text-[10px] text-slate-500">
            {currentVersion ? `Version ${currentVersion.version_number ?? "—"}` : "No version selected"}
            {currentVersion?.extraction_status ? ` · ${String(currentVersion.extraction_status).replaceAll("_", " ").toLowerCase()}` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={goToPreviousPage} disabled={currentPage <= 1} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40" aria-label="Previous page">Previous</button>
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <span className="sr-only">Page number</span>
            <input
              key={currentPage}
              ref={pageInputRef}
              type="number"
              min={1}
              max={totalPages || undefined}
              defaultValue={currentPage}
              onBlur={commitPageInput}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  commitPageInput();
                  event.currentTarget.blur();
                }
              }}
              className="w-16 rounded-md border border-slate-300 px-2 py-1.5 text-center text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-200"
            />
            <span>of {totalPages || "—"}</span>
          </label>
          <button type="button" onClick={goToNextPage} disabled={totalPages > 0 && currentPage >= totalPages} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40" aria-label="Next page">Next</button>
          <span className="mx-1 h-5 border-l border-slate-200" />
          <button type="button" onClick={() => updateZoom(-0.1)} disabled={zoom <= MIN_ZOOM} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-40" aria-label="Zoom out">−</button>
          <span className="min-w-12 text-center text-sm tabular-nums text-slate-600">{Math.round(zoom * 100)}%</span>
          <button type="button" onClick={() => updateZoom(0.1)} disabled={zoom >= MAX_ZOOM} className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-40" aria-label="Zoom in">+</button>
        </div>
      </header>

      <div ref={viewportRef} className="min-h-0 flex-1 overflow-auto p-4">
        {!resolvedFile ? (
          <div className="flex h-full min-h-64 items-center justify-center rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center">
            <div>
              <p className="font-semibold text-slate-800">No document selected</p>
              <p className="mt-1 max-w-sm text-sm text-slate-500">Select an extracted patent document version to open its authorized PDF.</p>
            </div>
          </div>
        ) : loadError ? (
          <DocumentUnavailable message={loadError} onRetry={retryDocument} />
        ) : (
          <Document
            key={retryCount}
            file={resolvedFile}
            onLoadSuccess={handleDocumentLoad}
            onLoadError={handlePageError}
            onSourceError={handlePageError}
            loading={<p className="py-12 text-center text-sm text-slate-500" role="status">Loading PDF…</p>}
            error={<DocumentUnavailable message="The patent PDF could not be loaded." onRetry={retryDocument} />}
          >
            <div className="flex min-w-max justify-center">
              <div ref={pageContainerRef} className="relative mx-auto h-fit w-fit bg-white shadow-lg" data-testid="pdf-page-container">
                <Page
                  pageNumber={currentPage}
                  width={Math.max(220, viewportWidth - 32) * zoom}
                  renderTextLayer
                  renderAnnotationLayer={false}
                  onRenderSuccess={measurePage}
                  onRenderError={handlePageError}
                  loading={<p className="p-8 text-sm text-slate-500">Rendering page…</p>}
                />
                {pixelBounds && (
                  <div className="pointer-events-none absolute inset-0 z-10" aria-label="Evidence bounding-box highlight" data-testid="pdf-highlight-overlay">
                    <div className="absolute rounded border-2 border-amber-500 bg-amber-400/30 shadow-[0_0_0_1px_rgba(255,255,255,0.65)]" style={{ left: pixelBounds.left, top: pixelBounds.top, width: pixelBounds.width, height: pixelBounds.height }} />
                  </div>
                )}
                {!pixelBounds && quoteRects.length > 0 && isHighlightPage && (
                  <div className="pointer-events-none absolute inset-0 z-10" aria-label="Evidence quote text highlight" data-testid="pdf-quote-highlight-overlay">
                    {quoteRects.map((rect, index) => (
                      <div
                        key={`${rect.left.toFixed(2)}-${rect.top.toFixed(2)}-${index}`}
                        className="absolute rounded-sm border border-amber-500/80 bg-amber-300/45 shadow-[0_0_0_1px_rgba(255,255,255,0.6)]"
                        style={{ left: rect.left, top: rect.top, width: rect.width, height: rect.height }}
                      />
                    ))}
                  </div>
                )}
              </div>
            </div>
            {isHighlightPage && activeHighlight?.exact_quote && (
              <div className="mx-auto mt-3 max-w-3xl rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-950" role="status">
                <span className="font-semibold">Selected evidence: </span>
                <span>“{activeHighlight.exact_quote}”</span>
                {activeHighlight.bbox && !pixelBounds && <span className="ml-1 text-amber-800">(position overlay unavailable)</span>}
                {!activeHighlight.bbox && quoteRects.length === 0 && <span className="ml-1 text-amber-800">(text location could not be matched on this rendered page)</span>}
              </div>
            )}
          </Document>
        )}
      </div>
    </section>
  );
}

export default function PDFViewer(props) {
  return (
    <PdfViewerErrorBoundary>
      <PDFViewerContent {...props} />
    </PdfViewerErrorBoundary>
  );
}
