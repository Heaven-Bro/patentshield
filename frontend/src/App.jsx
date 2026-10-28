import { useCallback, useEffect, useMemo, useState } from "react";

import AnalysisConsole from "./components/AnalysisConsole";
import LoginPage from "./components/LoginPage";
import PatentsDashboard from "./components/PatentsDashboard";
import SurveillanceDashboard from "./components/SurveillanceDashboard";
import OfficeActionAssistant from "./components/OfficeActionAssistant";
import MultilingualSearch from "./components/MultilingualSearch";
import FTOWorkspace from "./components/FTOWorkspace";
import LandscapeAnalytics from "./components/LandscapeAnalytics";
import EnterpriseAdminConsole from "./components/EnterpriseAdminConsole";
import TenantSwitcher from "./components/TenantSwitcher";
import { ApiError, apiRequest, bootstrapCsrfToken } from "./lib/apiClient";
import { useAuthStore } from "./stores/authStore";
import { useDocumentStore } from "./stores/documentStore";
import { useMatrixStore } from "./stores/matrixStore";

/**
 * Root authentication gate and tenant-aware application shell.
 * Authenticated tenant shell connecting document ingestion and analysis review.
 */
export default function App() {
  const status = useAuthStore((state) => state.status);
  const user = useAuthStore((state) => state.user);
  const tenants = useAuthStore((state) => state.tenants);
  const activeTenantId = useAuthStore((state) => state.activeTenantId);
  const setSession = useAuthStore((state) => state.setSession);
  const setActiveTenant = useAuthStore((state) => state.setActiveTenant);
  const setAnonymous = useAuthStore((state) => state.setAnonymous);
  const setInitializationError = useAuthStore((state) => state.setInitializationError);
  const setChecking = useAuthStore((state) => state.setChecking);
  const initializationError = useAuthStore((state) => state.initializationError);

  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [logoutError, setLogoutError] = useState("");
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [patents, setPatents] = useState([]);
  const [patentsLoading, setPatentsLoading] = useState(false);
  const [patentsError, setPatentsError] = useState("");
  const [refreshCounter, setRefreshCounter] = useState(0);
  const [activeSection, setActiveSection] = useState("workspace");

  const activeTenant = useMemo(
    () => tenants.find((tenant) => tenant.id === activeTenantId) || null,
    [tenants, activeTenantId],
  );

  const refreshSession = useCallback(async () => {
    setChecking();
    try {
      await bootstrapCsrfToken({ force: true });
      const session = await apiRequest("/api/v1/auth/me/");
      const membershipPayload = await apiRequest("/api/v1/tenants/");
      setSession(session.user, membershipPayload.results || []);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setAnonymous();
        return;
      }
      setInitializationError(
        error instanceof Error
          ? error.message
          : "Unable to contact the PatentShield authentication service.",
      );
    }
  }, [setAnonymous, setChecking, setInitializationError, setSession]);

  useEffect(() => {
    let active = true;
    const initialize = async () => {
      try {
        await bootstrapCsrfToken();
        const session = await apiRequest("/api/v1/auth/me/");
        const membershipPayload = await apiRequest("/api/v1/tenants/");
        if (active) setSession(session.user, membershipPayload.results || []);
      } catch (error) {
        if (!active) return;
        if (error instanceof ApiError && error.status === 401) {
          setAnonymous();
        } else {
          setInitializationError(
            error instanceof Error
              ? error.message
              : "Unable to contact the PatentShield authentication service.",
          );
        }
      }
    };
    void initialize();
    return () => { active = false; };
  }, [setAnonymous, setInitializationError, setSession]);

  const loadPatents = useCallback(async (signal) => {
    setPatentsLoading(true);
    setPatentsError("");
    try {
      const response = await apiRequest("/api/v1/patents/", {
        tenantId: activeTenantId,
        signal,
      });
      if (signal?.aborted || useAuthStore.getState().activeTenantId !== activeTenantId) return;
      setPatents(Array.isArray(response.results) ? response.results : []);
    } catch (error) {
      if (error?.name === "AbortError" || signal?.aborted) return;
      if (useAuthStore.getState().activeTenantId !== activeTenantId) return;
      if (error instanceof ApiError && error.status === 401) {
        setPatents([]);
        setPatentsError("");
        setAnonymous();
        return;
      }
      setPatents([]);
      setPatentsError(
        error instanceof Error ? error.message : "Could not load this tenant's patent library.",
      );
    } finally {
      if (!signal?.aborted) setPatentsLoading(false);
    }
  }, [activeTenantId, setAnonymous]);

  useEffect(() => {
    if (status !== "authenticated" || !activeTenantId) return undefined;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void loadPatents(controller.signal);
    }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [activeTenantId, loadPatents, refreshCounter, status]);

  // Continue refreshing the library while extraction or embedding is active.
  // This makes the upload form's status visible without requiring a page reload.
  useEffect(() => {
    if (status !== "authenticated" || !activeTenantId) return undefined;
    const hasPendingWork = patents.some((patent) => {
      const patentPending = ["UPLOADED", "PROCESSING"].includes(patent.status);
      const versionPending = Array.isArray(patent.versions) && patent.versions.some(
        (version) => ["PENDING", "EXTRACTING"].includes(version.extraction_status),
      );
      return patentPending || versionPending;
    });
    if (!hasPendingWork) return undefined;
    const timer = window.setTimeout(() => {
      setRefreshCounter((value) => value + 1);
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [activeTenantId, patents, status]);

  const refreshPatentLibrary = useCallback(() => {
    setRefreshCounter((value) => value + 1);
  }, []);

  const resetWorkspaceState = useCallback(() => {
    useDocumentStore.getState().setDocumentContext(null, null);
    useMatrixStore.getState().setAnalysisData({});
  }, []);

  function handleTenantChange(tenantId) {
    // Clear prior-tenant rows in the same event that changes the tenant context.
    setPatents([]);
    setPatentsError("");
    setPatentsLoading(false);
    setActiveSection("workspace");
    resetWorkspaceState();
    setActiveTenant(tenantId);
  }

  async function handleLogin(credentials) {
    setLoginBusy(true);
    setLoginError("");
    try {
      await bootstrapCsrfToken({ force: true });
      await apiRequest("/api/v1/auth/login/", {
        method: "POST",
        body: credentials,
      });
      const session = await apiRequest("/api/v1/auth/me/");
      const membershipPayload = await apiRequest("/api/v1/tenants/");
      setSession(session.user, membershipPayload.results || []);
    } catch (error) {
      setLoginError(
        error instanceof Error
          ? error.message
          : "Sign in failed. Check your credentials and try again.",
      );
    } finally {
      setLoginBusy(false);
    }
  }

  async function handleLogout() {
    setLogoutBusy(true);
    setLogoutError("");
    try {
      await apiRequest("/api/v1/auth/logout/", { method: "POST" });
      setAnonymous();
      setPatents([]);
      resetWorkspaceState();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setAnonymous();
        setPatents([]);
        resetWorkspaceState();
      } else {
        setLogoutError(
          error instanceof Error ? error.message : "Sign out failed; please retry.",
        );
      }
    } finally {
      setLogoutBusy(false);
    }
  }

  if (status === "checking") {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-950 px-4 text-white">
        <div className="text-center" role="status" aria-live="polite">
          <div className="mx-auto h-10 w-10 animate-spin rounded-full border-2 border-white/20 border-t-indigo-300" />
          <p className="mt-4 text-sm text-slate-300">Checking your secure session…</p>
        </div>
      </main>
    );
  }

  if (status === "error") {
    return (
      <main className="flex min-h-screen items-center justify-center bg-slate-950 px-4">
        <section className="w-full max-w-lg rounded-2xl bg-white p-7 shadow-xl">
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-rose-700">Connection issue</p>
          <h1 className="mt-2 text-xl font-semibold text-slate-950">Could not initialize PatentShield</h1>
          <p className="mt-2 text-sm leading-6 text-slate-600">{initializationError}</p>
          <button type="button" onClick={() => void refreshSession()} className="mt-5 rounded-lg bg-indigo-700 px-4 py-2.5 text-sm font-semibold text-white hover:bg-indigo-800">Retry connection</button>
        </section>
      </main>
    );
  }

  if (status !== "authenticated") {
    return <LoginPage onSubmit={handleLogin} busy={loginBusy} error={loginError} />;
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/95 shadow-sm backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-col gap-4 px-4 py-4 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-indigo-700 font-black text-white shadow-sm">P</div>
            <div>
              <p className="text-sm font-bold tracking-tight text-slate-950">PatentShield</p>
              <p className="text-xs text-slate-500">Enterprise IP analysis workspace</p>
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-[minmax(240px,1fr)_auto] sm:items-end lg:min-w-[540px]">
            <TenantSwitcher
              tenants={tenants}
              value={activeTenantId}
              onChange={handleTenantChange}
              disabled={logoutBusy}
            />
            <div className="flex flex-wrap items-center gap-2 sm:justify-end">
              <button
                type="button"
                onClick={() => setActiveSection("workspace")}
                aria-pressed={activeSection === "workspace"}
                className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${activeSection === "workspace" ? "bg-indigo-700 text-white" : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50"}`}
              >Workspace</button>
              <button
                type="button"
                onClick={() => setActiveSection("office-actions")}
                aria-pressed={activeSection === "office-actions"}
                className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${activeSection === "office-actions" ? "bg-indigo-700 text-white" : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50"}`}
              >Prosecution</button>
              <button
                type="button"
                onClick={() => setActiveSection("surveillance")}
                aria-pressed={activeSection === "surveillance"}
                className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${activeSection === "surveillance" ? "bg-indigo-700 text-white" : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50"}`}
              >Surveillance</button>
              <button
                type="button"
                onClick={() => setActiveSection("multilingual-search")}
                aria-pressed={activeSection === "multilingual-search"}
                className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${activeSection === "multilingual-search" ? "bg-indigo-700 text-white" : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50"}`}
              >CJK Search</button>
              <button
                type="button"
                onClick={() => setActiveSection("fto")}
                aria-pressed={activeSection === "fto"}
                className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${activeSection === "fto" ? "bg-indigo-700 text-white" : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50"}`}
              >FTO Mapping</button>
              <button
                type="button"
                onClick={() => setActiveSection("landscape")}
                aria-pressed={activeSection === "landscape"}
                className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${activeSection === "landscape" ? "bg-indigo-700 text-white" : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50"}`}
              >Landscape</button>
              {activeTenant && ["OWNER", "ADMIN"].includes(activeTenant.role) && <button
                type="button"
                onClick={() => setActiveSection("enterprise-admin")}
                aria-pressed={activeSection === "enterprise-admin"}
                className={`rounded-lg px-3 py-2 text-xs font-semibold transition ${activeSection === "enterprise-admin" ? "bg-indigo-700 text-white" : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50"}`}
              >Enterprise Admin</button>}
              <div className="flex items-center justify-between gap-3 sm:justify-end">
              <div className="min-w-0 text-right">
                <p className="max-w-48 truncate text-sm font-semibold text-slate-800">{user?.full_name || user?.username}</p>
                <p className="max-w-48 truncate text-xs text-slate-500">{user?.email || user?.username}</p>
              </div>
              <button
                type="button"
                onClick={() => void handleLogout()}
                disabled={logoutBusy}
                className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60"
              >
                {logoutBusy ? "Signing out…" : "Sign out"}
              </button>
              </div>
            </div>
          </div>
        </div>
        {logoutError && <div className="border-t border-rose-100 bg-rose-50 px-4 py-2 text-center text-xs text-rose-800" role="alert">{logoutError}</div>}
      </header>

      {tenants.length === 0 ? (
        <main className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
          <div className="rounded-2xl border border-amber-200 bg-amber-50 p-6">
            <h1 className="text-lg font-semibold text-amber-950">No active tenant memberships</h1>
            <p className="mt-2 text-sm leading-6 text-amber-900">Your account is signed in, but no active workspace memberships were returned. Ask a PatentShield workspace owner to invite you or reactivate your membership.</p>
          </div>
        </main>
      ) : !activeTenant ? (
        <main className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
          <div className="rounded-2xl border border-indigo-100 bg-white p-7 shadow-sm">
            <h1 className="text-xl font-semibold text-slate-950">Choose a workspace</h1>
            <p className="mt-2 text-sm leading-6 text-slate-500">Your account belongs to multiple tenants. Select the workspace you intend to access before loading any tenant-owned data.</p>
            <div className="mt-5 max-w-lg">
              <TenantSwitcher tenants={tenants} value={activeTenantId} onChange={handleTenantChange} />
            </div>
          </div>
        </main>
      ) : activeSection === "surveillance" ? (
        <SurveillanceDashboard key={activeTenant.id} tenant={activeTenant} />
      ) : activeSection === "office-actions" ? (
        <OfficeActionAssistant key={activeTenant.id} tenant={activeTenant} patents={patents} />
      ) : activeSection === "multilingual-search" ? (
        <MultilingualSearch key={activeTenant.id} tenant={activeTenant} />
      ) : activeSection === "fto" ? (
        <FTOWorkspace key={activeTenant.id} tenant={activeTenant} patents={patents} />
      ) : activeSection === "landscape" ? (
        <LandscapeAnalytics key={activeTenant.id} tenant={activeTenant} />
      ) : activeSection === "enterprise-admin" && ["OWNER", "ADMIN"].includes(activeTenant.role) ? (
        <EnterpriseAdminConsole key={activeTenant.id} tenant={activeTenant} user={user} />
      ) : (
        <>
          <PatentsDashboard
            tenant={activeTenant}
            patents={patents}
            loading={patentsLoading}
            error={patentsError}
            onRetry={refreshPatentLibrary}
            onRefresh={refreshPatentLibrary}
            onUploadSubmitted={refreshPatentLibrary}
          />
          <AnalysisConsole
            key={activeTenant.id}
            tenant={activeTenant}
            patents={patents}
            onRefreshPatents={refreshPatentLibrary}
          />
        </>
      )}
    </div>
  );
}
