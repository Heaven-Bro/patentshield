import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import { useDocumentStore } from "../stores/documentStore";

// Vite resolves this worker URL from the installed pdfjs-dist package.
pdfjs.GlobalWorkerOptions.workerSrc = new URL(
  "pdfjs-dist/build/pdf.worker.min.mjs",
  import.meta.url,
).toString();

const MIN_ZOOM = 0.25;
const MAX_ZOOM = 4;

/** @typedef {{x: number, y: number, width: number, height: number}} NormalizedBoundingBox */

/**
 * Resolve a readable PDF source from the explicit prop or the current document
 * version. Prefer passing a short-lived, authorized URL or a File/Blob directly.
 * @param {string | File | Blob | object | null | undefined} file
 * @param {object | null} currentVersion
 * @returns {string | File | Blob | object | null}
 */
function resolvePdfFile(file, currentVersion) {
  if (file) return file;
  if (!currentVersion) return null;
  return (
    currentVersion.file_url ??
    currentVersion.pdf_url ??
    currentVersion.download_url ??
    currentVersion.url ??
    currentVersion.file ??
    null
  );
}

/**
 * Clamp a normalized box to the visible page so malformed upstream coordinates
 * cannot create a negative or off-page overlay.
 * @param {NormalizedBoundingBox} bbox
 * @param {number} pageWidth
 * @param {number} pageHeight
 * @returns {{left: number, top: number, width: number, height: number}}
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
 * PDF.js-based viewer with normalized schematic/evidence highlight overlays.
 * `currentPage` is one-based; evidence `page_index` values are zero-based.
 * @param {{file?: string | File | Blob | object | null, className?: string}} props
 */
