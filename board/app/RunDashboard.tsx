"use client";

import { useCallback, useEffect, useState } from "react";
import type { RunSnapshot, RunWorker } from "@/lib/run";

const STATUS_GLYPH: Record<string, string> = {
  working: "●",
  lagging: "◐",
  stuck: "✗",
  done: "✓",
  failed: "✗",
};

// Show the most-actionable workers first: stuck/failed, then live, then done.
const STATUS_ORDER: Record<string, number> = {
  stuck: 0,
  failed: 1,
  working: 2,
  lagging: 3,
  done: 4,
};

function fmtSecs(secs: number | null | undefined): string {
  if (secs == null) return "—";
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return `${m}m${String(s).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, "0")}m`;
}

function relTime(iso: string): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.round(mins / 60)}h ago`;
}

function WorkerRow({ w }: { w: RunWorker }) {
  const pct = w.total ? Math.min(100, Math.round((w.done / w.total) * 100)) : 0;
  return (
    <li className="run-worker" data-status={w.status}>
      <span className="run-glyph">{STATUS_GLYPH[w.status] ?? "·"}</span>
      <span className="run-wid">{w.id}</span>
      <span className="run-bar" aria-label={`${w.done} of ${w.total}`}>
        <span className="run-bar-fill" style={{ width: `${pct}%` }} data-status={w.status} />
        <span className="run-bar-label">
          {w.total ? `${w.done}/${w.total}` : `${w.done}`}
        </span>
      </span>
      <span className="run-yield">
        <span className="run-found">{w.found}✓</span>
        {w.missed ? <span className="run-missed">{w.missed}✗</span> : null}
      </span>
      <span className="run-elapsed">{fmtSecs(w.elapsed_s)}</span>
      <span className="run-rate">{w.rate_per_min ? `${w.rate_per_min}/m` : ""}</span>
      <span className="run-item">
        {w.current_item}
        {w.last_error ? <span className="run-err"> ⚠ {w.last_error}</span> : null}
      </span>
      {(w.status === "working" || w.status === "lagging" || w.status === "stuck") &&
      w.beat_age_s != null ? (
        <span className="run-beat" data-stale={w.status === "stuck" || undefined}>
          {w.beat_age_s}s
        </span>
      ) : (
        <span className="run-beat" />
      )}
    </li>
  );
}

export default function RunDashboard() {
  const [run, setRun] = useState<RunSnapshot | null>(null);
  const [error, setError] = useState("");
  const [mounted, setMounted] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/run", { cache: "no-store" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { error?: string }).error || res.statusText);
      }
      setRun((await res.json()) as RunSnapshot);
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "run refresh failed");
    }
  }, []);

  useEffect(() => {
    setMounted(true);
    refresh();
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [refresh]);

  const bs = run?.by_status ?? {};
  const workers = [...(run?.workers ?? [])].sort(
    (a, b) =>
      (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9) ||
      a.id.localeCompare(b.id),
  );
  const units = run?.totals?.units;

  return (
    <div className="run-page">
      <div className="run-summary">
        <span className="run-stat" data-status="working">
          <strong>{bs.working ?? 0}</strong> working
        </span>
        <span className="run-stat" data-status="lagging">
          <strong>{bs.lagging ?? 0}</strong> lagging
        </span>
        <span className="run-stat" data-status="stuck">
          <strong>{(bs.stuck ?? 0) + (bs.failed ?? 0)}</strong> stuck/failed
        </span>
        <span className="run-stat" data-status="done">
          <strong>{bs.done ?? 0}</strong> done
        </span>
        <span className="run-stat run-stat-dim">
          {run?.workers_finished ?? 0}/{run?.workers_total ?? 0} workers
        </span>
        {run?.wall_clock_s != null ? (
          <span className="run-stat run-stat-dim">
            wall {fmtSecs(run.wall_clock_s)}
            {run.parallel_efficiency ? ` · ${run.parallel_efficiency}× parallel` : ""}
          </span>
        ) : null}
        {mounted && run?.generated_at ? (
          <span className="run-updated">· updated {relTime(run.generated_at)}</span>
        ) : null}
      </div>

      {run?.run_id ? (
        <div className="run-head">
          <h2 className="run-id">{run.run_id}</h2>
          {run.goal ? <p className="run-goal">{run.goal}</p> : null}
          <p className="run-resolved">
            {run.resolved.done} processed · {run.resolved.found} found /{" "}
            {run.resolved.missed} missed
            {units ? ` (of ${units})` : ""}
          </p>
          {Object.keys(run.sources ?? {}).length ? (
            <ul className="run-sources">
              {Object.entries(run.sources).map(([name, v]) => (
                <li key={name} className="run-source" data-rl={v.ratelimited ? true : undefined}>
                  <span className="run-source-name">{name}</span>
                  <span className="run-source-ok">{v.ok}✓</span>
                  {v.fail ? <span className="run-source-fail">{v.fail}✗</span> : null}
                  {v.ratelimited ? <span className="run-source-rl">rl{v.ratelimited}</span> : null}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}

      {error ? <p className="run-error">{error}</p> : null}

      {!run?.run_id && !error ? (
        <div className="run-empty">
          <p>No active run.</p>
          <p className="run-hint">
            Start one: <code>celeborn run start &lt;id&gt; --goal &quot;…&quot; --shards N --units M</code>
          </p>
        </div>
      ) : null}

      <div className="run-body">
        <ul className="run-workers">
          {workers.map((w) => (
            <WorkerRow key={w.id} w={w} />
          ))}
        </ul>

        <aside className="run-blackboard">
          <h3>📌 Blackboard</h3>
          <p className="run-bb-sub">What the elves have learned from each other</p>
          {run?.blackboard?.length ? (
            <ul>
              {[...run.blackboard].reverse().map((b, i) => (
                <li key={i}>
                  <span className="run-bb-lesson">{b.lesson}</span>
                  {b.worker ? <span className="run-bb-who">@{b.worker}</span> : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="run-quiet">No shared learnings yet.</p>
          )}
        </aside>
      </div>
    </div>
  );
}
