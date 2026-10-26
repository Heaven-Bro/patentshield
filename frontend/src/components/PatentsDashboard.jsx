const STATUS_STYLES = {
  UPLOADED: "border-sky-200 bg-sky-50 text-sky-800",
  PROCESSING: "border-amber-200 bg-amber-50 text-amber-800",
  INDEXED: "border-emerald-200 bg-emerald-50 text-emerald-800",
  FAILED: "border-rose-200 bg-rose-50 text-rose-800",
};

const ROLE_CAPABILITIES = {
  OWNER: ["Upload patent documents", "Review claim assessments"],
  ADMIN: ["Upload patent documents", "Review claim assessments"],
  ATTORNEY: ["Upload patent documents", "Review claim assessments"],
  ANALYST: ["Upload patent documents", "Review claim assessments"],
  REVIEWER: ["Review claim assessments"],
  READ_ONLY: ["View authorized documents"],
};

function formatDate(value) {
  if (!value) return "Unknown date";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Unknown date"
    : new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric" }).format(date);
}

/**
 * Tenant-scoped patent index and role/capability summary.
 * @param {{tenant:object|null, patents:object[], loading:boolean, error:string, onRetry:()=>void, onRefresh:()=>void}} props
 */
export default function PatentsDashboard({ tenant, patents, loading, error, onRetry, onRefresh }) {
  const capabilities = ROLE_CAPABILITIES[tenant?.role] || [];

  return (
    <main className="mx-auto w-full max-w-7xl px-4 py-7 sm:px-6 lg:px-8">
      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-indigo-700">Patent library</p>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight text-slate-950 sm:text-3xl">
            {tenant ? `${tenant.name} workspace` : "Your workspaces"}
          </h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-500">
            View document processing status for patents available in your active tenant. Access is enforced by the API for every request.
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
              {tenant.role.replaceAll("_", " ")}
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

      <section className="mt-7" aria-label="Patent documents">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-semibold text-slate-900">Documents</h2>
          <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">
            {patents.length} {patents.length === 1 ? "patent" : "patents"}
          </span>
        </div>

        {error && (
          <div className="flex flex-col gap-3 rounded-xl border border-rose-200 bg-rose-50 p-4 sm:flex-row sm:items-center sm:justify-between" role="alert">
            <p className="text-sm text-rose-800">{error}</p>
            <button type="button" onClick={onRetry} className="rounded-lg border border-rose-300 bg-white px-3 py-2 text-xs font-semibold text-rose-800 hover:bg-rose-100">Try again</button>
          </div>
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
              This tenant does not currently have any patent records visible to your account.
            </p>
          </div>
        )}

        {!loading && patents.length > 0 && (
          <div className="grid gap-3 lg:grid-cols-2">
            {patents.map((patent) => {
              const latestVersion = Array.isArray(patent.versions) ? patent.versions[0] : null;
              const status = patent.status || "UPLOADED";
              return (
                <article key={patent.id} className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm transition hover:border-slate-300 hover:shadow-md">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.13em] text-slate-400">
                        {patent.jurisdiction || "Patent record"}{patent.publication_number ? ` · ${patent.publication_number}` : ""}
                      </p>
                      <h3 className="mt-1 break-words text-base font-semibold leading-6 text-slate-900">{patent.title || "Untitled patent"}</h3>
                    </div>
                    <span className={`shrink-0 rounded-full border px-2.5 py-1 text-[10px] font-bold tracking-wide ${STATUS_STYLES[status] || "border-slate-200 bg-slate-50 text-slate-700"}`}>
                      {status.replaceAll("_", " ")}
                    </span>
                  </div>
                  {patent.abstract && <p className="mt-3 line-clamp-3 text-sm leading-5 text-slate-500">{patent.abstract}</p>}
                  <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 pt-3 text-xs text-slate-500">
                    <span>Added {formatDate(patent.created_at)}</span>
                    <span>{patent.versions?.length || 0} {patent.versions?.length === 1 ? "document version" : "document versions"}</span>
                  </div>
                  {latestVersion && (
                    <div className="mt-3 flex items-center justify-between rounded-lg bg-slate-50 px-3 py-2 text-xs">
                      <span className="font-medium text-slate-700">Version {latestVersion.version_number}</span>
                      <span className="text-slate-500">Extraction: {String(latestVersion.extraction_status || "PENDING").replaceAll("_", " ")}</span>
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
