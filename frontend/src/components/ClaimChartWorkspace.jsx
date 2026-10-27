import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiRequest, downloadArtifact } from "../lib/apiClient";
import { useDocumentStore } from "../stores/documentStore";

const EDIT_ROLES = new Set(["OWNER", "ADMIN", "ATTORNEY", "ANALYST"]);
const REVIEW_ROLES = new Set(["OWNER", "ADMIN", "ATTORNEY", "ANALYST", "REVIEWER"]);
const EXPORT_FORMATS = ["DOCX", "XLSX", "PDF", "JSON"];
const EXPORT_ACTIVE = new Set(["QUEUED", "RUNNING"]);

function formatStatus(value) {
  return String(value || "UNKNOWN").replaceAll("_", " ").toLowerCase();
}

function statusStyle(value) {
  const styles = {
    DIRECT_SUPPORT: "border-emerald-200 bg-emerald-50 text-emerald-800",
    PARTIAL_SUPPORT: "border-blue-200 bg-blue-50 text-blue-800",
    POSSIBLE_EQUIVALENT: "border-violet-200 bg-violet-50 text-violet-800",
    CONTRADICTORY: "border-rose-200 bg-rose-50 text-rose-800",
    NOT_FOUND: "border-slate-200 bg-slate-50 text-slate-700",
    UNCERTAIN: "border-amber-200 bg-amber-50 text-amber-800",
  };
  return styles[value] || "border-slate-200 bg-slate-50 text-slate-700";
}

/**
 * Attorney-editable claim-chart interface with immutable revisions and queued exports.
 * @param {{analysisId:string, tenantId:string, role:string}} props
 */
