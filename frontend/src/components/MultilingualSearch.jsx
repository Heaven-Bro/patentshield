import { useCallback, useState } from "react";

import { ApiError, apiRequest, requireResultsArray } from "../lib/apiClient";

const API_ROOT = "/api/v1/multilingual-search";
const inputClass = "ps-input";
const primaryButton = "ps-button-primary";
const secondaryButton = "ps-button-secondary";

const languageNames = {
  en: "English",
  zh: "Chinese",
  ja: "Japanese",
  ko: "Korean",
  und: "Undetermined / non-CJK",
};

function friendlyError(error) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "The request could not be completed.";
}

function Score({ label, value }) {
  if (typeof value !== "number" || value === 0) return null;
  return <span className="rounded-md bg-slate-100 px-2 py-1 text-[11px] text-slate-600">{label}: {value.toFixed(3)}</span>;
}

export default function MultilingualSearch({ tenant }) {
  const [query, setQuery] = useState("");
  const [topN, setTopN] = useState("8");
  const [targetLanguage, setTargetLanguage] = useState("en");
  const [results, setResults] = useState([]);
  const [queryMetadata, setQueryMetadata] = useState(null);
  const [searchBusy, setSearchBusy] = useState(false);
  const [translationBusy, setTranslationBusy] = useState({});
  const [translations, setTranslations] = useState({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const request = useCallback((path, options = {}) => (
    apiRequest(path, { tenantId: tenant.id, ...options })
  ), [tenant.id]);

  async function search(event) {
    event.preventDefault();
    if (!query.trim()) return;
    setSearchBusy(true);
    setError("");
    setNotice("");
    setTranslations({});
    try {
      const payload = await request(`${API_ROOT}/search/`, {
        method: "POST",
        body: { query: query.trim(), top_n: Number(topN) },
      });
      setQueryMetadata(payload);
      setResults(requireResultsArray(payload, "multilingual search results"));
      setNotice(`Search complete — ${payload.count || 0} source passages returned. Original-language text was preserved.`);
    } catch (searchError) {
      setError(friendlyError(searchError));
      setResults([]);
      setQueryMetadata(null);
    } finally {
      setSearchBusy(false);
    }
  }

  async function translateResult(result) {
    setTranslationBusy((current) => ({ ...current, [result.chunk_id]: true }));
    setError("");
    setNotice("");
    try {
      const payload = await request(`${API_ROOT}/translate/`, {
        method: "POST",
        body: { chunk_id: result.chunk_id, target_language: targetLanguage },
      });
      setTranslations((current) => ({ ...current, [result.chunk_id]: payload }));
      setNotice(
        payload.identity_translation
          ? "Source is already in the selected language; the original text was preserved."
          : payload.cached
            ? "Translation loaded from the tenant-scoped cache."
            : "Translation completed on demand.",
      );
    } catch (translationError) {
      setError(friendlyError(translationError));
    } finally {
      setTranslationBusy((current) => ({ ...current, [result.chunk_id]: false }));
    }
  }

  return (
    <main className="ps-page">
      <section className="ps-hero">
        <p className="text-xs font-semibold uppercase tracking-[0.2em] text-indigo-300">Phase 5 · Cross-lingual retrieval</p>
        <div className="mt-3 grid gap-6 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Multilingual Patent Search</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-300">Search Chinese, Japanese, Korean, and mixed-language patent text using multilingual BGE-M3 embeddings and original-language BM25 tokenization. Translation is optional and happens only for a selected source passage.</p>
          </div>
          <div className="rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm">
            <p className="font-semibold text-white">Original source preserved</p>
            <p className="mt-1 text-xs text-slate-300">No corpus-wide translation</p>
          </div>
        </div>
      </section>

      <section className="ps-panel">
        <form onSubmit={search} className="space-y-4">
          <label className="block">
            <span className="mb-1.5 block text-sm font-semibold text-slate-800">Patent query</span>
            <textarea
              className={`${inputClass} min-h-28 resize-y`}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              maxLength={1000}
              placeholder="例如：无线通信中的功率控制方法 · 例: 無線通信の電力制御方法 · 예: 무선 통신 전력 제어 방법"
              required
            />
          </label>
          <div className="grid gap-3 sm:grid-cols-[160px_220px_auto] sm:items-end">
            <label className="block">
              <span className="mb-1.5 block text-xs font-semibold text-slate-600">Result count</span>
              <select className={inputClass} value={topN} onChange={(event) => setTopN(event.target.value)}>
                {[5, 8, 10, 15, 20].map((value) => <option key={value} value={value}>{value} results</option>)}
              </select>
            </label>
            <div>
              <p className="mb-1.5 text-xs font-semibold text-slate-600">Translate a selected result to</p>
              <select className={inputClass} value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value)}>
                <option value="en">English</option>
                <option value="zh">Chinese</option>
                <option value="ja">Japanese</option>
                <option value="ko">Korean</option>
              </select>
            </div>
            <button className={primaryButton} type="submit" disabled={searchBusy || !query.trim()}>
              {searchBusy ? "Searching local index…" : "Search patent passages"}
            </button>
          </div>
          <p className="text-xs leading-5 text-slate-500">Search runs against the active tenant's indexed documents. Search queries are not sent to a translation provider. Translation is separately gated by deployment policy and an explicit tenant allowlist.</p>
        </form>
      </section>

      {error && <div role="alert" className="ps-alert-error">{error}</div>}
      {notice && <div role="status" className="ps-alert-success">{notice}</div>}

      {queryMetadata && (
        <section className="rounded-xl border border-indigo-100 bg-indigo-50 px-4 py-3 text-sm text-indigo-950">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            <span><strong>Detected query language:</strong> {languageNames[queryMetadata.query_language?.language] || queryMetadata.query_language?.language || "Unknown"}</span>
            <span><strong>Confidence:</strong> {queryMetadata.query_language?.confidence || "not reported"}</span>
            <span><strong>Retrieval:</strong> {queryMetadata.retrieval?.mode?.replaceAll("_", " ")}</span>
            <span><strong>Dense model:</strong> {queryMetadata.retrieval?.embedding_model}</span>
          </div>
        </section>
      )}

      <section className="space-y-4">
        {queryMetadata && results.length === 0 && (
          <div className="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-12 text-center">
            <h2 className="text-base font-semibold text-slate-900">No matching passages found</h2>
            <p className="mt-1 text-sm text-slate-500">Try a shorter query, an exact technical phrase, or the original-language terminology.</p>
          </div>
        )}
        {results.map((result, index) => {
          const translation = translations[result.chunk_id];
          return (
            <article key={result.chunk_id} className="ps-panel">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <p className="text-xs font-semibold uppercase tracking-wide text-indigo-700">Result {index + 1} · {result.section_type}{result.claim_number ? ` · Claim ${result.claim_number}` : ""}</p>
                  <p className="mt-1 break-all text-xs text-slate-500">Patent {result.patent_id} · Document version {result.document_version_id}</p>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  <span className="rounded-md bg-blue-50 px-2 py-1 text-[11px] font-semibold text-blue-800">{languageNames[result.source_language] || result.source_language}</span>
                  <Score label="Dense" value={result.scores?.dense} />
                  <Score label="BM25" value={result.scores?.lexical} />
                  <Score label="Rerank" value={result.scores?.rerank} />
                </div>
              </div>
              <p className="mt-4 whitespace-pre-wrap break-words text-sm leading-7 text-slate-800">{result.text}</p>
              {(result.page_start !== null || result.page_end !== null) && (
                <p className="mt-2 text-xs text-slate-500">Source page(s): {result.page_start ?? "?"}{result.page_end !== result.page_start ? `–${result.page_end ?? "?"}` : ""}</p>
              )}
              <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-4">
                <button type="button" className={secondaryButton} disabled={translationBusy[result.chunk_id]} onClick={() => void translateResult(result)}>
                  {translationBusy[result.chunk_id] ? "Translating…" : `Translate to ${languageNames[targetLanguage]}`}
                </button>
                {translation && <span className="text-xs text-slate-500">{translation.cached ? "Cached" : "Fresh translation"} · source unchanged</span>}
              </div>
              {translation && (
                <div className="mt-4 grid gap-4 lg:grid-cols-2">
                  <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                    <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Original · {languageNames[translation.source_language] || translation.source_language}</p>
                    <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">{translation.original_text}</p>
                  </div>
                  <div className="rounded-xl border border-emerald-200 bg-emerald-50/70 p-4">
                    <p className="text-xs font-semibold uppercase tracking-wide text-emerald-800">Translation · {languageNames[translation.target_language] || translation.target_language}</p>
                    <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-6 text-slate-800">{translation.translated_text}</p>
                    <p className="mt-3 text-xs leading-5 text-emerald-900">{translation.legal_notice}</p>
                  </div>
                </div>
              )}
            </article>
          );
        })}
      </section>
    </main>
  );
}
