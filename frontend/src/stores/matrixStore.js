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
    set({
      selectedClaim: data.selectedClaim ?? null,
      assessments: Array.isArray(data.assessments) ? data.assessments : [],
      screeningMetrics: data.screeningMetrics ?? null,
      selectedLimitationId: null,
    }),

  /** @param {string | null} limitationId */
  selectLimitation: (limitationId) => set({ selectedLimitationId: limitationId }),
}));

export default useMatrixStore;
