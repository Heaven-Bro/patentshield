import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import WorkspaceLayout from "./WorkspaceLayout";
import { ApiError, apiRequest, downloadArtifact } from "../lib/apiClient";
import { useAnalysisProgress } from "../hooks/useAnalysisProgress";
import { useAuthStore } from "../stores/authStore";
import { useDocumentStore } from "../stores/documentStore";
import { useMatrixStore } from "../stores/matrixStore";

const RUN_ROLES = new Set(["OWNER", "ADMIN", "ATTORNEY", "ANALYST"]);
const ANALYSIS_ACTIVE = new Set(["QUEUED", "RUNNING"]);
const EXPORT_ACTIVE = new Set(["QUEUED", "RUNNING"]);
const ANALYSIS_SUCCESS = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS"]);

function labelStatus(value) {
  return String(value || "UNKNOWN").replaceAll("_", " ").toLowerCase();
}

function formatDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function statusClass(value) {
  if (["COMPLETED", "COMPLETED_WITH_WARNINGS"].includes(value)) {
    return "border-emerald-200 bg-emerald-50 text-emerald-800";
  }
  if (["FAILED", "CANCELLED"].includes(value)) {
    return "border-rose-200 bg-rose-50 text-rose-800";
  }
  if (["RUNNING", "EXPORTING"].includes(value)) {
    return "border-indigo-200 bg-indigo-50 text-indigo-800";
  }
  return "border-amber-200 bg-amber-50 text-amber-800";
}

/**
 * Authenticated claim analysis console with recoverable WebSocket progress,
 * authoritative REST polling, and tenant-scoped report downloads.
 * @param {{tenant:object, patents:Array<Record<string, any>>, onRefreshPatents?:()=>void}} props
 */
