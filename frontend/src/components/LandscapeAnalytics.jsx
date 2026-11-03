import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiRequest } from "../lib/apiClient";

const API_ROOT = "/api/v1/landscapes";
function safeHttpsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}
const inputClass = "ps-input";
const primaryButton = "ps-button-primary";
const secondaryButton = "ps-button-secondary";
const clusterColors = ["#4f46e5", "#0891b2", "#059669", "#d97706", "#db2777", "#7c3aed", "#65a30d", "#e11d48", "#0f766e", "#9333ea"];

function friendlyError(error) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "The request could not be completed.";
}

function countLabel(value) {
  return new Intl.NumberFormat().format(Number(value || 0));
}

function dateLabel(value) {
  if (!value) return "Undated";
  return String(value).slice(0, 10);
}

function StatCard({ label, value, detail }) {
  return (
    <article className="ps-metric-card">
      <p className="ps-metric-label">{label}</p>
      <p className="ps-metric-value">{countLabel(value)}</p>
      {detail && <p className="mt-1 text-xs text-slate-500">{detail}</p>}
    </article>
  );
}

function DistributionBars({ items, nameKey, title, emptyLabel = "No source data available." }) {
  const max = Math.max(1, ...items.map((item) => Number(item.count || 0)));
  return (
    <section className="ps-panel">
      <h3 className="text-sm font-bold tracking-tight text-slate-950">{title}</h3>
      {items.length === 0 ? (
        <p className="mt-4 text-sm text-slate-500">{emptyLabel}</p>
      ) : (
        <div className="mt-4 space-y-3">
          {items.map((item, index) => (
            <div key={`${item[nameKey]}-${index}`}>
              <div className="mb-1 flex items-center justify-between gap-3 text-xs">
                <span className="min-w-0 truncate font-medium text-slate-700" title={item[nameKey]}>{item[nameKey]}</span>
                <span className="shrink-0 tabular-nums text-slate-500">{countLabel(item.count)}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-slate-100">
                <div className="h-full rounded-full bg-indigo-600" style={{ width: `${Math.max(2, (Number(item.count || 0) / max) * 100)}%` }} />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function EmbeddingMap({ points, clusters }) {
  const plotted = useMemo(() => {
    const valid = points.filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    if (!valid.length) return [];
    const minX = Math.min(...valid.map((point) => point.x));
    const maxX = Math.max(...valid.map((point) => point.x));
    const minY = Math.min(...valid.map((point) => point.y));
    const maxY = Math.max(...valid.map((point) => point.y));
    const rangeX = maxX - minX || 1;
    const rangeY = maxY - minY || 1;
    return valid.map((point) => ({
      ...point,
      plotX: 36 + ((point.x - minX) / rangeX) * 688,
      plotY: 376 - ((point.y - minY) / rangeY) * 332,
    }));
  }, [points]);
  return (
    <section className="ps-panel">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-sm font-bold tracking-tight text-slate-950">Publication embedding map</h3>
          <p className="mt-1 text-xs leading-5 text-slate-500">Each point represents a pinned publication snapshot. Hover a point for its identifier and title.</p>
        </div>
        <span className="rounded-lg bg-slate-100 px-2.5 py-1.5 text-[11px] font-medium text-slate-600">2-D projection · exploratory</span>
      </div>
      {plotted.length === 0 ? (
        <div className="mt-4 flex min-h-60 items-center justify-center rounded-xl border border-dashed border-slate-300 bg-slate-50 px-4 text-center text-sm text-slate-500">
          Build the landscape to generate the embedding map.
        </div>
      ) : (
        <div className="mt-4 overflow-x-auto rounded-xl border border-slate-100 bg-slate-50 p-2">
          <svg viewBox="0 0 760 410" role="img" aria-label="Interactive patent embedding scatter plot" className="min-w-[560px] w-full">
            <line x1="30" y1="385" x2="740" y2="385" stroke="#cbd5e1" strokeWidth="1" />
            <line x1="30" y1="28" x2="30" y2="385" stroke="#cbd5e1" strokeWidth="1" />
            {[1, 2, 3, 4].map((i) => <line key={i} x1="30" y1={28 + i * 71} x2="740" y2={28 + i * 71} stroke="#e2e8f0" strokeDasharray="3 5" />)}
            {plotted.map((point) => {
              const color = point.cluster_number == null ? "#64748b" : clusterColors[point.cluster_number % clusterColors.length];
              return (
                <g key={point.id}>
                  <title>{`${point.publication_number || point.publication_id}\n${point.title}\n${point.cluster_label}`}</title>
                  <circle cx={point.plotX} cy={point.plotY} r="6" fill={color} fillOpacity="0.84" stroke="white" strokeWidth="1.5" />
                </g>
              );
            })}
          </svg>
        </div>
      )}
      {clusters.length > 0 && (
        <div className="mt-4 flex flex-wrap gap-2">
          {clusters.map((cluster) => (
            <span key={cluster.id} className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] text-slate-700">
              <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: clusterColors[cluster.cluster_number % clusterColors.length] }} />
              {cluster.label} <span className="text-slate-400">· {cluster.publication_count}</span>
            </span>
          ))}
        </div>
      )}
      <p className="mt-3 text-[11px] leading-5 text-slate-500">Projection is PCA when scikit-learn is installed, otherwise a fixed-seed random projection. Cluster labels are derived from source title terms and IPC/CPC codes, not independently validated taxonomy descriptions.</p>
    </section>
  );
}

export default function LandscapeAnalytics({ tenant }) {
  const [studies, setStudies] = useState([]);
  const [selectedStudyId, setSelectedStudyId] = useState("");
  const [study, setStudy] = useState(null);
  const [analytics, setAnalytics] = useState(null);
  const [studyPublications, setStudyPublications] = useState([]);
  const [annotations, setAnnotations] = useState([]);
  const [assignees, setAssignees] = useState([]);
  const [selectedAssigneeId, setSelectedAssigneeId] = useState("");
  const [aliasName, setAliasName] = useState("");
  const [assigneeStatus, setAssigneeStatus] = useState("UNREVIEWED");
  const [query, setQuery] = useState("");
  const [jurisdiction, setJurisdiction] = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [selectedPublicationIds, setSelectedPublicationIds] = useState([]);
  const [studyTitle, setStudyTitle] = useState("");
  const [studyDescription, setStudyDescription] = useState("");
  const [annotationBody, setAnnotationBody] = useState("");
  const [annotationKind, setAnnotationKind] = useState("OVERVIEW");
  const [annotationPublicationId, setAnnotationPublicationId] = useState("");
  const [busyKey, setBusyKey] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const request = useCallback((path, options = {}) => apiRequest(path, { tenantId: tenant.id, ...options }), [tenant.id]);

  const loadStudies = useCallback(async (signal) => {
    const payload = await request(`${API_ROOT}/studies/`, { signal });
    if (signal?.aborted) return;
    const rows = payload.results || [];
    setStudies(rows);
    setSelectedStudyId((current) => rows.some((row) => row.id === current) ? current : rows[0]?.id || "");
  }, [request]);

  const loadAssignees = useCallback(async (signal) => {
    const payload = await request(`${API_ROOT}/assignees/`, { signal });
    if (signal?.aborted) return;
    const rows = payload.results || [];
    setAssignees(rows);
    setSelectedAssigneeId((current) => rows.some((row) => row.id === current) ? current : rows[0]?.id || "");
  }, [request]);

  const loadStudyData = useCallback(async (studyId, signal) => {
    if (!studyId) {
      setStudy(null);
      setAnalytics(null);
      setStudyPublications([]);
      setAnnotations([]);
      return;
    }
    const [detail, analyticsPayload, publicationPayload, annotationPayload] = await Promise.all([
      request(`${API_ROOT}/studies/${studyId}/`, { signal }),
      request(`${API_ROOT}/studies/${studyId}/analytics/`, { signal }),
      request(`${API_ROOT}/studies/${studyId}/publications/`, { signal }),
      request(`${API_ROOT}/studies/${studyId}/annotations/`, { signal }),
    ]);
    if (signal?.aborted) return;
    setStudy(detail);
    setAnalytics(analyticsPayload);
    setStudyPublications(publicationPayload.results || []);
    setAnnotations(annotationPayload.results || []);
    setStudies((current) => current.map((row) => row.id === detail.id ? detail : row));
  }, [request]);

  useEffect(() => {
    const selected = assignees.find((item) => item.id === selectedAssigneeId);
    setAssigneeStatus(selected?.status || "UNREVIEWED");
  }, [assignees, selectedAssigneeId]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void loadStudies(controller.signal).catch((loadError) => {
      if (loadError?.name !== "AbortError" && !controller.signal.aborted) setError(friendlyError(loadError));
    }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    void loadAssignees(controller.signal).catch((loadError) => {
      if (loadError?.name !== "AbortError" && !controller.signal.aborted) setError(friendlyError(loadError));
    });
    return () => controller.abort();
  }, [loadStudies, loadAssignees]);

  useEffect(() => {
    const controller = new AbortController();
    void loadStudyData(selectedStudyId, controller.signal).catch((loadError) => {
      if (loadError?.name !== "AbortError" && !controller.signal.aborted) setError(friendlyError(loadError));
    });
    return () => controller.abort();
  }, [loadStudyData, selectedStudyId]);

  useEffect(() => {
    if (!selectedStudyId || study?.status !== "BUILDING") return undefined;
    const timer = window.setInterval(() => {
      void loadStudyData(selectedStudyId).catch((loadError) => setError(friendlyError(loadError)));
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loadStudyData, selectedStudyId, study?.status]);

  async function runBusy(key, action, successMessage) {
    setBusyKey(key);
    setError("");
    setNotice("");
    try {
      await action();
      if (successMessage) setNotice(successMessage);
    } catch (actionError) {
      setError(friendlyError(actionError));
    } finally {
      setBusyKey("");
    }
  }

  async function createStudy(event) {
    event.preventDefault();
    if (!studyTitle.trim()) return;
    await runBusy("create-study", async () => {
      const created = await request(`${API_ROOT}/studies/`, {
        method: "POST",
        body: { title: studyTitle.trim(), description: studyDescription.trim(), scope_filters: {} },
      });
      setStudyTitle("");
      setStudyDescription("");
      setSelectedStudyId(created.id);
      const payload = await request(`${API_ROOT}/studies/`);
      setStudies(payload.results || []);
    }, "Landscape study created.");
  }

  async function searchPublications(event) {
    event.preventDefault();
    await runBusy("search", async () => {
      const params = new URLSearchParams();
      if (query.trim()) params.set("q", query.trim());
      if (jurisdiction.trim()) params.set("jurisdiction", jurisdiction.trim().toUpperCase());
      params.set("limit", "50");
      const payload = await request(`${API_ROOT}/publications/search/?${params.toString()}`);
      setSearchResults(payload.results || []);
      setSelectedPublicationIds([]);
    }, "Local canonical publication search completed.");
  }

  function togglePublication(publicationId) {
    setSelectedPublicationIds((current) => current.includes(publicationId) ? current.filter((id) => id !== publicationId) : [...current, publicationId]);
  }

  async function addPublications() {
    if (!selectedStudyId || selectedPublicationIds.length === 0) return;
    await runBusy("add-publications", async () => {
      const result = await request(`${API_ROOT}/studies/${selectedStudyId}/publications/`, {
        method: "POST", body: { publication_ids: selectedPublicationIds },
      });
      setSelectedPublicationIds([]);
      await loadStudyData(selectedStudyId);
      await loadStudies();
      setNotice(`${result.created_count} publication snapshot(s) added to the study.`);
    });
  }

  async function buildLandscape() {
    if (!selectedStudyId) return;
    await runBusy("build", async () => {
      const result = await request(`${API_ROOT}/studies/${selectedStudyId}/build/`, { method: "POST", body: { force: study?.status === "READY" } });
      await loadStudyData(selectedStudyId);
      await loadStudies();
      await loadAssignees();
      setNotice(`Landscape build ${String(result.status || "QUEUED").toLowerCase()}. This uses the configured local BGE-M3 model and may take a while.`);
    });
  }

  async function addAssigneeAlias(event) {
    event.preventDefault();
    if (!selectedAssigneeId || !aliasName.trim()) return;
    await runBusy("assignee-alias", async () => {
      await request(`${API_ROOT}/assignees/${selectedAssigneeId}/aliases/`, { method: "POST", body: { original_name: aliasName.trim() } });
      setAliasName("");
      await loadAssignees();
    }, "Assignee alias saved. No fuzzy merges were performed.");
  }

  async function updateAssigneeStatus() {
    if (!selectedAssigneeId) return;
    await runBusy("assignee-status", async () => {
      await request(`${API_ROOT}/assignees/${selectedAssigneeId}/`, { method: "PATCH", body: { status: assigneeStatus } });
      await loadAssignees();
    }, "Assignee identity decision updated.");
  }

  async function createAnnotation(event) {
    event.preventDefault();
    if (!selectedStudyId || !annotationBody.trim()) return;
    await runBusy("annotation", async () => {
      await request(`${API_ROOT}/studies/${selectedStudyId}/annotations/`, {
        method: "POST",
        body: {
          kind: annotationKind,
          body: annotationBody.trim(),
          ...(annotationPublicationId ? { study_publication_id: annotationPublicationId } : {}),
        },
      });
      setAnnotationBody("");
      setAnnotationPublicationId("");
      await loadStudyData(selectedStudyId);
    }, "Attorney/analyst annotation saved.");
  }

  async function resolveAnnotation(annotation) {
    await runBusy(`resolve-${annotation.id}`, async () => {
      await request(`${API_ROOT}/annotations/${annotation.id}/`, { method: "PATCH", body: { status: annotation.status === "RESOLVED" ? "OPEN" : "RESOLVED" } });
      await loadStudyData(selectedStudyId);
    }, annotation.status === "RESOLVED" ? "Annotation reopened." : "Annotation resolved.");
  }

  const clusters = analytics?.clusters || [];
  const points = analytics?.points || [];
  const timeline = analytics?.filing_timeline || [];
  const topTimeline = Math.max(1, ...timeline.map((item) => item.count));

  return (
    <main className="ps-page">
      <section className="ps-hero">
        <p className="text-[10px] font-bold uppercase tracking-[0.2em] text-cyan-300">Phase 7 · Portfolio intelligence</p>
        <div className="mt-3 grid gap-6 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Patent Landscape & White-Space Analytics</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-300">Explore filing activity, assignee portfolios, classification coverage, geographic spread, and embedding-based technology clusters from synchronized public patent records. Each study pins the bibliographic fields used for its analysis.</p>
          </div>
          <div className="rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm">
            <p className="font-semibold text-white">Evidence-aware analysis</p>
            <p className="mt-1 text-xs text-slate-300">Clusters are discovery aids, not legal conclusions</p>
          </div>
        </div>
      </section>

      {error && <div role="alert" className="ps-alert-error">{error}</div>}
      {notice && <div role="status" className="ps-alert-success">{notice}</div>}

      <section className="grid gap-5 lg:grid-cols-[minmax(280px,0.8fr)_minmax(0,1.2fr)]">
        <article className="ps-panel">
          <h2 className="text-sm font-bold tracking-tight text-slate-950">Create a landscape study</h2>
          <form onSubmit={createStudy} className="mt-4 space-y-3">
            <label className="block"><span className="mb-1.5 block text-xs font-semibold text-slate-600">Study title</span><input className={inputClass} value={studyTitle} onChange={(event) => setStudyTitle(event.target.value)} maxLength={240} placeholder="e.g. Solid-state battery patent landscape" required /></label>
            <label className="block"><span className="mb-1.5 block text-xs font-semibold text-slate-600">Research question / scope note</span><textarea className={`${inputClass} min-h-20 resize-y`} value={studyDescription} onChange={(event) => setStudyDescription(event.target.value)} maxLength={5000} placeholder="Technology scope, date range, jurisdictions, and limitations…" /></label>
            <button className={primaryButton} disabled={busyKey === "create-study" || !studyTitle.trim()} type="submit">{busyKey === "create-study" ? "Creating…" : "Create study"}</button>
          </form>
        </article>

        <article className="ps-panel">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div><h2 className="text-sm font-bold tracking-tight text-slate-950">Study workspace</h2><p className="mt-1 text-xs text-slate-500">Select one tenant-owned study to review its snapshot and analytics.</p></div>
            <span className={`rounded-full px-2.5 py-1.5 text-[11px] font-semibold ${study?.status === "READY" ? "bg-emerald-100 text-emerald-800" : study?.status === "FAILED" ? "bg-rose-100 text-rose-800" : study?.status === "BUILDING" ? "bg-amber-100 text-amber-800" : "bg-slate-100 text-slate-700"}`}>{study?.status || (loading ? "Loading" : "No study")}</span>
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
            <select className={inputClass} value={selectedStudyId} onChange={(event) => setSelectedStudyId(event.target.value)} aria-label="Selected landscape study">
              <option value="">Select a study…</option>
              {studies.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
            </select>
            <button className={primaryButton} type="button" disabled={!study || !studyPublications.length || ["BUILDING", "ARCHIVED"].includes(study.status) || busyKey === "build"} onClick={() => void buildLandscape()}>{busyKey === "build" ? "Queueing…" : study?.status === "READY" ? "Rebuild analysis" : "Build landscape"}</button>
          </div>
          {study && <div className="mt-4 grid gap-3 sm:grid-cols-3"><div className="rounded-lg bg-slate-50 p-3"><p className="text-[11px] uppercase tracking-wide text-slate-500">Pinned publications</p><p className="mt-1 text-lg font-semibold text-slate-900">{countLabel(study.publication_count)}</p></div><div className="rounded-lg bg-slate-50 p-3"><p className="text-[11px] uppercase tracking-wide text-slate-500">Clusters</p><p className="mt-1 text-lg font-semibold text-slate-900">{countLabel(study.cluster_count)}</p></div><div className="rounded-lg bg-slate-50 p-3"><p className="text-[11px] uppercase tracking-wide text-slate-500">Last build</p><p className="mt-1 text-sm font-semibold text-slate-900">{dateLabel(study.completed_at)}</p></div></div>}
          {study?.error_code && <p className="mt-3 rounded-lg border border-rose-100 bg-rose-50 px-3 py-2 text-xs text-rose-800">Build state: {study.error_code.replaceAll("_", " ")}. Inspect worker logs and retry after correcting the deployment issue.</p>}
        </article>
      </section>

      <section className="ps-panel">
        <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 className="text-sm font-bold tracking-tight text-slate-950">Add synchronized publications</h2><p className="mt-1 text-xs leading-5 text-slate-500">Searches the existing canonical local database only. It does not trigger a provider request or add outside data.</p></div><span className="rounded-lg bg-slate-100 px-2.5 py-1.5 text-[11px] font-medium text-slate-600">Up to 500 per request</span></div>
        <form onSubmit={searchPublications} className="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_130px_auto]">
          <input className={inputClass} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Publication number, title, or abstract keyword" maxLength={200} />
          <input className={inputClass} value={jurisdiction} onChange={(event) => setJurisdiction(event.target.value)} placeholder="Jurisdiction" maxLength={10} />
          <button type="submit" className={secondaryButton} disabled={busyKey === "search"}>{busyKey === "search" ? "Searching…" : "Search records"}</button>
        </form>
        {searchResults.length > 0 && <div className="mt-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-3"><p className="text-xs font-semibold text-slate-600">{searchResults.length} result(s) shown</p><button type="button" onClick={() => void addPublications()} className={primaryButton} disabled={!selectedStudyId || selectedPublicationIds.length === 0 || busyKey === "add-publications"}>{busyKey === "add-publications" ? "Adding…" : `Add ${selectedPublicationIds.length || "selected"} to study`}</button></div>
          <div className="max-h-[26rem] space-y-2 overflow-y-auto pr-1">
            {searchResults.map((publication) => <label key={publication.id} className={`flex cursor-pointer gap-3 rounded-xl border p-3 transition ${selectedPublicationIds.includes(publication.id) ? "border-indigo-300 bg-indigo-50" : "border-slate-200 hover:bg-slate-50"}`}>
              <input className="mt-1 h-4 w-4 accent-indigo-700" type="checkbox" checked={selectedPublicationIds.includes(publication.id)} onChange={() => togglePublication(publication.id)} disabled={!selectedStudyId} />
              <span className="min-w-0 flex-1"><span className="flex flex-wrap items-center gap-2"><span className="text-xs font-semibold text-indigo-700">{publication.publication_number || publication.provider_record_id}</span><span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">{publication.jurisdiction}</span><span className="text-[11px] text-slate-500">Filed {dateLabel(publication.filing_date)}</span></span><span className="mt-1 block text-sm font-medium leading-5 text-slate-900">{publication.title}</span><span className="mt-1 block line-clamp-2 text-xs leading-5 text-slate-500">{publication.applicant_names?.join(" · ") || "Applicant not available"}{publication.cpc_codes?.length ? ` · CPC ${publication.cpc_codes.slice(0, 3).join(", ")}` : ""}</span></span>
            </label>)}
          </div>
        </div>}
      </section>

      {analytics && <>
        {analytics.coverage.analytics_truncated && <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-5 text-amber-900" role="status">This dashboard summarizes {countLabel(analytics.coverage.analyzed_publication_count)} of {countLabel(analytics.coverage.publication_count)} selected publications because the analytics response is capped at 1,000 rows. Coverage and distribution charts are partial; treat them accordingly.</div>}
        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <StatCard label="Selected publications" value={analytics.coverage.publication_count} detail="Pinned source snapshots" />
          <StatCard label="Patent families" value={analytics.coverage.family_count} detail="Where a family identifier exists" />
          <StatCard label="Jurisdictions" value={analytics.coverage.jurisdiction_count} detail="Represented in this dataset" />
          <StatCard label="Normalized assignees" value={analytics.coverage.assignee_count} detail="Tenant-scoped identities" />
          <StatCard label="Technology nodes" value={analytics.coverage.technology_code_count} detail="Observed IPC/CPC taxonomy codes" />
        </section>

        <section className="grid gap-5 xl:grid-cols-[minmax(0,1.25fr)_minmax(300px,0.75fr)]">
          <EmbeddingMap points={points} clusters={clusters} />
          <section className="ps-panel">
            <h3 className="text-sm font-bold tracking-tight text-slate-950">Filing timeline</h3>
            <p className="mt-1 text-xs text-slate-500">Counts by filing year from selected records; undated records are shown separately.</p>
            {timeline.length === 0 ? <p className="mt-4 text-sm text-slate-500">No filing-date data available.</p> : <div className="mt-5 space-y-3">
              {timeline.map((item) => <div key={item.year} className="grid grid-cols-[70px_minmax(0,1fr)_42px] items-center gap-2 text-xs"><span className="text-slate-600">{item.year}</span><div className="h-3 overflow-hidden rounded-full bg-slate-100"><div className="h-full rounded-full bg-cyan-600" style={{ width: `${Math.max(2, (item.count / topTimeline) * 100)}%` }} /></div><span className="text-right tabular-nums text-slate-500">{item.count}</span></div>)}
            </div>}
          </section>
        </section>

        <section className="grid gap-5 lg:grid-cols-2 xl:grid-cols-4">
          <DistributionBars title="Geographic coverage" items={(analytics.geographic_coverage || []).map((item) => ({ ...item, label: item.jurisdiction }))} nameKey="label" />
          <DistributionBars title="Top normalized assignees" items={(analytics.top_assignees || []).map((item) => ({ ...item, label: item.name }))} nameKey="label" />
          <DistributionBars title="Technology classification codes" items={(analytics.technology_distribution || []).map((item) => ({ ...item, label: `${item.source} · ${item.code}` }))} nameKey="label" />
          <DistributionBars title="Patent family aggregation" items={(analytics.family_aggregation || []).map((item) => ({ ...item, count: item.publication_count, label: `${item.family_system}:${item.family_identifier}` }))} nameKey="label" emptyLabel="No provider family identifiers exist in the selected records." />
        </section>

        <section className="grid gap-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
          <article className="ps-panel">
            <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-bold tracking-tight text-slate-950">Assignee identity review</h3><span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] text-slate-600">{assignees.length} entities shown</span></div>
            <p className="mt-1 text-xs leading-5 text-slate-500">Normalization is exact and tenant-scoped. Confirm identities or flag records that must not be merged; ambiguous organizations are never fuzzy-merged automatically.</p>
            <div className="mt-4 space-y-3">
              <label className="block"><span className="mb-1.5 block text-xs font-semibold text-slate-600">Assignee entity</span><select className={inputClass} value={selectedAssigneeId} onChange={(event) => setSelectedAssigneeId(event.target.value)}><option value="">Select an assignee…</option>{assignees.map((entity) => <option key={entity.id} value={entity.id}>{entity.canonical_name} · {entity.status.replaceAll("_", " ")}</option>)}</select></label>
              {assignees.find((entity) => entity.id === selectedAssigneeId) && <div className="rounded-lg bg-slate-50 p-3"><p className="text-xs font-semibold text-slate-800">Normalized key</p><p className="mt-1 break-words font-mono text-[11px] text-slate-600">{assignees.find((entity) => entity.id === selectedAssigneeId)?.normalized_key}</p><p className="mt-2 text-xs text-slate-500">Aliases: {assignees.find((entity) => entity.id === selectedAssigneeId)?.alias_count ?? 0}</p></div>}
              <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]"><select className={inputClass} value={assigneeStatus} onChange={(event) => setAssigneeStatus(event.target.value)}><option value="UNREVIEWED">Unreviewed</option><option value="CONFIRMED">Confirmed</option><option value="DO_NOT_MERGE">Do not merge</option></select><button type="button" className={secondaryButton} onClick={() => void updateAssigneeStatus()} disabled={!selectedAssigneeId || busyKey === "assignee-status"}>{busyKey === "assignee-status" ? "Saving…" : "Save decision"}</button></div>
              <form onSubmit={addAssigneeAlias} className="flex flex-col gap-2 sm:flex-row"><input className={inputClass} value={aliasName} onChange={(event) => setAliasName(event.target.value)} maxLength={400} placeholder="Alternate spelling / source assignee name" /><button type="submit" className={secondaryButton} disabled={!selectedAssigneeId || !aliasName.trim() || busyKey === "assignee-alias"}>{busyKey === "assignee-alias" ? "Adding…" : "Add alias"}</button></form>
            </div>
          </article>
          <article className="ps-panel"><h3 className="text-sm font-bold tracking-tight text-slate-950">Study publication snapshot</h3><p className="mt-1 text-xs leading-5 text-slate-500">The study stores the bibliographic fields used during analysis, its source payload hash, and a snapshot SHA-256 digest. Later provider syncs do not silently rewrite these study rows.</p><div className="mt-4 max-h-72 space-y-2 overflow-y-auto">{studyPublications.length === 0 ? <p className="text-sm text-slate-500">No publications added to this study.</p> : studyPublications.map((publication) => <article key={publication.id} className="rounded-lg border border-slate-200 p-3"><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs font-semibold text-indigo-700">{publication.publication_number || publication.publication}</span><span className="text-[10px] text-slate-500">{publication.jurisdiction} · {dateLabel(publication.filing_date)}</span></div><p className="mt-1 text-sm font-medium text-slate-900">{publication.title}</p><p className="mt-2 break-all font-mono text-[10px] text-slate-400">Snapshot {publication.snapshot_sha256}</p>{safeHttpsUrl(publication.source_url) && <a className="mt-2 inline-flex text-xs font-semibold text-indigo-700 underline underline-offset-2" href={safeHttpsUrl(publication.source_url)} target="_blank" rel="noreferrer">Open provider source</a>}</article>)}</div></article>
        </section>

        <section className="ps-panel">
          <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-bold tracking-tight text-slate-950">Technology clusters</h3><p className="mt-1 text-xs leading-5 text-slate-500">Cluster labels are generated from source classification prefixes and title terms. Use the evidence links and cluster membership to interpret them.</p></div><span className="rounded-lg bg-amber-50 px-2.5 py-1.5 text-[11px] font-semibold text-amber-800">Discovery aid only</span></div>
          {clusters.length === 0 ? <p className="mt-4 rounded-lg bg-slate-50 px-3 py-4 text-sm text-slate-500">No clusters yet. Select publications and build the landscape.</p> : <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">{clusters.map((cluster) => <article key={cluster.id} className="rounded-xl border border-slate-200 p-4"><div className="flex items-start justify-between gap-3"><h4 className="text-sm font-bold tracking-tight text-slate-950">{cluster.label}</h4><span className="rounded-md bg-slate-100 px-2 py-1 text-[11px] text-slate-600">{cluster.publication_count} docs</span></div><p className="mt-2 text-xs leading-5 text-slate-600">{cluster.explanation}</p>{cluster.top_classification_codes?.length > 0 && <p className="mt-3 text-[11px] text-slate-500">Classes: {cluster.top_classification_codes.join(", ")}</p>}{cluster.top_terms?.length > 0 && <div className="mt-2 flex flex-wrap gap-1.5">{cluster.top_terms.map((term) => <span key={term} className="rounded bg-indigo-50 px-2 py-1 text-[10px] text-indigo-700">{term}</span>)}</div>}</article>)}</div>}
        </section>
      </>}

      <section className="grid gap-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
        <article className="ps-panel">
          <h3 className="text-sm font-bold tracking-tight text-slate-950">Attorney / analyst annotation</h3>
          <p className="mt-1 text-xs leading-5 text-slate-500">Record scope limitations, entity-normalization decisions, taxonomy interpretation, or questions raised by a cluster.</p>
          <form className="mt-4 space-y-3" onSubmit={createAnnotation}>
            <select className={inputClass} value={annotationKind} onChange={(event) => setAnnotationKind(event.target.value)}><option value="OVERVIEW">Study overview</option><option value="ASSIGNEE">Assignee normalization</option><option value="TAXONOMY">Technology taxonomy</option><option value="CLUSTER">Cluster interpretation</option><option value="DATA_QUALITY">Data quality / coverage limitation</option></select>
            <select className={inputClass} value={annotationPublicationId} onChange={(event) => setAnnotationPublicationId(event.target.value)}><option value="">Study-level annotation</option>{studyPublications.map((item) => <option key={item.id} value={item.id}>{item.publication_number || item.title.slice(0, 70)}</option>)}</select>
            <textarea className={`${inputClass} min-h-28 resize-y`} value={annotationBody} onChange={(event) => setAnnotationBody(event.target.value)} maxLength={12000} placeholder="Document an interpretation, uncertainty, or follow-up task…" required />
            <button className={primaryButton} type="submit" disabled={!selectedStudyId || !annotationBody.trim() || busyKey === "annotation"}>{busyKey === "annotation" ? "Saving…" : "Save annotation"}</button>
          </form>
        </article>
        <article className="ps-panel">
          <div className="flex items-center justify-between gap-3"><h3 className="text-sm font-bold tracking-tight text-slate-950">Annotation log</h3><span className="rounded-full bg-slate-100 px-2.5 py-1 text-[11px] text-slate-600">{annotations.length} shown</span></div>
          {!selectedStudyId || annotations.length === 0 ? <p className="mt-4 rounded-lg bg-slate-50 px-3 py-5 text-sm text-slate-500">No annotations recorded for this study yet.</p> : <div className="mt-4 max-h-[34rem] space-y-3 overflow-y-auto pr-1">{annotations.map((annotation) => <article key={annotation.id} className="rounded-xl border border-slate-200 p-3"><div className="flex flex-wrap items-center justify-between gap-2"><div className="flex flex-wrap items-center gap-2"><span className="rounded bg-indigo-50 px-2 py-1 text-[10px] font-semibold text-indigo-700">{annotation.kind.replaceAll("_", " ")}</span><span className={`rounded px-2 py-1 text-[10px] font-semibold ${annotation.status === "RESOLVED" ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-800"}`}>{annotation.status}</span></div><button className={secondaryButton} type="button" disabled={busyKey === `resolve-${annotation.id}`} onClick={() => void resolveAnnotation(annotation)}>{annotation.status === "RESOLVED" ? "Reopen" : "Resolve"}</button></div><p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-700">{annotation.body}</p><p className="mt-2 text-[11px] text-slate-400">{annotation.creator} · {new Date(annotation.created_at).toLocaleString()}</p></article>)}</div>}
        </article>
      </section>

      <section className="rounded-2xl border border-amber-200 bg-amber-50 p-4 sm:p-5">
        <h3 className="text-sm font-semibold text-amber-950">Method and coverage limitations</h3>
        <p className="mt-1 text-xs leading-5 text-amber-900">Analytics cover only publications already synchronized into this PatentShield instance and manually selected for this study. Missing sources, jurisdictions, families, assignee aliases, or classification metadata can materially change results. Low density or a small cluster is not proof that a technology area is unpatented. Cluster membership and white-space indicators are exploratory outputs, not legal opinions, freedom-to-operate conclusions, or proof of absence of patent coverage.</p>
      </section>
    </main>
  );
}
