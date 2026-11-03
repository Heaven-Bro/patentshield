import { useCallback, useEffect, useState } from "react";
import { apiRequest, downloadArtifact, requireResultsArray } from "../lib/apiClient";

const ROOT = "/api/v1/enterprise-auth";
const MATTER_TYPES = ["patent", "analysis", "claim_chart", "office_action", "fto_study", "product_matter", "landscape_study", "portfolio_valuation"];
const ROLES = ["OWNER", "ADMIN", "ATTORNEY", "ANALYST", "REVIEWER", "READ_ONLY"];
const LEVELS = ["READ", "EDIT", "REVIEW", "ADMIN"];

const emptyProvider = () => ({
  slug: "", name: "", protocol: "OIDC", enabled: false, jit_provisioning: false,
  allow_existing_email_link: false, trust_email_from_idp: false, default_role: "READ_ONLY",
  allowed_email_domains: [], group_claim: "groups", group_role_mapping: {}, issuer: "",
  discovery_url: "", client_id: "", client_secret: "", client_auth_method: "client_secret_basic",
  scopes: ["openid", "email", "profile"], sp_entity_id: "", idp_entity_id: "", sso_url: "",
  idp_x509_certificate: "", saml_email_attribute: "email", saml_groups_attribute: "groups",
});

function ErrorBanner({ value }) {
  if (!value) return null;
  return <div role="alert" className="ps-alert-error">{value}</div>;
}

function Section({ title, description, children }) {
  return <section className="ps-panel">
    <div className="mb-4"><h2 className="ps-panel-title">{title}</h2>{description && <p className="ps-panel-description">{description}</p>}</div>
    {children}
  </section>;
}

function Field({ label, value, onChange, placeholder = "", type = "text", required = false }) {
  return <label className="block text-sm"><span className="mb-1 block font-medium text-slate-700">{label}</span><input type={type} value={value ?? ""} required={required} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} className="ps-input" /></label>;
}

