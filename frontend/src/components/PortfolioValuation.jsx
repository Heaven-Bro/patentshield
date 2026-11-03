import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiRequest, requireResultsArray } from "../lib/apiClient";

const API_ROOT = "/api/v1/valuation";
const INPUT = "ps-input";
const BUTTON = "ps-button-primary";
const SECONDARY = "ps-button-secondary";

function errorText(error) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "The request could not be completed.";
}

function numberLabel(value) {
  return new Intl.NumberFormat().format(Number(value || 0));
}

function assumptionsExample(method) {
  if (method === "COST") return JSON.stringify({ replacement_cost: "100000", obsolescence_rate: "0.20", documented_additional_costs: "0", basis_note: "Replace with documented engineering estimate and its source." }, null, 2);
  if (method === "MARKET") return JSON.stringify({ comparables: [{ label: "Comparable transaction A", transaction_value: "500000", weight: "1", adjustment_rate: "0", evidence_reference: "Enter a licensed transaction database ID or source record", source_url: "", transaction_date: "", notes: "" }] }, null, 2);
  return JSON.stringify({ annual_revenue: "1000000", royalty_rate: "0.03", annual_revenue_growth_rate: "0.04", discount_rate: "0.12", tax_rate: "0.25", projection_years: 5, include_terminal_value: false, terminal_growth_rate: "0.02", basis_note: "Replace every input with approved, documented assumptions." }, null, 2);
}

function Section({ title, description, children }) {
  return (
    <section className="ps-panel">
      <div className="mb-4">
        <h2 className="ps-panel-title">{title}</h2>
        {description && <p className="ps-panel-description">{description}</p>}
      </div>
      {children}
    </section>
  );
}

