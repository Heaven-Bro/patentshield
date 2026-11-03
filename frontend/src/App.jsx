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
import PortfolioValuation from "./components/PortfolioValuation";
import TenantSwitcher from "./components/TenantSwitcher";
import { ApiError, apiRequest, bootstrapCsrfToken } from "./lib/apiClient";
import { useAuthStore } from "./stores/authStore";
import { useDocumentStore } from "./stores/documentStore";
import { useMatrixStore } from "./stores/matrixStore";

const NAV_ITEMS = [
  { id: "workspace", label: "IP workspace", description: "Library & analysis", heading: "Patent intelligence workspace", summary: "Review source documents, run prior-art analysis, and manage evidence.", icon: "workspace" },
  { id: "office-actions", label: "Prosecution", description: "Office action response", icon: "prosecution" },
  { id: "surveillance", label: "Surveillance", description: "Competitor monitoring", icon: "surveillance" },
  { id: "multilingual-search", label: "CJK search", description: "Cross-lingual retrieval", icon: "search" },
  { id: "fto", label: "FTO mapping", description: "Product-to-claim review", icon: "mapping" },
  { id: "landscape", label: "Landscape", description: "Technology intelligence", icon: "landscape" },
  { id: "portfolio-valuation", label: "Valuation", description: "Portfolio diligence", icon: "valuation" },
  { id: "enterprise-admin", label: "Enterprise admin", description: "Identity & governance", icon: "admin" },
];

const NAV_GROUPS = [
  { label: "Discover", ids: ["workspace", "multilingual-search"] },
  { label: "Review & monitor", ids: ["office-actions", "surveillance", "fto"] },
  { label: "Portfolio", ids: ["landscape", "portfolio-valuation"] },
  { label: "Governance", ids: ["enterprise-admin"] },
];

const NAV_ICON_PATHS = {
  workspace: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  prosecution: "M8 4H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-3 M8 2h8v4H8z M7 12h10 M7 16h7",
  surveillance: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z",
  search: "M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15z M16 16l5 5",
  mapping: "M4 5h16 M4 12h16 M4 19h16 M8 2v6 M16 9v6 M10 16v6",
  landscape: "M3 20V5 M3 20h18 M7 16l4-5 3 2 5-7",
  valuation: "M4 5h16 M4 12h16 M4 19h16 M7 3v4 M12 10v4 M17 17v4",
  admin: "M12 3l8 4v5c0 5-3.5 8-8 10-4.5-2-8-5-8-10V7l8-4z M9 12l2 2 4-4",
};

