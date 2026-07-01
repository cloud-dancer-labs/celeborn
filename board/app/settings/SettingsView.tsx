"use client";

import { useCallback, useEffect, useState } from "react";

// ---- shapes mirrored from `celeborn permissions --json` / `celeborn skills --json` -----------------
type RuleFlag = { rule: string; active: boolean; prefix?: string };
type ScopeFile = { path: string; exists: boolean; allow: string[]; defaultMode: string | null };
type PermState = {
  effective_default_mode: string | null;
  baseline: {
    tools: RuleFlag[];
    bash_prefixes: RuleFlag[];
    default_mode: { value: string; active: boolean };
    all_active: boolean;
  };
  danger: {
    spectrum: RuleFlag[];
    default_mode: { value: string; active: boolean };
    armed: boolean;
    confirm_phrase: string;
  };
  current_allow: string[];
  scopes: Record<string, ScopeFile>;
  error?: string;
};
type Skill = { name: string; description?: string; command?: string; installed?: boolean };
type SkillsState = {
  harness?: string;
  recommended_note?: string;
  core: Skill[];
  recommended: Skill[];
  mattpocock: {
    source: string;
    install_cmd: string;
    setup_hint: string;
    installed_count: number;
    total: number;
    claude_only?: boolean;
    last_refresh?: string | null;
    autoupdate?: boolean;
    refresh_days?: number;
    skills: Skill[];
  };
  error?: string;
};

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { cache: "no-store" });
  return (await r.json()) as T;
}
async function postJson<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await r.json()) as T;
}

function Pill({ on, onText = "Active", offText = "Inactive" }: { on: boolean; onText?: string; offText?: string }) {
  return <span className={`perm-pill ${on ? "on" : "off"}`}>{on ? onText : offText}</span>;
}

