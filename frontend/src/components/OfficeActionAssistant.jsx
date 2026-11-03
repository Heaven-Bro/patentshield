import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiRequest, downloadArtifact } from "../lib/apiClient";

const API_ROOT = "/api/v1/office-actions";
const inputClass = "ps-input";
const buttonClass = "ps-button-primary";
const secondaryButtonClass = "ps-button-secondary";

function friendlyError(error) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "The request could not be completed.";
}

function Card({ title, subtitle, children, className = "" }) {
  return (
    <section className={`ps-panel ${className}`}>
      <div className="mb-4">
        <h2 className="ps-panel-title">{title}</h2>
        {subtitle && <p className="ps-panel-description">{subtitle}</p>}
      </div>
      {children}
    </section>
  );
}

function Badge({ value }) {
  const classes = {
    EXTRACTED: "bg-emerald-50 text-emerald-800 ring-emerald-200",
    COMPLETED: "bg-emerald-50 text-emerald-800 ring-emerald-200",
    NEEDS_REVIEW: "bg-amber-50 text-amber-800 ring-amber-200",
    PENDING: "bg-slate-100 text-slate-700 ring-slate-200",
    PROCESSING: "bg-blue-50 text-blue-800 ring-blue-200",
    RETRYING: "bg-amber-50 text-amber-800 ring-amber-200",
    FAILED: "bg-rose-50 text-rose-800 ring-rose-200",
    APPROVED: "bg-emerald-50 text-emerald-800 ring-emerald-200",
    CHANGES_REQUESTED: "bg-amber-50 text-amber-800 ring-amber-200",
  };
  return <span className={`inline-flex rounded-full px-2.5 py-1 text-[11px] font-semibold ring-1 ring-inset ${classes[value] || "bg-slate-100 text-slate-700 ring-slate-200"}`}>{(value || "UNKNOWN").replaceAll("_", " ")}</span>;
}

function formatDate(value) {
  if (!value) return "Not recorded";
  return new Date(`${value}T00:00:00`).toLocaleDateString();
}
function formatConfidence(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "Not reported";
  return `${(Math.max(0, Math.min(1, value)) * 100).toFixed(0)}%`;
}

