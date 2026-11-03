import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiRequest, requireResultsArray } from "../lib/apiClient";

const API_ROOT = "/api/v1/patents/surveillance";
function safeHttpsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}
const RULE_TYPES = [
  ["ASSIGNEE", "Competitor / assignee"],
  ["PUBLICATION", "Publication or application number"],
  ["FAMILY", "Patent family"],
  ["CLASSIFICATION", "IPC/CPC classification"],
  ["SAVED_SEARCH", "Saved provider search"],
];
const ALERT_TYPES = [
  ["PUBLICATION_MATCHED", "Publication matched"],
  ["PUBLICATION_UPDATED", "Publication metadata changed"],
  ["FAMILY_CHANGED", "Family changed"],
  ["STATUS_CHANGED", "Legal status changed"],
  ["LEGAL_EVENT", "New legal event"],
];

function friendlyError(error) {
  if (error instanceof ApiError) return error.message;
  return error instanceof Error ? error.message : "The request could not be completed.";
}

function Card({ title, subtitle, children, className = "" }) {
  return (
    <section className={`ps-panel ${className}`}>
      <div className="mb-4">
        <h2 className="text-base font-semibold text-slate-950">{title}</h2>
        {subtitle && <p className="mt-1 text-sm leading-5 text-slate-500">{subtitle}</p>}
      </div>
      {children}
    </section>
  );
}

function Field({ label, children }) {
  return (
    <label className="block text-sm font-medium text-slate-700">
      <span className="mb-1.5 block">{label}</span>
      {children}
    </label>
  );
}

const inputClass = "ps-input";
const buttonClass = "ps-button-primary";
const secondaryButtonClass = "ps-button-secondary";

function StateBadge({ value }) {
  const styles = {
    OPEN: "bg-amber-50 text-amber-800 ring-amber-200",
    ACKNOWLEDGED: "bg-blue-50 text-blue-800 ring-blue-200",
    RESOLVED: "bg-emerald-50 text-emerald-800 ring-emerald-200",
    DELIVERED: "bg-emerald-50 text-emerald-800 ring-emerald-200",
    RETRYING: "bg-amber-50 text-amber-800 ring-amber-200",
    DEAD_LETTER: "bg-rose-50 text-rose-800 ring-rose-200",
    FAILED: "bg-rose-50 text-rose-800 ring-rose-200",
    SUCCEEDED: "bg-emerald-50 text-emerald-800 ring-emerald-200",
    RUNNING: "bg-blue-50 text-blue-800 ring-blue-200",
  };
  return (
    <span className={`inline-flex rounded-full px-2.5 py-1 text-[11px] font-semibold ring-1 ring-inset ${styles[value] || "bg-slate-100 text-slate-700 ring-slate-200"}`}>
      {(value || "UNKNOWN").replaceAll("_", " ")}
    </span>
  );
}