export default function AnalysisConsole({ tenant, patents, onRefreshPatents }) {
  const tenantId = tenant?.id || null;
  const canRunAnalysis = RUN_ROLES.has(tenant?.role);

  const setCurrentPatent = useDocumentStore((state) => state.setPatent);
  const setCurrentVersion = useDocumentStore((state) => state.setVersion);
  const setAnalysisData = useMatrixStore((state) => state.setAnalysisData);

  const [selectedPatentId, setSelectedPatentId] = useState("");
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [claims, setClaims] = useState([]);
  const [selectedClaimId, setSelectedClaimId] = useState("");
  const [claimsLoading, setClaimsLoading] = useState(false);
  const [claimsError, setClaimsError] = useState("");
  const [history, setHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [activeAnalysisId, setActiveAnalysisId] = useState(null);
  const [analysisStatus, setAnalysisStatus] = useState(null);
  const [analysisErrorCode, setAnalysisErrorCode] = useState(null);
  const [exportStatus, setExportStatus] = useState("NOT_REQUESTED");
  const [exportErrorCode, setExportErrorCode] = useState(null);
  const [artifactsAvailable, setArtifactsAvailable] = useState({ pdf: false, json: false });
  const [consoleError, setConsoleError] = useState("");
  const [createBusy, setCreateBusy] = useState(false);
  const [retryBusy, setRetryBusy] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [pollNonce, setPollNonce] = useState(0);
  const [lastProgressAt, setLastProgressAt] = useState(null);

  const selectedPatent = useMemo(
    () => patents.find((patent) => patent.id === selectedPatentId) || null,
    [patents, selectedPatentId],
  );
  const availableVersions = useMemo(
    () => (selectedPatent?.versions || []).filter(
      (version) => version.extraction_status === "EXTRACTED",
    ),
    [selectedPatent],
  );
  const selectedVersion = useMemo(
    () => availableVersions.find((version) => version.id === selectedVersionId) || null,
    [availableVersions, selectedVersionId],
  );
  const selectedClaim = useMemo(
    () => claims.find((claim) => claim.id === selectedClaimId) || null,
    [claims, selectedClaimId],
  );
  const canStartAnalysis = canRunAnalysis && selectedPatent?.status === "INDEXED";

  const {
    connectionState,
    lastEvent,
    analysisProgress,
    reportProgress,
    reconnectAttempt,
  } = useAnalysisProgress({ analysisId: activeAnalysisId, enabled: Boolean(tenantId) });

  // Clear cross-tenant workspace state on mount/unmount. The API remains the
  // security boundary; this additionally avoids stale prior-tenant UI flashes.
  useEffect(() => {
    setAnalysisData({});
    setCurrentPatent(null);
    setCurrentVersion(null);
    return () => {
      setAnalysisData({});
      setCurrentPatent(null);
      setCurrentVersion(null);
    };
  }, [tenantId, setAnalysisData, setCurrentPatent, setCurrentVersion]);

  useEffect(() => {
    if (!tenantId || patents.length === 0) {
      if (patents.length === 0) {
        setSelectedPatentId("");
        setSelectedVersionId("");
      }
      return;
    }
    const selectedIsValid = patents.some((patent) => patent.id === selectedPatentId);
    if (selectedIsValid) {
      const patent = patents.find((item) => item.id === selectedPatentId);
      const versions = (patent?.versions || []).filter(
        (version) => version.extraction_status === "EXTRACTED",
      );
      if (!versions.some((version) => version.id === selectedVersionId)) {
        setSelectedVersionId(versions[0]?.id || "");
      }
      return;
    }
    const preferredPatent = patents.find(
      (patent) => patent.status === "INDEXED"
        && (patent.versions || []).some((version) => version.extraction_status === "EXTRACTED"),
    ) || patents[0];
    const preferredVersion = (preferredPatent?.versions || []).find(
      (version) => version.extraction_status === "EXTRACTED",
    );
    setSelectedPatentId(preferredPatent?.id || "");
    setSelectedVersionId(preferredVersion?.id || "");
  }, [tenantId, patents, selectedPatentId, selectedVersionId]);

  useEffect(() => {
    if (!selectedPatent || !selectedVersion) return;
    const current = useDocumentStore.getState();
    if (current.currentPatent?.id !== selectedPatent.id) setCurrentPatent(selectedPatent);
    if (current.currentVersion?.id !== selectedVersion.id) setCurrentVersion(selectedVersion);
  }, [selectedPatent, selectedVersion, setCurrentPatent, setCurrentVersion]);

  const loadHistory = useCallback(async (signal) => {
    if (!tenantId) return;
    setHistoryLoading(true);
    setHistoryError("");
    try {
      const payload = await apiRequest("/api/v1/analyses/?limit=20", {
        tenantId,
        signal,
      });
      if (useAuthStore.getState().activeTenantId !== tenantId) return;
      setHistory(Array.isArray(payload.results) ? payload.results : []);
    } catch (error) {
      if (error?.name === "AbortError") return;
      if (useAuthStore.getState().activeTenantId !== tenantId) return;
      setHistoryError(error instanceof Error ? error.message : "Could not load analysis history.");
    } finally {
      if (!signal?.aborted && useAuthStore.getState().activeTenantId === tenantId) {
        setHistoryLoading(false);
      }
    }
  }, [tenantId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadHistory(controller.signal);
    return () => controller.abort();
  }, [loadHistory]);

  useEffect(() => {
    setClaims([]);
    setSelectedClaimId("");
    setClaimsError("");
    if (!tenantId || !selectedPatentId || !selectedVersionId) {
      setClaimsLoading(false);
      return undefined;
    }

    const controller = new AbortController();
    setClaimsLoading(true);
    apiRequest(
      `/api/v1/patents/${selectedPatentId}/claims/?document_version_id=${encodeURIComponent(selectedVersionId)}`,
      { tenantId, signal: controller.signal },
    )
      .then((payload) => {
        if (controller.signal.aborted || useAuthStore.getState().activeTenantId !== tenantId) return;
        const rows = Array.isArray(payload.results) ? payload.results : [];
        setClaims(rows);
        setSelectedClaimId(rows[0]?.id || "");
      })
      .catch((error) => {
        if (controller.signal.aborted || error?.name === "AbortError") return;
        if (useAuthStore.getState().activeTenantId !== tenantId) return;
        setClaims([]);
        setClaimsError(
          error instanceof Error ? error.message : "Could not load extracted claims.",
        );
      })
      .finally(() => {
        if (!controller.signal.aborted && useAuthStore.getState().activeTenantId === tenantId) {
          setClaimsLoading(false);
        }
      });

    return () => controller.abort();
  }, [tenantId, selectedPatentId, selectedVersionId]);

  const loadAnalysisDetail = useCallback(async (analysisId) => {
    if (!tenantId || !analysisId) return null;
    const payload = await apiRequest(`/api/v1/analyses/${analysisId}/`, { tenantId });
    if (useAuthStore.getState().activeTenantId !== tenantId) return null;
    const record = payload.analysis || {};
    setAnalysisStatus(record.status || "UNKNOWN");
    setAnalysisErrorCode(record.error_code || null);
    setExportStatus(record.report_export_status || "NOT_REQUESTED");
    setExportErrorCode(record.report_export_error_code || null);
    setArtifactsAvailable({
      pdf: Boolean(record.report_available?.pdf),
      json: Boolean(record.report_available?.json),
    });
    setAnalysisData({
      selectedClaim: payload.selectedClaim || null,
      assessments: Array.isArray(payload.assessments) ? payload.assessments : [],
      screeningMetrics: payload.screeningMetrics || null,
    });

    const patent = patents.find((item) => item.id === record.patent_id);
    const version = patent?.versions?.find(
      (item) => item.id === record.target_document_version_id,
    );
    if (patent && version) {
      setSelectedPatentId(patent.id);
      setSelectedVersionId(version.id);
      const current = useDocumentStore.getState();
      if (current.currentPatent?.id !== patent.id) setCurrentPatent(patent);
      if (current.currentVersion?.id !== version.id) setCurrentVersion(version);
    }
    return record;
  }, [tenantId, patents, setAnalysisData, setCurrentPatent, setCurrentVersion]);

  const loadReportStatus = useCallback(async (analysisId) => {
    if (!tenantId || !analysisId) return null;
    const payload = await apiRequest(`/api/v1/reports/${analysisId}/status/`, { tenantId });
    if (useAuthStore.getState().activeTenantId !== tenantId) return null;
    setExportStatus(payload.export_status || "NOT_REQUESTED");
    setExportErrorCode(payload.export_error_code || null);
    setArtifactsAvailable({
      pdf: Boolean(payload.artifacts?.pdf?.available),
      json: Boolean(payload.artifacts?.json?.available),
    });
    return payload;
  }, [tenantId]);

  const refreshCurrentAnalysisRef = useRef(null);
  refreshCurrentAnalysisRef.current = async (analysisId) => {
    const analysis = await loadAnalysisDetail(analysisId);
    if (!analysis) return null;
    let exportInfo = null;
    try {
      exportInfo = await loadReportStatus(analysisId);
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 404)) {
        throw error;
      }
    }
    return {
      analysisStatus: analysis.status,
      exportStatus: exportInfo?.export_status || analysis.report_export_status || "NOT_REQUESTED",
    };
  };

  useEffect(() => {
    if (!activeAnalysisId || !tenantId) return undefined;
    let cancelled = false;
    let timeoutId = null;

    const poll = async () => {
      try {
        const current = await refreshCurrentAnalysisRef.current?.(activeAnalysisId);
        if (cancelled || !current) return;
        const shouldContinue = ANALYSIS_ACTIVE.has(current.analysisStatus)
          || EXPORT_ACTIVE.has(current.exportStatus);
        if (shouldContinue) timeoutId = window.setTimeout(poll, 2500);
      } catch {
        if (!cancelled) timeoutId = window.setTimeout(poll, 5000);
      }
    };

    void poll();
    return () => {
      cancelled = true;
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    };
  }, [activeAnalysisId, tenantId, pollNonce]);

  useEffect(() => {
    if (!lastEvent || !activeAnalysisId) return;
    setLastProgressAt(lastEvent.receivedAt || Date.now());
    if (["job.completed", "report.completed"].includes(lastEvent.type)) {
      setPollNonce((value) => value + 1);
      void loadHistory();
    }
  }, [lastEvent, activeAnalysisId, loadHistory]);

  const handlePatentChange = (patentId) => {
    const patent = patents.find((item) => item.id === patentId) || null;
    const version = patent?.versions?.find(
      (item) => item.extraction_status === "EXTRACTED",
    ) || null;
    setSelectedPatentId(patentId);
    setSelectedVersionId(version?.id || "");
    setSelectedClaimId("");
    setActiveAnalysisId(null);
    setAnalysisStatus(null);
    setExportStatus("NOT_REQUESTED");
    setAnalysisData({});
    setCurrentPatent(patent);
    setCurrentVersion(version);
    setConsoleError("");
  };

  const handleVersionChange = (versionId) => {
    const version = availableVersions.find((item) => item.id === versionId) || null;
    setSelectedVersionId(versionId);
    setSelectedClaimId("");
    setActiveAnalysisId(null);
    setAnalysisStatus(null);
    setExportStatus("NOT_REQUESTED");
    setAnalysisData({});
    setCurrentPatent(selectedPatent);
    setCurrentVersion(version);
    setConsoleError("");
  };

  const handleCreateAnalysis = async (event) => {
    event.preventDefault();
    if (!canRunAnalysis || !selectedPatent || !selectedVersion || !selectedClaim || !tenantId) return;
    setCreateBusy(true);
    setConsoleError("");
    try {
      const response = await apiRequest("/api/v1/analyses/", {
        method: "POST",
        tenantId,
        body: {
          patent_id: selectedPatent.id,
          document_version_id: selectedVersion.id,
          claim_id: selectedClaim.id,
          analysis_type: "PRIOR_ART_OVERLAP",
        },
      });
      const created = response.analysis;
      if (!created?.id) throw new Error("The server did not return an analysis identifier.");
      setActiveAnalysisId(created.id);
      setAnalysisStatus(created.status || "QUEUED");
      setAnalysisErrorCode(null);
      setExportStatus(created.report_export_status || "NOT_REQUESTED");
      setAnalysisData({
        selectedClaim: {
          ...selectedClaim,
          limitations: [],
        },
        assessments: [],
        screeningMetrics: null,
      });
      setPollNonce((value) => value + 1);
      await loadHistory();
    } catch (error) {
      setConsoleError(error instanceof Error ? error.message : "Could not queue the analysis.");
      if (error instanceof ApiError && error.status === 503) {
        await loadHistory();
      }
    } finally {
      setCreateBusy(false);
    }
  };

  const handleSelectAnalysis = (analysisId) => {
    setConsoleError("");
    setActiveAnalysisId(analysisId);
    setLastProgressAt(null);
    setPollNonce((value) => value + 1);
  };

  const handleRetryAnalysis = async () => {
    if (!activeAnalysisId || !tenantId) return;
    setRetryBusy(true);
    setConsoleError("");
    try {
      const response = await apiRequest(`/api/v1/analyses/${activeAnalysisId}/retry/`, {
        method: "POST",
        tenantId,
        body: {},
      });
      setAnalysisStatus(response.analysis?.status || "QUEUED");
      setAnalysisErrorCode(null);
      setPollNonce((value) => value + 1);
      await loadHistory();
    } catch (error) {
      setConsoleError(error instanceof Error ? error.message : "Could not retry this analysis.");
      await loadHistory();
    } finally {
      setRetryBusy(false);
    }
  };

  const handleGenerateReport = async () => {
    if (!activeAnalysisId || !tenantId) return;
    setExportBusy(true);
    setConsoleError("");
    try {
      const response = await apiRequest(`/api/v1/reports/${activeAnalysisId}/generate/`, {
        method: "POST",
        tenantId,
        body: {},
      });
      setExportStatus(response.export_status || "QUEUED");
      setExportErrorCode(null);
      setPollNonce((value) => value + 1);
    } catch (error) {
      setConsoleError(error instanceof Error ? error.message : "Could not start report export.");
      try {
        await loadReportStatus(activeAnalysisId);
      } catch {
        // The displayed API error is sufficient if status polling is unavailable.
      }
    } finally {
      setExportBusy(false);
    }
  };

  const handleDownload = async (format) => {
    if (!activeAnalysisId || !tenantId) return;
    setConsoleError("");
    try {
      await downloadArtifact(`/api/v1/reports/${activeAnalysisId}/${format}/`, {
        tenantId,
        fallbackFilename: `patentshield-analysis-${activeAnalysisId}.${format}`,
      });
    } catch (error) {
      setConsoleError(error instanceof Error ? error.message : `Could not download the ${format.toUpperCase()} report.`);
    }
  };

  const progressValue = ANALYSIS_SUCCESS.has(analysisStatus) || analysisStatus === "FAILED"
    ? 100
    : Math.max(0, Math.min(100, Number(analysisProgress?.progress) || 0));
  const activeAnalysis = history.find((item) => item.id === activeAnalysisId) || null;
  const exportCanStart = Boolean(activeAnalysisId)
    && ANALYSIS_SUCCESS.has(analysisStatus)
    && !EXPORT_ACTIVE.has(exportStatus)
    && !exportBusy;

  if (!tenantId) return null;

  return (
    <section className="mx-auto w-full max-w-[1800px] px-4 pb-10 pt-3 sm:px-6 lg:px-8" aria-label="Analysis execution and reports">
      <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
        <div className="flex flex-col justify-between gap-3 lg:flex-row lg:items-start">
          <div>
            <p className="text-xs font-semibold uppercase tracking-[0.16em] text-indigo-700">Prior-art analysis</p>
            <h2 className="mt-1 text-xl font-semibold tracking-tight text-slate-950">Run and monitor an analysis</h2>
            <p className="mt-1 max-w-2xl text-sm leading-6 text-slate-500">
              Choose an indexed patent claim to start a tenant-isolated analysis. Results and report downloads are available only to active members of this workspace.
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => onRefreshPatents?.()}
              className="shrink-0 rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50"
            >
              Refresh patents
            </button>
            <button
              type="button"
              onClick={() => void loadHistory()}
              disabled={historyLoading}
              className="shrink-0 rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {historyLoading ? "Refreshing history…" : "Refresh history"}
            </button>
          </div>
        </div>

        <form onSubmit={handleCreateAnalysis} className="mt-5 grid gap-3 md:grid-cols-2 xl:grid-cols-[1.15fr_1fr_1.4fr_auto] xl:items-end">
          <div>
            <label htmlFor="analysis-patent" className="mb-1 block text-xs font-semibold text-slate-700">Patent</label>
            <select id="analysis-patent" value={selectedPatentId} onChange={(event) => handlePatentChange(event.target.value)} className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100">
              <option value="">Choose a patent</option>
              {patents.map((patent) => <option key={patent.id} value={patent.id}>{patent.title} · {patent.status}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor="analysis-version" className="mb-1 block text-xs font-semibold text-slate-700">Extracted version</label>
            <select id="analysis-version" value={selectedVersionId} onChange={(event) => handleVersionChange(event.target.value)} disabled={!availableVersions.length} className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-100">
              <option value="">{availableVersions.length ? "Choose a version" : "No extracted version"}</option>
              {availableVersions.map((version) => <option key={version.id} value={version.id}>Version {version.version_number}</option>)}
            </select>
          </div>
          <div>
            <label htmlFor="analysis-claim" className="mb-1 block text-xs font-semibold text-slate-700">Target claim</label>
            <select id="analysis-claim" value={selectedClaimId} onChange={(event) => setSelectedClaimId(event.target.value)} disabled={claimsLoading || claims.length === 0} className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-800 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-100 disabled:bg-slate-100">
              <option value="">{claimsLoading ? "Loading claims…" : "Choose a claim"}</option>
              {claims.map((claim) => <option key={claim.id} value={claim.id}>Claim {claim.claim_number} · {claim.claim_type}</option>)}
            </select>
          </div>
          <button type="submit" disabled={!canStartAnalysis || !selectedPatent || !selectedVersion || !selectedClaim || createBusy} className="rounded-lg bg-indigo-700 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-indigo-800 disabled:cursor-not-allowed disabled:bg-slate-300">
            {createBusy ? "Queueing…" : "Run analysis"}
          </button>
        </form>
        {!canRunAnalysis && <p className="mt-2 text-xs text-slate-500">Your {labelStatus(tenant.role)} role can review existing analyses but cannot start new ones.</p>}
        {selectedPatent && !availableVersions.length && <p className="mt-2 text-xs text-amber-800">This patent has no extracted document version available for analysis yet.</p>}
        {claimsError && <p className="mt-2 text-xs text-rose-700" role="alert">{claimsError}</p>}
        {consoleError && <div className="mt-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800" role="alert">{consoleError}</div>}
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,1.25fr)_minmax(280px,0.75fr)]">
        <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm" aria-label="Analysis history">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold text-slate-900">Recent analyses</h3>
            <span className="text-xs text-slate-500">{history.length} shown</span>
          </div>
          {historyError && <p className="mb-3 rounded-lg bg-rose-50 p-3 text-xs text-rose-700" role="alert">{historyError}</p>}
          {history.length === 0 && !historyLoading ? (
            <p className="rounded-lg border border-dashed border-slate-300 p-5 text-sm text-slate-500">No analyses are recorded for this tenant yet.</p>
          ) : (
            <div className="max-h-64 space-y-2 overflow-auto pr-1">
              {history.map((item) => (
                <button key={item.id} type="button" onClick={() => handleSelectAnalysis(item.id)} className={`flex w-full flex-col gap-2 rounded-xl border p-3 text-left transition sm:flex-row sm:items-center sm:justify-between ${item.id === activeAnalysisId ? "border-indigo-300 bg-indigo-50" : "border-slate-200 hover:border-slate-300 hover:bg-slate-50"}`}>
                  <span className="min-w-0">
                    <span className="block truncate text-sm font-semibold text-slate-800">{item.patent_title || "Patent analysis"}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">Claim {item.claim_number || "—"} · {formatDate(item.created_at)}</span>
                  </span>
                  <span className={`inline-flex w-fit shrink-0 rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase ${statusClass(item.status)}`}>{labelStatus(item.status)}</span>
                </button>
              ))}
            </div>
          )}
        </section>

        <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm" aria-label="Analysis and report status">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h3 className="text-sm font-semibold text-slate-900">Execution status</h3>
              <p className="mt-1 text-xs text-slate-500">Progress events are real-time; REST polling recovers missed WebSocket updates.</p>
            </div>
            {activeAnalysisId && <span className={`inline-flex rounded-full border px-2.5 py-1 text-[10px] font-bold uppercase ${statusClass(analysisStatus)}`}>{labelStatus(analysisStatus)}</span>}
          </div>
          {!activeAnalysisId ? (
            <p className="mt-4 rounded-lg bg-slate-50 p-4 text-sm text-slate-500">Select an analysis from history or create one to monitor progress and export a report.</p>
          ) : (
            <>
              <div className="mt-4 flex items-center justify-between text-xs text-slate-600">
                <span>{analysisProgress?.stage ? labelStatus(analysisProgress.stage) : labelStatus(analysisStatus)}</span>
                <span className="font-semibold tabular-nums">{progressValue}%</span>
              </div>
              <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progressValue}>
                <div className="h-full rounded-full bg-indigo-600 transition-all duration-500" style={{ width: `${progressValue}%` }} />
              </div>
              <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-500">
                <span>WebSocket: {labelStatus(connectionState)}{reconnectAttempt ? ` (${reconnectAttempt})` : ""}</span>
                <span>{lastProgressAt ? `Last event ${new Date(lastProgressAt).toLocaleTimeString()}` : "Awaiting progress event"}</span>
              </div>
              {analysisErrorCode && <p className="mt-2 text-xs text-rose-700">Execution error: {analysisErrorCode}</p>}
              <div className="mt-5 border-t border-slate-100 pt-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-xs font-semibold text-slate-800">Report export</p>
                    <p className="mt-1 text-xs text-slate-500">JSON snapshot and PDF summary</p>
                  </div>
                  <span className={`inline-flex rounded-full border px-2 py-1 text-[10px] font-bold uppercase ${statusClass(exportStatus)}`}>{labelStatus(exportStatus)}</span>
                </div>
                {reportProgress?.stage && EXPORT_ACTIVE.has(exportStatus) && (
                  <p className="mt-2 text-xs text-slate-500">Export stage: {labelStatus(reportProgress.stage)}{Number.isFinite(reportProgress.progress) ? ` · ${reportProgress.progress}%` : ""}</p>
                )}
                {exportErrorCode && <p className="mt-2 text-xs text-rose-700">Export error: {exportErrorCode}</p>}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button type="button" onClick={() => void handleGenerateReport()} disabled={!exportCanStart} className="rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:bg-slate-300">
                    {exportBusy || EXPORT_ACTIVE.has(exportStatus) ? "Exporting…" : exportStatus === "COMPLETED" ? "Regenerate reports" : "Generate reports"}
                  </button>
                  <button type="button" onClick={() => void handleDownload("pdf")} disabled={!artifactsAvailable.pdf || exportStatus !== "COMPLETED"} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40">Download PDF</button>
                  <button type="button" onClick={() => void handleDownload("json")} disabled={!artifactsAvailable.json || exportStatus !== "COMPLETED"} className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40">Download JSON</button>
                  {analysisStatus === "FAILED" && canRunAnalysis && (
                    <button type="button" onClick={() => void handleRetryAnalysis()} disabled={retryBusy} className="rounded-lg border border-rose-300 px-3 py-2 text-xs font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50">{retryBusy ? "Retrying…" : "Retry analysis"}</button>
                  )}
                </div>
              </div>
            </>
          )}
        </section>
      </div>

      {activeAnalysisId && (
        <div className="mt-5 h-[min(78vh,900px)] min-h-[520px]">
          <WorkspaceLayout className="!h-full" />
        </div>
      )}
      {activeAnalysisId && activeAnalysis && analysisStatus === "FAILED" && (
        <p className="mt-3 text-xs text-slate-500">This run failed. Its persisted results, if any, remain available for review; use Retry analysis to requeue it.</p>
      )}
    </section>
  );
}
