import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiRequest, downloadArtifact } from "../lib/apiClient";

const API_ROOT = "/api/v1/fto";

function displayError(error) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "The FTO request could not be completed.";
}

function bandClass(band) {
  switch (band) {
    case "HIGH_ATTENTION": return "border-rose-200 bg-rose-100 text-rose-900";
    case "MODERATE_ATTENTION": return "border-amber-200 bg-amber-100 text-amber-900";
    case "ELEVATED_ATTENTION": return "border-orange-200 bg-orange-100 text-orange-900";
    case "LOWER_ATTENTION": return "border-emerald-200 bg-emerald-100 text-emerald-900";
    default: return "border-slate-200 bg-slate-100 text-slate-700";
  }
}

function statusLabel(value) {
  return String(value || "UNASSESSED").replaceAll("_", " ").toLowerCase();
}

export default function FTOWorkspace({ tenant, patents = [] }) {
  const [matters, setMatters] = useState([]);
  const [studies, setStudies] = useState([]);
  const [versions, setVersions] = useState([]);
  const [features, setFeatures] = useState([]);
  const [heatmap, setHeatmap] = useState(null);
  const [selectedMatterId, setSelectedMatterId] = useState("");
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [selectedStudyId, setSelectedStudyId] = useState("");
  const [selectedPatentVersion, setSelectedPatentVersion] = useState("");
  const [productName, setProductName] = useState("");
  const [productCode, setProductCode] = useState("");
  const [productDescription, setProductDescription] = useState("");
  const [specFile, setSpecFile] = useState(null);
  const [featureSelections, setFeatureSelections] = useState({});
  const [evidenceKinds, setEvidenceKinds] = useState({});
  const [evidenceNotes, setEvidenceNotes] = useState({});
  const [triage, setTriage] = useState({});
  const [annotations, setAnnotations] = useState({});
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedMatter = useMemo(
    () => matters.find((item) => item.id === selectedMatterId) || null,
    [matters, selectedMatterId],
  );
  const selectedVersion = useMemo(
    () => versions.find((item) => item.id === selectedVersionId) || null,
    [versions, selectedVersionId],
  );
  const selectedPatentParts = useMemo(() => {
    if (!selectedPatentVersion) return { patentId: "", versionId: "" };
    const [patentId, versionId] = selectedPatentVersion.split("::");
    return { patentId, versionId };
  }, [selectedPatentVersion]);

  const loadMattersAndStudies = useCallback(async (signal) => {
    setLoading(true);
    setError("");
    try {
      const [matterPayload, studyPayload] = await Promise.all([
        apiRequest(`${API_ROOT}/products/`, { tenantId: tenant.id, signal }),
        apiRequest(`${API_ROOT}/studies/`, { tenantId: tenant.id, signal }),
      ]);
      if (signal?.aborted) return;
      setMatters(matterPayload.results || []);
      setStudies(studyPayload.results || []);
      setSelectedMatterId((current) => current || matterPayload.results?.[0]?.id || "");
      setSelectedStudyId((current) => current || studyPayload.results?.[0]?.id || "");
    } catch (loadError) {
      if (loadError?.name !== "AbortError" && !signal?.aborted) setError(displayError(loadError));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [tenant.id]);

  const loadVersions = useCallback(async (matterId, signal) => {
    if (!matterId) {
      setVersions([]);
      setSelectedVersionId("");
      return;
    }
    try {
      const payload = await apiRequest(`${API_ROOT}/products/${matterId}/versions/`, { tenantId: tenant.id, signal });
      if (signal?.aborted) return;
      const rows = payload.results || [];
      setVersions(rows);
      setSelectedVersionId((current) => rows.some((row) => row.id === current) ? current : rows[0]?.id || "");
    } catch (loadError) {
      if (loadError?.name !== "AbortError" && !signal?.aborted) setError(displayError(loadError));
    }
  }, [tenant.id]);

  const loadFeatures = useCallback(async (versionId, signal) => {
    if (!versionId) {
      setFeatures([]);
      return;
    }
    try {
      const payload = await apiRequest(`${API_ROOT}/versions/${versionId}/features/`, { tenantId: tenant.id, signal });
      if (!signal?.aborted) setFeatures(payload.results || []);
    } catch (loadError) {
      if (loadError?.name !== "AbortError" && !signal?.aborted) setError(displayError(loadError));
    }
  }, [tenant.id]);

  const loadHeatmap = useCallback(async (studyId, signal) => {
    if (!studyId) {
      setHeatmap(null);
      return;
    }
    try {
      const payload = await apiRequest(`${API_ROOT}/studies/${studyId}/risk-heatmap/`, { tenantId: tenant.id, signal });
      if (!signal?.aborted) {
        setHeatmap(payload);
        if (payload.study?.product_id) setSelectedMatterId(payload.study.product_id);
        if (payload.study?.product_version_id) setSelectedVersionId(payload.study.product_version_id);
      }
    } catch (loadError) {
      if (loadError?.name !== "AbortError" && !signal?.aborted) setError(displayError(loadError));
    }
  }, [tenant.id]);

  useEffect(() => {
    const controller = new AbortController();
    void loadMattersAndStudies(controller.signal);
    return () => controller.abort();
  }, [loadMattersAndStudies]);

  useEffect(() => {
    const controller = new AbortController();
    void loadVersions(selectedMatterId, controller.signal);
    return () => controller.abort();
  }, [loadVersions, selectedMatterId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadFeatures(selectedVersionId, controller.signal);
    return () => controller.abort();
  }, [loadFeatures, selectedVersionId, selectedVersion?.extraction_status]);

  useEffect(() => {
    if (!selectedMatterId || !selectedVersion || !["PENDING", "PROCESSING"].includes(selectedVersion.extraction_status)) return undefined;
    const timer = window.setInterval(() => { void loadVersions(selectedMatterId); }, 3000);
    return () => window.clearInterval(timer);
  }, [loadVersions, selectedMatterId, selectedVersion?.extraction_status, selectedVersion]);

  useEffect(() => {
    const controller = new AbortController();
    void loadHeatmap(selectedStudyId, controller.signal);
    return () => controller.abort();
  }, [loadHeatmap, selectedStudyId]);

  async function runBusy(key, action, successMessage) {
    setBusyKey(key);
    setError("");
    setNotice("");
    try {
      await action();
      if (successMessage) setNotice(successMessage);
    } catch (actionError) {
      setError(displayError(actionError));
    } finally {
      setBusyKey("");
    }
  }

  async function createMatter(event) {
    event.preventDefault();
    await runBusy("create-matter", async () => {
      const created = await apiRequest(`${API_ROOT}/products/`, {
        method: "POST", tenantId: tenant.id,
        body: { name: productName, product_code: productCode, description: productDescription, jurisdictions: [] },
      });
      setProductName(""); setProductCode(""); setProductDescription("");
      await loadMattersAndStudies();
      setSelectedMatterId(created.id);
    }, "Product matter created.");
  }

  async function uploadSpec(event) {
    event.preventDefault();
    const uploadForm = event.currentTarget;
    if (!selectedMatterId || !specFile) return;
    await runBusy("upload-spec", async () => {
      const formData = new FormData();
      formData.append("file", specFile);
      const uploaded = await apiRequest(`${API_ROOT}/products/${selectedMatterId}/versions/`, {
        method: "POST", tenantId: tenant.id, body: formData,
      });
      setSpecFile(null);
      uploadForm.reset();
      await loadVersions(selectedMatterId);
      setSelectedVersionId(uploaded.id);
      await loadMattersAndStudies();
    }, "Specification uploaded. Extraction and feature-candidate generation are queued.");
  }

  async function reviewFeature(feature, reviewStatus) {
    await runBusy(`feature-${feature.id}`, async () => {
      await apiRequest(`${API_ROOT}/features/${feature.id}/`, {
        method: "PATCH", tenantId: tenant.id, body: { review_status: reviewStatus },
      });
      await loadFeatures(selectedVersionId);
    }, reviewStatus === "CONFIRMED" ? "Feature confirmed and available for evidence mapping." : "Feature review updated.");
  }

  async function downloadSpec() {
    if (!selectedVersionId) return;
    await runBusy("download-spec", async () => {
      await downloadArtifact(`${API_ROOT}/versions/${selectedVersionId}/file/`, { tenantId: tenant.id, fallbackFilename: "product-specification.pdf" });
    }, "Source PDF downloaded.");
  }

  async function createStudy(event) {
    event.preventDefault();
    if (!selectedVersionId || !selectedPatentParts.patentId || !selectedPatentParts.versionId) return;
    await runBusy("create-study", async () => {
      const created = await apiRequest(`${API_ROOT}/studies/`, {
        method: "POST", tenantId: tenant.id,
        body: {
          product_version_id: selectedVersionId,
          patent_id: selectedPatentParts.patentId,
          patent_document_version_id: selectedPatentParts.versionId,
          jurisdiction: patents.find((item) => item.id === selectedPatentParts.patentId)?.jurisdiction || "",
        },
      });
      await loadMattersAndStudies();
      setSelectedStudyId(created.id);
    }, "FTO study created with review-required claim limitation rows.");
  }

  async function linkEvidence(limitation) {
    const featureId = featureSelections[limitation.assessment_id];
    if (!featureId) return;
    await runBusy(`evidence-${limitation.assessment_id}`, async () => {
      await apiRequest(`${API_ROOT}/studies/${selectedStudyId}/limitations/${limitation.limitation_id}/evidence/`, {
        method: "POST", tenantId: tenant.id,
        body: {
          feature_id: featureId,
          kind: evidenceKinds[limitation.assessment_id] || "SUPPORTING",
          attorney_rationale: evidenceNotes[limitation.assessment_id] || "",
        },
      });
      await loadHeatmap(selectedStudyId);
    }, "Source feature linked as evidence.");
  }

  async function saveTriage(limitation) {
    const value = triage[limitation.assessment_id] || {};
    await runBusy(`triage-${limitation.assessment_id}`, async () => {
      await apiRequest(`${API_ROOT}/studies/${selectedStudyId}/limitations/${limitation.limitation_id}/assessment/`, {
        method: "PATCH", tenantId: tenant.id,
        body: { triage_status: value.status || limitation.triage_status, triage_rationale: value.rationale ?? limitation.triage_rationale },
      });
      await loadHeatmap(selectedStudyId);
    }, "Limitation triage saved.");
  }

  async function markLimitationReviewed(limitation) {
    await runBusy(`review-${limitation.assessment_id}`, async () => {
      await apiRequest(`${API_ROOT}/studies/${selectedStudyId}/limitations/${limitation.limitation_id}/assessment/`, {
        method: "PATCH", tenantId: tenant.id, body: { review_status: "REVIEWED" },
      });
      await loadHeatmap(selectedStudyId);
    }, "Limitation review recorded.");
  }

  async function resolveAnnotation(annotation) {
    await runBusy(`resolve-${annotation.id}`, async () => {
      await apiRequest(`${API_ROOT}/annotations/${annotation.id}/resolve/`, {
        method: "POST", tenantId: tenant.id, body: { resolved: !annotation.resolved },
      });
      await loadHeatmap(selectedStudyId);
    }, annotation.resolved ? "Annotation reopened." : "Annotation resolved.");
  }

  async function addAnnotation(limitation) {
    const body = annotations[limitation.assessment_id] || "";
    if (!body.trim()) return;
    await runBusy(`annotation-${limitation.assessment_id}`, async () => {
      await apiRequest(`${API_ROOT}/studies/${selectedStudyId}/annotations/`, {
        method: "POST", tenantId: tenant.id,
        body: { assessment_id: limitation.assessment_id, category: "NOTE", body },
      });
      setAnnotations((current) => ({ ...current, [limitation.assessment_id]: "" }));
      await loadHeatmap(selectedStudyId);
    }, "Attorney annotation saved.");
  }

  const patentVersionChoices = useMemo(() => patents.flatMap((patent) => (
    (patent.versions || []).map((version) => ({
      value: `${patent.id}::${version.id}`,
      patent,
      version,
    }))
  )).filter(({ version }) => version.extraction_status === "EXTRACTED"), [patents]);

  const currentFeatures = features.filter((feature) => feature.review_status === "CONFIRMED");
  const allHeatmapRows = (heatmap?.claims || []).flatMap((claim) => claim.limitations.map((limitation) => ({ ...limitation, claim_number: claim.claim_number })));

  return (
    <main className="ps-page">
      <section className="ps-hero">
        <p className="text-xs font-bold uppercase tracking-[0.22em] text-cyan-300">Phase 6 · FTO workspace</p>
        <div className="mt-3 grid gap-6 lg:grid-cols-[1fr_320px] lg:items-end">
          <div>
            <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">Product-to-claim risk mapping</h1>
            <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-300">Pin each assessment to a product specification version and patent document version. Confirm source features, map supporting and counter-evidence to claim limitations, and keep attorney annotations alongside the evidence.</p>
          </div>
          <div className="rounded-2xl border border-white/10 bg-white/5 p-4 text-sm text-slate-200">
            <p className="font-semibold text-white">Review-priority indicator</p>
            <p className="mt-1 leading-5">Heatmap bands guide triage only. They are not legal conclusions or an automated freedom-to-operate determination.</p>
          </div>
        </div>
      </section>

      {(error || notice) && <div className={`rounded-xl border px-4 py-3 text-sm ${error ? "border-rose-200 bg-rose-50 text-rose-800" : "border-emerald-200 bg-emerald-50 text-emerald-800"}`} role={error ? "alert" : "status"}>{error || notice}</div>}

      <div className="grid gap-6 xl:grid-cols-[minmax(300px,0.85fr)_minmax(0,1.5fr)]">
        <section className="space-y-5">
          <div className="ps-panel">
            <div className="flex items-center justify-between gap-3">
              <div><h2 className="ps-panel-title">1. Product matters</h2><p className="mt-1 text-xs text-slate-500">Create and version product specifications.</p></div>
              {loading && <span className="text-xs text-slate-500">Loading…</span>}
            </div>
            <form className="mt-4 space-y-3" onSubmit={createMatter}>
              <label className="block text-xs font-semibold text-slate-700">Product / family name<input required value={productName} onChange={(event) => setProductName(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm font-normal" placeholder="e.g. Adaptive Battery Controller" /></label>
              <label className="block text-xs font-semibold text-slate-700">Product code (optional)<input value={productCode} onChange={(event) => setProductCode(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2.5 text-sm font-normal" placeholder="ABC-2026" /></label>
              <label className="block text-xs font-semibold text-slate-700">Description<textarea value={productDescription} onChange={(event) => setProductDescription(event.target.value)} rows={2} className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm font-normal" placeholder="Product scope and version notes" /></label>
              <button disabled={busyKey === "create-matter"} className="ps-button-primary" type="submit">{busyKey === "create-matter" ? "Creating…" : "Create product matter"}</button>
            </form>
            <div className="mt-5 border-t border-slate-100 pt-4">
              <label className="block text-xs font-semibold text-slate-700">Active product matter<select value={selectedMatterId} onChange={(event) => { setSelectedMatterId(event.target.value); setSelectedVersionId(""); }} className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm">{matters.length === 0 && <option value="">No product matters yet</option>}{matters.map((matter) => <option key={matter.id} value={matter.id}>{matter.name}{matter.product_code ? ` · ${matter.product_code}` : ""}</option>)}</select></label>
              {selectedMatter && <p className="mt-2 text-xs text-slate-500">{selectedMatter.version_count ?? 0} version(s) · {selectedMatter.status.toLowerCase()}</p>}
            </div>
          </div>

          <div className="ps-panel">
            <h2 className="ps-panel-title">2. Product specification</h2>
            <p className="mt-1 text-xs leading-5 text-slate-500">Upload a selectable-text PDF. Scanned documents need OCR before feature extraction can run.</p>
            <form className="mt-4 space-y-3" onSubmit={uploadSpec}>
              <input key={selectedMatterId} type="file" accept="application/pdf,.pdf" required onChange={(event) => setSpecFile(event.target.files?.[0] || null)} className="block w-full text-xs text-slate-600 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-100 file:px-3 file:py-2 file:font-semibold file:text-slate-700" />
              <button disabled={!selectedMatterId || !specFile || busyKey === "upload-spec"} type="submit" className="ps-button-primary bg-slate-900 hover:bg-slate-800">{busyKey === "upload-spec" ? "Uploading…" : "Upload specification version"}</button>
            </form>
            <div className="mt-4 space-y-2">
              <label className="block text-xs font-semibold text-slate-700">Specification version<select value={selectedVersionId} onChange={(event) => setSelectedVersionId(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm"><option value="">Select a version</option>{versions.map((version) => <option key={version.id} value={version.id}>v{version.version_number}{version.version_label ? ` · ${version.version_label}` : ""} — {version.extraction_status.toLowerCase()}/{version.decomposition_status.toLowerCase()}</option>)}</select></label>
              {selectedVersion && <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-slate-50 p-3 text-xs"><span className="text-slate-600">{selectedVersion.feature_count ?? features.length} feature candidate(s) · SHA-256 pinned</span><button type="button" onClick={() => void downloadSpec()} disabled={busyKey === "download-spec"} className="font-semibold text-indigo-700 underline disabled:opacity-50">Download source PDF</button></div>}
            </div>
          </div>
        </section>

        <section className="space-y-6">
          <div className="ps-panel">
            <div className="flex flex-wrap items-end justify-between gap-3"><div><h2 className="ps-panel-title">3. Decomposed product features</h2><p className="mt-1 text-xs leading-5 text-slate-500">Candidates preserve exact source quotations and page offsets. Confirm only after reviewing the source.</p></div><span className="rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-600">{currentFeatures.length} confirmed</span></div>
            {!selectedVersion ? <p className="mt-5 rounded-xl border border-dashed border-slate-300 p-5 text-sm text-slate-500">Select a product specification version to review features.</p> : selectedVersion.extraction_status !== "EXTRACTED" ? <p className="mt-5 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">Extraction status: {selectedVersion.extraction_status}. {selectedVersion.error_code || "Refresh the version after the ingestion task completes."}</p> : features.length === 0 ? <p className="mt-5 rounded-xl border border-dashed border-slate-300 p-5 text-sm text-slate-500">No feature candidates were detected. Add manually selected source spans through the API or review whether the PDF has selectable text.</p> : <div className="mt-4 max-h-[520px] space-y-3 overflow-auto pr-1">{features.map((feature) => <article key={feature.id} className={`rounded-xl border p-4 ${feature.review_status === "CONFIRMED" ? "border-emerald-200 bg-emerald-50/40" : feature.review_status === "REJECTED" ? "border-slate-200 bg-slate-50 opacity-75" : "border-slate-200 bg-white"}`}>
              <div className="flex flex-wrap items-start justify-between gap-2"><h3 className="max-w-2xl text-sm font-bold text-slate-900">{feature.title}</h3><span className={`rounded-full px-2 py-1 text-[10px] font-bold uppercase tracking-wide ${feature.review_status === "CONFIRMED" ? "bg-emerald-100 text-emerald-800" : feature.review_status === "REJECTED" ? "bg-slate-200 text-slate-600" : "bg-amber-100 text-amber-900"}`}>{statusLabel(feature.review_status)}</span></div>
              <blockquote className="mt-3 border-l-2 border-indigo-300 pl-3 text-sm leading-6 text-slate-700">“{feature.source_quote}”</blockquote>
              <p className="mt-2 text-[11px] text-slate-500">Page {(feature.source_page_index ?? 0) + 1} · offsets {feature.source_start_offset}–{feature.source_end_offset} · heuristic confidence {feature.extraction_confidence ?? "n/a"}</p>
              {feature.review_status === "CANDIDATE" && <div className="mt-3 flex gap-2"><button type="button" onClick={() => void reviewFeature(feature, "CONFIRMED")} disabled={busyKey === `feature-${feature.id}`} className="inline-flex items-center justify-center rounded-xl bg-emerald-600 px-3.5 py-2.5 text-xs font-bold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-45">Confirm feature</button><button type="button" onClick={() => void reviewFeature(feature, "REJECTED")} disabled={busyKey === `feature-${feature.id}`} className="ps-button-secondary !px-3 !py-2">Reject candidate</button></div>}
            </article>)}</div>}
          </div>

          <div className="ps-panel">
            <h2 className="ps-panel-title">4. Start a product-to-patent study</h2>
            <p className="mt-1 text-xs leading-5 text-slate-500">A study pins one selected product version and one patent document version for reproducibility.</p>
            <form onSubmit={createStudy} className="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
              <label className="block text-xs font-semibold text-slate-700">Patent and document version<select value={selectedPatentVersion} onChange={(event) => setSelectedPatentVersion(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm"><option value="">Choose an extracted patent version</option>{patentVersionChoices.map(({ value, patent, version }) => <option key={value} value={value}>{patent.publication_number || patent.title} · v{version.version_number}</option>)}</select></label>
              <button type="submit" disabled={!selectedVersionId || selectedVersion?.extraction_status !== "EXTRACTED" || !selectedPatentVersion || busyKey === "create-study"} className="ps-button-primary">{busyKey === "create-study" ? "Creating…" : "Create FTO study"}</button>
            </form>
            <p className="mt-2 text-[11px] leading-5 text-slate-500">The backend selects all claims from the chosen patent version. It requires extracted claim records to be present first.</p>
          </div>

          <div className="ps-panel">
            <div className="flex flex-wrap items-end justify-between gap-3"><div><h2 className="ps-panel-title">5. Limitation risk heatmap</h2><p className="mt-1 text-xs leading-5 text-slate-500">Choose a study to inspect limitation-level evidence, counter-evidence and attorney notes.</p></div><label className="min-w-56 text-xs font-semibold text-slate-700">FTO study<select value={selectedStudyId} onChange={(event) => setSelectedStudyId(event.target.value)} className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm"><option value="">Select a study</option>{studies.map((study) => <option key={study.id} value={study.id}>{study.title}</option>)}</select></label></div>
            {!heatmap ? <p className="mt-5 rounded-xl border border-dashed border-slate-300 p-5 text-sm text-slate-500">Create or select a study to load its limitation heatmap.</p> : <>
              <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-950">{heatmap.disclaimer}</div>
              <div className="mt-4 flex flex-wrap gap-2 text-[10px] font-bold uppercase tracking-wide"><span className={`rounded-full border px-2 py-1 ${bandClass("HIGH_ATTENTION")}`}>High attention</span><span className={`rounded-full border px-2 py-1 ${bandClass("MODERATE_ATTENTION")}`}>Moderate</span><span className={`rounded-full border px-2 py-1 ${bandClass("ELEVATED_ATTENTION")}`}>Elevated</span><span className={`rounded-full border px-2 py-1 ${bandClass("LOWER_ATTENTION")}`}>Lower attention</span><span className={`rounded-full border px-2 py-1 ${bandClass("UNASSESSED")}`}>Unassessed</span></div>
              {allHeatmapRows.length === 0 ? <p className="mt-4 text-sm text-slate-500">No claim limitations are attached to this study.</p> : <>
                <div className="mt-4 space-y-3" aria-label="Claim limitation review-priority heatmap">
                  {heatmap.claims.map((claim) => <div key={claim.claim_id} className="rounded-xl border border-slate-200 p-3"><p className="mb-2 text-xs font-bold text-slate-800">Claim {claim.claim_number}</p><div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">{claim.limitations.map((cell) => <div key={cell.limitation_id} title={cell.text} className={`min-h-16 rounded-lg border p-2 ${bandClass(cell.heatmap_band)}`}><p className="text-[10px] font-bold uppercase">L{cell.limitation_index}</p><p className="mt-1 text-[10px] leading-4">{statusLabel(cell.heatmap_band)}</p><p className="mt-1 text-[9px] opacity-80">S:{cell.supporting_evidence_count} · C:{cell.counter_evidence_count} · X:{cell.context_evidence_count}</p></div>)}</div></div>)}
                </div>
                <div className="mt-5 space-y-4">{allHeatmapRows.map((limitation) => <article key={limitation.assessment_id} className="rounded-2xl border border-slate-200 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3"><div className="max-w-3xl"><p className="text-[10px] font-bold uppercase tracking-[0.15em] text-slate-500">Claim {limitation.claim_number} · Limitation {limitation.limitation_index}</p><p className="mt-2 text-sm leading-6 text-slate-900">{limitation.text}</p><p className="mt-2 text-[10px] font-semibold uppercase tracking-wide text-slate-500">Assessment review: {statusLabel(limitation.review_status)}</p></div><span className={`rounded-lg border px-2.5 py-1.5 text-[10px] font-bold uppercase tracking-wide ${bandClass(limitation.heatmap_band)}`}>{statusLabel(limitation.heatmap_band)}</span></div>
                <div className="mt-3 grid grid-cols-2 gap-2 sm:max-w-2xl sm:grid-cols-4"><div className="rounded-lg bg-emerald-50 p-2 text-center"><p className="text-lg font-bold text-emerald-800">{limitation.supporting_evidence_count}</p><p className="text-[10px] text-emerald-800">Supporting</p></div><div className="rounded-lg bg-rose-50 p-2 text-center"><p className="text-lg font-bold text-rose-800">{limitation.counter_evidence_count}</p><p className="text-[10px] text-rose-800">Counter</p></div><div className="rounded-lg bg-blue-50 p-2 text-center"><p className="text-lg font-bold text-blue-800">{limitation.context_evidence_count}</p><p className="text-[10px] text-blue-800">Context</p></div><div className="rounded-lg bg-slate-100 p-2 text-center"><p className="text-lg font-bold text-slate-800">{limitation.annotation_count}</p><p className="text-[10px] text-slate-600">Annotations</p></div></div>
                <div className="mt-4 grid gap-3 lg:grid-cols-2">
                  <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs font-bold text-slate-800">Link confirmed product evidence</p><select value={featureSelections[limitation.assessment_id] || ""} onChange={(event) => setFeatureSelections((current) => ({ ...current, [limitation.assessment_id]: event.target.value }))} className="mt-2 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-xs"><option value="">Select a confirmed feature</option>{currentFeatures.map((feature) => <option key={feature.id} value={feature.id}>P{(feature.source_page_index ?? 0) + 1} · {feature.title}</option>)}</select><select value={evidenceKinds[limitation.assessment_id] || "SUPPORTING"} onChange={(event) => setEvidenceKinds((current) => ({ ...current, [limitation.assessment_id]: event.target.value }))} className="mt-2 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-xs"><option value="SUPPORTING">Potentially supports correspondence</option><option value="COUNTER">Potentially counters correspondence</option><option value="CONTEXT">Context only</option></select><textarea value={evidenceNotes[limitation.assessment_id] || ""} onChange={(event) => setEvidenceNotes((current) => ({ ...current, [limitation.assessment_id]: event.target.value }))} rows={2} className="mt-2 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-xs" placeholder="Why is this source feature relevant?" /><button type="button" onClick={() => void linkEvidence(limitation)} disabled={!featureSelections[limitation.assessment_id] || busyKey === `evidence-${limitation.assessment_id}`} className="mt-2 rounded-lg bg-slate-900 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Link evidence</button></div>
                  <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs font-bold text-slate-800">Attorney triage</p><select value={triage[limitation.assessment_id]?.status || limitation.triage_status} onChange={(event) => setTriage((current) => ({ ...current, [limitation.assessment_id]: { ...current[limitation.assessment_id], status: event.target.value } }))} className="mt-2 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-xs">{["UNASSESSED", "POTENTIAL_MATCH", "PARTIAL_MATCH", "NO_MATCH_IDENTIFIED", "COUNTER_EVIDENCE_PRESENT", "CONFLICTING_EVIDENCE", "UNCERTAIN"].map((value) => <option key={value} value={value}>{statusLabel(value)}</option>)}</select><textarea value={triage[limitation.assessment_id]?.rationale ?? limitation.triage_rationale ?? ""} onChange={(event) => setTriage((current) => ({ ...current, [limitation.assessment_id]: { ...current[limitation.assessment_id], rationale: event.target.value } }))} rows={2} className="mt-2 w-full rounded-lg border border-slate-300 bg-white px-2.5 py-2 text-xs" placeholder="Explain the evidence and any limits of the reviewed scope." /><button type="button" onClick={() => void saveTriage(limitation)} disabled={busyKey === `triage-${limitation.assessment_id}`} className="mt-2 rounded-lg bg-indigo-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">Save triage</button><p className="mt-2 text-[10px] text-slate-500">Review state: {statusLabel(limitation.review_status)}. Independent review is restricted by the backend.</p></div>
                </div>
              {Array.isArray(limitation.evidence) && limitation.evidence.length > 0 && <div className="mt-4 space-y-2">{limitation.evidence.map((evidence) => <div key={evidence.id} className="rounded-lg border border-slate-200 bg-white p-3"><div className="flex flex-wrap items-center gap-2"><span className={`rounded-full px-2 py-1 text-[10px] font-bold uppercase ${evidence.kind === "SUPPORTING" ? "bg-emerald-100 text-emerald-800" : evidence.kind === "COUNTER" ? "bg-rose-100 text-rose-800" : "bg-slate-100 text-slate-700"}`}>{statusLabel(evidence.kind)}</span><span className="text-[10px] text-slate-500">Page {(evidence.source_page_index ?? 0) + 1} · {evidence.feature_title}</span></div><blockquote className="mt-2 border-l-2 border-indigo-300 pl-3 text-xs leading-5 text-slate-700">“{evidence.source_quote}”</blockquote>{evidence.attorney_rationale && <p className="mt-2 text-xs text-slate-600">{evidence.attorney_rationale}</p>}</div>)}</div>}
                {Array.isArray(limitation.annotations) && limitation.annotations.length > 0 && <div className="mt-3 space-y-2">{limitation.annotations.map((annotation) => <div key={annotation.id} className={`rounded-lg border p-3 ${annotation.resolved ? "border-slate-200 bg-slate-50" : "border-violet-200 bg-violet-50/50"}`}><div className="flex flex-wrap items-center justify-between gap-2"><p className="text-[10px] font-bold uppercase tracking-wide text-slate-600">{statusLabel(annotation.category)} · {annotation.created_by_username || "Tenant member"} · {annotation.resolved ? "resolved" : "open"}</p><button type="button" onClick={() => void resolveAnnotation(annotation)} disabled={busyKey === `resolve-${annotation.id}`} className="text-[10px] font-semibold text-indigo-700 underline disabled:opacity-50">{annotation.resolved ? "Reopen" : "Resolve"}</button></div><p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-slate-700">{annotation.body}</p></div>)}</div>}
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2"><div className="flex flex-1 flex-col gap-2 sm:flex-row"><input value={annotations[limitation.assessment_id] || ""} onChange={(event) => setAnnotations((current) => ({ ...current, [limitation.assessment_id]: event.target.value }))} className="min-w-0 flex-1 rounded-lg border border-slate-300 px-3 py-2 text-xs" placeholder="Add an attorney note, question, or counter-evidence context" /><button type="button" onClick={() => void addAnnotation(limitation)} disabled={!annotations[limitation.assessment_id]?.trim() || busyKey === `annotation-${limitation.assessment_id}`} className="ps-button-secondary !px-3 !py-2">Add annotation</button></div>{limitation.review_status !== "REVIEWED" && <button type="button" onClick={() => void markLimitationReviewed(limitation)} disabled={busyKey === `review-${limitation.assessment_id}`} className="inline-flex items-center justify-center rounded-xl bg-emerald-600 px-3.5 py-2.5 text-xs font-bold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:opacity-45">Mark reviewed</button>}</div>
              </article>)}</div></>}
            </>}
          </div>
        </section>
      </div>
    </main>
  );
}
