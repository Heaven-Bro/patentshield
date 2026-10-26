import { useState } from "react";

/**
 * Session-based sign-in screen for the PatentShield browser workspace.
 * @param {{onSubmit: (credentials:{username:string,password:string})=>Promise<void>, busy?:boolean, error?:string}} props
 */
export default function LoginPage({ onSubmit, busy = false, error = "" }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  async function handleSubmit(event) {
    event.preventDefault();
    await onSubmit({ username: username.trim(), password });
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-950 px-4 py-10">
      <div className="grid w-full max-w-5xl overflow-hidden rounded-3xl border border-white/10 bg-white shadow-2xl lg:grid-cols-2">
        <section className="relative hidden overflow-hidden bg-gradient-to-br from-indigo-700 via-indigo-800 to-slate-950 p-10 text-white lg:flex lg:flex-col lg:justify-between">
          <div aria-hidden="true" className="absolute -right-24 -top-24 h-80 w-80 rounded-full border border-white/10" />
          <div aria-hidden="true" className="absolute -right-12 -top-12 h-56 w-56 rounded-full border border-white/10" />
          <div className="relative">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-white text-lg font-black text-indigo-800 shadow-lg">P</div>
            <p className="mt-8 text-xs font-semibold uppercase tracking-[0.22em] text-indigo-200">PatentShield</p>
            <h1 className="mt-3 max-w-sm text-3xl font-semibold leading-tight">A secure workspace for patent evidence review.</h1>
            <p className="mt-4 max-w-sm text-sm leading-6 text-indigo-100/80">
              Keep source documents, prior-art evidence, and technical assessments inside the right tenant workspace.
            </p>
          </div>
          <div className="relative grid grid-cols-2 gap-3 text-xs">
            <div className="rounded-xl border border-white/10 bg-white/5 p-3">
              <p className="font-semibold text-white">Tenant-aware access</p>
              <p className="mt-1 text-indigo-100/70">Membership checks on API requests</p>
            </div>
            <div className="rounded-xl border border-white/10 bg-white/5 p-3">
              <p className="font-semibold text-white">Evidence provenance</p>
              <p className="mt-1 text-indigo-100/70">Review source-linked analysis</p>
            </div>
          </div>
        </section>

        <section className="p-7 sm:p-10 lg:p-12">
          <div className="mb-8 flex items-center gap-3 lg:hidden">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-700 font-black text-white">P</div>
            <span className="text-lg font-semibold text-slate-900">PatentShield</span>
          </div>
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-indigo-700">Secure sign in</p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight text-slate-950">Welcome back</h2>
          <p className="mt-2 text-sm leading-6 text-slate-500">Sign in with your PatentShield account to access your authorized workspaces.</p>

          <form className="mt-8 space-y-5" onSubmit={handleSubmit}>
            <div>
              <label htmlFor="username" className="mb-1.5 block text-sm font-medium text-slate-700">Username</label>
              <input
                id="username"
                name="username"
                type="text"
                autoComplete="username"
                required
                maxLength={150}
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                disabled={busy}
                className="w-full rounded-xl border border-slate-300 bg-white px-3.5 py-3 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-indigo-500 focus:ring-4 focus:ring-indigo-100 disabled:bg-slate-50"
                placeholder="Enter your username"
              />
            </div>
            <div>
              <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-slate-700">Password</label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={busy}
                className="w-full rounded-xl border border-slate-300 bg-white px-3.5 py-3 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-indigo-500 focus:ring-4 focus:ring-indigo-100 disabled:bg-slate-50"
                placeholder="Enter your password"
              />
            </div>

            {error && (
              <div className="rounded-xl border border-rose-200 bg-rose-50 px-3.5 py-3 text-sm text-rose-800" role="alert">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={busy || !username.trim() || !password}
              className="flex w-full items-center justify-center rounded-xl bg-indigo-700 px-4 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-800 focus:outline-none focus:ring-4 focus:ring-indigo-200 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {busy ? "Signing in…" : "Sign in securely"}
            </button>
          </form>
          <p className="mt-6 text-xs leading-5 text-slate-400">Your session is managed by Django using HTTP-only cookies and CSRF protection. Passwords are never stored in browser storage.</p>
        </section>
      </div>
    </main>
  );
}
