import { useRef, useState } from "react";

import { ApiError, apiRequest } from "../lib/apiClient";

const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

/**
 * Tenant-authorized PDF upload form.
 *
 * @param {{tenant: {id: string, role?: string}, onUploaded?: (result: object) => void}} props
 */
export default function DocumentUploadPanel({ tenant, onUploaded }) {
  const formRef = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const canUpload = Boolean(tenant?.id) && [
    "OWNER",
    "ADMIN",
    "ATTORNEY",
    "ANALYST",
  ].includes(tenant?.role);

  async function handleSubmit(event) {
    event.preventDefault();
    if (!canUpload || busy || !formRef.current) return;

    setBusy(true);
    setError("");
    setSuccess("");

    const formData = new FormData(formRef.current);
    const file = formData.get("file");
    if (!(file instanceof File) || file.size <= 0) {
      setError("Choose a non-empty PDF file to upload.");
      setBusy(false);
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setError("The PDF exceeds the 100 MiB upload limit.");
      setBusy(false);
      return;
    }
    if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
      setError("Choose a PDF file. The server also validates the PDF signature.");
      setBusy(false);
      return;
    }

    try {
      const response = await apiRequest("/api/v1/patents/upload/", {
        method: "POST",
        tenantId: tenant.id,
        body: formData,
      });
      const title = response?.patent?.title || formData.get("title") || "Patent document";
      setSuccess(`${title} was uploaded. Ingestion has been queued.`);
      formRef.current.reset();
      onUploaded?.(response);
    } catch (requestError) {
      // HTTP 503 can mean the PDF was persisted but the broker was unavailable.
      // Refresh the library in that case so the failed version remains visible.
      if (
        requestError instanceof ApiError
        && requestError.status === 503
        && requestError.payload?.patent_id
        && requestError.payload?.document_version_id
      ) {
        onUploaded?.(requestError.payload);
        setError(
          "The PDF was stored, but ingestion could not be queued. The document is in the library; retry processing after the worker/broker is available.",
        );
      } else {
        setError(
          requestError instanceof Error
            ? requestError.message
            : "The PDF could not be uploaded.",
        );
      }
    } finally {
      setBusy(false);
    }
  }

  if (!canUpload) {
    return (
      <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
        <h2 className="text-sm font-semibold text-slate-800">Upload a patent PDF</h2>
        <p className="mt-1 text-xs leading-5 text-slate-500">
          Upload access is available to tenant owners, administrators, attorneys, and analysts.
        </p>
      </div>
    );
  }

  return (
    <section className="rounded-2xl border border-indigo-100 bg-white p-4 shadow-sm sm:p-5" aria-labelledby="document-upload-heading">
      <div className="flex flex-col justify-between gap-2 sm:flex-row sm:items-start">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-indigo-700">Document ingestion</p>
          <h2 id="document-upload-heading" className="mt-1 text-base font-semibold text-slate-950">Upload a patent PDF</h2>
          <p className="mt-1 text-sm leading-5 text-slate-500">
            Documents are stored in this workspace, then asynchronously extracted and indexed.
          </p>
        </div>
        <span className="w-fit rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-[10px] font-semibold uppercase tracking-wide text-slate-600">
          Max 100 MiB
        </span>
      </div>

      <form ref={formRef} onSubmit={handleSubmit} className="mt-4 grid gap-3 md:grid-cols-2" encType="multipart/form-data">
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-slate-700">Patent title <span className="text-rose-600">*</span></span>
          <input
            name="title"
            type="text"
            maxLength={500}
            required
            placeholder="e.g. Adaptive wireless sensor system"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-slate-700">Publication number</span>
          <input
            name="publication_number"
            type="text"
            maxLength={100}
            placeholder="Optional"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-slate-700">Application number</span>
          <input
            name="application_number"
            type="text"
            maxLength={100}
            placeholder="Optional"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-xs font-semibold text-slate-700">Jurisdiction</span>
          <input
            name="jurisdiction"
            type="text"
            maxLength={10}
            placeholder="e.g. US, EP, WO"
            className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
          />
        </label>
        <label className="block md:col-span-2">
          <span className="mb-1 block text-xs font-semibold text-slate-700">Abstract</span>
          <textarea
            name="abstract"
            rows={2}
            placeholder="Optional patent abstract or short summary"
            className="w-full resize-y rounded-lg border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100"
          />
        </label>
        <label className="block md:col-span-2">
          <span className="mb-1 block text-xs font-semibold text-slate-700">PDF file <span className="text-rose-600">*</span></span>
          <input
            name="file"
            type="file"
            accept="application/pdf,.pdf"
            required
            className="block w-full rounded-lg border border-slate-300 bg-white text-sm text-slate-700 file:mr-3 file:border-0 file:border-r file:border-slate-300 file:bg-slate-50 file:px-4 file:py-2.5 file:text-xs file:font-semibold file:text-slate-700 hover:file:bg-slate-100"
          />
        </label>
        {error && <p className="md:col-span-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs leading-5 text-rose-800" role="alert">{error}</p>}
        {success && <p className="md:col-span-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs leading-5 text-emerald-800" role="status" aria-live="polite">{success}</p>}
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center md:col-span-2">
          <button
            type="submit"
            disabled={busy}
            className="inline-flex items-center justify-center rounded-lg bg-indigo-700 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-800 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {busy ? "Uploading and queueing…" : "Upload and process PDF"}
          </button>
          <p className="text-[11px] leading-5 text-slate-500">
            The server validates file size and PDF signature. Processing may take time; the library refreshes while work is pending.
          </p>
        </div>
      </form>
    </section>
  );
}
