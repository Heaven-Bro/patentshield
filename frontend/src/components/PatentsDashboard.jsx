import { useState } from "react";

import DocumentUploadPanel from "./DocumentUploadPanel";
import { apiRequest } from "../lib/apiClient";

const STATUS_STYLES = {
  UPLOADED: "border-sky-200 bg-sky-50 text-sky-800",
  PROCESSING: "border-amber-200 bg-amber-50 text-amber-800",
  INDEXED: "border-emerald-200 bg-emerald-50 text-emerald-800",
  FAILED: "border-rose-200 bg-rose-50 text-rose-800",
};

const ROLE_CAPABILITIES = {
  OWNER: ["Upload patent documents", "Run analyses", "Review assessments"],
  ADMIN: ["Upload patent documents", "Run analyses", "Review assessments"],
  ATTORNEY: ["Upload patent documents", "Run analyses", "Review assessments"],
  ANALYST: ["Upload patent documents", "Run analyses", "Review assessments"],
  REVIEWER: ["Review claim assessments"],
  READ_ONLY: ["View authorized documents"],
};

const UPLOAD_ROLES = new Set(["OWNER", "ADMIN", "ATTORNEY", "ANALYST"]);

function formatDate(value) {
  if (!value) return "Unknown date";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unknown date"
    : new Intl.DateTimeFormat(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    }).format(date);
}

function displayStatus(value) {
  return String(value || "UNKNOWN").replaceAll("_", " ");
}

/**
 * Tenant-scoped patent library, PDF upload, and ingestion retry controls.
 *
 * @param {{tenant: object|null, patents: object[], loading: boolean, error: string,
 * onRetry: () => void, onRefresh: () => void, onUploadSubmitted?: (payload: object) => void}} props
 */
