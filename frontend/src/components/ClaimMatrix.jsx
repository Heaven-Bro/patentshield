import { useMemo } from "react";
import { useDocumentStore } from "../stores/documentStore";
import { useMatrixStore } from "../stores/matrixStore";

const STATUS_STYLES = {
  DIRECT_SUPPORT: "border-emerald-200 bg-emerald-50 text-emerald-800",
  PARTIAL_SUPPORT: "border-blue-200 bg-blue-50 text-blue-800",
  POSSIBLE_EQUIVALENT: "border-purple-200 bg-purple-50 text-purple-800",
  CONTRADICTORY: "border-rose-200 bg-rose-50 text-rose-800",
  NOT_FOUND: "border-slate-200 bg-slate-100 text-slate-700",
  UNCERTAIN: "border-amber-200 bg-amber-50 text-amber-800",
};

/**
 * @param {unknown} value
 * @returns {string}
 */
function displayText(value) {
  return typeof value === "string" ? value : "";
}

/**
 * @param {unknown} confidence
 * @returns {string}
 */
function formatConfidence(confidence) {
  if (typeof confidence !== "number" || !Number.isFinite(confidence)) return "—";
  const percent = confidence <= 1 ? confidence * 100 : confidence;
  return `${Math.max(0, Math.min(100, percent)).toFixed(0)}%`;
}

/** @param {string} status */
function statusLabel(status) {
  return status.replaceAll("_", " ");
}

/**
 * Convert a matrix evidence source into a page navigation and highlight action.
 * Backend page indices are zero-based, while react-pdf page numbers are one-based.
 * @param {Record<string, any>} evidence
 * @param {(page: number) => void} setPage
 * @param {(highlight: {page_index:number, bbox:object|null, exact_quote:string|null, quote_verified?:boolean}) => void} setActiveHighlight
 * @param {string} limitationId
 * @param {(limitationId:string|null) => void} selectLimitation
 * @param {Array<Record<string, any>>} patents
 * @param {object|null} currentPatent
 * @param {object|null} currentVersion
 * @param {(patent:object|null, version:object|null) => void} setDocumentContext
 * @returns {boolean} True when the source document and page can be opened safely.
 */
function findEvidenceDocument(evidence, patents, currentPatent, currentVersion) {
  const versionId = evidence.source_version_id;
  const patentId = evidence.source_patent_id;
  if (versionId && currentVersion?.id === versionId) {
    if (!patentId || currentPatent?.id === patentId) {
      return { patent: currentPatent, version: currentVersion };
    }
  }
  if (!versionId) {
    return patentId && currentPatent?.id === patentId && currentVersion
      ? { patent: currentPatent, version: currentVersion }
      : null;
  }
  for (const patent of patents) {
    if (patentId && patent.id !== patentId) continue;
    const version = (patent.versions || []).find((item) => item.id === versionId);
    if (version) return { patent, version };
  }
  return null;
}

function navigateToEvidence(
  evidence,
  setPage,
  setActiveHighlight,
  limitationId,
  selectLimitation,
  patents,
  currentPatent,
  currentVersion,
  setDocumentContext,
) {
  selectLimitation(limitationId);
  if (!Number.isInteger(evidence.page_index) || evidence.page_index < 0) return false;
  const sourceDocument = findEvidenceDocument(evidence, patents, currentPatent, currentVersion);
  if (!sourceDocument) return false;
  if (
    sourceDocument.patent?.id !== currentPatent?.id
    || sourceDocument.version?.id !== currentVersion?.id
  ) {
    setDocumentContext(sourceDocument.patent, sourceDocument.version);
  }

  const bbox = evidence.bbox && typeof evidence.bbox === "object" ? evidence.bbox : null;
  const exactQuote = displayText(evidence.exact_quote) || null;
  setPage(evidence.page_index + 1);
  setActiveHighlight({
    page_index: evidence.page_index,
    bbox,
    exact_quote: exactQuote,
    quote_verified: evidence.quote_verified,
    source_version_id: evidence.source_version_id || null,
  });
  return true;
}

/**
 * Interactive limitation matrix connected to Zustand document and analysis stores.
 * @param {{className?: string, patents?: Array<Record<string, any>>}} props
 */