export default function SurveillanceDashboard({ tenant }) {
  const [watchlists, setWatchlists] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [destinations, setDestinations] = useState([]);
  const [deliveries, setDeliveries] = useState([]);
  const [selectedWatchlistId, setSelectedWatchlistId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [watchlistForm, setWatchlistForm] = useState({ name: "", description: "", poll_interval_minutes: 360 });
  const [ruleForm, setRuleForm] = useState({ rule_type: "ASSIGNEE", value: "", match_mode: "CONTAINS", providers: ["USPTO", "EPO"] });
  const [destinationForm, setDestinationForm] = useState({ name: "", channel: "EMAIL", email_address: "", webhook_url: "" });
  const [subscriptionForm, setSubscriptionForm] = useState({ destination_id: "", alert_types: [] });

  const selectedWatchlist = useMemo(
    () => watchlists.find((item) => item.id === selectedWatchlistId) || watchlists[0] || null,
    [selectedWatchlistId, watchlists],
  );

  const request = useCallback(
    (path, options = {}) => apiRequest(path, { tenantId: tenant.id, ...options }),
    [tenant.id],
  );

  const loadAll = useCallback(async (signal) => {
    setLoading(true);
    setError("");
    try {
      const [watchlistPayload, alertPayload, destinationPayload, deliveryPayload] = await Promise.all([
        request(`${API_ROOT}/watchlists/?limit=100`, { signal }),
        request(`${API_ROOT}/alerts/?limit=50`, { signal }),
        request(`${API_ROOT}/destinations/?limit=100`, { signal }),
        request(`${API_ROOT}/deliveries/?limit=30`, { signal }),
      ]);
      if (signal?.aborted) return;
      const nextWatchlists = requireResultsArray(watchlistPayload, "surveillance watchlist");
      setWatchlists(nextWatchlists);
      setAlerts(requireResultsArray(alertPayload, "surveillance alert list"));
      setDestinations(requireResultsArray(destinationPayload, "surveillance destination list"));
      setDeliveries(requireResultsArray(deliveryPayload, "surveillance delivery list"));
      setSelectedWatchlistId((current) =>
        nextWatchlists.some((item) => item.id === current) ? current : (nextWatchlists[0]?.id || ""),
      );
    } catch (requestError) {
      if (!signal?.aborted) setError(friendlyError(requestError));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [request]);

  useEffect(() => {
  const controller = new AbortController();

  Promise.resolve().then(() => {
    if (!controller.signal.aborted) {
      void loadAll(controller.signal);
    }
  });

  return () => controller.abort();
}, [loadAll]);

  async function runAction(action, successMessage) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice(successMessage);
      await loadAll();
    } catch (requestError) {
      setError(friendlyError(requestError));
    } finally {
      setBusy(false);
    }
  }

  async function createWatchlist(event) {
    event.preventDefault();
    await runAction(async () => {
      const created = await request(`${API_ROOT}/watchlists/`, {
        method: "POST",
        body: {
          ...watchlistForm,
          poll_interval_minutes: Number(watchlistForm.poll_interval_minutes),
        },
      });
      setSelectedWatchlistId(created.id);
      setWatchlistForm({ name: "", description: "", poll_interval_minutes: 360 });
    }, "Watchlist created.");
  }

  async function createRule(event) {
    event.preventDefault();
    if (!selectedWatchlist) return;
    await runAction(async () => {
      await request(`${API_ROOT}/watchlists/${selectedWatchlist.id}/rules/`, {
        method: "POST",
        body: ruleForm,
      });
      setRuleForm((current) => ({ ...current, value: "" }));
    }, "Monitoring rule added.");
  }

  async function createDestination(event) {
    event.preventDefault();
    await runAction(async () => {
      await request(`${API_ROOT}/destinations/`, {
        method: "POST",
        body: destinationForm,
      });
      setDestinationForm({ name: "", channel: "EMAIL", email_address: "", webhook_url: "" });
    }, "Notification destination saved.");
  }

  async function createSubscription(event) {
    event.preventDefault();
    if (!selectedWatchlist || !subscriptionForm.destination_id) return;
    await runAction(async () => {
      await request(`${API_ROOT}/watchlists/${selectedWatchlist.id}/subscriptions/`, {
        method: "POST",
        body: subscriptionForm,
      });
      setSubscriptionForm({ destination_id: "", alert_types: [] });
    }, "Alert subscription saved.");
  }

  async function updateAlert(alert, action) {
    await runAction(
      () => request(`${API_ROOT}/alerts/${alert.id}/${action}/`, { method: "POST", body: {} }),
      action === "acknowledge" ? "Alert acknowledged." : "Alert resolved.",
    );
  }

  async function pollWatchlist() {
    if (!selectedWatchlist) return;
    await runAction(
      () => request(`${API_ROOT}/watchlists/${selectedWatchlist.id}/poll/`, { method: "POST", body: {} }),
      "Poll queued. A queued task is not proof that delivery or provider sync has completed.",
    );
  }

  async function retryDelivery(delivery) {
    await runAction(
      () => request(`${API_ROOT}/deliveries/${delivery.id}/retry/`, { method: "POST", body: {} }),
      "Delivery retry queued.",
    );
  }

  function toggleAlertType(value) {
    setSubscriptionForm((current) => ({
      ...current,
      alert_types: current.alert_types.includes(value)
        ? current.alert_types.filter((item) => item !== value)
        : [...current.alert_types, value],
    }));
  }

  return (
    <main className="ps-page">
      <div className="mb-7 flex flex-col gap-4 md:flex-row md:items-end md:justify-between">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-indigo-700">IP intelligence · Phase 3</p>
          <h1 className="mt-2 text-2xl font-bold tracking-tight text-slate-950 sm:text-3xl">Competitor surveillance</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
            Monitor public patent records, family mappings, legal status and source events. Alerts link back to provider evidence; they are not legal conclusions.
          </p>
        </div>
        <button type="button" className={secondaryButtonClass} onClick={() => void loadAll()} disabled={loading || busy}>
          {loading ? "Refreshing…" : "Refresh dashboard"}
        </button>
      </div>

      {error && <div className="mb-4 ps-alert-error" role="alert">{error}</div>}
      {notice && <div className="mb-4 ps-alert-success" role="status">{notice}</div>}

      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[
          ["Watchlists", watchlists.length],
          ["Open alerts", alerts.filter((alert) => alert.state === "OPEN").length],
          ["Destinations", destinations.filter((destination) => destination.is_active).length],
          ["Delivery issues", deliveries.filter((delivery) => ["RETRYING", "DEAD_LETTER"].includes(delivery.status)).length],
        ].map(([label, value]) => (
          <div key={label} className="ps-metric-card">
            <p className="text-xs font-medium text-slate-500">{label}</p>
            <p className="mt-1 text-2xl font-bold text-slate-950">{value}</p>
          </div>
        ))}
      </div>

      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)]">
        <div className="space-y-5">
          <Card title="Watchlists" subtitle="Each list contains provider search criteria and tenant-specific observation history.">
            <div className="flex flex-col gap-3 sm:flex-row">
              <Field label="Current watchlist">
                <select className={inputClass} value={selectedWatchlist?.id || ""} onChange={(event) => setSelectedWatchlistId(event.target.value)}>
                  {watchlists.length === 0 && <option value="">No watchlists yet</option>}
                  {watchlists.map((watchlist) => <option key={watchlist.id} value={watchlist.id}>{watchlist.name}</option>)}
                </select>
              </Field>
              <div className="flex items-end">
                <button type="button" className={buttonClass} onClick={() => void pollWatchlist()} disabled={!selectedWatchlist || busy || !selectedWatchlist.rule_count}>
                  Poll now
                </button>
              </div>
            </div>
            {selectedWatchlist && (
              <div className="mt-4 rounded-xl bg-slate-50 p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="font-semibold text-slate-900">{selectedWatchlist.name}</p>
                    <p className="mt-1 text-xs text-slate-500">{selectedWatchlist.description || "No description provided."}</p>
                  </div>
                  <StateBadge value={selectedWatchlist.poll_status} />
                </div>
                <div className="mt-3 grid grid-cols-2 gap-3 text-xs text-slate-600">
                  <p>Rules <strong className="text-slate-900">{selectedWatchlist.rule_count}</strong></p>
                  <p>Alerts <strong className="text-slate-900">{selectedWatchlist.alert_count}</strong></p>
                  <p>Last poll <strong className="block pt-1 font-medium text-slate-800">{selectedWatchlist.last_polled_at ? new Date(selectedWatchlist.last_polled_at).toLocaleString() : "Not yet polled"}</strong></p>
                  <p>Next poll <strong className="block pt-1 font-medium text-slate-800">{selectedWatchlist.next_poll_at ? new Date(selectedWatchlist.next_poll_at).toLocaleString() : "Not scheduled"}</strong></p>
                </div>
                {selectedWatchlist.last_error_code && <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs text-amber-900">Last operational error: {selectedWatchlist.last_error_code}</p>}
                {selectedWatchlist.rules?.length > 0 && <div className="mt-4 flex flex-wrap gap-2">{selectedWatchlist.rules.map((rule) => <span key={rule.id} className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-700">{rule.rule_type.replaceAll("_", " ")}: {rule.value}</span>)}</div>}
              </div>
            )}
          </Card>

          <Card title="Create a watchlist" subtitle="Polling can run only when deployment surveillance and the existing external-disclosure policy are enabled.">
            <form className="space-y-3" onSubmit={createWatchlist}>
              <Field label="Name"><input className={inputClass} maxLength={180} required value={watchlistForm.name} onChange={(event) => setWatchlistForm({ ...watchlistForm, name: event.target.value })} placeholder="Example: Photonics competitors" /></Field>
              <Field label="Description"><textarea className={inputClass} rows={2} value={watchlistForm.description} onChange={(event) => setWatchlistForm({ ...watchlistForm, description: event.target.value })} placeholder="Optional context for this watchlist" /></Field>
              <Field label="Polling interval (minutes)"><input className={inputClass} type="number" min="15" max="10080" required value={watchlistForm.poll_interval_minutes} onChange={(event) => setWatchlistForm({ ...watchlistForm, poll_interval_minutes: event.target.value })} /></Field>
              <button type="submit" className={buttonClass} disabled={busy}>{busy ? "Saving…" : "Create watchlist"}</button>
            </form>
          </Card>

          <Card title="Add a monitoring rule" subtitle="Provider queries are logged through the existing HMAC audit flow before external disclosure.">
            <form className="space-y-3" onSubmit={createRule}>
              <Field label="Rule type"><select className={inputClass} value={ruleForm.rule_type} onChange={(event) => setRuleForm({ ...ruleForm, rule_type: event.target.value })}>{RULE_TYPES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
              <Field label="Value or saved search"><input className={inputClass} maxLength={500} required value={ruleForm.value} onChange={(event) => setRuleForm({ ...ruleForm, value: event.target.value })} placeholder="Competitor name, publication number, CPC code, or search" /></Field>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Match mode"><select className={inputClass} value={ruleForm.match_mode} onChange={(event) => setRuleForm({ ...ruleForm, match_mode: event.target.value })}><option value="CONTAINS">Contains</option><option value="EXACT">Exact</option></select></Field>
                <Field label="Providers"><select className={inputClass} value={ruleForm.providers.join(",")} onChange={(event) => setRuleForm({ ...ruleForm, providers: event.target.value.split(",") })}><option value="USPTO,EPO">USPTO + EPO</option><option value="USPTO">USPTO</option><option value="EPO">EPO</option></select></Field>
              </div>
              <button type="submit" className={buttonClass} disabled={busy || !selectedWatchlist}>Add rule to selected list</button>
            </form>
          </Card>
        </div>

        <div className="space-y-5">
          <Card title="Alert history" subtitle="Every alert is deduplicated and tied to a canonical publication and source snapshot.">
            {loading ? <p className="text-sm text-slate-500">Loading alerts…</p> : alerts.length === 0 ? <p className="rounded-xl border border-dashed border-slate-300 px-4 py-8 text-center text-sm text-slate-500">No alerts yet. Add a rule and run a poll to begin monitoring.</p> : (
              <div className="space-y-3">
                {alerts.map((alert) => {
                  const sourceUrl = safeHttpsUrl(alert.evidence?.source_url || alert.publication?.source_url);
                  return (
                    <article key={alert.id} className="rounded-xl border border-slate-200 p-4">
                      <div className="flex flex-wrap items-start justify-between gap-2"><p className="max-w-xl font-semibold leading-5 text-slate-900">{alert.title}</p><StateBadge value={alert.state} /></div>
                      <p className="mt-2 text-xs text-slate-500">{alert.alert_type.replaceAll("_", " ")} · {alert.watchlist_name} · {new Date(alert.created_at).toLocaleString()}</p>
                      <p className="mt-2 text-sm leading-5 text-slate-700">{alert.summary}</p>
                      <div className="mt-3 flex flex-wrap items-center gap-2">
                        {sourceUrl && <a className="text-xs font-semibold text-indigo-700 underline underline-offset-2" href={sourceUrl} target="_blank" rel="noreferrer">Open provider source</a>}
                        {alert.state === "OPEN" && <button type="button" className={secondaryButtonClass} onClick={() => void updateAlert(alert, "acknowledge")} disabled={busy}>Acknowledge</button>}
                        {alert.state === "ACKNOWLEDGED" && <button type="button" className={secondaryButtonClass} onClick={() => void updateAlert(alert, "resolve")} disabled={busy}>Resolve</button>}
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </Card>

          <Card title="Notification destinations" subtitle="Webhook secrets are encrypted by the backend and never returned to the browser. The deployment must allowlist exact webhook hosts.">
            <form className="space-y-3" onSubmit={createDestination}>
              <Field label="Destination name"><input className={inputClass} required maxLength={160} value={destinationForm.name} onChange={(event) => setDestinationForm({ ...destinationForm, name: event.target.value })} placeholder="Legal ops notifications" /></Field>
              <Field label="Channel"><select className={inputClass} value={destinationForm.channel} onChange={(event) => setDestinationForm({ ...destinationForm, channel: event.target.value, email_address: "", webhook_url: "" })}><option value="EMAIL">Email</option><option value="SLACK">Slack webhook</option><option value="TEAMS">Microsoft Teams webhook</option></select></Field>
              {destinationForm.channel === "EMAIL" ? (
                <Field label="Email address"><input className={inputClass} type="email" required value={destinationForm.email_address} onChange={(event) => setDestinationForm({ ...destinationForm, email_address: event.target.value })} placeholder="alerts@example.com" /></Field>
              ) : (
                <Field label="Webhook URL (stored encrypted)"><input className={inputClass} type="url" required value={destinationForm.webhook_url} onChange={(event) => setDestinationForm({ ...destinationForm, webhook_url: event.target.value })} placeholder="https://…" autoComplete="off" /></Field>
              )}
              <button type="submit" className={buttonClass} disabled={busy}>Save destination</button>
            </form>
            {destinations.length > 0 && <div className="mt-4 space-y-2">{destinations.map((destination) => <div key={destination.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-50 px-3 py-2"><div><p className="text-sm font-semibold text-slate-800">{destination.name}</p><p className="text-xs text-slate-500">{destination.channel === "EMAIL" ? destination.email_address : (destination.has_webhook_secret ? "Encrypted webhook configured" : "Webhook not configured")}</p></div><StateBadge value={destination.is_active ? "ACTIVE" : "INACTIVE"} /></div>)}</div>}
          </Card>

          <Card title="Subscribe a watchlist" subtitle="Choose which alert classes reach a destination. No selected classes means all alert types.">
            <form className="space-y-3" onSubmit={createSubscription}>
              <Field label="Destination"><select className={inputClass} required value={subscriptionForm.destination_id} onChange={(event) => setSubscriptionForm({ ...subscriptionForm, destination_id: event.target.value })}><option value="">Select destination</option>{destinations.filter((item) => item.is_active).map((item) => <option key={item.id} value={item.id}>{item.name} ({item.channel})</option>)}</select></Field>
              <div><p className="mb-2 text-sm font-medium text-slate-700">Alert types</p><div className="grid gap-2 sm:grid-cols-2">{ALERT_TYPES.map(([value, label]) => <label key={value} className="flex items-center gap-2 text-xs text-slate-700"><input type="checkbox" checked={subscriptionForm.alert_types.includes(value)} onChange={() => toggleAlertType(value)} className="rounded border-slate-300 text-indigo-700 focus:ring-indigo-500" />{label}</label>)}</div></div>
              <button type="submit" className={buttonClass} disabled={busy || !selectedWatchlist || !subscriptionForm.destination_id}>Subscribe selected watchlist</button>
            </form>
          </Card>

          <Card title="Delivery status" subtitle="Queued means work was scheduled, not that a recipient received it.">
            {deliveries.length === 0 ? <p className="text-sm text-slate-500">No delivery attempts have been generated yet.</p> : <div className="space-y-2">{deliveries.map((delivery) => <div key={delivery.id} className="rounded-lg border border-slate-200 p-3"><div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm font-semibold text-slate-800">{delivery.destination_name}</p><StateBadge value={delivery.status} /></div><p className="mt-1 text-xs text-slate-500">{delivery.alert_title} · Attempts: {delivery.attempt_count}{delivery.last_http_status ? ` · HTTP ${delivery.last_http_status}` : ""}</p>{delivery.last_error_code && <p className="mt-1 text-xs text-rose-700">{delivery.last_error_code}</p>}{delivery.status === "DEAD_LETTER" && <button type="button" className={`${secondaryButtonClass} mt-2`} onClick={() => void retryDelivery(delivery)} disabled={busy}>Retry delivery</button>}</div>)}</div>}
          </Card>
        </div>
      </div>
      <p className="mt-7 text-xs leading-5 text-slate-500">Workspace: {tenant.name}. Monitoring uses public provider data and the configured source/API permissions. Review source records and qualified counsel before making legal or portfolio decisions.</p>
    </main>
  );
}
