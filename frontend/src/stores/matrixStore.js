import { create } from "zustand";

/**
 * @typedef {object} MatrixState
 * @property {object | null} selectedClaim
 * @property {Array<Record<string, any>>} assessments
 * @property {Record<string, any> | null} screeningMetrics
 * @property {string | null} selectedLimitationId
 * @property {(data: {selectedClaim?: object | null, assessments?: Array<Record<string, any>>, screeningMetrics?: Record<string, any> | null}) => void} setAnalysisData
 * @property {(limitationId: string | null) => void} selectLimitation
 */

/** @type {import("zustand").UseBoundStore<import("zustand").StoreApi<MatrixState>>} */
export const useMatrixStore = create((set) => ({
  selectedClaim: null,
  assessments: [],
  screeningMetrics: null,
  selectedLimitationId: null,

  /**
   * Replace displayed analysis data after a successful analysis response.
   * Missing fields are reset to safe empty defaults to avoid showing stale data.
   * @param {{selectedClaim?: object | null, assessments?: Array<Record<string, any>>, screeningMetrics?: Record<string, any> | null}} data
   */
  setAnalysisData: (data = {}) =>
    set((state) => {
      const selectedClaim = data.selectedClaim ?? null;
      const assessments = Array.isArray(data.assessments) ? data.assessments : [];
      const limitationIds = new Set([
        ...(Array.isArray(selectedClaim?.limitations)
          ? selectedClaim.limitations.map((item) => String(item?.limitation_id || ""))
          : []),
        ...assessments.map((item) => String(item?.limitation_id || "")),
      ].filter(Boolean));
      const keepSelection = Boolean(
        state.selectedLimitationId && limitationIds.has(state.selectedLimitationId),
      );
      return {
        selectedClaim,
        assessments,
        screeningMetrics: data.screeningMetrics ?? null,
        // REST polling refreshes rows during long analyses; keep the reviewer’s
        // selected limitation when it still belongs to the same response.
        selectedLimitationId: keepSelection ? state.selectedLimitationId : null,
      };
    }),

  /** @param {string | null} limitationId */
  selectLimitation: (limitationId) => set({ selectedLimitationId: limitationId }),
}));

export default useMatrixStore;