export default function OfficeActionAssistant({ tenant, patents = [] }) {
  const [actions, setActions] = useState([]);
  const [selectedActionId, setSelectedActionId] = useState("");
  const [detail, setDetail] = useState(null);
  const [drafts, setDrafts] = useState([]);
  const [selectedDraftId, setSelectedDraftId] = useState("");
  const [draftContent, setDraftContent] = useState("");
  const [amendmentText, setAmendmentText] = useState("");
  const [amendmentClaimNumber, setAmendmentClaimNumber] = useState("1");
  const [amendmentResult, setAmendmentResult] = useState(null);
  const [reviewComments, setReviewComments] = useState("");
  const [file, setFile] = useState(null);
  const [uploadForm, setUploadForm] = useState({
    patent_id: "", jurisdiction: "US", action_type: "NON_FINAL", application_number: "", issued_date: "", response_due_date: "",
  });
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const request = useCallback((path, options = {}) => apiRequest(path, { tenantId: tenant.id, ...options }), [tenant.id]);
  const currentDraft = useMemo(() => drafts.find((item) => item.id === selectedDraftId) || null, [drafts, selectedDraftId]);

  const loadActions = useCallback(async (signal) => {
    setError("");
    try {
      const payload = await request(`${API_ROOT}/?limit=100`, { signal });
      if (signal?.aborted) return;
      const next = payload.results || [];
      setActions(next);
      setSelectedActionId((current) => next.some((item) => item.id === current) ? current : (next[0]?.id || ""));
    } catch (loadError) {
      if (!signal?.aborted) setError(friendlyError(loadError));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [request]);

  useEffect(() => {
    const controller = new AbortController();
    void loadActions(controller.signal);
    return () => controller.abort();
  }, [loadActions]);

  const loadDetail = useCallback(async (actionId, signal) => {
    if (!actionId) { setDetail(null); setDrafts([]); setSelectedDraftId(""); return; }
    try {
      const [actionPayload, draftPayload] = await Promise.all([
        request(`${API_ROOT}/${actionId}/`, { signal }),
        request(`${API_ROOT}/${actionId}/drafts/`, { signal }),
      ]);
      if (signal?.aborted) return;
      setDetail(actionPayload);
      const nextDrafts = draftPayload.results || [];
      setDrafts(nextDrafts);
      setSelectedDraftId((current) => nextDrafts.some((draft) => draft.id === current) ? current : (nextDrafts[0]?.id || ""));
    } catch (loadError) {
      if (!signal?.aborted) setError(friendlyError(loadError));
    }
  }, [request]);

  useEffect(() => {
    const controller = new AbortController();
    void loadDetail(selectedActionId, controller.signal);
    return () => controller.abort();
  }, [loadDetail, selectedActionId]);

  useEffect(() => {
    const active = actions.some((item) => ["PENDING", "PROCESSING", "RETRYING"].includes(item.extraction_status));
    if (!active) return undefined;
    const timer = window.setTimeout(async () => {
      await loadActions();
      if (selectedActionId) void loadDetail(selectedActionId);
    }, 3500);
    return () => window.clearTimeout(timer);
  }, [actions, loadActions, loadDetail, selectedActionId]);

  useEffect(() => {
    const revision = currentDraft?.current_revision;
    setDraftContent(revision?.content_markdown || "");
    setReviewComments("");
  }, [currentDraft?.id, currentDraft?.current_revision_number, currentDraft?.current_revision?.content_markdown]);

  async function runAction(action, successText) {
    setBusy(true); setError(""); setNotice("");
    try {
      await action();
      setNotice(successText);
      await loadActions();
      if (selectedActionId) await loadDetail(selectedActionId);
    } catch (actionError) {
      setError(friendlyError(actionError));
    } finally {
      setBusy(false);
    }
  }

  async function uploadOfficeAction(event) {
    event.preventDefault();
    const formElement = event.currentTarget;
    if (!file) { setError("Choose a PDF office-action document first."); return; }
    await runAction(async () => {
      const body = new FormData();
      body.append("file", file);
      body.append("patent_id", uploadForm.patent_id);
      body.append("jurisdiction", uploadForm.jurisdiction);
      body.append("action_type", uploadForm.action_type);
      if (uploadForm.application_number) body.append("application_number", uploadForm.application_number);
      if (uploadForm.issued_date) body.append("issued_date", uploadForm.issued_date);
      if (uploadForm.response_due_date) body.append("response_due_date", uploadForm.response_due_date);
      const created = await request(`${API_ROOT}/`, { method: "POST", body });
      setSelectedActionId(created.id);
      setFile(null);
      formElement.reset();
      setUploadForm({ patent_id: "", jurisdiction: "US", action_type: "NON_FINAL", application_number: "", issued_date: "", response_due_date: "" });
    }, "Office action uploaded. Text extraction is queued.");
  }

  async function downloadSourcePdf() {
    if (!selectedActionId) return;
    try {
      setError("");
      await downloadArtifact(`${API_ROOT}/${selectedActionId}/file/`, { tenantId: tenant.id, fallbackFilename: "office-action.pdf" });
    } catch (downloadError) {
      setError(friendlyError(downloadError));
    }
  }

  async function processAgain() {
    if (!selectedActionId) return;
    await runAction(async () => { await request(`${API_ROOT}/${selectedActionId}/process/`, { method: "POST", body: {} }); }, "Processing was queued.");
  }

  async function generateDraft() {
    if (!selectedActionId) return;
    await runAction(async () => {
      const generated = await request(`${API_ROOT}/${selectedActionId}/drafts/generate/`, { method: "POST", body: {} });
      setSelectedDraftId(generated.id);
    }, "Evidence-linked draft framework generated. Attorney review is required.");
  }

  async function saveRevision(event) {
    event.preventDefault();
    if (!selectedDraftId) return;
    await runAction(async () => {
      await request(`${API_ROOT}/drafts/${selectedDraftId}/revisions/`, { method: "POST", body: { content_markdown: draftContent } });
    }, "A new immutable draft revision was saved.");
  }

  async function submitDraftForReview() {
    if (!selectedDraftId) return;
    await runAction(async () => {
      await request(`${API_ROOT}/drafts/${selectedDraftId}/submit-for-review/`, { method: "POST", body: {} });
    }, "Draft submitted for independent review.");
  }

  async function reviewRevision(decision) {
    const revision = currentDraft?.current_revision;
    if (!revision?.id) return;
    await runAction(async () => {
      await request(`${API_ROOT}/revisions/${revision.id}/review/`, { method: "POST", body: { decision, comments: reviewComments } });
    }, `Review recorded: ${decision.toLowerCase().replaceAll("_", " ")}.`);
  }

  async function checkAmendmentSupport(event) {
    event.preventDefault();
    if (!selectedActionId) return;
    await runAction(async () => {
      const result = await request(`${API_ROOT}/${selectedActionId}/amendment-support/`, {
        method: "POST", body: { claim_number: amendmentClaimNumber, proposed_claim_text: amendmentText },
      });
      setAmendmentResult(result);
    }, "Specification text lookup completed. This is not a legal sufficiency decision.");
  }

  return (
    <main className="ps-page">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="ps-eyebrow">Prosecution workspace</p>
          <h1 className="mt-1 text-2xl font-bold tracking-tight text-slate-950 sm:text-3xl">Office Action Response Assistant</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">Extract rejection grounds and examiner-cited patent identifiers, retain source passages, and build traceable response drafts from the tenant's prosecution record.</p>
        </div>
        <div className="ps-alert-warning text-xs md:max-w-sm">
          <strong>Attorney review required.</strong> Parsing is heuristic, reference identifiers are not externally verified, and drafts are not legal advice or filing-ready responses. PatentShield never submits filings.
        </div>
      </div>

      {error && <div className="ps-alert-error" role="alert">{error}</div>}
      {notice && <div className="ps-alert-success" role="status">{notice}</div>}

      <div className="grid gap-6 xl:grid-cols-[minmax(320px,0.8fr)_minmax(0,1.5fr)]">
        <div className="space-y-6">
          <Card title="Upload examiner communication" subtitle="Attach a PDF to a patent already in this tenant's library.">
            <form onSubmit={uploadOfficeAction} className="space-y-3">
              <label className="block text-sm font-medium text-slate-700">Patent matter
                <select required className={`${inputClass} mt-1.5`} value={uploadForm.patent_id} onChange={(event) => setUploadForm((current) => ({ ...current, patent_id: event.target.value }))}>
                  <option value="">Choose a patent…</option>
                  {patents.map((patent) => <option key={patent.id} value={patent.id}>{patent.title} {patent.application_number ? `(${patent.application_number})` : ""}</option>)}
                </select>
              </label>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-sm font-medium text-slate-700">Jurisdiction
                  <select className={`${inputClass} mt-1.5`} value={uploadForm.jurisdiction} onChange={(event) => setUploadForm((current) => ({ ...current, jurisdiction: event.target.value }))}>
                    <option value="US">United States</option><option value="EP">European Patent Office</option><option value="PCT">PCT</option><option value="OTHER">Other</option>
                  </select>
                </label>
                <label className="block text-sm font-medium text-slate-700">Communication type
                  <select className={`${inputClass} mt-1.5`} value={uploadForm.action_type} onChange={(event) => setUploadForm((current) => ({ ...current, action_type: event.target.value }))}>
                    <option value="NON_FINAL">Non-final office action</option><option value="FINAL">Final office action</option><option value="NOTICE_OF_ALLOWANCE">Notice of allowance</option><option value="EXAMINER_INTERVIEW">Interview summary</option><option value="OTHER">Other communication</option>
                  </select>
                </label>
                <label className="block text-sm font-medium text-slate-700">Application number
                  <input className={`${inputClass} mt-1.5`} value={uploadForm.application_number} onChange={(event) => setUploadForm((current) => ({ ...current, application_number: event.target.value }))} placeholder="Optional" />
                </label>
                <label className="block text-sm font-medium text-slate-700">Issue date
                  <input type="date" className={`${inputClass} mt-1.5`} value={uploadForm.issued_date} onChange={(event) => setUploadForm((current) => ({ ...current, issued_date: event.target.value }))} />
                </label>
                <label className="block text-sm font-medium text-slate-700 sm:col-span-2">Response due date (verify independently)
                  <input type="date" className={`${inputClass} mt-1.5`} value={uploadForm.response_due_date} onChange={(event) => setUploadForm((current) => ({ ...current, response_due_date: event.target.value }))} />
                </label>
              </div>
              <label className="block text-sm font-medium text-slate-700">Office-action PDF
                <input required type="file" accept="application/pdf,.pdf" className={`${inputClass} mt-1.5 file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-xs file:font-semibold`} onChange={(event) => setFile(event.target.files?.[0] || null)} />
              </label>
              <button type="submit" className={`${buttonClass} w-full`} disabled={busy || patents.length === 0}>{busy ? "Working…" : "Upload and extract"}</button>
              {patents.length === 0 && <p className="text-xs text-amber-700">Add a patent and its current specification to the tenant library first.</p>}
            </form>
          </Card>

          <Card title="Uploaded communications" subtitle="Only records in the selected tenant are shown.">
            {loading ? <div className="space-y-3" role="status" aria-label="Loading prosecution records"><div className="ps-skeleton h-20 w-full" /><div className="ps-skeleton h-20 w-full" /><div className="ps-skeleton h-20 w-11/12" /><span className="sr-only">Loading prosecution records…</span></div> : actions.length === 0 ? <p className="text-sm text-slate-500">No office actions uploaded yet.</p> : (
              <div className="space-y-2">
                {actions.map((item) => (
                  <button key={item.id} type="button" onClick={() => setSelectedActionId(item.id)} className={`w-full rounded-xl border p-3 text-left transition ${selectedActionId === item.id ? "border-indigo-300 bg-indigo-50 ring-1 ring-indigo-100" : "border-slate-200 hover:bg-slate-50"}`}>
                    <div className="flex items-start justify-between gap-3"><span className="text-sm font-semibold text-slate-900">{item.application_number || item.patent_title || "Office action"}</span><Badge value={item.extraction_status} /></div>
                    <p className="mt-1 text-xs text-slate-500">{item.patent_title} · {item.jurisdiction} · {item.action_type.replaceAll("_", " ")}</p>
                    <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-slate-600"><span>{item.rejection_count || 0} issues</span><span>·</span><span>{item.cited_reference_count || 0} references</span><span>·</span><Badge value={item.parse_status} /></div>
                  </button>
                ))}
              </div>
            )}
          </Card>
        </div>

        <div className="space-y-6">
          {!detail ? <Card title="Matter detail" subtitle="Select an uploaded office action to inspect extracted evidence."><p className="text-sm text-slate-500">Choose a communication from the list or upload a new one.</p></Card> : <>
            <Card title={detail.application_number || detail.patent_title || "Office action"} subtitle={`${detail.patent_title} · ${detail.jurisdiction} · ${detail.source_filename}`}>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs text-slate-500">Text extraction</p><div className="mt-1"><Badge value={detail.extraction_status} /></div></div>
                <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs text-slate-500">Parsing</p><div className="mt-1"><Badge value={detail.parse_status} /></div></div>
                <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs text-slate-500">Issued</p><p className="mt-1 text-sm font-semibold">{formatDate(detail.issued_date)}</p></div>
                <div className="rounded-xl bg-slate-50 p-3"><p className="text-xs text-slate-500">Due date</p><p className="mt-1 text-sm font-semibold">{formatDate(detail.response_due_date)}</p></div>
              </div>
              {detail.extraction_error_code && <p className="mt-3 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">Extraction failed ({detail.extraction_error_code}). Scanned PDFs may require a separately configured OCR workflow.</p>}
              <div className="mt-4 flex flex-wrap gap-2">
                <button type="button" className={secondaryButtonClass} onClick={() => void downloadSourcePdf()}>Download original PDF</button>
                <button type="button" className={secondaryButtonClass} onClick={() => void processAgain()} disabled={busy || ["PENDING", "PROCESSING", "RETRYING"].includes(detail.extraction_status)}>Re-run extraction</button>
                <button type="button" className={buttonClass} onClick={() => void generateDraft()} disabled={busy || detail.extraction_status !== "EXTRACTED"}>Generate evidence-linked draft</button>
              </div>
            </Card>

            <Card title={`Structured issues (${detail.rejections?.length || 0})`} subtitle="Each parsed issue keeps its original PDF page and an exact source excerpt. Heuristic results need human verification.">
              {!detail.rejections?.length ? <p className="text-sm text-slate-500">No rejection grounds have been parsed. Confirm extraction completed and review the original communication manually.</p> : <div className="space-y-4">
                {detail.rejections.map((rejection, index) => (
                  <article key={rejection.id} className="rounded-xl border border-slate-200 p-4">
                    <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-sm font-semibold text-slate-950">Issue {index + 1}: {rejection.ground_label || rejection.ground}</h3><span className="text-xs text-slate-500">PDF page {rejection.source_page || "—"}</span></div>
                    <p className="mt-1 text-xs text-slate-500">Claims: {rejection.claim_numbers?.length ? rejection.claim_numbers.join(", ") : "not confidently parsed"} · parser confidence {formatConfidence(rejection.parser_confidence)}</p>
                    {rejection.needs_attorney_review && <p className="mt-2 inline-flex rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide text-amber-900">Attorney verification required</p>}
                    <blockquote className="mt-3 whitespace-pre-wrap border-l-2 border-indigo-300 pl-3 text-sm leading-6 text-slate-700">{rejection.source_quote}</blockquote>
                    {rejection.examiner_reasoning && <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3"><p className="text-[10px] font-bold uppercase tracking-wide text-slate-500">Examiner reasoning · extracted text</p><p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-slate-700">{rejection.examiner_reasoning}</p></div>}
                    <div className="mt-3 space-y-2">
                      {(rejection.reference_links || []).map((link) => <div key={link.id} className="rounded-lg bg-slate-50 p-3"><p className="text-sm font-semibold text-indigo-800">{link.cited_reference.identifier}</p><p className="mt-1 text-xs leading-5 text-slate-600">{link.source_quote}</p><p className="mt-1 text-[11px] font-medium text-amber-700">Identifier not externally verified</p></div>)}
                    </div>
                  </article>
                ))}
              </div>}
              {!!detail.cited_references?.length && <div className="mt-4 border-t border-slate-100 pt-4"><h3 className="text-sm font-semibold text-slate-800">All detected patent-style identifiers</h3><div className="mt-2 flex flex-wrap gap-2">{detail.cited_references.map((reference) => <span key={reference.id} title={reference.source_quote} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-indigo-800">{reference.identifier} · p.{reference.source_page || "?"}</span>)}</div></div>}
            </Card>

            <Card title="Amendment disclosure text check" subtitle="Looks for literal text in indexed SPECIFICATION chunks only. This is not a written-description, added-matter, or enablement opinion.">
              <form onSubmit={checkAmendmentSupport} className="space-y-3">
                <label className="block text-sm font-medium text-slate-700">Claim number<input required className={`${inputClass} mt-1.5`} value={amendmentClaimNumber} onChange={(event) => setAmendmentClaimNumber(event.target.value)} /></label>
                <label className="block text-sm font-medium text-slate-700">Proposed claim language<textarea required minLength={8} rows={4} className={`${inputClass} mt-1.5`} value={amendmentText} onChange={(event) => setAmendmentText(event.target.value)} placeholder="Paste the exact proposed phrase or amendment language…" /></label>
                <button type="submit" className={secondaryButtonClass} disabled={busy || !amendmentText.trim()}>Check specification text</button>
              </form>
              {amendmentResult && <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50 p-4"><div className="flex flex-wrap items-center gap-2"><span className="text-sm font-semibold">Text lookup result</span><Badge value={amendmentResult.status} /></div><p className="mt-2 text-sm leading-6 text-slate-700">{amendmentResult.explanation}</p>{amendmentResult.source_manifest?.matches?.map((match, index) => <p key={`${match.chunk_id}-${index}`} className="mt-2 text-xs text-slate-500">Match in chunk {match.chunk_id}, pages {match.page_start ?? "?"}–{match.page_end ?? "?"}: “{match.matching_text}”</p>)}</div>}
            </Card>
          </>}

          {detail && <Card title="Response draft and review" subtitle="Draft snapshots are immutable. Editing creates a new revision; a separate reviewer records approval or requests changes.">
            {drafts.length > 0 && <label className="mb-4 block text-sm font-medium text-slate-700">Draft
              <select className={`${inputClass} mt-1.5`} value={selectedDraftId} onChange={(event) => setSelectedDraftId(event.target.value)}>{drafts.map((draft) => <option key={draft.id} value={draft.id}>{draft.title} · {draft.status} · rev {draft.current_revision_number}</option>)}</select>
            </label>}
            {!currentDraft ? <p className="text-sm text-slate-500">Generate a draft after text extraction is complete.</p> : <>
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div><p className="text-sm font-semibold text-slate-900">Revision {currentDraft.current_revision_number}</p><p className="mt-1 text-xs text-slate-500">SHA-256: {currentDraft.current_revision?.content_sha256 || "—"}</p></div><Badge value={currentDraft.status} /></div>
              <form onSubmit={saveRevision} className="space-y-3">
                <textarea value={draftContent} onChange={(event) => setDraftContent(event.target.value)} rows={22} className={`${inputClass} font-mono text-xs leading-5`} aria-label="Editable response draft" />
                <button type="submit" className={buttonClass} disabled={busy || !draftContent.trim()}>Save as new revision</button>
              </form>
              <div className="mt-5 border-t border-slate-200 pt-4">
                <button type="button" className={`${secondaryButtonClass} mb-4`} onClick={() => void submitDraftForReview()} disabled={busy || !currentDraft.current_revision?.id}>Submit current revision for review</button>
                <label className="block text-sm font-medium text-slate-700">Reviewer comments<textarea className={`${inputClass} mt-1.5`} rows={2} value={reviewComments} onChange={(event) => setReviewComments(event.target.value)} placeholder="Comments are attached to the selected revision…" /></label>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button type="button" className={secondaryButtonClass} onClick={() => void reviewRevision("APPROVED")} disabled={busy || !currentDraft.current_revision?.id || currentDraft.status !== "IN_REVIEW"}>Approve revision</button>
                  <button type="button" className={secondaryButtonClass} onClick={() => void reviewRevision("CHANGES_REQUESTED")} disabled={busy || !currentDraft.current_revision?.id}>Request changes</button>
                  <button type="button" className={secondaryButtonClass} onClick={() => void reviewRevision("REJECTED")} disabled={busy || !currentDraft.current_revision?.id}>Reject draft</button>
                </div>
                <p className="mt-2 text-xs text-slate-500">Approvals are blocked when the reviewer is also the revision author. Server-side role authorization is enforced.</p>
                {(currentDraft.current_revision?.reviews || []).length > 0 && <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3"><h3 className="text-xs font-bold uppercase tracking-wide text-slate-700">Review history · revision {currentDraft.current_revision_number}</h3><div className="mt-2 space-y-2">{currentDraft.current_revision.reviews.map((review) => <article key={review.id} className="rounded-md bg-white p-2"><div className="flex flex-wrap items-center justify-between gap-2"><span className="text-xs font-semibold text-slate-800">{review.reviewer_username || "Reviewer"} · {review.decision.replaceAll("_", " ")}</span><time className="text-[10px] text-slate-500">{new Date(review.created_at).toLocaleString()}</time></div>{review.comments && <p className="mt-1 whitespace-pre-wrap text-xs leading-5 text-slate-600">{review.comments}</p>}</article>)}</div></div>}
              </div>
            </>}
          </Card>}
        </div>
      </div>
    </main>
  );
}
