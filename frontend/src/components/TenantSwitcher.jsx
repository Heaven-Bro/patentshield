/**
 * Tenant picker restricted to memberships returned by the authenticated API.
 * @param {{tenants:Array<{id:string,name:string,slug:string,role:string}>, value:string|null, onChange:(tenantId:string)=>void, disabled?:boolean}} props
 */
export default function TenantSwitcher({ tenants, value, onChange, disabled = false, dark = false, id = "tenant-switcher" }) {
  const selectedTenant = tenants.find((tenant) => tenant.id === value) || null;

  return (
    <div className="min-w-0">
      <label htmlFor={id} className={`mb-1 block text-[11px] font-semibold uppercase tracking-[0.14em] ${dark ? "text-slate-400" : "text-slate-500"}`}>
        Active workspace
      </label>
      <select
        id={id}
        value={selectedTenant?.id || ""}
        disabled={disabled || tenants.length === 0}
        onChange={(event) => onChange(event.target.value)}
        className="w-full min-w-0 rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-sm font-semibold text-slate-800 shadow-sm outline-none transition focus:border-indigo-500 focus:ring-4 focus:ring-indigo-100 disabled:cursor-not-allowed disabled:bg-slate-100"
        aria-describedby={`${id}-role`}
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
      <p id={`${id}-role`} className={`mt-1 truncate text-xs ${dark ? "text-slate-400" : "text-slate-500"}`}>
        {selectedTenant ? `Your role: ${selectedTenant.role.replaceAll("_", " ")}` : "Select a workspace you belong to."}
      </p>
    </div>
  );
}
