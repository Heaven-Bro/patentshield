import { create } from "zustand";

const ACTIVE_TENANT_KEY = "patentshield.activeTenantId";

function readStoredTenantId() {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(ACTIVE_TENANT_KEY);
  } catch {
    return null;
  }
}

function storeTenantId(tenantId) {
  if (typeof window === "undefined") return;
  try {
    if (tenantId) window.sessionStorage.setItem(ACTIVE_TENANT_KEY, tenantId);
    else window.sessionStorage.removeItem(ACTIVE_TENANT_KEY);
  } catch {
    // Tenant selection still works for the current tab if storage is disabled.
  }
}

/**
 * @typedef {{id:string, name:string, slug:string, role:string, membership_status:string}} TenantMembershipView
 */

/**
 * Authentication and tenant context state. No password, session cookie, or
 * reusable bearer token is stored in browser storage.
 */
export const useAuthStore = create((set, get) => ({
  status: "checking", // checking | authenticated | anonymous | error
  user: null,
  tenants: /** @type {TenantMembershipView[]} */ ([]),
  activeTenantId: readStoredTenantId(),
  initializationError: "",

  /** @param {{id:string, username:string, email:string, full_name:string, is_staff:boolean}} user @param {TenantMembershipView[]} tenants */
  setSession: (user, tenants) => {
    const safeTenants = Array.isArray(tenants) ? tenants : [];
    const previousId = get().activeTenantId || readStoredTenantId();
    const previousIsValid = safeTenants.some((tenant) => tenant.id === previousId);
    const selectedId = previousIsValid
      ? previousId
      : safeTenants.length === 1
        ? safeTenants[0].id
        : null;
    storeTenantId(selectedId);
    set({
      status: "authenticated",
      user,
      tenants: safeTenants,
      activeTenantId: selectedId,
      initializationError: "",
    });
  },

  setActiveTenant: (tenantId) => {
    const allowed = get().tenants.some((tenant) => tenant.id === tenantId);
    if (!allowed) return;
    storeTenantId(tenantId);
    set({ activeTenantId: tenantId });
  },

  setAnonymous: () => {
    storeTenantId(null);
    set({
      status: "anonymous",
      user: null,
      tenants: [],
      activeTenantId: null,
      initializationError: "",
    });
  },

  setInitializationError: (message) =>
    set({ status: "error", initializationError: message }),

  setChecking: () => set({ status: "checking", initializationError: "" }),
}));

export default useAuthStore;
