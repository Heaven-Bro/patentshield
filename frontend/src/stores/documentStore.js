import { create } from "zustand";

/**
 * @typedef {object} NormalizedBoundingBox
 * @property {number} x Left position in the inclusive range [0, 1].
 * @property {number} y Top position in the inclusive range [0, 1].
 * @property {number} width Normalized width in the inclusive range [0, 1].
 * @property {number} height Normalized height in the inclusive range [0, 1].
 */

/**
 * @typedef {object} ActiveHighlight
 * @property {number} page_index Zero-based PDF page index.
 * @property {NormalizedBoundingBox | null} bbox Normalized page-relative bounds, if known.
 * @property {string | null} exact_quote Exact source quote associated with the highlight.
 */

/**
 * @typedef {object} DocumentState
 * @property {object | null} currentPatent
 * @property {object | null} currentVersion
 * @property {number} currentPage One-based page number used by react-pdf.
 * @property {number} totalPages
 * @property {number} zoom
 * @property {ActiveHighlight | null} activeHighlight
 * @property {(patent: object | null) => void} setPatent
 * @property {(version: object | null) => void} setVersion
 * @property {(page: number) => void} setPage
 * @property {(zoom: number) => void} setZoom
 * @property {(highlight: ActiveHighlight | null) => void} setActiveHighlight
 * @property {() => void} clearHighlight
 * @property {(totalPages: number) => void} setTotalPages
 */

const MIN_ZOOM = 0.25;
const MAX_ZOOM = 4;

/**
 * Shared viewer state. Page indices on evidence/highlights are zero-based;
 * `currentPage` is one-based to match react-pdf's `pageNumber` prop.
 * @type {import("zustand").UseBoundStore<import("zustand").StoreApi<DocumentState>>}
 */
export const useDocumentStore = create((set, get) => ({
  currentPatent: null,
  currentVersion: null,
  currentPage: 1,
  totalPages: 0,
  zoom: 1.0,
  activeHighlight: null,

  /** @param {object | null} patent */
  setPatent: (patent) => set({ currentPatent: patent }),

  /** @param {object | null} version */
  setVersion: (version) =>
    set({
      currentVersion: version,
      currentPage: 1,
      totalPages: 0,
      activeHighlight: null,
    }),

  /** @param {number} page */
  setPage: (page) => {
    if (!Number.isFinite(page)) return;
    const requestedPage = Math.max(1, Math.trunc(page));
    const totalPages = get().totalPages;
    set({
      currentPage: totalPages > 0 ? Math.min(requestedPage, totalPages) : requestedPage,
    });
  },

  /** @param {number} zoom */
  setZoom: (zoom) => {
    if (!Number.isFinite(zoom)) return;
    const normalizedZoom = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, zoom));
    set({ zoom: Number(normalizedZoom.toFixed(2)) });
  },

  /** @param {ActiveHighlight | null} highlight */
  setActiveHighlight: (highlight) => set({ activeHighlight: highlight }),

  clearHighlight: () => set({ activeHighlight: null }),

  /** @param {number} totalPages */
  setTotalPages: (totalPages) => {
    const normalizedTotal = Number.isFinite(totalPages)
      ? Math.max(0, Math.trunc(totalPages))
      : 0;
    set((state) => ({
      totalPages: normalizedTotal,
      currentPage:
        normalizedTotal > 0
          ? Math.min(state.currentPage, normalizedTotal)
          : state.currentPage,
    }));
  },
}));

export default useDocumentStore;