export default function ClaimMatrix({ className = "", patents = [] }) {
  const selectedClaim = useMatrixStore((state) => state.selectedClaim);
  const assessments = useMatrixStore((state) => state.assessments);
  const selectedLimitationId = useMatrixStore((state) => state.selectedLimitationId);
  const selectLimitation = useMatrixStore((state) => state.selectLimitation);
  const setPage = useDocumentStore((state) => state.setPage);
  const setActiveHighlight = useDocumentStore((state) => state.setActiveHighlight);
  const currentPatent = useDocumentStore((state) => state.currentPatent);
  const currentVersion = useDocumentStore((state) => state.currentVersion);
  const setDocumentContext = useDocumentStore((state) => state.setDocumentContext);

  const rows = useMemo(() => {
    const limitations = Array.isArray(selectedClaim?.limitations)
      ? selectedClaim.limitations
      : Array.isArray(selectedClaim?.analysis?.limitations)
        ? selectedClaim.analysis.limitations
        : [];
    const limitationMap = new Map(
      limitations
        .filter((item) => item && item.limitation_id)
        .map((item) => [String(item.limitation_id), item]),
    );
    const assessmentMap = new Map(
      (Array.isArray(assessments) ? assessments : [])
        .filter((item) => item && item.limitation_id)
        .map((item) => [String(item.limitation_id), item]),
    );
    const ids = [...new Set([...limitationMap.keys(), ...assessmentMap.keys()])];

    return ids
      .map((id, index) => {
        const limitation = limitationMap.get(id) ?? {};
        const assessment = assessmentMap.get(id) ?? {};
        return {
          ...limitation,
          ...assessment,
          limitation_id: id,
          limitation_index:
            limitation.limitation_index ?? assessment.limitation_index ?? index + 1,
          text:
            displayText(limitation.text) ||
            displayText(limitation.limitation_text) ||
            displayText(assessment.limitation_text) ||
            displayText(assessment.text) ||
            "Limitation text unavailable",
          status: displayText(assessment.status) || "UNCERTAIN",
          evidence: Array.isArray(assessment.evidence) ? assessment.evidence : [],
        };
      })
      .sort((a, b) => Number(a.limitation_index) - Number(b.limitation_index));
  }, [selectedClaim, assessments]);

  return (
    <section className={`flex min-h-0 flex-col ${className}`} aria-label="Claim limitation matrix">
      <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3">
        <div>
          <h2 className="text-base font-semibold text-slate-900">Claim Matrix</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {selectedClaim?.claim_number
              ? `Claim ${selectedClaim.claim_number}`
              : "Limitation-by-limitation evidence review"}
          </p>
        </div>
        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
          {rows.length} {rows.length === 1 ? "limitation" : "limitations"}
        </span>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        <table className="w-full min-w-[760px] border-separate border-spacing-0 text-left text-sm">
          <thead className="sticky top-0 z-20 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th scope="col" className="border-b border-slate-200 px-3 py-3 font-semibold">
                Limitation
              </th>
              <th scope="col" className="border-b border-slate-200 px-3 py-3 font-semibold">
                Status
              </th>
              <th scope="col" className="border-b border-slate-200 px-3 py-3 font-semibold">
                Exact evidence quote
              </th>
              <th scope="col" className="border-b border-slate-200 px-3 py-3 font-semibold">
                Confidence / action
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {rows.map((row) => {
              const isSelected = selectedLimitationId === row.limitation_id;
              const evidenceItems = row.evidence;
              const confidence = formatConfidence(row.confidence);
              const badgeClass =
                STATUS_STYLES[row.status] ?? STATUS_STYLES.UNCERTAIN;

              return (
                <tr
                  key={row.limitation_id}
                  className={isSelected ? "bg-indigo-50/70" : "hover:bg-slate-50"}
                >
                  <td className="w-[28%] border-b border-slate-100 px-3 py-3 align-top">
                    <button
                      type="button"
                      onClick={() => selectLimitation(row.limitation_id)}
                      aria-pressed={isSelected}
                      className="text-left focus:outline-none focus:ring-2 focus:ring-indigo-400 focus:ring-offset-1"
                    >
                      <span className="block text-xs font-semibold text-slate-500">
                        Limitation {row.limitation_index}
                      </span>
                      <span className="mt-1 block whitespace-pre-wrap text-sm leading-5 text-slate-800">
                        {row.text}
                      </span>
                    </button>
                    {row.explanation && (
                      <p className="mt-2 text-xs leading-5 text-slate-500">{row.explanation}</p>
                    )}
                  </td>
                  <td className="border-b border-slate-100 px-3 py-3 align-top">
                    <span
                      className={`inline-flex max-w-32 rounded-full border px-2 py-1 text-[11px] font-semibold leading-4 ${badgeClass}`}
                      title={row.status}
                    >
                      {statusLabel(row.status)}
                    </span>
                  </td>
                  <td className="max-w-sm border-b border-slate-100 px-3 py-3 align-top">
                    {evidenceItems.length === 0 ? (
                      <p className="text-xs italic text-slate-400">No evidence citation available.</p>
                    ) : (
                      <div className="space-y-3">
                        {evidenceItems.map((evidence, evidenceIndex) => {
                          const evidenceKey =
                            evidence.evidence_id ??
                            evidence.chunk_id ??
                            `${row.limitation_id}-evidence-${evidenceIndex}`;
                          const verified =
                            typeof evidence.quote_verified === "boolean"
                              ? evidence.quote_verified
                              : row.quote_verified;
                          const quote = displayText(evidence.exact_quote);
                          const hasPage =
                            Number.isInteger(evidence.page_index) && evidence.page_index >= 0;
                          const sourceDocument = findEvidenceDocument(
                            evidence,
                            patents,
                            currentPatent,
                            currentVersion,
                          );
                          const canOpenSource = hasPage && Boolean(sourceDocument) && Boolean(quote);

                          return (
                            <div key={String(evidenceKey)} className="space-y-1.5">
                              <button
                                type="button"
                                onClick={() =>
                                  navigateToEvidence(
                                    evidence,
                                    setPage,
                                    setActiveHighlight,
                                    row.limitation_id,
                                    selectLimitation,
                                    patents,
                                    currentPatent,
                                    currentVersion,
                                    setDocumentContext,
                                  )
                                }
                                disabled={!canOpenSource}
                                className="block w-full rounded-md border border-slate-200 bg-white p-2 text-left text-xs leading-5 text-slate-700 hover:border-indigo-300 hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-60"
                                title={canOpenSource ? "Open this source location in the PDF" : "The source PDF is not linked or available in this tenant"}
                              >
                                <span className="line-clamp-5">“{quote || "Quote unavailable"}”</span>
                                {hasPage && (
                                  <span className="mt-1 block text-[11px] font-medium text-indigo-700">
                                    Page {evidence.page_index + 1} · View in PDF
                                  </span>
                                )}
                              </button>
                              {!sourceDocument && hasPage && (
                                <p className="text-[10px] text-slate-500">No tenant-local PDF is linked to this citation.</p>
                              )}
                              <span
                                className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                                  verified === true
                                    ? "bg-emerald-50 text-emerald-700"
                                    : verified === false
                                      ? "bg-rose-50 text-rose-700"
                                      : "bg-amber-50 text-amber-800"
                                }`}
                              >
                                {verified === true
                                  ? "✓ Quote verified"
                                  : verified === false
                                    ? "⚠ Quote verification failed"
                                    : "! Verification pending"}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </td>
                  <td className="border-b border-slate-100 px-3 py-3 align-top">
                    <span className="block font-semibold tabular-nums text-slate-800">
                      {confidence}
                    </span>
                    {evidenceItems.some((item) => (
                      Number.isInteger(item.page_index)
                      && item.page_index >= 0
                      && Boolean(findEvidenceDocument(item, patents, currentPatent, currentVersion))
                    )) && (
                      <button
                        type="button"
                        onClick={() => {
                          const firstPageEvidence = evidenceItems.find((item) => (
                            Number.isInteger(item.page_index)
                            && item.page_index >= 0
                            && Boolean(findEvidenceDocument(item, patents, currentPatent, currentVersion))
                          ));
                          if (firstPageEvidence) {
                            navigateToEvidence(
                              firstPageEvidence,
                              setPage,
                              setActiveHighlight,
                              row.limitation_id,
                              selectLimitation,
                              patents,
                              currentPatent,
                              currentVersion,
                              setDocumentContext,
                            );
                          }
                        }}
                        className="mt-2 text-xs font-semibold text-indigo-700 underline decoration-indigo-300 underline-offset-2 hover:text-indigo-900"
                      >
                        Open source
                      </button>
                    )}
                    {row.quote_verified === false && (
                      <p className="mt-2 text-[11px] font-medium text-rose-700">
                        Guardrail flagged citation
                      </p>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td colSpan={4} className="px-4 py-12 text-center">
                  <p className="font-medium text-slate-700">No claim analysis loaded</p>
                  <p className="mt-1 text-sm text-slate-500">
                    Select a claim or run an analysis to populate the evidence matrix.
                  </p>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