function NavIcon({ name }) {
  return <svg viewBox="0 0 24 24" className="h-[18px] w-[18px]" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={NAV_ICON_PATHS[name] || NAV_ICON_PATHS.workspace} /></svg>;
}

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
  const [apiStatus, setApiStatus] = useState("checking");
  const [refreshCounter, setRefreshCounter] = useState(0);
  const [activeSection, setActiveSection] = useState("workspace");
  const [documentSelectionRequest, setDocumentSelectionRequest] = useState(null);

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
    setApiStatus("checking");
    setPatentsError("");
    try {
      const response = await apiRequest("/api/v1/patents/", {
        tenantId: activeTenantId,
        signal,
      });
      if (signal?.aborted || useAuthStore.getState().activeTenantId !== activeTenantId) return;
      setPatents(Array.isArray(response.results) ? response.results : []);
      setApiStatus("online");
    } catch (error) {
      if (error?.name === "AbortError" || signal?.aborted) return;
      if (useAuthStore.getState().activeTenantId !== activeTenantId) return;
      setApiStatus("offline");
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

  function openDocumentInReview(patent, version) {
    setDocumentSelectionRequest({
      tenantId: activeTenantId,
      patentId: patent.id,
      versionId: version.id,
    });
    window.setTimeout(() => {
      document.getElementById("analysis-console")?.scrollIntoView({ block: "start", behavior: "smooth" });
    }, 0);
  }

  const resetWorkspaceState = useCallback(() => {
    useDocumentStore.getState().setDocumentContext(null, null);
    useMatrixStore.getState().setAnalysisData({});
  }, []);

  function handleTenantChange(tenantId) {
    // Clear prior-tenant rows in the same event that changes the tenant context.
    setPatents([]);
    setPatentsError("");
    setPatentsLoading(false);
    setApiStatus("checking");
    setActiveSection("workspace");
    setDocumentSelectionRequest(null);
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
      setDocumentSelectionRequest(null);
      resetWorkspaceState();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setAnonymous();
        setPatents([]);
        setDocumentSelectionRequest(null);
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

  const activeNavItem = NAV_ITEMS.find((item) => item.id === activeSection) || NAV_ITEMS[0];
  const displayName = user?.full_name || user?.username || user?.email || "PatentShield user";
  const avatarLabel = displayName.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "P";
  const apiBadge = apiStatus === "offline"
    ? { label: "API unavailable", tone: "border-rose-200 bg-rose-50 text-rose-700", dot: "bg-rose-500" }
    : apiStatus === "checking" || patentsLoading
      ? { label: "Syncing API", tone: "border-amber-200 bg-amber-50 text-amber-700", dot: "bg-amber-500" }
      : { label: "API connected", tone: "border-emerald-200 bg-emerald-50 text-emerald-700", dot: "bg-emerald-500" };

  return (
    <div className="min-h-screen bg-paper text-slate-900 lg:flex">
      <aside className="relative z-30 flex w-full flex-col border-b border-slate-800 bg-navy-950 text-white lg:sticky lg:top-0 lg:h-screen lg:w-[272px] lg:shrink-0 lg:border-b-0 lg:border-r lg:border-white/10">
        <div className="flex items-center justify-between gap-3 px-5 pb-4 pt-5 lg:block lg:px-5 lg:pb-5 lg:pt-6">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-indigo-300/25 bg-indigo-500/15 font-editorial text-xl font-bold text-indigo-100">P</div>
            <div className="min-w-0">
              <p className="text-[15px] font-semibold tracking-tight text-white">PatentShield</p>
              <p className="mt-1 text-[9px] font-semibold uppercase tracking-[0.2em] text-slate-400">Evidence Atlas</p>
            </div>
          </div>
          <span className="hidden items-center gap-1.5 rounded-full border border-emerald-400/20 bg-emerald-400/10 px-2.5 py-1 text-[10px] font-semibold text-emerald-200 sm:inline-flex lg:hidden"><span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />Secure session</span>
        </div>

        <div className="px-4 pb-4 lg:pb-5">
          <div className="rounded-lg border border-white/10 bg-white/[0.035] p-3">
            <p className="mb-2 flex items-center gap-2 text-[9px] font-bold uppercase tracking-[0.16em] text-slate-400">
              <span className="h-1.5 w-1.5 rounded-full bg-indigo-400" /> Workspace
            </p>
            <TenantSwitcher tenants={tenants} value={activeTenantId} onChange={handleTenantChange} disabled={logoutBusy} dark id="sidebar-tenant-switcher" />
          </div>
        </div>

        <nav className="flex gap-2 overflow-x-auto px-3 pb-3 lg:flex-1 lg:flex-col lg:gap-4 lg:overflow-x-visible lg:overflow-y-auto lg:px-3 lg:pb-4" aria-label="Primary navigation">
          {NAV_GROUPS.map((group) => {
            const groupItems = group.ids
              .map((id) => NAV_ITEMS.find((item) => item.id === id))
              .filter((item) => item && (item.id !== "enterprise-admin" || (activeTenant && ["OWNER", "ADMIN"].includes(activeTenant.role))));
            if (groupItems.length === 0) return null;
            return (
              <div key={group.label} className="flex shrink-0 flex-row gap-1 lg:w-full lg:flex-col">
                <p className="hidden px-3 pb-1 text-[9px] font-bold uppercase tracking-[0.18em] text-slate-500 lg:block">{group.label}</p>
                {groupItems.map((item) => {
                  const isActive = activeSection === item.id;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      onClick={() => setActiveSection(item.id)}
                      aria-current={isActive ? "page" : undefined}
                      aria-pressed={isActive}
                      className={`group relative flex min-w-max items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300 lg:w-full lg:gap-3 lg:px-3 lg:py-2.5 lg:text-sm ${isActive ? "bg-indigo-400/15 text-white ring-1 ring-inset ring-indigo-300/20" : "text-slate-400 hover:bg-white/[0.06] hover:text-slate-100"}`}
                    >
                      <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-md ${isActive ? "bg-indigo-500 text-white" : "bg-white/[0.045] text-slate-400 group-hover:text-slate-200"}`} aria-hidden="true">
                        <NavIcon name={item.icon} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block whitespace-nowrap">{item.label}</span>
                        <span className={`mt-0.5 hidden text-[10px] font-normal lg:block ${isActive ? "text-indigo-200" : "text-slate-500"}`}>{item.description}</span>
                      </span>
                      {isActive && <span className="hidden h-1.5 w-1.5 rounded-full bg-indigo-300 lg:block" />}
                    </button>
                  );
                })}
              </div>
            );
          })}
        </nav>

        <div className="hidden border-t border-white/10 p-4 lg:block">
          <div className="flex items-center gap-3 rounded-xl px-2 py-2">
            <span className={`h-2 w-2 rounded-full ${apiBadge.dot} ${apiStatus === "checking" || patentsLoading ? "animate-pulse" : ""}`} />
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold text-slate-200">{apiBadge.label}</p>
              <p className="mt-0.5 text-[10px] text-slate-500">Tenant-scoped session</p>
            </div>
            <span className="rounded-md border border-white/10 px-1.5 py-1 text-[9px] font-bold tracking-wider text-slate-500">TLS</span>
          </div>
        </div>
      </aside>

      <div className="flex min-h-screen min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-paper-line bg-paper-soft/95 shadow-[0_1px_2px_rgba(15,23,42,0.025)] backdrop-blur">
          <div className="flex flex-col gap-3 px-4 py-3.5 sm:px-6 lg:flex-row lg:items-center lg:justify-between lg:px-8">
            <div className="min-w-0">
              <div className="flex items-center gap-2 text-[9px] font-bold uppercase tracking-[0.18em] text-slate-400">
                <span>Evidence Atlas</span><span className="text-slate-300">/</span><span className="text-indigo-700">{activeNavItem.label}</span>
              </div>
              <h1 className="mt-1 font-editorial text-xl font-medium tracking-tight text-slate-950 sm:text-2xl">{activeNavItem.heading || activeNavItem.label}</h1>
              <p className="mt-0.5 hidden text-xs text-slate-500 sm:block">{activeTenant?.name ? `${activeTenant.name} · ` : ""}{activeNavItem.summary || activeNavItem.description}</p>
            </div>
            <div className="flex flex-wrap items-center gap-2.5 sm:justify-between lg:justify-end">
              <div className={`inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] font-semibold ${apiBadge.tone}`} role="status" aria-live="polite">
                <span className={`h-1.5 w-1.5 rounded-full ${apiBadge.dot} ${apiStatus === "checking" || patentsLoading ? "animate-pulse" : ""}`} />
                <span className="hidden sm:inline">{apiBadge.label}</span><span className="sm:hidden">{apiStatus === "offline" ? "Offline" : apiStatus === "checking" || patentsLoading ? "Syncing" : "Connected"}</span>
              </div>
              <div className="hidden h-7 w-px bg-slate-200 sm:block" />
              <div className="flex min-w-0 items-center gap-2.5">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-slate-800 to-indigo-900 text-xs font-bold text-white shadow-sm" aria-label={`Avatar for ${displayName}`}>{avatarLabel}</div>
                <div className="min-w-0">
                  <p className="max-w-44 truncate text-xs font-bold text-slate-900 sm:text-sm">{displayName}</p>
                  <p className="max-w-44 truncate text-[10px] text-slate-500 sm:text-xs">{user?.email || user?.username}</p>
                </div>
              </div>
              <button type="button" onClick={() => void handleLogout()} disabled={logoutBusy} className="inline-flex items-center justify-center rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs font-semibold text-slate-600 shadow-sm transition hover:border-rose-200 hover:bg-rose-50 hover:text-rose-700 disabled:cursor-not-allowed disabled:opacity-50">
                {logoutBusy ? "Signing out…" : "Sign out"}
              </button>
            </div>
          </div>
          {logoutError && <div className="border-t border-rose-200 bg-rose-50 px-4 py-2 text-center text-xs text-rose-800" role="alert">{logoutError}</div>}
        </header>

        <div className="min-w-0 flex-1 bg-paper">
          {tenants.length === 0 ? (
            <main className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
              <div className="rounded-2xl border border-amber-200 bg-amber-50 p-6 shadow-sm">
                <h2 className="text-lg font-semibold text-amber-950">No active tenant memberships</h2>
                <p className="mt-2 text-sm leading-6 text-amber-900">Your account is signed in, but no active workspace memberships were returned. Ask a PatentShield workspace owner to invite you or reactivate your membership.</p>
              </div>
            </main>
          ) : !activeTenant ? (
            <main className="mx-auto max-w-3xl px-4 py-16 sm:px-6">
              <div className="rounded-2xl border border-indigo-100 bg-white p-7 shadow-lg shadow-indigo-950/5">
                <p className="text-xs font-bold uppercase tracking-[0.18em] text-indigo-600">Workspace selection</p>
                <h2 className="mt-2 text-xl font-semibold text-slate-950">Choose a workspace</h2>
                <p className="mt-2 text-sm leading-6 text-slate-500">Your account belongs to multiple tenants. Select the workspace you intend to access before loading any tenant-owned data.</p>
                <div className="mt-5 max-w-lg"><TenantSwitcher tenants={tenants} value={activeTenantId} onChange={handleTenantChange} /></div>
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
          ) : activeSection === "portfolio-valuation" ? (
            <PortfolioValuation key={activeTenant.id} tenant={activeTenant} />
          ) : activeSection === "enterprise-admin" && ["OWNER", "ADMIN"].includes(activeTenant.role) ? (
            <EnterpriseAdminConsole key={activeTenant.id} tenant={activeTenant} user={user} />
          ) : (
            <>
              <PatentsDashboard tenant={activeTenant} patents={patents} loading={patentsLoading} error={patentsError} onRetry={refreshPatentLibrary} onRefresh={refreshPatentLibrary} onUploadSubmitted={refreshPatentLibrary} onOpenDocument={openDocumentInReview} />
              <AnalysisConsole key={activeTenant.id} tenant={activeTenant} patents={patents} onRefreshPatents={refreshPatentLibrary} documentSelectionRequest={documentSelectionRequest} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
