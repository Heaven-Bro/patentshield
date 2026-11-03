import { useMemo, useState } from "react";

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

function patentSearchText(patent) {
  const versionText = Array.isArray(patent.versions)
    ? patent.versions.map((version) => [
      version.version_number,
      version.extraction_status,
      version.text_indexing_status,
      version.page_count,
    ].join(" ")).join(" ")
    : "";
  return [
    patent.title,
    patent.abstract,
    patent.publication_number,
    patent.application_number,
    patent.jurisdiction,
    patent.source_type,
    patent.status,
    versionText,
  ].filter(Boolean).join(" ").toLocaleLowerCase();
}

/**
 * Tenant-scoped patent library, PDF upload, and ingestion retry controls.
 * Search and status filters operate only on the already authorized list response.
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
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("ALL");
  const capabilities = ROLE_CAPABILITIES[tenant?.role] || [];
  const canUpload = UPLOAD_ROLES.has(tenant?.role);

  const statusOptions = useMemo(
    () => [...new Set(patents.map((patent) => String(patent.status || "UPLOADED")))].sort(),
    [patents],
  );
  const filteredPatents = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase();
    return patents.filter((patent) => {
      const matchesStatus = statusFilter === "ALL"
        || String(patent.status || "UPLOADED") === statusFilter;
      const matchesQuery = !normalizedQuery || patentSearchText(patent).includes(normalizedQuery);
      return matchesStatus && matchesQuery;
    });
  }, [patents, query, statusFilter]);

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

  function clearFilters() {
    setQuery("");
    setStatusFilter("ALL");
  }

  return (
    <main className="ps-page space-y-5" aria-label="Patent library">
      <section className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(290px,0.42fr)] xl:items-end">
        <div>
          <div className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.17em] text-slate-500">
            <span className="h-px w-6 bg-indigo-500" /> Patent records
          </div>
          <h2 className="mt-2 font-editorial text-3xl font-medium tracking-tight text-slate-950 sm:text-4xl">
            {tenant ? tenant.name : "Your workspaces"}
          </h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-600">
            A tenant-scoped library of source records and document versions. Search visible metadata, review processing state, then continue into the existing analysis workspace below.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 xl:justify-end">
          {tenant && (
            <div className="mr-auto flex items-center gap-2 rounded-lg border border-paper-line bg-paper-soft px-3 py-2 xl:mr-1">
              <span className="h-2 w-2 rounded-full bg-indigo-500" aria-hidden="true" />
              <div>
                <p className="text-[9px] font-bold uppercase tracking-[0.15em] text-slate-500">Workspace role</p>
                <p className="mt-0.5 text-xs font-semibold text-slate-800">{displayStatus(tenant.role)}</p>
                <p className="mt-1 max-w-64 text-[10px] leading-4 text-slate-500">{capabilities.join(" · ")}</p>
              </div>
            </div>
          )}
          <button
            type="button"
            onClick={onRefresh}
            disabled={!tenant || loading}
            className="ps-button-secondary min-h-10"
          >
            <svg viewBox="0 0 20 20" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.7" aria-hidden="true">
              <path d="M16.2 8A6.4 6.4 0 0 0 5.3 5.4L3.8 7M3.8 7V3.5M3.8 7h3.5M3.8 12a6.4 6.4 0 0 0 10.9 2.6l1.5-1.6m0 0v3.5m0-3.5h-3.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {loading ? "Refreshing…" : "Refresh library"}
          </button>
        </div>
      </section>

      {tenant && canUpload && (
        <section aria-label="Upload a new patent">
          <DocumentUploadPanel tenant={tenant} onUploaded={onUploadSubmitted} />
        </section>
      )}

      {tenant && !canUpload && (
        <section className="flex flex-col gap-2 rounded-lg border border-paper-line bg-paper-soft px-4 py-3 sm:flex-row sm:items-center sm:justify-between" aria-label="Document ingestion permissions">
          <div>
            <h3 className="text-xs font-semibold text-slate-900">Document ingestion</h3>
            <p className="mt-0.5 text-xs leading-5 text-slate-600">Your current role can review authorized documents, but cannot upload or reprocess patent PDFs.</p>
          </div>
          <span className="inline-flex w-fit items-center rounded-full border border-slate-200 bg-white px-2.5 py-1 text-[9px] font-bold uppercase tracking-wide text-slate-600">View access</span>
        </section>
      )}

      <section className="overflow-hidden rounded-xl border border-paper-line bg-paper-soft shadow-[0_1px_2px_rgba(15,23,42,0.035)]" aria-label="Patent documents">
        <div className="border-b border-paper-line px-4 py-4 sm:px-5">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <p className="text-[9px] font-bold uppercase tracking-[0.17em] text-slate-500">Evidence index</p>
              <div className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <h3 className="font-editorial text-xl font-medium text-slate-950">Patent documents</h3>
                <span className="text-xs text-slate-500" aria-live="polite">
                  {loading && patents.length === 0
                    ? "Loading records…"
                    : `Showing ${filteredPatents.length} of ${patents.length} ${patents.length === 1 ? "record" : "records"}`}
                </span>
              </div>
            </div>
            <div className="grid gap-2 sm:grid-cols-[minmax(220px,1fr)_180px] lg:w-[min(100%,570px)]">
              <label className="relative block">
                <span className="sr-only">Search visible patent records</span>
                <svg viewBox="0 0 20 20" className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
                  <circle cx="8.7" cy="8.7" r="5.7" />
                  <path d="m13 13 4 4" strokeLinecap="round" />
                </svg>
                <input
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Title, publication number, abstract…"
                  className="ps-input min-h-10 pl-9"
                />
              </label>
              <label className="block">
                <span className="sr-only">Filter by patent status</span>
                <select
                  value={statusFilter}
                  onChange={(event) => setStatusFilter(event.target.value)}
                  className="ps-input min-h-10"
                >
                  <option value="ALL">All statuses</option>
                  {statusOptions.map((status) => (
                    <option key={status} value={status}>{displayStatus(status)}</option>
                  ))}
                </select>
              </label>
            </div>
          </div>
        </div>

        <div className="p-4 sm:p-5">
          {error && (
            <div className="mb-4 flex flex-col gap-3 rounded-lg border border-rose-200 bg-rose-50 p-4 sm:flex-row sm:items-center sm:justify-between" role="alert">
              <div>
                <p className="text-[9px] font-bold uppercase tracking-[0.15em] text-rose-700">Library unavailable</p>
                <p className="mt-1 text-sm text-rose-800">{error}</p>
              </div>
              <button type="button" onClick={onRetry} className="ps-button-secondary shrink-0 border-rose-200 text-rose-800 hover:bg-rose-100">Try again</button>
            </div>
          )}
          {processingActionError && (
            <p className="mb-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800" role="alert">
              {processingActionError}
            </p>
          )}

          {loading && patents.length === 0 && (
            <div className="grid gap-3 lg:grid-cols-2" role="status" aria-label="Loading patent records">
              {[0, 1, 2, 3].map((item) => (
                <div key={item} className="h-44 animate-pulse rounded-lg border border-paper-line bg-white" />
              ))}
            </div>
          )}

          {!loading && !error && tenant && patents.length === 0 && (
            <div className="ps-empty-state border-paper-line bg-white py-14">
              <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-lg border border-indigo-100 bg-indigo-50 text-indigo-700" aria-hidden="true">
                <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M7 3.75h7l4 4v12.5H7a2 2 0 0 1-2-2v-12.5a2 2 0 0 1 2-2Z"/><path d="M14 4v4h4M8 13h7M8 16.5h7" strokeLinecap="round"/></svg>
              </div>
              <h4 className="mt-4 font-editorial text-xl text-slate-950">No patent documents yet</h4>
              <p className="mx-auto mt-1 max-w-md text-sm leading-6 text-slate-600">
                {canUpload
                  ? "Add an authorized source document to begin extraction, claim parsing, and evidence review."
                  : "No patent documents are currently visible in this workspace. Contact a workspace owner if you expected access."}
              </p>
            </div>
          )}

          {!loading && !error && patents.length > 0 && filteredPatents.length === 0 && (
            <div className="ps-empty-state py-12">
              <h4 className="font-editorial text-lg text-slate-950">No records match this view</h4>
              <p className="mt-1 text-sm text-slate-600">Try another search term or status, or clear the current filters.</p>
              <button type="button" onClick={clearFilters} className="ps-button-secondary mt-4">Clear filters</button>
            </div>
          )}

          {filteredPatents.length > 0 && (
            <div className="grid gap-3 xl:grid-cols-2">
              {filteredPatents.map((patent) => {
                const versions = Array.isArray(patent.versions) ? patent.versions : [];
                const latestVersion = versions[0] || null;
                const status = patent.status || "UPLOADED";
                const versionStatus = latestVersion?.extraction_status || "PENDING";
                const statusClasses = STATUS_STYLES[status] || "border-slate-200 bg-slate-50 text-slate-700";
                return (
                  <article key={patent.id} className="group min-w-0 rounded-lg border border-paper-line bg-white p-4 transition-colors hover:border-indigo-200 sm:p-5">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <p className="text-[9px] font-bold uppercase tracking-[0.16em] text-slate-500">
                          {patent.jurisdiction || "Patent record"}
                          {patent.publication_number ? <span className="ml-2 font-mono text-[10px] font-semibold tracking-normal text-indigo-800">{patent.publication_number}</span> : null}
                        </p>
                        <h4 className="mt-2 break-words font-editorial text-lg leading-6 text-slate-950 sm:text-xl">{patent.title || "Untitled patent"}</h4>
                        {patent.application_number && (
                          <p className="mt-1 text-[11px] text-slate-500">Application <span className="font-mono tabular-nums text-slate-700">{patent.application_number}</span></p>
                        )}
                      </div>
                      <span className={`ps-status-pill shrink-0 ${statusClasses}`}>
                        <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
                        {displayStatus(status)}
                      </span>
                    </div>

                    {patent.abstract && (
                      <p className="mt-3 line-clamp-3 text-sm leading-6 text-slate-600">{patent.abstract}</p>
                    )}

                    <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-slate-100 pt-3 text-[11px] text-slate-500">
                      <span>Added <time dateTime={patent.created_at}>{formatDate(patent.created_at)}</time></span>
                      {patent.source_type && <span>Source: {displayStatus(patent.source_type)}</span>}
                      <span>{versions.length} {versions.length === 1 ? "document version" : "document versions"}</span>
                    </div>

                    {latestVersion ? (
                      <div className="mt-3 flex flex-col gap-3 rounded-md border border-paper-line bg-paper-soft px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
                        <div className="min-w-0 text-xs">
                          <p className="text-[9px] font-bold uppercase tracking-[0.14em] text-slate-500">Latest source version</p>
                          <div className="mt-1 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-slate-700">
                            <span className="font-semibold tabular-nums">Version {latestVersion.version_number}</span>
                            <span className="inline-flex items-center gap-1.5">
                              <span className={`h-1.5 w-1.5 rounded-full ${versionStatus === "EXTRACTED" ? "bg-emerald-600" : versionStatus.includes("FAILED") ? "bg-rose-500" : "bg-amber-500"}`} aria-hidden="true" />
                              {displayStatus(versionStatus)}
                            </span>
                            {latestVersion.page_count ? <span className="tabular-nums">{latestVersion.page_count} pages</span> : null}
                          </div>
                        </div>
                        {canUpload && versionStatus === "FAILED" && (
                          <button
                            type="button"
                            onClick={() => void retryProcessing(patent, latestVersion)}
                            disabled={retryingVersionId === latestVersion.id}
                            className="ps-button-secondary shrink-0 border-rose-200 text-rose-700 hover:border-rose-300 hover:bg-rose-50"
                          >
                            {retryingVersionId === latestVersion.id ? "Queueing…" : "Retry processing"}
                          </button>
                        )}
                      </div>
                    ) : (
                      <p className="mt-3 text-xs text-slate-500">No document versions are included in this record.</p>
                    )}
                  </article>
                );
              })}
            </div>
          )}
        </div>
      </section>
    </main>
  );
}