function SelectField({ label, value, onChange, options }) {
  return <label className="block text-sm"><span className="mb-1 block font-medium text-slate-700">{label}</span><select value={value} onChange={(event) => onChange(event.target.value)} className="ps-input">{options.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>;
}

function CheckField({ label, checked, onChange }) {
  return <label className="flex items-center gap-2 text-sm text-slate-700"><input type="checkbox" checked={Boolean(checked)} onChange={(event) => onChange(event.target.checked)} className="rounded border-slate-300 text-indigo-700 focus:ring-indigo-600" />{label}</label>;
}

function Button({ children, disabled = false, onClick, variant = "primary", type = "button" }) {
  const look = variant === "danger" ? "border-rose-200 bg-rose-50 text-rose-800 hover:bg-rose-100" : variant === "secondary" ? "border-slate-200 bg-white text-slate-700 hover:border-indigo-200 hover:bg-indigo-50/60 hover:text-indigo-800" : "border-transparent bg-indigo-600 text-white shadow-indigo-700/15 hover:bg-indigo-700";
  return <button type={type} disabled={disabled} onClick={onClick} className={`inline-flex items-center justify-center rounded-xl border px-3.5 py-2.5 text-xs font-bold shadow-sm transition duration-150 hover:-translate-y-px disabled:cursor-not-allowed disabled:opacity-45 ${look}`}>{children}</button>;
}

export default function EnterpriseAdminConsole({ tenant, user }) {
  const [providers, setProviders] = useState([]);
  const [tokens, setTokens] = useState([]);
  const [policies, setPolicies] = useState([]);
  const [grants, setGrants] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [editingProvider, setEditingProvider] = useState(null);
  const [providerForm, setProviderForm] = useState(emptyProvider);
  const [newTokenLabel, setNewTokenLabel] = useState("Directory sync");
  const [newTokenScopes, setNewTokenScopes] = useState(["Users:read", "Users:write"]);
  const [oneTimeToken, setOneTimeToken] = useState("");
  const [policyForm, setPolicyForm] = useState({ matter_type: "patent", matter_id: "", enforce_acl: false });
  const [grantForm, setGrantForm] = useState({ matter_type: "patent", matter_id: "", subject_user: "", subject_role: "", level: "READ" });

  const request = useCallback((path, options = {}) => apiRequest(`${ROOT}${path}`, { ...options, tenantId: tenant?.id }), [tenant?.id]);

  const refresh = useCallback(async () => {
    if (!tenant?.id) return;
    setBusy(true);
    setError("");
    try {
      const [providerData, tokenData, policyData, grantData, sessionData] = await Promise.all([
        request("/providers/"), request("/scim-tokens/"), request("/matter-policies/"), request("/matter-grants/"), request("/sessions/"),
      ]);
      setProviders(requireResultsArray(providerData, "identity provider list"));
      setTokens(requireResultsArray(tokenData, "SCIM token list"));
      setPolicies(requireResultsArray(policyData, "matter policy list"));
      setGrants(requireResultsArray(grantData, "matter grant list"));
      setSessions(requireResultsArray(sessionData, "session list"));
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Could not load enterprise settings.");
    } finally {
      setBusy(false);
    }
  }, [request, tenant?.id]);

  useEffect(() => { void refresh(); }, [refresh]);

  function setProviderField(key, value) { setProviderForm((current) => ({ ...current, [key]: value })); }
  function startEditProvider(provider) {
    setEditingProvider(provider.id);
    setProviderForm({ ...emptyProvider(), ...provider, client_secret: "", allowed_email_domains: provider.allowed_email_domains || [], group_role_mapping: provider.group_role_mapping || {}, scopes: provider.scopes || ["openid", "email", "profile"] });
    setNotice("");
  }
  function resetProviderForm() { setEditingProvider(null); setProviderForm(emptyProvider()); }

  async function saveProvider(event) {
    event.preventDefault();
    setBusy(true); setError(""); setNotice("");
    try {
      const payload = { ...providerForm, allowed_email_domains: typeof providerForm.allowed_email_domains === "string" ? providerForm.allowed_email_domains.split(",").map((value) => value.trim()).filter(Boolean) : providerForm.allowed_email_domains };
      if (!payload.client_secret) delete payload.client_secret;
      const result = await request(editingProvider ? `/providers/${editingProvider}/` : "/providers/", { method: editingProvider ? "PATCH" : "POST", body: payload });
      setNotice(`Identity provider ${result.name} saved and is ${result.enabled ? "enabled" : "disabled"}.`);
      resetProviderForm();
      await refresh();
    } catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Provider save failed."); }
    finally { setBusy(false); }
  }

  async function disableProvider(provider) {
    setBusy(true); setError("");
    try { await request(`/providers/${provider.id}/`, { method: "PATCH", body: { enabled: false } }); setNotice(`${provider.name} disabled.`); await refresh(); }
    catch (requestError) { setError(requestError.message || "Could not disable provider."); }
    finally { setBusy(false); }
  }

  async function createToken(event) {
    event.preventDefault(); setBusy(true); setError(""); setNotice(""); setOneTimeToken("");
    try {
      const result = await request("/scim-tokens/", { method: "POST", body: { label: newTokenLabel, scopes: newTokenScopes } });
      setOneTimeToken(result.token || ""); setNotice("SCIM token created. Copy it now; it is not displayed again."); await refresh();
    } catch (requestError) { setError(requestError.message || "Token creation failed."); }
    finally { setBusy(false); }
  }

  async function revokeToken(token) {
    setBusy(true); setError("");
    try { await request(`/scim-tokens/${token.id}/revoke/`, { method: "POST", body: {} }); setNotice(`Token “${token.label}” revoked.`); await refresh(); }
    catch (requestError) { setError(requestError.message || "Could not revoke token."); }
    finally { setBusy(false); }
  }

  async function savePolicy(event) {
    event.preventDefault(); setBusy(true); setError("");
    try { await request("/matter-policies/", { method: "POST", body: { ...policyForm, matter_id: policyForm.matter_id.trim() } }); setNotice("Matter access policy saved. Ensure grants exist before enforcing restricted access."); setPolicyForm((old) => ({ ...old, matter_id: "" })); await refresh(); }
    catch (requestError) { setError(requestError.message || "Could not save matter policy."); }
    finally { setBusy(false); }
  }

  async function togglePolicy(policy, enforceAcl) {
    if (enforceAcl && !policy.enforce_acl && !window.confirm(`Enable explicit ACL enforcement for ${policy.matter_type} ${policy.matter_id}? Users without a matching matter grant may lose access. Verify required user/role grants before continuing.`)) return;
    setBusy(true); setError("");
    try { await request(`/matter-policies/${policy.id}/`, { method: "PATCH", body: { enforce_acl: enforceAcl } }); setNotice(`ACL enforcement ${enforceAcl ? "enabled" : "disabled"} for ${policy.matter_type} ${policy.matter_id}.`); await refresh(); }
    catch (requestError) { setError(requestError.message || "Could not update policy."); }
    finally { setBusy(false); }
  }

  async function saveGrant(event) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      const body = { matter_type: grantForm.matter_type, matter_id: grantForm.matter_id.trim(), level: grantForm.level };
      if (grantForm.subject_user.trim()) body.subject_user = grantForm.subject_user.trim();
      else if (grantForm.subject_role) body.subject_role = grantForm.subject_role;
      else throw new Error("Specify a user UUID or a tenant role for the grant.");
      await request("/matter-grants/", { method: "POST", body });
      setNotice("Matter grant created."); setGrantForm((old) => ({ ...old, subject_user: "", subject_role: "", matter_id: "" })); await refresh();
    } catch (requestError) { setError(requestError.message || "Could not create grant."); }
    finally { setBusy(false); }
  }

  async function deleteGrant(grant) {
    setBusy(true); setError("");
    try { await request(`/matter-grants/${grant.id}/`, { method: "DELETE" }); setNotice("Matter grant revoked."); await refresh(); }
    catch (requestError) { setError(requestError.message || "Could not revoke grant."); }
    finally { setBusy(false); }
  }

  async function revokeSession(session) {
    setBusy(true); setError("");
    try { await request(`/sessions/${session.id}/revoke/`, { method: "POST", body: {} }); setNotice(`Session revoked for ${session.username}.`); await refresh(); }
    catch (requestError) { setError(requestError.message || "Could not revoke session."); }
    finally { setBusy(false); }
  }

  async function revokeUserSessions(session) {
    if (!window.confirm(`Revoke every server-side session for ${session.username}? This affects the user's sessions across all workspaces.`)) return;
    setBusy(true); setError("");
    try { await request(`/users/${session.user_id}/sessions/revoke-all/`, { method: "POST", body: {} }); setNotice(`All sessions revoked for ${session.username}.`); await refresh(); }
    catch (requestError) { setError(requestError.message || "Could not revoke user sessions."); }
    finally { setBusy(false); }
  }

  async function exportAudit(format) {
    setError("");
    try { await downloadArtifact(`${ROOT}/audit-export/?format=${format}&limit=10000`, { tenantId: tenant?.id, fallbackFilename: `patentshield-audit-${tenant?.slug}.${format === "jsonl" ? "jsonl" : "csv"}` }); setNotice(`Audit ${format.toUpperCase()} export downloaded. The export is capped at 10,000 records.`); }
    catch (requestError) { setError(requestError instanceof Error ? requestError.message : "Audit export failed."); }
  }

  function toggleTokenScope(scope) {
    setNewTokenScopes((current) => {
      if (current.includes(scope)) {
        const next = current.filter((item) => item !== scope);
        if (scope === "Roles:manage") return next.filter((item) => item !== "Roles:admin");
        return next;
      }
      const next = [...current, scope];
      if (scope === "Roles:admin" && !next.includes("Roles:manage")) next.push("Roles:manage");
      return next;
    });
  }

  const scimUrl = `${window.location.origin}${ROOT}/scim/v2/${tenant?.slug}/Users/`;

  return <main className="ps-page">
    <div className="ps-hero">
      <p className="text-xs font-bold uppercase tracking-[0.18em] text-indigo-300">Identity & Governance</p>
      <div className="mt-2 flex flex-col justify-between gap-3 sm:flex-row sm:items-end"><div><h1 className="text-2xl font-bold tracking-tight">Enterprise administration</h1><p className="mt-2 max-w-3xl text-sm leading-6 text-slate-300">Configure SSO providers, issue scoped directory tokens, protect confidential matters, revoke sessions, and export audit history for {tenant?.name}.</p></div><Button onClick={() => void refresh()} disabled={busy} variant="secondary">{busy ? "Refreshing…" : "Refresh data"}</Button></div>
    </div>
    <ErrorBanner value={error} />
    {notice && <div role="status" className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">{notice}</div>}

    <Section title="Federated identity providers" description="OIDC authorization-code login with PKCE and SAML 2.0 assertion validation. New providers are disabled by default; complete IdP registration and callback URL configuration before enabling. Group claims cannot grant tenant ownership.">
      <form onSubmit={saveProvider} className="space-y-4 rounded-xl border border-slate-200 bg-slate-50 p-4">
        <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="font-semibold text-slate-900">{editingProvider ? "Edit provider" : "Add identity provider"}</h3>{editingProvider && <Button variant="secondary" onClick={resetProviderForm}>Cancel edit</Button>}</div>
        <div className="grid gap-3 md:grid-cols-3"><Field label="Slug" value={providerForm.slug} onChange={(v) => setProviderField("slug", v)} required placeholder="company-sso" /><Field label="Display name" value={providerForm.name} onChange={(v) => setProviderField("name", v)} required placeholder="Company SSO" /><SelectField label="Protocol" value={providerForm.protocol} onChange={(v) => setProviderField("protocol", v)} options={["OIDC", "SAML"]} /></div>
        {providerForm.protocol === "OIDC" ? <div className="grid gap-3 md:grid-cols-2"><Field label="Issuer URL" value={providerForm.issuer} onChange={(v) => setProviderField("issuer", v)} required placeholder="https://idp.example.com" /><Field label="Discovery URL" value={providerForm.discovery_url} onChange={(v) => setProviderField("discovery_url", v)} required placeholder="https://idp.example.com/.well-known/openid-configuration" /><Field label="Client ID" value={providerForm.client_id} onChange={(v) => setProviderField("client_id", v)} required /><Field label="Client secret (write-only)" value={providerForm.client_secret} onChange={(v) => setProviderField("client_secret", v)} type="password" placeholder={editingProvider ? "Leave blank to keep saved secret" : "Required before enabling OIDC"} /><SelectField label="Token endpoint auth" value={providerForm.client_auth_method} onChange={(v) => setProviderField("client_auth_method", v)} options={["client_secret_basic", "client_secret_post"]} /></div> : <div className="grid gap-3 md:grid-cols-2"><Field label="IdP entity ID" value={providerForm.idp_entity_id} onChange={(v) => setProviderField("idp_entity_id", v)} required /><Field label="SSO URL" value={providerForm.sso_url} onChange={(v) => setProviderField("sso_url", v)} required placeholder="https://idp.example.com/sso" /><Field label="SP entity ID (optional)" value={providerForm.sp_entity_id} onChange={(v) => setProviderField("sp_entity_id", v)} /><label className="block text-sm md:col-span-2"><span className="mb-1 block font-medium text-slate-700">IdP X.509 certificate (PEM or base64 body)</span><textarea value={providerForm.idp_x509_certificate} onChange={(event) => setProviderField("idp_x509_certificate", event.target.value)} rows={4} required className="ps-input font-mono text-xs" /></label><Field label="Email attribute" value={providerForm.saml_email_attribute} onChange={(v) => setProviderField("saml_email_attribute", v)} /><Field label="Groups attribute" value={providerForm.saml_groups_attribute} onChange={(v) => setProviderField("saml_groups_attribute", v)} /></div>}
        <div className="grid gap-3 md:grid-cols-2"><Field label="Allowed email domains (comma-separated; blank = no domain allowlist)" value={Array.isArray(providerForm.allowed_email_domains) ? providerForm.allowed_email_domains.join(", ") : providerForm.allowed_email_domains} onChange={(v) => setProviderField("allowed_email_domains", v)} placeholder="example.com, subsidiary.example.com" /><SelectField label="Default tenant role" value={providerForm.default_role} onChange={(v) => setProviderField("default_role", v)} options={ROLES.filter((role) => role !== "OWNER")} /></div>
        <div className="flex flex-wrap gap-x-5 gap-y-2"><CheckField label="Enable provider (only after configuration is verified)" checked={providerForm.enabled} onChange={(v) => setProviderField("enabled", v)} /><CheckField label="Allow just-in-time provisioning" checked={providerForm.jit_provisioning} onChange={(v) => setProviderField("jit_provisioning", v)} /><CheckField label="Trust verified IdP email" checked={providerForm.trust_email_from_idp} onChange={(v) => setProviderField("trust_email_from_idp", v)} /><CheckField label="Allow explicitly configured existing-email link" checked={providerForm.allow_existing_email_link} onChange={(v) => setProviderField("allow_existing_email_link", v)} /></div>
        <div className="flex flex-wrap items-center gap-3"><Button type="submit" disabled={busy}>{editingProvider ? "Save provider" : "Create provider"}</Button><p className="text-xs text-slate-500">OIDC callback: <code className="break-all">/api/v1/enterprise-auth/oidc/callback/</code></p></div>
      </form>
      <div className="mt-4 space-y-3">{providers.length === 0 ? <p className="text-sm text-slate-500">No providers configured.</p> : providers.map((provider) => <div key={provider.id} className="flex flex-col justify-between gap-3 rounded-xl border border-slate-200 p-4 md:flex-row md:items-center"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><p className="font-semibold text-slate-900">{provider.name}</p><span className={`rounded-full px-2 py-0.5 text-xs font-bold ${provider.enabled ? "bg-emerald-100 text-emerald-800" : "bg-slate-100 text-slate-600"}`}>{provider.enabled ? "Enabled" : "Disabled"}</span><span className="rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-bold text-indigo-800">{provider.protocol}</span></div><p className="mt-1 break-all text-xs text-slate-500">slug: {provider.slug} · client secret saved: {provider.has_client_secret ? "yes" : "no"}</p><p className="mt-1 break-all text-xs text-slate-500">Login URL: {ROOT}/{provider.protocol === "OIDC" ? "oidc" : "saml"}/{tenant?.slug}/{provider.slug}/login/</p>{provider.protocol === "SAML" && <p className="mt-1 break-all text-xs text-slate-500">SP metadata: {ROOT}/saml/{tenant?.slug}/{provider.slug}/metadata/</p>}</div><div className="flex shrink-0 gap-2"><Button variant="secondary" disabled={busy} onClick={() => startEditProvider(provider)}>Edit</Button>{provider.enabled && <Button variant="danger" disabled={busy} onClick={() => void disableProvider(provider)}>Disable</Button>}</div></div>)}</div>
    </Section>

    <div className="grid gap-6 xl:grid-cols-2">
      <Section title="SCIM 2.0 directory provisioning" description="Create a bearer token with least-privilege scopes. The raw token is shown once; store it in your identity platform's secret manager. Deactivation removes the tenant membership and revokes sessions only when no other active tenant membership remains.">
        <div className="rounded-xl bg-slate-50 p-4"><p className="text-xs font-bold uppercase tracking-wide text-slate-500">SCIM Users endpoint</p><code className="mt-2 block break-all text-sm text-slate-800">{scimUrl}</code><Button variant="secondary" onClick={() => { void navigator.clipboard?.writeText(scimUrl); setNotice("SCIM endpoint copied when clipboard access is available."); }}>Copy endpoint</Button></div>
        <form onSubmit={createToken} className="mt-4 space-y-3"><Field label="Token label" value={newTokenLabel} onChange={setNewTokenLabel} required /><div className="flex flex-wrap gap-x-4 gap-y-2">{["Users:read", "Users:write", "Roles:manage", ...(tenant?.role === "OWNER" ? ["Roles:admin"] : [])].map((scope) => <CheckField key={scope} label={scope} checked={newTokenScopes.includes(scope)} onChange={() => toggleTokenScope(scope)} />)}</div><Button type="submit" disabled={busy || newTokenScopes.length === 0}>Create SCIM token</Button></form>
        {oneTimeToken && <div className="mt-4 rounded-xl border border-amber-300 bg-amber-50 p-4"><p className="text-sm font-bold text-amber-950">Copy this token now — it will not be shown again.</p><code className="mt-2 block break-all text-xs text-amber-900">{oneTimeToken}</code><Button variant="secondary" onClick={() => { void navigator.clipboard?.writeText(oneTimeToken); }}>Copy token</Button></div>}
        <div className="mt-4 space-y-2">{tokens.map((token) => <div key={token.id} className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 p-3"><div><p className="text-sm font-semibold">{token.label}</p><p className="text-xs text-slate-500">Scopes: {(token.scopes || []).join(", ")} · {token.revoked_at ? "Revoked" : token.expires_at ? `Expires ${new Date(token.expires_at).toLocaleString()}` : "No expiry set"}</p></div>{!token.revoked_at && <Button variant="danger" disabled={busy} onClick={() => void revokeToken(token)}>Revoke</Button>}</div>)}</div>
      </Section>

      <Section title="Matter-level access policies" description="Tenant role access remains unchanged unless an ACL is explicitly enforced for a matter. Before enabling enforcement, create user or role grants with appropriate access levels.">
        <form onSubmit={savePolicy} className="space-y-3"><div className="grid gap-3 sm:grid-cols-2"><SelectField label="Matter type" value={policyForm.matter_type} onChange={(v) => setPolicyForm((old) => ({ ...old, matter_type: v }))} options={MATTER_TYPES} /><Field label="Matter UUID" value={policyForm.matter_id} onChange={(v) => setPolicyForm((old) => ({ ...old, matter_id: v }))} required placeholder="UUID from the matter URL" /></div><CheckField label="Enforce explicit grants for this matter" checked={policyForm.enforce_acl} onChange={(v) => setPolicyForm((old) => ({ ...old, enforce_acl: v }))} /><Button type="submit" disabled={busy}>Save policy</Button></form>
        <div className="mt-4 space-y-2">{policies.map((policy) => <div key={policy.id} className="rounded-lg border border-slate-200 p-3"><div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm font-semibold">{policy.matter_type} · <code>{policy.matter_id}</code></p><Button variant={policy.enforce_acl ? "danger" : "secondary"} disabled={busy} onClick={() => void togglePolicy(policy, !policy.enforce_acl)}>{policy.enforce_acl ? "Disable ACL" : "Enable ACL"}</Button></div><p className="mt-1 text-xs text-slate-500">{policy.enforce_acl ? "Only explicit user/role grants allow access." : "Existing tenant roles continue to govern access."}</p></div>)}</div>
      </Section>
    </div>

    <Section title="Matter grants" description="Grant access to a tenant member by user UUID or by tenant role. Users granted directly must already have active membership in this tenant.">
      <form onSubmit={saveGrant} className="grid gap-3 md:grid-cols-3"><SelectField label="Matter type" value={grantForm.matter_type} onChange={(v) => setGrantForm((old) => ({ ...old, matter_type: v }))} options={MATTER_TYPES} /><Field label="Matter UUID" value={grantForm.matter_id} onChange={(v) => setGrantForm((old) => ({ ...old, matter_id: v }))} required /><Field label="Subject user UUID (or leave blank for role)" value={grantForm.subject_user} onChange={(v) => setGrantForm((old) => ({ ...old, subject_user: v, subject_role: v ? "" : old.subject_role }))} placeholder="User UUID" /><SelectField label="Subject role" value={grantForm.subject_role} onChange={(v) => setGrantForm((old) => ({ ...old, subject_role: v, subject_user: v ? "" : old.subject_user }))} options={["", ...ROLES]} /><SelectField label="Permission level" value={grantForm.level} onChange={(v) => setGrantForm((old) => ({ ...old, level: v }))} options={LEVELS} /><div className="flex items-end"><Button type="submit" disabled={busy}>Add grant</Button></div></form>
      <div className="mt-4 space-y-2">{grants.map((grant) => <div key={grant.id} className="flex flex-col justify-between gap-2 rounded-lg border border-slate-200 p-3 sm:flex-row sm:items-center"><div><p className="text-sm font-semibold">{grant.matter_type} · {grant.level}</p><p className="break-all text-xs text-slate-500">Matter: {grant.matter_id} · Subject: {grant.subject_username || grant.subject_user || `Role ${grant.subject_role}`}</p></div><Button variant="danger" disabled={busy} onClick={() => void deleteGrant(grant)}>Revoke grant</Button></div>)}</div>
    </Section>

    <Section title="Managed sessions" description="Session registry stores keyed hashes, not raw Django session keys. Revoking an individual or all sessions for a user is global to that user's PatentShield account and can sign them out of other workspaces.">
      <div className="space-y-2">{sessions.length === 0 ? <p className="text-sm text-slate-500">No managed sessions are currently registered for active tenant members.</p> : sessions.map((session) => <div key={session.id} className="flex flex-col justify-between gap-3 rounded-lg border border-slate-200 p-3 sm:flex-row sm:items-center"><div><div className="flex flex-wrap items-center gap-2"><p className="text-sm font-semibold">{session.username} · {session.auth_source}</p>{session.is_current && <span className="rounded-full bg-indigo-100 px-2 py-0.5 text-xs font-semibold text-indigo-800">Current session</span>}{session.revoked_at && <span className="rounded-full bg-rose-100 px-2 py-0.5 text-xs font-semibold text-rose-800">Revoked</span>}</div><p className="mt-1 text-xs text-slate-500">{session.email} · last seen {session.last_seen_at ? new Date(session.last_seen_at).toLocaleString() : "unknown"}</p><p className="mt-1 break-all text-xs text-slate-400">{session.user_agent || "User agent unavailable"}</p></div><div className="flex flex-wrap gap-2">{!session.revoked_at && <Button variant="danger" disabled={busy || session.is_current} onClick={() => void revokeSession(session)}>Revoke session</Button>}<Button variant="secondary" disabled={busy} onClick={() => void revokeUserSessions(session)}>Revoke all for user</Button></div></div>)}</div>
    </Section>

    <Section title="Administrative audit export" description="Exports tenant-scoped audit history, redacts sensitive metadata keys, neutralizes spreadsheet formulas in CSV, and caps each export at 10,000 rows. Export generation is itself audited.">
      <div className="flex flex-wrap gap-3"><Button onClick={() => void exportAudit("csv")}>Download CSV</Button><Button variant="secondary" onClick={() => void exportAudit("jsonl")}>Download JSONL</Button></div>
    </Section>
    <p className="text-xs leading-5 text-slate-500">Signed-in administrator: {user?.username || user?.email || "current user"}. Federation, provisioning and ACL configuration should be tested against a non-production identity provider before enabling for production users.</p>
  </main>;
}