export default function ClaimChartWorkspace({ analysisId, tenantId, role }) {
  const canEdit = EDIT_ROLES.has(role);
  const canReview = REVIEW_ROLES.has(role);
  const canApprove = ["OWNER", "ADMIN", "ATTORNEY"].includes(role);
  const setDocumentContext = useDocumentStore((state) => state.setDocumentContext);
  const setPage = useDocumentStore((state) => state.setPage);
  const setActiveHighlight = useDocumentStore((state) => state.setActiveHighlight);

  const [charts, setCharts] = useState([]);
  const [chartId, setChartId] = useState("");
  const [chart, setChart] = useState(null);
  const [chartTitle, setChartTitle] = useState("");
  const [drafts, setDrafts] = useState({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [activeExport, setActiveExport] = useState(null);
  const [revisions, setRevisions] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const chartSnapshot = chart?.snapshot || null;
  const rows = useMemo(() => chartSnapshot?.rows || [], [chartSnapshot]);

  const refreshCharts = useCallback(async () => {
    if (!analysisId || !tenantId) return;
    setLoading(true);
    setError("");
    try {
      const result = await apiRequest(
        `/api/v1/reports/claim-charts/?analysis_id=${encodeURIComponent(analysisId)}`,
        { tenantId },
      );
      const nextCharts = Array.isArray(result?.results) ? result.results : [];
      setCharts(nextCharts);
      setChartId((current) => (
        nextCharts.some((item) => item.chart_id === current)
          ? current
          : nextCharts[0]?.chart_id || ""
      ));
      if (nextCharts.length === 0) setChart(null);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Could not load claim charts.");
    } finally {
      setLoading(false);
    }
  }, [analysisId, tenantId]);

  const loadChart = useCallback(async (requestedChartId) => {
    if (!requestedChartId || !tenantId) {
      setChart(null);
      return;
    }
    setBusy("load");
    setError("");
    try {
      const result = await apiRequest(
        `/api/v1/reports/claim-charts/${requestedChartId}/`,
        { tenantId },
      );
      if (result?.chart?.analysis_id !== analysisId) {
        throw new Error("The selected claim chart does not belong to this analysis.");
      }
      setChart(result.chart);
      setDrafts(Object.fromEntries((result.chart.snapshot?.rows || []).map((row) => [
        row.row_id,
        {
          mapping_summary: row.mapping_summary || "",
          attorney_notes: row.attorney_notes || "",
          attorney_decision: row.attorney_decision || "UNREVIEWED",
        },
      ])));
      const history = await apiRequest(
        `/api/v1/reports/claim-charts/${requestedChartId}/revisions/`,
        { tenantId },
      );
      setRevisions(Array.isArray(history?.results) ? history.results : []);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "Could not load the claim chart.");
    } finally {
      setBusy("");
    }
  }, [analysisId, tenantId]);

  useEffect(() => {
    setCharts([]);
    setChartId("");
    setChart(null);
    setActiveExport(null);
    setDrafts({});
    void refreshCharts();
  }, [analysisId, tenantId, refreshCharts]);

  useEffect(() => {
    if (chartId) void loadChart(chartId);
  }, [chartId, loadChart]);

  useEffect(() => {
    const exportId = activeExport?.export_id;
    if (!exportId || !tenantId || !EXPORT_ACTIVE.has(activeExport.status)) return undefined;
    let cancelled = false;
    let timerId;
    const poll = async () => {
      try {
        const result = await apiRequest(
          `/api/v1/reports/claim-chart-exports/${exportId}/`,
          { tenantId },
        );
        if (cancelled) return;
        setActiveExport(result);
        if (EXPORT_ACTIVE.has(result.status)) timerId = window.setTimeout(poll, 1400);
        else if (result.status === "COMPLETED") setNotice(`${result.format} export is ready.`);
        else setError(`Export failed: ${result.error_code || "EXPORT_FAILED"}`);
      } catch (pollError) {
        if (!cancelled) {
          setError(pollError instanceof Error ? pollError.message : "Could not refresh export status.");
          timerId = window.setTimeout(poll, 3500);
        }
      }
    };
    timerId = window.setTimeout(poll, 1200);
    return () => {
      cancelled = true;
      if (timerId) window.clearTimeout(timerId);
    };
  }, [activeExport?.export_id, activeExport?.status, tenantId]);

  const createChart = async (event) => {
    event.preventDefault();
    if (!analysisId || !tenantId || !canEdit) return;
    setBusy("create");
    setError("");
    setNotice("");
    try {
      const result = await apiRequest("/api/v1/reports/claim-charts/", {
        method: "POST",
        tenantId,
        body: { analysis_id: analysisId, title: chartTitle.trim() || undefined },
      });
      const created = result.chart;
      setCharts((previous) => [
        { chart_id: created.chart_id, analysis_id: created.analysis_id, title: created.title, status: created.status, revision: created.revision, row_count: created.row_count },
        ...previous.filter((item) => item.chart_id !== created.chart_id),
      ]);
      setChartId(created.chart_id);
      setChartTitle("");
      setNotice("Claim chart created from the persisted analysis evidence.");
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : "Could not create claim chart.");
    } finally {
      setBusy("");
    }
  };

  const saveRow = async (row) => {
    if (!chart || !tenantId || !canReview) return;
    const draft = drafts[row.row_id] || {};
    const patch = canEdit
      ? draft
      : {
          attorney_notes: draft.attorney_notes ?? row.attorney_notes ?? "",
          attorney_decision: draft.attorney_decision || row.attorney_decision || "UNREVIEWED",
        };
    setBusy(`row:${row.row_id}`);
    setError("");
    setNotice("");
    try {
      const result = await apiRequest(
        `/api/v1/reports/claim-charts/${chart.chart_id}/rows/${row.row_id}/`,
        { method: "PATCH", tenantId, body: patch },
      );
      setChart(result.chart);
      setNotice(`Limitation ${row.limitation_index} saved as revision ${result.chart.revision}.`);
      await refreshCharts();
      const history = await apiRequest(
        `/api/v1/reports/claim-charts/${chart.chart_id}/revisions/`,
        { tenantId },
      );
      setRevisions(Array.isArray(history?.results) ? history.results : []);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "Could not save the limitation mapping.");
    } finally {
      setBusy("");
    }
  };

  const updateDraft = (rowId, field, value) => {
    setDrafts((current) => ({
      ...current,
      [rowId]: { ...(current[rowId] || {}), [field]: value },
    }));
  };

  const updateChartStatus = async (nextStatus) => {
    if (!chart || !tenantId || !canEdit) return;
    setBusy("status");
    setError("");
    try {
      const result = await apiRequest(`/api/v1/reports/claim-charts/${chart.chart_id}/`, {
        method: "PATCH",
        tenantId,
        body: { status: nextStatus },
      });
      setChart(result.chart);
      setNotice(`Claim chart moved to ${formatStatus(nextStatus)}.`);
      await refreshCharts();
      const history = await apiRequest(`/api/v1/reports/claim-charts/${chart.chart_id}/revisions/`, { tenantId });
      setRevisions(Array.isArray(history?.results) ? history.results : []);
    } catch (statusError) {
      setError(statusError instanceof Error ? statusError.message : "Could not update chart status.");
    } finally {
      setBusy("");
    }
  };

  const startExport = async (format) => {
    if (!chart || !tenantId || !canReview) return;
    setBusy(`export:${format}`);
    setError("");
    setNotice("");
    try {
      const result = await apiRequest(`/api/v1/reports/claim-charts/${chart.chart_id}/exports/`, {
        method: "POST",
        tenantId,
        body: { format },
      });
      setActiveExport(result);
      setNotice(`${format} export queued for chart revision ${result.revision}.`);
    } catch (exportError) {
      setError(exportError instanceof Error ? exportError.message : `Could not queue ${format} export.`);
    } finally {
      setBusy("");
    }
  };

  const downloadExport = async () => {
    if (!activeExport?.export_id || !activeExport.download_available || !tenantId) return;
    try {
      await downloadArtifact(
        `/api/v1/reports/claim-chart-exports/${activeExport.export_id}/download/`,
        {
          tenantId,
          fallbackFilename: `patentshield-claim-chart-${chart?.chart_id}-r${activeExport.revision}.${String(activeExport.format).toLowerCase()}`,
        },
      );
    } catch (downloadError) {
      setError(downloadError instanceof ApiError || downloadError instanceof Error
        ? downloadError.message
        : "Could not download the claim-chart export.");
    }
  };

  const openEvidence = async (evidence) => {
    if (!tenantId || !evidence?.source_patent_id || !evidence?.source_document_version_id) return;
    setBusy(`evidence:${evidence.evidence_id}`);
    setError("");
    try {
      const patent = await apiRequest(`/api/v1/patents/${evidence.source_patent_id}/`, { tenantId });
      const version = (patent.versions || []).find((item) => item.id === evidence.source_document_version_id);
      if (!version) throw new Error("The cited document version is not available in this tenant.");
      setDocumentContext(patent, version);
      const pageIndex = Number.isInteger(evidence.page_index) ? evidence.page_index : 0;
      setPage(pageIndex + 1);
      setActiveHighlight({
        page_index: pageIndex,
        bbox: evidence.bbox || null,
        exact_quote: evidence.exact_quote || null,
        quote_verified: evidence.quote_verified === true,
        source_version_id: evidence.source_document_version_id,
      });
      setNotice("Opened the cited source document and selected its canonical evidence quote.");
    } catch (openError) {
      setError(openError instanceof Error ? openError.message : "Could not open the cited source document.");
    } finally {
      setBusy("");
    }
  };

  if (!analysisId || !tenantId) return null;

  return (
    <section className="mt-5 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6" aria-label="Attorney claim chart and evidence matrix">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.15em] text-indigo-700">Attorney work product</p>
          <h2 className="mt-1 text-xl font-semibold text-slate-950">Claim chart & evidence matrix</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 text-slate-500">Create a versioned, editable chart from persisted limitation assessments. Evidence quotations and source identifiers remain attached to each row.</p>
        </div>
        <span className="rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold text-slate-700">{chart ? `Revision ${chart.revision}` : "Not created"}</span>
      </div>

      {error && <p className="mt-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800" role="alert">{error}</p>}
      {notice && <p className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800" role="status">{notice}</p>}

      <div className="mt-5 grid gap-3 md:grid-cols-[minmax(220px,1fr)_minmax(180px,0.7fr)_auto] md:items-end">
        <div>
          <label htmlFor="claim-chart-select" className="mb-1 block text-xs font-semibold text-slate-700">Saved chart</label>
          <select
            id="claim-chart-select"
            value={chartId}
            onChange={(event) => {
              const nextChartId = event.target.value;
              setChartId(nextChartId);
              if (!nextChartId) {
                setChart(null);
                setDrafts({});
                setRevisions([]);
                setActiveExport(null);
              }
            }}
            disabled={loading || charts.length === 0}
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-800 disabled:bg-slate-100"
          >
            <option value="">
              {loading ? "Loading charts…" : charts.length ? "Create a new chart…" : "No saved chart"}
            </option>
            {charts.map((item) => <option key={item.chart_id} value={item.chart_id}>{item.title} · r{item.revision} · {formatStatus(item.status)}</option>)}
          </select>
        </div>
        {canEdit && !chart && (
          <form onSubmit={createChart} className="contents">
            <div>
              <label htmlFor="claim-chart-title" className="mb-1 block text-xs font-semibold text-slate-700">New chart title (optional)</label>
              <input id="claim-chart-title" value={chartTitle} onChange={(event) => setChartTitle(event.target.value)} maxLength={240} placeholder="e.g. Product sensor claim chart" className="w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm" />
            </div>
            <button type="submit" disabled={busy === "create" || loading} className="rounded-lg bg-indigo-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-indigo-800 disabled:opacity-50">{busy === "create" ? "Creating…" : "Create from analysis"}</button>
          </form>
        )}
        {chart && (
          <div className="flex flex-wrap gap-2">
            {chart.status === "DRAFT" && canEdit && <button type="button" onClick={() => void updateChartStatus("IN_REVIEW")} disabled={busy === "status"} className="rounded-lg border border-indigo-200 px-3 py-2 text-xs font-semibold text-indigo-700 disabled:opacity-40">Submit for review</button>}
            {chart.status === "IN_REVIEW" && canEdit && <button type="button" onClick={() => void updateChartStatus("DRAFT")} disabled={busy === "status"} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700 disabled:opacity-40">Return to draft</button>}
            {chart.status === "IN_REVIEW" && canApprove && <button type="button" onClick={() => void updateChartStatus("APPROVED")} disabled={busy === "status" || rows.some((row) => !row.attorney_decision || row.attorney_decision === "UNREVIEWED")} title={rows.some((row) => !row.attorney_decision || row.attorney_decision === "UNREVIEWED") ? "Record an attorney decision for every limitation before approval." : "Approve this reviewed claim chart."} className="rounded-lg bg-emerald-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-40">Approve chart</button>}
            {chart.status !== "ARCHIVED" && canEdit && <button type="button" onClick={() => void updateChartStatus("ARCHIVED")} disabled={busy === "status"} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700 disabled:opacity-40">Archive</button>}
          </div>
        )}
      </div>

      {chart && (
        <>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-slate-50 p-3">
            <div>
              <p className="text-sm font-semibold text-slate-800">Export revision {chart.revision}</p>
              <p className="mt-1 text-xs text-slate-500">Exports are immutable snapshots. Later edits produce a new revision.</p>
            </div>
            <div className="flex flex-wrap gap-2">
              {EXPORT_FORMATS.map((format) => (
                <button key={format} type="button" onClick={() => void startExport(format)} disabled={!canReview || Boolean(busy.startsWith("export:")) || EXPORT_ACTIVE.has(activeExport?.status)} className="rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300">{busy === `export:${format}` ? "Queueing…" : `Export ${format}`}</button>
              ))}
            </div>
          </div>

          {activeExport && (
            <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 p-3">
              <div>
                <p className="text-sm font-semibold text-slate-800">{activeExport.format} export · revision {activeExport.revision}</p>
                <p className="mt-1 text-xs text-slate-500">Status: {formatStatus(activeExport.status)}{activeExport.error_code ? ` · ${activeExport.error_code}` : ""}</p>
              </div>
              {activeExport.download_available && <button type="button" onClick={() => void downloadExport()} className="rounded-lg border border-emerald-300 px-3 py-2 text-xs font-semibold text-emerald-800 hover:bg-emerald-50">Download {activeExport.format}</button>}
            </div>
          )}

          <div className="mt-5 overflow-x-auto rounded-xl border border-slate-200">
            <table className="min-w-[1050px] w-full border-collapse text-left text-sm">
              <thead className="bg-slate-900 text-xs uppercase tracking-wide text-white">
                <tr>
                  <th className="px-3 py-3">Limitation</th>
                  <th className="px-3 py-3">Assessment</th>
                  <th className="px-3 py-3">Source evidence</th>
                  <th className="px-3 py-3">Attorney review</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-200">
                {rows.map((row) => {
                  const draft = drafts[row.row_id] || {};
                  return (
                    <tr key={row.row_id} className="align-top">
                      <td className="max-w-xs px-3 py-4">
                        <p className="text-xs font-bold uppercase text-slate-500">Limitation {row.limitation_index} · Claim {row.claim_number || "—"}</p>
                        <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-900">{row.limitation_text}</p>
                        <p className="mt-3 text-xs text-slate-500">Confidence: {typeof row.confidence === "number" ? `${(Math.max(0, Math.min(1, row.confidence)) * 100).toFixed(0)}%` : "—"}</p>
                      </td>
                      <td className="min-w-64 px-3 py-4">
                        <span className={`inline-flex rounded-full border px-2 py-1 text-[10px] font-bold uppercase ${statusStyle(row.mapping_status)}`}>{formatStatus(row.mapping_status)}</span>
                        <label className="mt-3 block text-xs font-semibold text-slate-700" htmlFor={`mapping-summary-${row.row_id}`}>Mapping summary</label>
                        <textarea id={`mapping-summary-${row.row_id}`} value={draft.mapping_summary ?? row.mapping_summary ?? ""} onChange={(event) => updateDraft(row.row_id, "mapping_summary", event.target.value)} disabled={!canEdit || !["DRAFT", "IN_REVIEW"].includes(chart.status)} rows={4} className="mt-1 w-full rounded-lg border border-slate-300 p-2 text-xs leading-5 disabled:bg-slate-100" />
                      </td>
                      <td className="min-w-[340px] px-3 py-4">
                        {(row.evidence || []).length === 0 && <p className="text-xs text-slate-500">No evidence references for this limitation.</p>}
                        {(row.evidence || []).map((evidence) => (
                          <article key={evidence.evidence_id} className="mb-3 rounded-lg border border-slate-200 p-3 last:mb-0">
                            <div className="flex flex-wrap items-center gap-2 text-[10px] font-semibold uppercase text-slate-500">
                              <span>{evidence.source_publication_number || evidence.source_patent_id || "Source not identified"}</span>
                              {evidence.page_number && <span>Page {evidence.page_number}</span>}
                              <span className={evidence.quote_verified ? "text-emerald-700" : "text-amber-700"}>{evidence.quote_verified ? "✓ Quote verified" : "⚠ Quote unverified"}</span>
                            </div>
                            <button type="button" disabled={!evidence.source_patent_id || !evidence.source_document_version_id || Boolean(busy.startsWith("evidence:"))} onClick={() => void openEvidence(evidence)} className="mt-2 block w-full rounded-md bg-slate-50 p-2 text-left text-xs leading-5 text-slate-800 hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-70">
                              “{evidence.exact_quote || "No source quotation"}”
                            </button>
                            {evidence.source_section && <p className="mt-1 text-[10px] text-slate-500">Section: {evidence.source_section}</p>}
                          </article>
                        ))}
                      </td>
                      <td className="min-w-64 px-3 py-4">
                        <label className="block text-xs font-semibold text-slate-700" htmlFor={`decision-${row.row_id}`}>Decision</label>
                        <select id={`decision-${row.row_id}`} value={draft.attorney_decision || row.attorney_decision} onChange={(event) => updateDraft(row.row_id, "attorney_decision", event.target.value)} disabled={!canReview || !["DRAFT", "IN_REVIEW"].includes(chart.status)} className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-2 py-2 text-xs disabled:bg-slate-100">
                          <option value="UNREVIEWED">Unreviewed</option>
                          <option value="ACCEPTED">Accepted</option>
                          <option value="CORRECTED">Corrected</option>
                          <option value="REJECTED">Rejected</option>
                        </select>
                        <label className="mt-3 block text-xs font-semibold text-slate-700" htmlFor={`attorney-notes-${row.row_id}`}>Attorney notes</label>
                        <textarea id={`attorney-notes-${row.row_id}`} value={draft.attorney_notes ?? row.attorney_notes ?? ""} onChange={(event) => updateDraft(row.row_id, "attorney_notes", event.target.value)} disabled={!canReview || !["DRAFT", "IN_REVIEW"].includes(chart.status)} rows={4} className="mt-1 w-full rounded-lg border border-slate-300 p-2 text-xs leading-5 disabled:bg-slate-100" />
                        {canReview && ["DRAFT", "IN_REVIEW"].includes(chart.status) && <button type="button" onClick={() => void saveRow(row)} disabled={busy === `row:${row.row_id}`} className="mt-2 rounded-lg border border-indigo-200 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-50 disabled:opacity-40">{busy === `row:${row.row_id}` ? "Saving…" : "Save revision"}</button>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="mt-4 rounded-xl border border-slate-200 p-3">
            <h3 className="text-sm font-semibold text-slate-800">Revision history</h3>
            <div className="mt-2 flex flex-wrap gap-2">
              {revisions.map((revision) => (
                <div key={revision.revision_id} className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
                  <p className="font-semibold text-slate-800">Revision {revision.revision_number}</p>
                  <p>{revision.change_summary || "Chart snapshot"}</p>
                  <p className="mt-1 font-mono text-[10px]">SHA-256: {String(revision.snapshot_sha256 || "").slice(0, 16)}…</p>
                </div>
              ))}
              {revisions.length === 0 && <p className="text-xs text-slate-500">No revision history available.</p>}
            </div>
          </div>
          <p className="mt-3 text-[11px] leading-5 text-slate-500">This is an AI-assisted research work product. Assessments, source quotations, and mappings require qualified professional review; the chart is not a legal opinion or a finding of infringement, validity, novelty, or freedom to operate.</p>
        </>
      )}
    </section>
  );
}