export default function PortfolioValuation({ tenant }) {
  const request = useCallback((path, options = {}) => apiRequest(path, { tenantId: tenant.id, ...options }), [tenant.id]);
  const canManagePortfolio = ["OWNER", "ADMIN", "ATTORNEY", "ANALYST"].includes(tenant?.role);
  const canAddReviewAnnotation = ["OWNER", "ADMIN", "ATTORNEY", "ANALYST", "REVIEWER"].includes(tenant?.role);
  const [studies, setStudies] = useState([]);
  const [studyId, setStudyId] = useState("");
  const [study, setStudy] = useState(null);
  const [assets, setAssets] = useState([]);
  const [assetTotalCount, setAssetTotalCount] = useState(0);
  const [assetsTruncated, setAssetsTruncated] = useState(false);
  const [scenarios, setScenarios] = useState([]);
  const [diligenceRows, setDiligenceRows] = useState([]);
  const [annotations, setAnnotations] = useState([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState([]);
  const [selectedPublicationIds, setSelectedPublicationIds] = useState([]);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [currency, setCurrency] = useState("USD");
  const [scenarioName, setScenarioName] = useState("Base case");
  const [scenarioMethod, setScenarioMethod] = useState("INCOME");
  const [revisionTargetId, setRevisionTargetId] = useState("");
  const [assumptionsText, setAssumptionsText] = useState(() => assumptionsExample("INCOME"));
  const [annotationText, setAnnotationText] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedStudy = useMemo(() => studies.find((row) => row.id === studyId) || null, [studies, studyId]);
  const latestScenarioMetrics = useMemo(() => ["INCOME", "MARKET", "COST"].map((method) => {
    const candidates = scenarios
      .filter((scenario) => scenario.method === method && scenario.latest_revision?.calculated_result)
      .sort((left, right) => {
        const leftDate = Date.parse(left.latest_revision?.created_at || left.updated_at || left.created_at || "") || 0;
        const rightDate = Date.parse(right.latest_revision?.created_at || right.updated_at || right.created_at || "") || 0;
        return rightDate - leftDate;
      });
    return { method, scenario: candidates[0] || null, result: candidates[0]?.latest_revision?.calculated_result || null };
  }), [scenarios]);

  const loadStudies = useCallback(async (signal) => {
    const payload = await request(`${API_ROOT}/studies/`, { signal });
    if (signal?.aborted) return;
    const rows = requireResultsArray(payload, "valuation study list");
    setStudies(rows);
    setStudyId((current) => rows.some((row) => row.id === current) ? current : rows[0]?.id || "");
  }, [request]);

  const loadStudyData = useCallback(async (signal) => {
    if (!studyId) {
      setStudy(null); setAssets([]); setAssetTotalCount(0); setAssetsTruncated(false); setScenarios([]); setDiligenceRows([]); setAnnotations([]);
      return;
    }
    const [studyPayload, assetPayload, scenarioPayload, diligencePayload, annotationPayload] = await Promise.all([
      request(`${API_ROOT}/studies/${studyId}/`, { signal }),
      request(`${API_ROOT}/studies/${studyId}/assets/`, { signal }),
      request(`${API_ROOT}/studies/${studyId}/scenarios/`, { signal }),
      request(`${API_ROOT}/studies/${studyId}/diligence/`, { signal }),
      request(`${API_ROOT}/studies/${studyId}/annotations/`, { signal }),
    ]);
    if (signal?.aborted) return;
    setStudy(studyPayload);
    const assetRows = requireResultsArray(assetPayload, "valuation asset list");
    const scenarioRows = requireResultsArray(scenarioPayload, "valuation scenario list");
    const diligenceList = requireResultsArray(diligencePayload, "diligence list");
    const annotationList = requireResultsArray(annotationPayload, "valuation annotation list");
    setAssets(assetRows);
    setAssetTotalCount(Number(assetPayload.count ?? assetRows.length));
    setAssetsTruncated(Boolean(assetPayload.truncated));
    setScenarios(scenarioRows);
    setDiligenceRows(diligenceList);
    setAnnotations(annotationList);
  }, [request, studyId]);

  useEffect(() => {
    const controller = new AbortController();
    loadStudies(controller.signal).catch((loadError) => {
      if (!controller.signal.aborted) setError(errorText(loadError));
    });
    return () => controller.abort();
  }, [loadStudies]);

  useEffect(() => {
    const controller = new AbortController();
    loadStudyData(controller.signal).catch((loadError) => {
      if (!controller.signal.aborted) setError(errorText(loadError));
    });
    return () => controller.abort();
  }, [loadStudyData]);

  async function runAction(key, callback, successMessage) {
    setBusy(key); setError(""); setNotice("");
    try {
      await callback();
      if (successMessage) setNotice(successMessage);
      const controller = new AbortController();
      await loadStudies(controller.signal);
      if (studyId) await loadStudyData(controller.signal);
    } catch (actionError) {
      setError(errorText(actionError));
    } finally {
      setBusy("");
    }
  }

  async function createStudy(event) {
    event.preventDefault();
    await runAction("create-study", async () => {
      const payload = await request(`${API_ROOT}/studies/`, { method: "POST", body: { title, description, currency } });
      setStudies((rows) => [payload, ...rows]);
      setStudy(null); setAssets([]); setAssetTotalCount(0); setAssetsTruncated(false); setScenarios([]); setDiligenceRows([]); setAnnotations([]);
      setStudyId(payload.id); setTitle(""); setDescription("");
    }, "Valuation study created.");
  }

  async function searchPublications(event) {
    event.preventDefault();
    await runAction("search", async () => {
      const url = `/api/v1/landscapes/publications/search/?q=${encodeURIComponent(searchQuery)}&limit=50`;
      const payload = await request(url);
      setSearchResults(requireResultsArray(payload, "publication search"));
      setSelectedPublicationIds([]);
    });
  }

  async function addAssets() {
    if (!studyId || selectedPublicationIds.length === 0) return;
    await runAction("add-assets", async () => {
      await request(`${API_ROOT}/studies/${studyId}/assets/`, { method: "POST", body: { publication_ids: selectedPublicationIds } });
      setSelectedPublicationIds([]);
    }, "Selected publications pinned to this study.");
  }

  async function createDiligenceSnapshot() {
    await runAction("diligence", async () => {
      await request(`${API_ROOT}/studies/${studyId}/diligence/`, { method: "POST", body: {} });
    }, "Diligence indicators calculated and saved as an immutable snapshot.");
  }

  async function createScenario(event) {
    event.preventDefault();
    let assumptions;
    try { assumptions = JSON.parse(assumptionsText); } catch { setError("Assumptions must be valid JSON."); return; }
    await runAction("scenario", async () => {
      await request(`${API_ROOT}/studies/${studyId}/scenarios/`, { method: "POST", body: { name: scenarioName, method: scenarioMethod, assumptions } });
      setRevisionTargetId("");
    }, "Scenario calculated and its assumptions/result revision pinned.");
  }

  async function reviseScenario() {
    if (!revisionTargetId) return;
    let assumptions;
    try { assumptions = JSON.parse(assumptionsText); } catch { setError("Assumptions must be valid JSON."); return; }
    await runAction("scenario-revision", async () => {
      await request(`${API_ROOT}/scenarios/${revisionTargetId}/revisions/`, { method: "POST", body: { assumptions } });
    }, "A new immutable revision has been saved against the current asset manifest.");
  }

  function selectRevisionTarget(value) {
    setRevisionTargetId(value);
    const target = scenarios.find((row) => row.id === value);
    if (target) {
      setScenarioMethod(target.method);
      setScenarioName(target.name);
      setAssumptionsText(JSON.stringify(target.latest_revision?.assumptions || {}, null, 2));
    }
  }

  async function addAnnotation(event) {
    event.preventDefault();
    await runAction("annotation", async () => {
      await request(`${API_ROOT}/studies/${studyId}/annotations/`, { method: "POST", body: { kind: "DILIGENCE", body: annotationText } });
      setAnnotationText("");
    }, "Annotation saved.");
  }

  return (
    <main className="ps-page">
      <header className="ps-hero">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-indigo-200">Phase 10 · Portfolio intelligence</p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">Portfolio Valuation & Diligence</h1>
        <p className="mt-3 max-w-3xl text-sm leading-6 text-indigo-100">Evidence-pinned portfolio composition indicators and transparent cost, income, and market scenarios. Every scenario stores its assumptions, methodology version, source-asset manifest, and reproducible result.</p>
      </header>

      {error && <div role="alert" className="ps-alert-error">{error}</div>}
      {notice && <div role="status" className="ps-alert-success">{notice}</div>}
      {!canManagePortfolio && <div role="status" className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-950">View-only portfolio access. Study, asset, scenario and diligence changes are unavailable to your current tenant role; server-side matter permissions still apply.</div>}

      <div className="grid gap-6 lg:grid-cols-[minmax(280px,0.85fr)_minmax(0,1.5fr)]">
        <Section title="Create a diligence study" description="Currency is fixed after the first scenario revision to prevent accidental currency mixing.">
          <form onSubmit={createStudy} className="space-y-3">
            <label className="block text-xs font-semibold text-slate-600">Study title<input required value={title} onChange={(event) => setTitle(event.target.value)} className={`${INPUT} mt-1`} placeholder="Example: Connected device portfolio" /></label>
            <label className="block text-xs font-semibold text-slate-600">Description<textarea value={description} onChange={(event) => setDescription(event.target.value)} className={`${INPUT} mt-1 min-h-20`} placeholder="Scope, decision context, and source limitations" /></label>
            <label className="block text-xs font-semibold text-slate-600">Currency<select value={currency} onChange={(event) => setCurrency(event.target.value)} className={`${INPUT} mt-1`}><option>USD</option><option>EUR</option><option>GBP</option><option>JPY</option><option>BDT</option><option>CHF</option></select></label>
            <button className={BUTTON} disabled={!canManagePortfolio || Boolean(busy)} type="submit">{busy === "create-study" ? "Creating…" : "Create study"}</button>
          </form>
          <div className="mt-6 border-t border-slate-200 pt-5">
            <label className="block text-xs font-semibold text-slate-600">Active study<select value={studyId} onChange={(event) => { setStudy(null); setAssets([]); setAssetTotalCount(0); setAssetsTruncated(false); setScenarios([]); setDiligenceRows([]); setAnnotations([]); setStudyId(event.target.value); }} className={`${INPUT} mt-1`}><option value="">Select a study</option>{studies.map((row) => <option key={row.id} value={row.id}>{row.title} · {row.currency}</option>)}</select></label>
            {selectedStudy && <div className="mt-3 grid grid-cols-2 gap-2"><div className="rounded-lg bg-slate-50 p-3"><p className="text-[11px] text-slate-500">Pinned assets</p><p className="mt-1 text-xl font-semibold">{numberLabel(selectedStudy.asset_count)}</p></div><div className="rounded-lg bg-slate-50 p-3"><p className="text-[11px] text-slate-500">Scenario methods</p><p className="mt-1 text-xl font-semibold">{numberLabel(selectedStudy.scenario_count)}</p></div></div>}
          </div>
        </Section>

        <Section title="Select source publications" description="Searches only PatentShield's locally synchronized canonical publication table. Selecting a publication pins its latest available provider snapshot; no source count is invented if it is missing.">
          {!studyId ? <p className="text-sm text-slate-500">Create or select a study to start.</p> : <>
            <form onSubmit={searchPublications} className="flex flex-col gap-2 sm:flex-row">
              <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} className={INPUT} placeholder="Title, abstract or publication number" />
              <button className={SECONDARY} disabled={Boolean(busy)} type="submit">{busy === "search" ? "Searching…" : "Search local records"}</button>
            </form>
            {searchResults.length > 0 && <>
              <div className="mt-3 max-h-72 space-y-2 overflow-y-auto">{searchResults.map((row) => <label key={row.id} className="flex gap-3 rounded-xl border border-slate-200 p-3 hover:bg-slate-50"><input type="checkbox" className="mt-1" checked={selectedPublicationIds.includes(row.id)} onChange={(event) => setSelectedPublicationIds((current) => event.target.checked ? [...current, row.id] : current.filter((id) => id !== row.id))} /><span className="min-w-0"><span className="block text-xs font-semibold text-slate-800">{row.publication_number || row.provider_record_id} · {row.jurisdiction}</span><span className="mt-1 block text-sm text-slate-700">{row.title}</span><span className="mt-1 block text-[11px] text-slate-500">Status: {row.legal_status} · Family: {row.family_identifier || "unknown"}</span></span></label>)}</div>
              <button type="button" className={`${BUTTON} mt-3`} disabled={!canManagePortfolio || Boolean(busy) || selectedPublicationIds.length === 0} onClick={() => void addAssets()}>Pin selected ({selectedPublicationIds.length})</button>
            </>}
          </>}
        </Section>
      </div>

      {studyId && <>
        <section className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <div className="ps-metric-card"><p className="ps-metric-label">Composition indicator</p><p className="ps-metric-value">{study?.latest_diligence_snapshot?.composition_score ?? "—"}<span className="ml-1 text-sm font-medium text-slate-400">/100</span></p><p className="mt-1 text-[11px] text-slate-500">{study?.latest_diligence_snapshot?.is_current_for_current_assets === false ? "Snapshot is stale after asset changes; recalculate." : "Not market value or legal strength."}</p></div>
          <div className="ps-metric-card"><p className="ps-metric-label">Known families</p><p className="ps-metric-value">{numberLabel(study?.latest_diligence_snapshot?.metrics?.family_metrics?.distinct_known_families)}</p><p className="mt-1 text-[11px] text-slate-500">Provider-scoped family IDs only.</p></div>
          <div className="ps-metric-card"><p className="ps-metric-label">Forward citation coverage</p><p className="ps-metric-value">{study?.latest_diligence_snapshot?.metrics?.citation_metrics?.citation_coverage_pct_forward ?? "—"}<span className="ml-1 text-sm font-medium text-slate-400">%</span></p><p className="mt-1 text-[11px] text-slate-500">Missing citation data is unknown.</p></div>
          <div className="ps-metric-card"><p className="ps-metric-label">Old provider syncs</p><p className="ps-metric-value">{numberLabel(study?.latest_diligence_snapshot?.metrics?.freshness_metrics?.assets_with_source_older_than_180_days)}</p><p className="mt-1 text-[11px] text-slate-500">Older than the declared 180-day flag.</p></div>
        </section>

        <section className="grid gap-4 md:grid-cols-3" aria-label="Latest valuation scenario indicators">
          {latestScenarioMetrics.map(({ method, scenario, result }) => {
            const accent = method === "INCOME" ? "border-indigo-200 before:bg-indigo-500" : method === "MARKET" ? "border-sky-200 before:bg-sky-500" : "border-emerald-200 before:bg-emerald-500";
            return (
              <article key={method} className={`relative overflow-hidden rounded-2xl border bg-white p-5 shadow-sm shadow-slate-900/[0.03] transition duration-200 before:absolute before:inset-y-0 before:left-0 before:w-1 hover:-translate-y-0.5 hover:shadow-lg ${accent}`}>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="ps-metric-label">{method} approach</p>
                    <p className="mt-1 text-xs text-slate-500">{scenario?.name || "No saved scenario"}</p>
                  </div>
                  <span className={`rounded-lg px-2 py-1 text-[10px] font-bold uppercase tracking-wide ${method === "INCOME" ? "bg-indigo-50 text-indigo-700" : method === "MARKET" ? "bg-sky-50 text-sky-700" : "bg-emerald-50 text-emerald-700"}`}>{method}</span>
                </div>
                <p className="mt-4 break-words text-2xl font-bold tracking-tight text-slate-950 tabular-nums sm:text-3xl">{result?.indicated_value != null ? `${scenario?.latest_revision?.currency || study?.currency || currency} ${Number(result.indicated_value).toLocaleString()}` : "Not calculated"}</p>
                <p className="mt-2 line-clamp-2 text-xs leading-5 text-slate-500">{result?.calculation || "Create and save a versioned scenario to display this method's latest indication."}</p>
                {scenario?.latest_revision?.is_current_for_current_assets === false && <p className="mt-3 inline-flex rounded-full bg-amber-50 px-2.5 py-1 text-[10px] font-semibold text-amber-800">Asset snapshot is stale</p>}
              </article>
            );
          })}
        </section>

        <Section title="Pinned portfolio assets" description="Legal-status and citation indicators are source-derived snapshots. Confirm current status in the relevant patent office before relying on them.">
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3"><p className="text-sm text-slate-600">{numberLabel(assetTotalCount)} assets in study · showing {numberLabel(assets.length)}{assetsTruncated ? " (first 1,000)" : ""} · {study?.currency}</p><button className={BUTTON} disabled={!canManagePortfolio || Boolean(busy) || assetTotalCount === 0} onClick={() => void createDiligenceSnapshot()}>{busy === "diligence" ? "Calculating…" : "Calculate diligence indicators"}</button></div>
          {assetsTruncated && <p className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">The table is capped at 1,000 rows. The saved diligence calculation uses the full selected portfolio; use the manifest hash to identify its exact input set.</p>}
          {assets.length === 0 ? <p className="rounded-lg border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500">No publications pinned yet. Search and select local records above.</p> : <div className="ps-table-wrap"><table className="ps-table text-xs"><thead className="sticky top-0 z-10 bg-slate-900 text-slate-200"><tr><th className="px-4 py-3.5">Publication</th><th className="px-4 py-3.5">Status</th><th className="px-4 py-3.5">Family</th><th className="px-4 py-3.5">Forward cites</th><th className="px-4 py-3.5">Backward cites</th><th className="px-4 py-3.5">Latest observed legal event</th><th className="px-4 py-3.5">Last sync</th></tr></thead><tbody>{assets.map((asset) => <tr key={asset.id} className="border-t border-slate-100 transition-colors hover:bg-indigo-50/40"><td className="max-w-md px-4 py-3.5"><span className="block font-semibold text-slate-800">{asset.publication_number || asset.application_number || "Identifier unavailable"}</span><span className="mt-1 block text-slate-500">{asset.title}</span></td><td className="px-4 py-3.5">{asset.legal_status}</td><td className="px-4 py-3.5">{asset.family_identifier || "Unknown"}</td><td className="px-4 py-3.5">{asset.forward_citation_count ?? "Unknown"}</td><td className="px-4 py-3.5">{asset.backward_citation_count ?? "Unknown"}</td><td className="max-w-xs px-4 py-3.5">{asset.latest_legal_event && Object.keys(asset.latest_legal_event).length > 0 ? <><span className="block font-medium">{asset.latest_legal_event.event_code || "Event"} · {asset.latest_legal_event.event_date || "Date unknown"}</span><span className="mt-1 block text-slate-500">{asset.latest_legal_event.title || "No event title supplied"}</span></> : "No event observed in synchronized records"}</td><td className="px-4 py-3.5">{asset.source_last_synced_at ? String(asset.source_last_synced_at).slice(0, 10) : "Unknown"}</td></tr>)}</tbody></table></div>}
        </Section>

        <div className="grid gap-6 lg:grid-cols-2">
          <Section title="Create a valuation scenario" description="Examples are illustrative only. Replace every example assumption with approved and referenced information before using any result.">
            <form onSubmit={createScenario} className="space-y-3">
              <label className="block text-xs font-semibold text-slate-600">Revise an existing scenario (optional)<select value={revisionTargetId} onChange={(event) => selectRevisionTarget(event.target.value)} className={`${INPUT} mt-1`}><option value="">Create a new scenario instead</option>{scenarios.map((scenario) => <option key={scenario.id} value={scenario.id}>{scenario.name} · {scenario.method} · r{scenario.latest_revision?.revision_number ?? 0}</option>)}</select></label>
              <label className="block text-xs font-semibold text-slate-600">Scenario name<input value={scenarioName} onChange={(event) => setScenarioName(event.target.value)} className={`${INPUT} mt-1`} required maxLength={160} /></label>
              <label className="block text-xs font-semibold text-slate-600">Approach<select disabled={Boolean(revisionTargetId)} value={scenarioMethod} onChange={(event) => { setScenarioMethod(event.target.value); setAssumptionsText(assumptionsExample(event.target.value)); }} className={`${INPUT} mt-1 disabled:bg-slate-100`}><option value="INCOME">Income · relief from royalty</option><option value="MARKET">Market · comparable transactions</option><option value="COST">Cost · replacement cost</option></select></label>
              <label className="block text-xs font-semibold text-slate-600">Explicit assumptions (JSON)<textarea value={assumptionsText} onChange={(event) => setAssumptionsText(event.target.value)} className={`${INPUT} mt-1 min-h-64 font-mono text-xs`} spellCheck={false} /></label>
              <div className="flex flex-wrap gap-2">
                <button className={BUTTON} disabled={!canManagePortfolio || Boolean(busy)} type="submit">{busy === "scenario" ? "Calculating…" : "Create scenario"}</button>
                {revisionTargetId && <button className={SECONDARY} disabled={!canManagePortfolio || Boolean(busy)} type="button" onClick={() => void reviseScenario()}>{busy === "scenario-revision" ? "Saving revision…" : "Save new revision"}</button>}
              </div>
            </form>
          </Section>

          <Section title="Scenario outputs" description="Income, market, and cost indications are shown separately. Their spread is not a statistical confidence interval.">
            {scenarios.length === 0 ? <p className="text-sm text-slate-500">No scenario revisions yet.</p> : <div className="space-y-3">{scenarios.map((scenario) => <article key={scenario.id} className="rounded-xl border border-slate-200 p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="text-sm font-semibold text-slate-900">{scenario.name}</h3><p className="mt-1 text-[11px] text-slate-500">{scenario.method} · revision {scenario.latest_revision?.revision_number ?? "—"}</p></div><p className="text-lg font-semibold tabular-nums text-indigo-800">{scenario.latest_revision?.currency || study?.currency} {scenario.latest_revision?.calculated_result?.indicated_value ?? "—"}</p></div><p className="mt-2 text-[11px] text-slate-500">Inputs: {scenario.latest_revision?.input_sha256?.slice(0, 16) || "—"}… · Asset manifest: {scenario.latest_revision?.asset_manifest_sha256?.slice(0, 16) || "—"}…</p><p className="mt-2 text-xs text-slate-600">{scenario.latest_revision?.calculated_result?.calculation}</p>{scenario.latest_revision?.is_current_for_current_assets === false && <p className="mt-2 text-[11px] font-semibold text-rose-700">This revision uses an older asset manifest. Create a new revision to pin the current selected assets.</p>}<p className="mt-2 text-[11px] text-amber-700">{scenario.latest_revision?.calculated_result?.disclaimer}</p></article>)}</div>}
            {study?.latest_diligence_snapshot && <div className="mt-4 rounded-xl bg-slate-50 p-3"><p className="text-xs font-semibold text-slate-800">Latest diligence snapshot</p><p className="mt-1 text-[11px] text-slate-500">{study.latest_diligence_snapshot.created_at ? new Date(study.latest_diligence_snapshot.created_at).toLocaleString() : "Saved"} · assets SHA-256 {study.latest_diligence_snapshot.asset_manifest_sha256}</p><div className="mt-3 space-y-1">{Object.entries(study.latest_diligence_snapshot.metrics?.legal_status_counts || {}).map(([key, value]) => <p key={key} className="flex justify-between text-xs"><span className="text-slate-600">{key}</span><span className="font-semibold">{numberLabel(value)}</span></p>)}</div></div>}
          </Section>
        </div>

        <div className="grid gap-6 lg:grid-cols-2">
          <Section title="Diligence annotations" description="Record assumptions, source limitations, status checks, and open reviewer questions without changing the immutable source snapshot.">
            <form onSubmit={addAnnotation} className="space-y-3"><textarea required maxLength={12000} value={annotationText} onChange={(event) => setAnnotationText(event.target.value)} className={`${INPUT} min-h-24`} placeholder="Record a diligence note, evidence reference, or reviewer question." /><button className={BUTTON} disabled={!canAddReviewAnnotation || Boolean(busy)} type="submit">Save annotation</button>{!canAddReviewAnnotation && <p className="text-xs text-slate-500">Annotation entry requires an owner, administrator, attorney, or reviewer role.</p>}</form>
            <div className="mt-4 space-y-2">{annotations.slice(0, 20).map((item) => <article key={item.id} className="rounded-lg border border-slate-200 p-3"><p className="whitespace-pre-wrap text-sm text-slate-700">{item.body}</p><p className="mt-2 text-[11px] text-slate-500">{item.kind} · {item.created_by} · {new Date(item.created_at).toLocaleString()}</p></article>)}</div>
          </Section>
          <Section title="Calculation history" description="Diligence snapshots and scenario revisions preserve the inputs, method version, and asset manifest used at calculation time.">
            {diligenceRows.length === 0 ? <p className="text-sm text-slate-500">No diligence snapshots have been saved.</p> : <div className="space-y-2">{diligenceRows.slice(0, 12).map((row) => <div key={row.id} className="rounded-lg border border-slate-200 p-3"><div className="flex items-center justify-between gap-3"><span className="text-xs font-semibold text-slate-800">{row.methodology_version} · {row.asset_count} assets</span><span className="text-sm font-semibold">{row.composition_score ?? "—"}/100</span></div><p className="mt-1 break-all text-[10px] text-slate-500">Manifest SHA-256: {row.asset_manifest_sha256}</p><p className="mt-1 text-[11px] text-slate-500">{new Date(row.created_at).toLocaleString()}</p></div>)}</div>}
            <p className="mt-4 text-xs leading-5 text-slate-500">All scores are descriptive diligence indicators. They are not intrinsic patent strength, infringement risk, ownership verification, investment advice, or an objectively realizable portfolio price.</p>
          </Section>
        </div>
      </>}
    </main>
  );
}