export default function SettingsView() {
  const [perm, setPerm] = useState<PermState | null>(null);
  const [skills, setSkills] = useState<SkillsState | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Auto-allow scope + Danger Zone arm state.
  const [baseScope, setBaseScope] = useState<"global" | "shared">("global");
  const [dangerScope, setDangerScope] = useState<"local" | "shared" | "global">("local");
  const [confirmText, setConfirmText] = useState("");
  const [showAllow, setShowAllow] = useState(false);

  const refresh = useCallback(async () => {
    const [p, s] = await Promise.all([
      getJson<PermState>("/api/permissions"),
      getJson<SkillsState>("/api/skills"),
    ]);
    setPerm(p);
    setSkills(s);
  }, []);

  useEffect(() => { refresh().catch((e) => setErr(String(e))); }, [refresh]);

  const act = useCallback(
    async (label: string, url: string, body: unknown, after?: (r: PermState | SkillsState) => void) => {
      setBusy(label);
      setErr(null);
      try {
        const r = await postJson<PermState | SkillsState>(url, body);
        if ((r as { error?: string }).error) {
          setErr((r as { error?: string }).error || "failed");
        } else if (after) {
          after(r);
        }
        await refresh();
      } catch (e) {
        setErr(String(e));
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  if (err && !perm && !skills) {
    return <section className="settings"><p className="settings-error">Failed to load settings: {err}</p></section>;
  }
  if (!perm || !skills) {
    return <section className="settings"><p className="settings-dim">Loading settings…</p></section>;
  }

  const dangerConfirm = perm.danger.confirm_phrase;
  const armed = perm.danger.armed;

  return (
    <section className="settings">
      {err ? <p className="settings-error">{err}</p> : null}

      {/* ---------------------------------------------------------------- SKILLS */}
      <div className="settings-section">
        <h2>Skills</h2>
        <p className="settings-dim">
          What Celeborn brings to a session: its own memory verbs, the Claude skills its advisor points
          you at, and the Matt Pocock skill suite (installed by default).
        </p>

        <h3 className="settings-sub">Celeborn core — the five verbs</h3>
        <div className="skill-grid">
          {skills.core.map((s) => (
            <div className="skill-card" key={s.name}>
              <div className="skill-head"><span className="skill-name">{s.name}</span>
                {s.command ? <code className="skill-cmd">{s.command}</code> : null}</div>
              <p>{s.description}</p>
            </div>
          ))}
        </div>

        <h3 className="settings-sub">
          Recommended — Claude skills the advisor surfaces <span className="harness-badge">Claude</span>
        </h3>
        <p className="settings-dim">
          {skills.recommended_note ||
            "Claude Code slash-commands. On Grok / Codex the advisor surfaces the same recommendations as prose, not installable skills."}
        </p>
        <div className="skill-grid">
          {skills.recommended.map((s) => (
            <div className="skill-card" key={s.name}>
              <div className="skill-head"><span className="skill-name">{s.name}</span></div>
              <p>{s.description}</p>
            </div>
          ))}
        </div>

        <h3 className="settings-sub">
          Matt Pocock — {skills.mattpocock.installed_count}/{skills.mattpocock.total} installed{" "}
          <span className="harness-badge">Claude</span>{" "}
          <a className="skill-src" href={skills.mattpocock.source} target="_blank" rel="noreferrer">
            {skills.mattpocock.source}
          </a>
        </h3>
        <div className="settings-actions">
          <button
            type="button"
            className="text-btn"
            disabled={busy !== null}
            onClick={() => act("install-mattpocock", "/api/skills", { action: "install-mattpocock", scope: "global" })}
          >
            {busy === "install-mattpocock" ? "Installing…" : "Enable Matt Pocock skills"}
          </button>
          <button
            type="button"
            className="text-btn"
            disabled={busy !== null}
            onClick={() => act("update-mattpocock", "/api/skills", { action: "update-mattpocock", scope: "global" })}
          >
            {busy === "update-mattpocock" ? "Updating…" : "Update to latest"}
          </button>
          <span className="settings-dim">
            installed to <code>.claude/skills/</code> (Claude-only). Auto-refreshes ~every{" "}
            {skills.mattpocock.refresh_days ?? 7} days{skills.mattpocock.autoupdate === false ? " (off)" : ""};
            last refresh: <strong>{skills.mattpocock.last_refresh || "never"}</strong>.
          </span>
        </div>
        <div className="skill-grid">
          {skills.mattpocock.skills.map((s) => (
            <div className="skill-card" key={s.name}>
              <div className="skill-head">
                <span className="skill-name">{s.name}</span>
                <Pill on={!!s.installed} onText="Installed" offText="Not installed" />
              </div>
              <p>{s.description}</p>
            </div>
          ))}
        </div>
      </div>

      {/* ---------------------------------------------------------------- AUTO-ALLOWS */}
      <div className="settings-section">
        <h2>Auto-allows</h2>
        <p className="settings-dim">
          The safe permission baseline Celeborn merges into <code>~/.claude/settings.json</code> on{" "}
          <code>wire --global</code> — read-only built-ins and trivially-reversible shell commands, plus{" "}
          <code>defaultMode: acceptEdits</code> (file edits auto-approve; Bash and anything
          outward-facing still prompts). Active = currently in effect on this machine.
        </p>

        <div className="settings-actions">
          <label className="settings-dim">Scope:&nbsp;
            <select value={baseScope} onChange={(e) => setBaseScope(e.target.value as "global" | "shared")}>
              <option value="global">Global (~/.claude — every project)</option>
              <option value="shared">This project (committed .claude/settings.json)</option>
            </select>
          </label>
          <button type="button" className="text-btn" disabled={busy !== null}
            onClick={() => act("baseline-on", "/api/permissions", { action: "baseline-on", scope: baseScope })}>
            {busy === "baseline-on" ? "Enabling…" : "Enable safe baseline"}
          </button>
          <button type="button" className="text-btn" disabled={busy !== null}
            onClick={() => act("baseline-off", "/api/permissions", { action: "baseline-off", scope: baseScope })}>
            {busy === "baseline-off" ? "Disabling…" : "Disable"}
          </button>
          <span className="settings-dim">
            effective <code>defaultMode</code>: <strong>{perm.effective_default_mode || "ask (unset)"}</strong>
          </span>
        </div>

        <h3 className="settings-sub">Read-only built-in tools</h3>
        <ul className="perm-list">
          {perm.baseline.tools.map((t) => (
            <li key={t.rule}><code>{t.rule}</code><Pill on={t.active} /></li>
          ))}
          <li>
            <code>defaultMode: {perm.baseline.default_mode.value}</code>
            <Pill on={perm.baseline.default_mode.active} />
          </li>
        </ul>

        <h3 className="settings-sub">Safe Bash prefixes (read-only / trivially reversible)</h3>
        <ul className="perm-list perm-list-wide">
          {perm.baseline.bash_prefixes.map((b) => (
            <li key={b.rule}><code>{b.rule}</code><Pill on={b.active} /></li>
          ))}
        </ul>

        <button type="button" className="settings-link" onClick={() => setShowAllow((v) => !v)}>
          {showAllow ? "Hide" : "Show"} the full resolved allow-list ({perm.current_allow.length} rules)
        </button>
        {showAllow ? (
          <ul className="perm-list perm-allow-dump">
            {perm.current_allow.map((r, i) => <li key={`${r}-${i}`}><code>{r}</code></li>)}
          </ul>
        ) : null}
      </div>

      {/* ---------------------------------------------------------------- DANGER ZONE */}
      <div className={`settings-section danger-zone ${armed ? "armed" : ""}`}>
        <h2>⚠ Danger Zone {armed ? <span className="danger-armed-tag">ARMED</span> : null}</h2>
        <p className="danger-warn">
          These auto-allows are <strong>inherently unsafe</strong>. Enabling them lets the agent run{" "}
          <strong>ANY command</strong>, read/write <strong>ANY file</strong>, reach <strong>ANY network
          host</strong>, and use every MCP tool — and <code>{perm.danger.default_mode.value}</code> stops
          Claude from <strong>asking permission for anything</strong>. Only enable on a throwaway or
          sandboxed machine you fully control. There is no undo for what an agent does once this is on.
        </p>

        <h3 className="settings-sub">The full spectrum this enables</h3>
        <ul className="perm-list">
          {perm.danger.spectrum.map((d) => (
            <li key={d.rule}><code>{d.rule}</code><Pill on={d.active} onText="Allowed" offText="—" /></li>
          ))}
          <li>
            <code>defaultMode: {perm.danger.default_mode.value}</code>
            <Pill on={perm.danger.default_mode.active} onText="Active" offText="—" />
          </li>
        </ul>

        {armed ? (
          <div className="settings-actions">
            <button type="button" className="text-btn danger-disarm" disabled={busy !== null}
              onClick={() => act("danger-disarm", "/api/permissions", { action: "danger-disarm", scope: dangerScope })}>
              {busy === "danger-disarm" ? "Disarming…" : "Disarm — restore safe defaults"}
            </button>
          </div>
        ) : (
          <div className="danger-arm-box">
            <label className="settings-dim">Scope:&nbsp;
              <select value={dangerScope} onChange={(e) => setDangerScope(e.target.value as "local" | "shared" | "global")}>
                <option value="local">This project, your machine (settings.local.json)</option>
                <option value="shared">This project (committed settings.json)</option>
                <option value="global">Global (~/.claude — every project)</option>
              </select>
            </label>
            <p className="settings-dim">
              To arm, type <code>{dangerConfirm}</code> exactly:
            </p>
            <input
              className="danger-confirm-input"
              type="text"
              value={confirmText}
              placeholder={dangerConfirm}
              onChange={(e) => setConfirmText(e.target.value)}
            />
            <button
              type="button"
              className="text-btn danger-arm"
              disabled={busy !== null || confirmText !== dangerConfirm}
              onClick={() =>
                act("danger-arm", "/api/permissions",
                  { action: "danger-arm", scope: dangerScope, confirm: confirmText },
                  () => setConfirmText(""))
              }
            >
              {busy === "danger-arm" ? "Arming…" : "Arm the Danger Zone"}
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