export default function PDFViewer({ file = null, className = "" }) {
  const currentVersion = useDocumentStore((state) => state.currentVersion);
  const currentPage = useDocumentStore((state) => state.currentPage);
  const totalPages = useDocumentStore((state) => state.totalPages);
  const zoom = useDocumentStore((state) => state.zoom);
  const activeHighlight = useDocumentStore((state) => state.activeHighlight);
  const setPage = useDocumentStore((state) => state.setPage);
  const setZoom = useDocumentStore((state) => state.setZoom);
  const setTotalPages = useDocumentStore((state) => state.setTotalPages);
  const clearHighlight = useDocumentStore((state) => state.clearHighlight);

  const [loadError, setLoadError] = useState("");
  const [viewportWidth, setViewportWidth] = useState(720);
  const [pageSize, setPageSize] = useState({ width: 0, height: 0 });
  const [pageInput, setPageInput] = useState(String(currentPage));
  const viewportRef = useRef(null);
  const pageContainerRef = useRef(null);
  const resolvedFile = useMemo(
    () => resolvePdfFile(file, currentVersion),
    [file, currentVersion],
  );

  useEffect(() => {
    setPageInput(String(currentPage));
  }, [currentPage]);

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
      setPageSize((previous) =>
        previous.width === width && previous.height === height
          ? previous
          : { width, height },
      );
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

  const pageWidth = Math.max(220, viewportWidth - 32) * zoom;
  const highlightMatchesPage =
    activeHighlight !== null && activeHighlight.page_index === currentPage - 1;
  const canHighlight =
    highlightMatchesPage &&
    activeHighlight.bbox &&
    pageSize.width > 0 &&
    pageSize.height > 0;
  const pixelBounds = canHighlight
    ? getPixelBounds(activeHighlight.bbox, pageSize.width, pageSize.height)
    : null;

  const handleDocumentLoad = ({ numPages }) => {
    setLoadError("");
    setTotalPages(numPages);
  };

  const handlePageError = (error) => {
    // Keep UI feedback useful without dumping server URLs or provider responses.
    setLoadError("The PDF could not be rendered. Check the document URL and access permissions.");
    if (import.meta.env?.DEV) console.error("PatentShield PDF rendering failed:", error);
  };

  const commitPageInput = () => {
    const nextPage = Number.parseInt(pageInput, 10);
    if (!Number.isFinite(nextPage)) {
      setPageInput(String(currentPage));
      return;
    }
    setPage(nextPage);
    clearHighlightIfPageChanges(nextPage);
  };

  /**
   * Keep a citation highlight when it points to the page being navigated to;
   * otherwise clear it so an unrelated quote is not displayed as selected.
   * @param {number} nextPage
   */
  function clearHighlightIfPageChanges(nextPage) {
    if (activeHighlight && activeHighlight.page_index !== Math.trunc(nextPage) - 1) {
      clearHighlight();
    }
  }

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
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 bg-white px-3 py-2">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={goToPreviousPage}
            disabled={currentPage <= 1}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
            aria-label="Previous page"
          >
            Previous
          </button>
          <label className="flex items-center gap-2 text-sm text-slate-600">
            <span className="sr-only">Page number</span>
            <input
              type="number"
              min={1}
              max={totalPages || undefined}
              value={pageInput}
              onChange={(event) => setPageInput(event.target.value)}
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
          <button
            type="button"
            onClick={goToNextPage}
            disabled={totalPages > 0 && currentPage >= totalPages}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
            aria-label="Next page"
          >
            Next
          </button>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => updateZoom(-0.1)}
            disabled={zoom <= MIN_ZOOM}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-40"
            aria-label="Zoom out"
          >
            −
          </button>
          <span className="min-w-12 text-center text-sm tabular-nums text-slate-600">
            {Math.round(zoom * 100)}%
          </span>
          <button
            type="button"
            onClick={() => updateZoom(0.1)}
            disabled={zoom >= MAX_ZOOM}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-40"
            aria-label="Zoom in"
          >
            +
          </button>
        </div>
      </div>

      <div ref={viewportRef} className="min-h-0 flex-1 overflow-auto p-4">
        {!resolvedFile ? (
          <div className="flex h-full min-h-64 items-center justify-center rounded-lg border border-dashed border-slate-300 bg-white p-6 text-center">
            <div>
              <p className="font-semibold text-slate-800">No document selected</p>
              <p className="mt-1 max-w-sm text-sm text-slate-500">
                Select a patent document version or pass an authorized PDF URL to the viewer.
              </p>
            </div>
          </div>
        ) : (
          <Document
            file={resolvedFile}
            onLoadSuccess={handleDocumentLoad}
            onLoadError={handlePageError}
            onSourceError={handlePageError}
            loading={<p className="py-12 text-center text-sm text-slate-500">Loading PDF…</p>}
            error={
              <p role="alert" className="rounded-lg bg-rose-50 p-4 text-sm text-rose-700">
                {loadError || "Unable to load this PDF."}
              </p>
            }
          >
            <div className="flex min-w-max justify-center">
              <div
                ref={pageContainerRef}
                className="relative mx-auto h-fit w-fit bg-white shadow-lg"
                data-testid="pdf-page-container"
              >
                <Page
                  pageNumber={currentPage}
                  width={pageWidth}
                  renderTextLayer={false}
                  renderAnnotationLayer={false}
                  onRenderSuccess={measurePage}
                  onRenderError={handlePageError}
                  loading={<p className="p-8 text-sm text-slate-500">Rendering page…</p>}
                />
                {pixelBounds && (
                  <div
                    className="pointer-events-none absolute inset-0 z-10"
                    aria-label="Verified evidence location"
                    data-testid="pdf-highlight-overlay"
                  >
                    <div
                      className="absolute rounded border-2 border-amber-500 bg-amber-400/30 shadow-[0_0_0_1px_rgba(255,255,255,0.65)]"
                      style={{
                        left: `${pixelBounds.left}px`,
                        top: `${pixelBounds.top}px`,
                        width: `${pixelBounds.width}px`,
                        height: `${pixelBounds.height}px`,
                      }}
                    />
                  </div>
                )}
              </div>
            </div>
            {loadError && (
              <p role="alert" className="mt-3 rounded-lg bg-rose-50 p-3 text-sm text-rose-700">
                {loadError}
              </p>
            )}
          </Document>
        )}
      </div>
    </section>
  );
}
