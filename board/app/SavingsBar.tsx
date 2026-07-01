"use client";

import { useCallback, useState } from "react";
import { useJsonPoll } from "@/lib/poll";
import type { Savings } from "@/lib/savings";

// The economy bar (t68): one line under the control buttons that surfaces the savings Celeborn would
// otherwise push as `flex` updates — this project's running totals since start, and the aggregate
// across every registered Celeborn project. Each stat leads with an emoji for what was saved:
//   💰 dollars (tokens→$) · 🧠 context tokens never re-loaded · ♻️ restarts (sessions+compactions) bridged.
// Polled, not server-rendered, so the figures stay live without a reload and never trip hydration.

const POLL_MS = 30_000;

/** 15,258,913 → "15.3M", 4_200 → "4.2K". Keeps the line short while staying honest about scale. */
function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${n}`;
}

/** Whole dollars once it's meaningful, cents while small — mirrors the CLI's `_fmt_usd`. */
function usd(n: number): string {
  return n >= 100 ? `$${Math.round(n).toLocaleString()}` : `$${n.toFixed(2)}`;
}

function Stat({ emoji, value, label }: { emoji: string; value: string; label?: string }) {
  return (
    <span className="savings-stat">
      <span className="savings-emoji" aria-hidden="true">
        {emoji}
      </span>
      <span className="savings-value">{value}</span>
      {label ? <span className="savings-label">{label}</span> : null}
    </span>
  );
}

export default function SavingsBar() {
  const [data, setData] = useState<Savings | null>(null);
  const onData = useCallback((next: Savings) => setData(next), []);
  useJsonPoll<Savings>("/api/savings", POLL_MS, false, onData);

  if (!data) return null;
  const { project, fleet } = data;
  const adv = project.advisor;
  // "Prompts auto-allowed" = every permission interruption avoided: advisor allow-list rules
  // generalized + CMM's per-call pre-clear of structural-query tools (CELE-t92) + the settings.json
  // allow-list (the t100 safe baseline + the user's own rules) matched per tool call, summed by the CLI.
  const autoAllowed = project.prompts_auto_allowed ?? 0;
  const stillManual = adv?.skipped_bottlenecks_total ?? 0;

  return (
    <section className="savings-bar" aria-label="Celeborn savings — this project and across the fleet">
      <div className="savings-group">
        <span className="savings-scope" title="Saved on this project since Celeborn started tracking">
          {project.project}
        </span>
        <Stat emoji="💰" value={usd(project.dollars_saved)} label="saved" />
        <Stat emoji="🧠" value={compact(project.tokens_saved)} label="tokens kept warm" />
        <Stat emoji="♻️" value={`${project.restarts_avoided}`} label="restarts avoided" />
        {autoAllowed > 0 ? (
          <Stat emoji="🔓" value={`${autoAllowed}`} label="prompts auto-allowed" />
        ) : null}
        {stillManual > 0 ? (
          <Stat emoji="⚠️" value={`${stillManual}`} label="prompts still manual" />
        ) : null}
      </div>
      <span className="savings-divider" aria-hidden="true" />
      <div className="savings-group">
        <span className="savings-scope" title="Aggregate across every registered Celeborn project">
          🌐 {fleet.projects} project{fleet.projects === 1 ? "" : "s"}
        </span>
        <Stat emoji="💰" value={usd(fleet.dollars_saved)} />
        <Stat emoji="🧠" value={compact(fleet.tokens_saved)} />
        <Stat emoji="♻️" value={`${fleet.restarts_avoided}`} />
        {(fleet.prompts_auto_allowed ?? 0) > 0 ? (
          <Stat emoji="🔓" value={`${fleet.prompts_auto_allowed}`} />
        ) : null}
      </div>
    </section>
  );
}
