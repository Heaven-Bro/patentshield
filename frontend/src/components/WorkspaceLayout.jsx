import PDFViewer from "./PDFViewer";
import ClaimMatrix from "./ClaimMatrix";
import { useMatrixStore } from "../stores/matrixStore";

const METRIC_LABELS = [
  ["retrieval_relevance", "Retrieval relevance"],
  ["limitation_coverage", "Limitation coverage"],
  ["evidence_confidence", "Evidence confidence"],
  ["technical_correspondence", "Technical correspondence"],
];

/**
 * Render a 0–1 metric as a percentage, tolerating a 0–100 input as well.
 * @param {unknown} value
 * @returns {string}
 */
function formatMetric(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const percentage = value <= 1 ? value * 100 : value;
  return `${Math.max(0, Math.min(100, percentage)).toFixed(0)}%`;
}

/**
 * Dual-pane PatentShield review workspace: PDF viewer on the left and analysis
 * matrix with screening priority metrics on the right.
 * @param {{pdfFile?: string | File | Blob | object | null, className?: string, patents?: Array<Record<string, any>>}} props
 */
export default function WorkspaceLayout({ pdfFile = null, className = "", patents = [] }) {
  const screeningMetrics = useMatrixStore((state) => state.screeningMetrics);
  const score = screeningMetrics?.screening_priority_score;
  const hasScore = typeof score === "number" && Number.isFinite(score);

  return (
    <main
      className={`grid h-screen min-h-0 grid-cols-12 grid-rows-2 gap-4 overflow-hidden bg-gradient-to-br from-slate-100 via-slate-50 to-indigo-50/40 p-3 sm:p-4 lg:grid-rows-1 ${className}`}
      aria-label="PatentShield analysis workspace"
    >
      <section className="col-span-12 flex min-h-0 flex-col lg:col-span-7" aria-label="Source document">
        <PDFViewer file={pdfFile} className="flex-1" />
      </section>

      <section className="col-span-12 flex min-h-0 flex-col overflow-hidden rounded-2xl border border-slate-200/90 bg-white shadow-panel lg:col-span-5">
        <header className="shrink-0 border-b border-slate-200/80 bg-gradient-to-br from-white to-slate-50 p-4 sm:p-5">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="text-[10px] font-bold uppercase tracking-[0.2em] text-indigo-600">
                Screening overview
              </p>
              <h1 className="mt-1 text-lg font-bold tracking-tight text-slate-950">
                Patent evidence review
              </h1>
              <p className="mt-1 max-w-md text-xs leading-5 text-slate-500">
                Use verified source passages to review each technical limitation. This score prioritizes review; it is not a legal conclusion.
              </p>
            </div>
            <div className="min-w-24 rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-right shadow-sm">
              <span className="block text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                Priority score
              </span>
              <span className="mt-0.5 block text-2xl font-bold tabular-nums text-slate-900">
                {hasScore ? Math.max(0, Math.min(100, score)).toFixed(0) : "—"}
                <span className="ml-0.5 text-xs font-medium text-slate-500">/100</span>
              </span>
            </div>
          </div>

          <div className="mt-4 grid grid-cols-2 gap-2.5">
            {METRIC_LABELS.map(([key, label]) => (
              <div key={key} className="rounded-xl border border-slate-200/70 bg-white px-3 py-2.5">
                <span className="block text-[11px] text-slate-500">{label}</span>
                <span className="mt-0.5 block text-sm font-semibold tabular-nums text-slate-800">
                  {formatMetric(screeningMetrics?.[key])}
                </span>
              </div>
            ))}
          </div>
        </header>

        <ClaimMatrix className="flex-1" patents={patents} />
      </section>
    </main>
  );
}
