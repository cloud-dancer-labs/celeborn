"use client";

import { useCallback, useEffect, useState } from "react";

type JiraStatus = {
  connected: boolean;
  reason?: string;
  site?: string;
  project_key?: string;
  email?: string;
  display_name?: string;
};

type ReconcileRow = {
  id?: string;
  jira?: string;
  key?: string;
  title: string;
  state?: string;
  celeborn_state?: string;
  jira_state?: string;
};

type ReconcileReport = {
  celeborn_truth?: boolean;
  project_key?: string;
  linked_count?: number;
  jira_orphans?: ReconcileRow[];
  celeborn_unlinked?: ReconcileRow[];
  state_drift?: ReconcileRow[];
  stale_links?: ReconcileRow[];
  applied?: boolean;
  pushed_count?: number;
  error?: string;
};

const TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens";

export default function JiraPanel() {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<JiraStatus | null>(null);
  const [report, setReport] = useState<ReconcileReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");

  const [site, setSite] = useState("");
  const [email, setEmail] = useState("");
  const [project, setProject] = useState("");
  const [token, setToken] = useState("");

  const showToast = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast(""), 3200);
  }, []);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await fetch("/api/jira/status", { cache: "no-store" });
      const doc = (await res.json()) as JiraStatus;
      setStatus(doc);
      if (doc.connected) {
        setSite(doc.site || "");
        setEmail(doc.email || "");
        setProject(doc.project_key || "");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadReconcile = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/jira/reconcile", { cache: "no-store" });
      const doc = (await res.json()) as ReconcileReport;
      if (doc.error) throw new Error(doc.error);
      setReport(doc);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (open && status?.connected) void loadReconcile();
  }, [open, status?.connected, loadReconcile]);

  async function handleConnect(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/jira/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ site, email, project, token }),
      });
      const doc = (await res.json()) as JiraStatus & { ok?: boolean; reconcile?: ReconcileReport; error?: string };
      if (!res.ok || doc.ok === false) {
        throw new Error(doc.error || "connect failed");
      }
      setToken("");
      setStatus(doc);
      if (doc.reconcile) setReport(doc.reconcile);
      showToast("Connected to Jira");
      setOpen(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleApply() {
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/jira/reconcile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ apply: true }),
      });
      const doc = (await res.json()) as ReconcileReport;
      if (doc.error) throw new Error(doc.error);
      setReport(doc);
      showToast(`Pushed ${doc.pushed_count ?? 0} card(s) to Jira`);
      await loadStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const connected = Boolean(status?.connected);
  const orphans = report?.jira_orphans?.length ?? 0;
  const drift = report?.state_drift?.length ?? 0;
  const unlinked = report?.celeborn_unlinked?.length ?? 0;
  const stale = report?.stale_links?.length ?? 0;

  return (
    <>
      <button
        type="button"
        className="jira-trigger"
        data-connected={connected || undefined}
        onClick={() => setOpen(true)}
        title={connected ? `Jira · ${status?.project_key}` : "Connect Jira"}
      >
        <span className="jira-trigger-dot" data-live={connected || undefined} />
        {connected ? `Jira · ${status?.project_key}` : "Connect Jira"}
      </button>

      {open ? (
        <div className="jira-overlay" role="presentation" onClick={() => setOpen(false)}>
          <aside
            className="jira-panel"
            role="dialog"
            aria-labelledby="jira-panel-title"
            onClick={(e) => e.stopPropagation()}
          >
            <header className="jira-panel-head">
              <div>
                <h2 id="jira-panel-title">Jira integration</h2>
                <p className="jira-panel-lede">
                  Celeborn is the source of truth — agents work from live context here. Jira is for
                  humans, stakeholders, and legacy bug intake; it receives updates, it does not drive
                  execution.
                </p>
              </div>
              <button type="button" className="jira-close" onClick={() => setOpen(false)} aria-label="Close">
                ✕
              </button>
            </header>

            {loading ? <p className="jira-muted">Checking connection…</p> : null}
            {error ? <p className="jira-error">{error}</p> : null}

            {!connected ? (
              <section className="jira-section">
                <h3>Connect your Jira Cloud site</h3>
                <ol className="jira-steps">
                  <li>
                    Create an API token at{" "}
                    <a href={TOKEN_URL} target="_blank" rel="noreferrer">
                      id.atlassian.com → Security → API tokens
                    </a>
                    . Paste it only here — never into chat.
                  </li>
                  <li>Enter your site URL, the Atlassian email for this project, and the project key.</li>
                  <li>
                    On first connect, Celeborn compares boards and flags stale Jira-only tickets without
                    importing them.
                  </li>
                </ol>

                <form className="jira-form" onSubmit={handleConnect}>
                  <label>
                    Site URL
                    <input
                      value={site}
                      onChange={(e) => setSite(e.target.value)}
                      placeholder="yourname.atlassian.net"
                      autoComplete="off"
                      required
                    />
                  </label>
                  <label>
                    Atlassian email
                    <input
                      type="email"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      placeholder="you@example.com"
                      autoComplete="email"
                      required
                    />
                  </label>
                  <label>
                    Project key
                    <input
                      value={project}
                      onChange={(e) => setProject(e.target.value.toUpperCase())}
                      placeholder="SCRUM"
                      autoComplete="off"
                      required
                    />
                  </label>
                  <label>
                    API token
                    <input
                      type="password"
                      value={token}
                      onChange={(e) => setToken(e.target.value)}
                      placeholder="Paste token (stored outside repo)"
                      autoComplete="off"
                      required
                    />
                  </label>
                  <button type="submit" className="jira-primary" disabled={busy}>
                    {busy ? "Connecting…" : "Connect & compare boards"}
                  </button>
                </form>
              </section>
            ) : (
              <>
                <section className="jira-section jira-connected-banner">
                  <div>
                    <strong>{status?.display_name}</strong>
                    <span className="jira-muted">
                      {" "}
                      · {status?.site} · {status?.project_key}
                    </span>
                  </div>
                  <button type="button" className="jira-ghost" onClick={() => void loadReconcile()} disabled={busy}>
                    Refresh audit
                  </button>
                </section>

                {report ? (
                  <section className="jira-section">
                    <h3>Board audit — Celeborn wins</h3>
                    <div className="jira-stats">
                      <span>{report.linked_count ?? 0} linked</span>
                      <span>{unlinked} Celeborn-only</span>
                      <span>{orphans} Jira-only (stale)</span>
                      <span>{drift} state drift</span>
                      <span>{stale} broken links</span>
                    </div>

                    {orphans > 0 ? (
                      <div className="jira-callout jira-callout-warn">
                        <strong>{orphans} Jira-only issue(s)</strong> — legacy or stakeholder tickets not
                        tracked in Celeborn. They stay in Jira for visibility; Celeborn will not import or
                        duplicate them.
                        <ul className="jira-list">
                          {(report.jira_orphans || []).slice(0, 6).map((row) => (
                            <li key={row.key}>
                              {row.key} — {row.title.slice(0, 72)}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}

                    {stale > 0 ? (
                      <div className="jira-callout jira-callout-warn">
                        <strong>{stale} broken jira: link(s)</strong> — Celeborn cards point at deleted Jira
                        issues. Reconcile apply will recreate missing issues for unlinked cards only.
                        <ul className="jira-list">
                          {(report.stale_links || []).slice(0, 6).map((row) => (
                            <li key={row.id}>
                              [{row.id}] {row.jira} — {row.title.slice(0, 60)}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}

                    {drift > 0 ? (
                      <div className="jira-callout">
                        <strong>{drift} state drift</strong> — linked cards where Jira lags Celeborn. Apply
                        pushes Celeborn columns outward.
                        <ul className="jira-list">
                          {(report.state_drift || []).slice(0, 6).map((row) => (
                            <li key={row.id}>
                              [{row.id}] {row.jira}: Celeborn {row.celeborn_state} → Jira {row.jira_state}
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : null}

                    {unlinked > 0 ? (
                      <div className="jira-callout">
                        <strong>{unlinked} Celeborn card(s)</strong> without a Jira link — apply creates
                        issues and links them back.
                      </div>
                    ) : null}

                    <div className="jira-actions">
                      <button type="button" className="jira-primary" onClick={() => void handleApply()} disabled={busy}>
                        {busy ? "Syncing…" : "Push Celeborn → Jira"}
                      </button>
                      <p className="jira-muted">
                        Auto-push keeps Jira current after card edits. Run this once after connecting to
                        align legacy tickets.
                      </p>
                    </div>
                  </section>
                ) : busy ? (
                  <p className="jira-muted">Auditing boards…</p>
                ) : null}
              </>
            )}
          </aside>
        </div>
      ) : null}

      {toast ? <div className="toast">{toast}</div> : null}
    </>
  );
}