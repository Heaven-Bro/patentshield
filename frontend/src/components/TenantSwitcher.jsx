/**
 * Tenant picker restricted to memberships returned by the authenticated API.
 * @param {{tenants:Array<{id:string,name:string,slug:string,role:string}>, value:string|null, onChange:(tenantId:string)=>void, disabled?:boolean}} props
 */
export default function TenantSwitcher({ tenants, value, onChange, disabled = false }) {
  const selectedTenant = tenants.find((tenant) => tenant.id === value) || null;

  return (
    <div className="min-w-0">
      <label htmlFor="tenant-switcher" className="mb-1 block text-[11px] font-semibold uppercase tracking-[0.14em] text-slate-500">
        Active workspace
      </label>
      <select
        id="tenant-switcher"
        value={selectedTenant?.id || ""}
        disabled={disabled || tenants.length === 0}
        onChange={(event) => onChange(event.target.value)}
        className="w-full min-w-0 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 outline-none focus:border-indigo-500 focus:ring-2 focus:ring-indigo-100 disabled:cursor-not-allowed disabled:bg-slate-100"
        aria-describedby="tenant-switcher-role"
      >
        <option value="" disabled>
          {tenants.length === 0 ? "No active workspaces" : "Choose a workspace"}
        </option>
        {tenants.map((tenant) => (
          <option key={tenant.id} value={tenant.id}>
            {tenant.name}
          </option>
        ))}
      </select>
      <p id="tenant-switcher-role" className="mt-1 truncate text-xs text-slate-500">
        {selectedTenant ? `Your role: ${selectedTenant.role.replaceAll("_", " ")}` : "Select a workspace you belong to."}
      </p>
    </div>
  );
}