export default function PatentsDashboard({
  tenant,
  patents,
  loading,
  error,
  onRetry,
  onRefresh,
  onUploadSubmitted,
}) {
  const [retryingVersionId, setRetryingVersionId] = useState(null);
  const [processingActionError, setProcessingActionError] = useState("");
  const capabilities = ROLE_CAPABILITIES[tenant?.role] || [];
  const canUpload = UPLOAD_ROLES.has(tenant?.role);

  async function retryProcessing(patent, version) {
    if (!tenant?.id || !patent?.id || !version?.id || retryingVersionId) return;
    setRetryingVersionId(version.id);
    setProcessingActionError("");
    try {
      const response = await apiRequest(
        `/api/v1/patents/${patent.id}/versions/${version.id}/process/`,
        { method: "POST", tenantId: tenant.id, body: {} },
      );
      onUploadSubmitted?.(response);
      onRefresh?.();
    } catch (requestError) {
      setProcessingActionError(
        requestError instanceof Error
          ? requestError.message
          : "Could not retry document processing.",
      );
    } finally {
      setRetryingVersionId(null);
    }
  }

  return (
    <main className="ps-page">
      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <p className="ps-eyebrow">Patent library</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-950 sm:text-3xl">
            {tenant ? `${tenant.name} workspace` : "Your workspaces"}
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-500">
            Upload a PDF, track extraction and indexing, and choose an indexed claim for analysis. Every document action is authorized by the backend for this tenant.
          </p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={!tenant || loading}
          className="inline-flex shrink-0 items-center justify-center rounded-lg border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 shadow-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? "Refreshing…" : "Refresh library"}
        </button>
      </div>

      {tenant && (
        <section className="mt-6 rounded-2xl border border-indigo-100 bg-gradient-to-r from-indigo-50 to-white p-4 sm:p-5" aria-label="Current role capabilities">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="text-sm font-semibold text-slate-900">Workspace access</p>
              <p className="mt-1 text-xs text-slate-500">Role-based capabilities for this tenant</p>
            </div>
            <span className="rounded-full border border-indigo-200 bg-white px-3 py-1 text-xs font-semibold text-indigo-800">
              {displayStatus(tenant.role)}
            </span>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            {capabilities.map((capability) => (
              <span key={capability} className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700">
                {capability}
              </span>
            ))}
          </div>
        </section>
      )}

      {tenant && canUpload && (
        <section className="mt-6" aria-label="Upload a new patent">
          <DocumentUploadPanel tenant={tenant} onUploaded={onUploadSubmitted} />
        </section>
      )}

      {tenant && !canUpload && (
        <section className="mt-6 ps-metric-card">
          <h2 className="text-sm font-semibold text-slate-800">Document ingestion</h2>
          <p className="mt-1 text-xs leading-5 text-slate-500">
            Your current role can review authorized documents, but cannot upload or reprocess patent PDFs.
          </p>
        </section>
      )}

      <section className="mt-7" aria-label="Patent documents">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-900">Documents</h2>
          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
            {patents.length} {patents.length === 1 ? "patent" : "patents"}
          </span>
        </div>

        {error && (
          <div className="mb-4 flex flex-col gap-3 rounded-xl border border-rose-200 bg-rose-50 p-4 sm:flex-row sm:items-center sm:justify-between" role="alert">
            <p className="text-sm text-rose-800">{error}</p>
            <button type="button" onClick={onRetry} className="rounded-lg border border-rose-300 bg-white px-3 py-2 text-xs font-semibold text-rose-800 hover:bg-rose-100">Try again</button>
          </div>
        )}
        {processingActionError && (
          <p className="mb-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800" role="alert">
            {processingActionError}
          </p>
        )}

        {loading && patents.length === 0 && (
          <div className="grid gap-3 md:grid-cols-2">
            {[0, 1, 2, 3].map((item) => (
              <div key={item} className="h-36 animate-pulse rounded-2xl border border-slate-200 bg-white" />
            ))}
          </div>
        )}

        {!loading && !error && tenant && patents.length === 0 && (
          <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
            <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-100 text-xl text-slate-500" aria-hidden="true">⌕</div>
            <h3 className="mt-4 text-sm font-semibold text-slate-900">No patent documents in this workspace</h3>
            <p className="mx-auto mt-1 max-w-md text-sm leading-6 text-slate-500">
              Upload a PDF above to start tenant-scoped extraction, claim parsing, embedding, and schematic indexing.
            </p>
          </div>
        )}

        {!loading && patents.length > 0 && (
          <div className="grid gap-3 lg:grid-cols-2">
            {patents.map((patent) => {
              const versions = Array.isArray(patent.versions) ? patent.versions : [];
              const latestVersion = versions[0] || null;
              const status = patent.status || "UPLOADED";
              const versionStatus = latestVersion?.extraction_status || "PENDING";
              return (
                <article key={patent.id} className="ps-panel transition hover:border-slate-300 hover:shadow-md">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.13em] text-slate-400">
                        {patent.jurisdiction || "Patent record"}{patent.publication_number ? ` · ${patent.publication_number}` : ""}
                      </p>
                      <h3 className="mt-1 break-words text-base font-semibold leading-6 text-slate-900">{patent.title || "Untitled patent"}</h3>
                    </div>
                    <span className={`shrink-0 rounded-full border px-2.5 py-1 text-[10px] font-bold tracking-wide ${STATUS_STYLES[status] || "border-slate-200 bg-slate-50 text-slate-700"}`}>
                      {displayStatus(status)}
                    </span>
                  </div>
                  {patent.abstract && <p className="mt-3 line-clamp-3 text-sm leading-5 text-slate-500">{patent.abstract}</p>}
                  <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3 text-xs text-slate-500">
                    <span>Added {formatDate(patent.created_at)}</span>
                    <span>{versions.length} {versions.length === 1 ? "document version" : "document versions"}</span>
                  </div>
                  {latestVersion && (
                    <div className="mt-3 flex flex-col gap-2 rounded-lg bg-slate-50 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
                      <div className="text-xs">
                        <span className="font-medium text-slate-700">Version {latestVersion.version_number}</span>
                        <span className="ml-2 text-slate-500">Extraction: {displayStatus(versionStatus)}</span>
                        {latestVersion.page_count ? <span className="ml-2 text-slate-500">{latestVersion.page_count} pages</span> : null}
                      </div>
                      {canUpload && versionStatus === "FAILED" && (
                        <button
                          type="button"
                          onClick={() => void retryProcessing(patent, latestVersion)}
                          disabled={retryingVersionId === latestVersion.id}
                          className="rounded-md border border-rose-300 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                        >
                          {retryingVersionId === latestVersion.id ? "Queueing…" : "Retry processing"}
                        </button>
                      )}
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>
    </main>
  );
}
