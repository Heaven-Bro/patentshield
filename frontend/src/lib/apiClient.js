const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000")
  .replace(/\/+$/, "");
const API_READ_TIMEOUT_MS = 30_000;

let csrfToken = "";
let csrfRequest = null;

/**
 * A typed-in-JSDoc HTTP error that preserves status and safe API error text.
 */
export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {number} status
   * @param {unknown} payload
   */
  constructor(message, status, payload = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

/**
 * A successful HTTP response that violates a known collection contract.
 */
export class ApiResponseShapeError extends Error {
  /** @param {string} resourceName */
  constructor(resourceName) {
    super(`The ${resourceName} response was not in the expected format. Please refresh and try again.`);
    this.name = "ApiResponseShapeError";
  }
}

/**
 * Read a collection envelope without treating malformed success as no records.
 * @param {unknown} payload
 * @param {string} [resourceName]
 * @returns {any[]}
 */
export function requireResultsArray(payload, resourceName = "collection") {
  if (!Array.isArray(payload?.results)) {
    throw new ApiResponseShapeError(resourceName);
  }
  return payload.results;
}

function createRequestDeadline(callerSignal, timeoutMs) {
  const controller = new AbortController();
  let timedOut = false;
  let timerId = null;
  const forwardAbort = () => controller.abort(callerSignal?.reason);
  if (callerSignal?.aborted) forwardAbort();
  else callerSignal?.addEventListener("abort", forwardAbort, { once: true });
  if (timeoutMs > 0) {
    timerId = globalThis.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
  }
  return {
    signal: controller.signal,
    didTimeout: () => timedOut,
    cleanup: () => {
      if (timerId !== null) globalThis.clearTimeout(timerId);
      callerSignal?.removeEventListener("abort", forwardAbort);
    },
  };
}

/**
 * Bootstrap a CSRF token. A single in-flight request is shared by concurrent
 * callers so React StrictMode does not issue duplicate bootstrap requests.
 * @param {{force?: boolean}} [options]
 * @returns {Promise<string>}
 */
export async function bootstrapCsrfToken({ force = false } = {}) {
  if (!force && csrfToken) return csrfToken;
  if (!force && csrfRequest) return csrfRequest;

  csrfRequest = (async () => {
    const deadline = createRequestDeadline(null, API_READ_TIMEOUT_MS);
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/auth/csrf/`, {
        method: "GET",
        credentials: "include",
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: deadline.signal,
      });
      const payload = await parseResponse(response);
      if (!response.ok || typeof payload?.csrfToken !== "string") {
        throw new ApiError(
          "Could not initialize browser security. Check the backend and CORS settings.",
          response.status,
          payload,
        );
      }
      csrfToken = payload.csrfToken;
      return csrfToken;
    } catch (requestError) {
      if (deadline.didTimeout()) {
        throw new ApiError("Browser security initialization timed out. Please try again.", 0);
      }
      throw requestError;
    } finally {
      deadline.cleanup();
    }
  })();

  try {
    return await csrfRequest;
  } finally {
    csrfRequest = null;
  }
}

/**
 * Store the rotated CSRF token returned after login/logout.
 * @param {string | null | undefined} value
 */
export function updateCsrfToken(value) {
  if (typeof value === "string" && value.length > 0) csrfToken = value;
}

/**
 * Build the API URL for JSON requests or a permission-checked resource URL.
 * @param {string} path
 * @returns {string}
 */
export function apiUrl(path) {
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE_URL}/${path.replace(/^\/+/, "")}`;
}

/**
 * Send a JSON or FormData request using Django's cookie session and CSRF scheme.
 * The tenant ID is an explicit selector, never an authorization credential;
 * the backend must still verify active membership on every tenant-scoped API.
 * @param {string} path
 * @param {{method?: string, tenantId?: string | null, body?: unknown, headers?: Record<string,string>, signal?: AbortSignal, timeoutMs?: number}} [options]
 * @returns {Promise<any>}
 */
export async function apiRequest(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = {
    Accept: "application/json",
    ...(options.headers || {}),
  };
  if (options.tenantId) headers["X-Tenant-ID"] = options.tenantId;

  let body;
  if (options.body !== undefined && options.body !== null) {
    if (
      typeof FormData !== "undefined" && options.body instanceof FormData
    ) {
      body = options.body;
    } else if (
      typeof Blob !== "undefined" && options.body instanceof Blob
    ) {
      body = options.body;
    } else if (typeof options.body === "string") {
      body = options.body;
      if (!headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
      }
    } else {
      body = JSON.stringify(options.body);
      headers["Content-Type"] = "application/json";
    }
  }

  if (!new Set(["GET", "HEAD", "OPTIONS", "TRACE"]).has(method)) {
    if (!csrfToken) await bootstrapCsrfToken();
    headers["X-CSRFToken"] = csrfToken;
  }

  const timeoutMs = options.timeoutMs === undefined
    ? (method === "GET" ? API_READ_TIMEOUT_MS : 0)
    : Number(options.timeoutMs);
  const deadline = createRequestDeadline(
    options.signal,
    Number.isFinite(timeoutMs) ? timeoutMs : 0,
  );
  try {
    const response = await fetch(apiUrl(path), {
      method,
      credentials: "include",
      headers,
      body,
      signal: deadline.signal,
      cache: "no-store",
    });
    const payload = await parseResponse(response);
    updateCsrfToken(payload?.csrfToken);

    if (!response.ok) {
      throw new ApiError(extractErrorMessage(payload, response.status), response.status, payload);
    }
    return payload;
  } catch (requestError) {
    if (deadline.didTimeout()) {
      throw new ApiError("This request timed out. Please try again.", 0);
    }
    throw requestError;
  } finally {
    deadline.cleanup();
  }
}

/**
 * Download a tenant-authorized binary artifact using the session cookie and
 * explicit tenant selector. Direct anchor URLs cannot attach X-Tenant-ID.
 * @param {string} path
 * @param {{tenantId?: string | null, fallbackFilename?: string}} [options]
 * @returns {Promise<void>}
 */
export async function downloadArtifact(path, options = {}) {
  const headers = {
    Accept: [
      "application/pdf",
      "application/json",
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "text/csv",
    ].join(", "),
  };
  if (options.tenantId) headers["X-Tenant-ID"] = options.tenantId;

  const response = await fetch(apiUrl(path), {
    method: "GET",
    credentials: "include",
    headers,
    cache: "no-store",
  });

  if (!response.ok) {
    let payload = null;
    if ((response.headers.get("content-type") || "").includes("application/json")) {
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
    }
    throw new ApiError(
      extractErrorMessage(payload, response.status),
      response.status,
      payload,
    );
  }

  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const encodedFilename = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plainFilename = disposition.match(/filename="?([^";]+)"?/i)?.[1];
  let filename = options.fallbackFilename || "patentshield-report";
  try {
    filename = encodedFilename
      ? decodeURIComponent(encodedFilename)
      : plainFilename || filename;
  } catch {
    filename = plainFilename || filename;
  }

  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  anchor.rel = "noopener noreferrer";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

/** @param {Response} response */
async function parseResponse(response) {
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  // Avoid surfacing HTML error pages or server internals to the browser UI.
  return null;
}

/** @param {any} payload @param {number} status */
function extractErrorMessage(payload, status) {
  if (typeof payload?.detail === "string") return payload.detail;
  if (typeof payload?.message === "string") return payload.message;
  if (typeof payload?.non_field_errors?.[0] === "string") {
    return payload.non_field_errors[0];
  }
  if (status === 401) return "Your session is not authenticated. Please sign in.";
  if (status === 403) return "The request was denied. Check your tenant access and CSRF token.";
  if (status === 404) return "The requested resource was not found in this workspace.";
  return `PatentShield request failed (HTTP ${status}).`;
}
