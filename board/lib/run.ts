import { celebornJson } from "@/lib/cli";

export type RunWorker = {
  id: string;
  shard: string;
  phase: string;
  status: "working" | "lagging" | "stuck" | "done" | "failed";
  current_item: string;
  done: number;
  total: number;
  found: number;
  missed: number;
  elapsed_s: number | null;
  rate_per_min: number | null;
  beat_age_s: number | null;
  last_error: string;
  sources: Record<string, { ok?: number; fail?: number; ratelimited?: number }>;
};

export type RunSnapshot = {
  schema: string;
  generated_at: string;
  run_id: string;
  goal: string;
  started_at: string;
  updated_at: string;
  totals: { shards?: number; units?: number };
  wall_clock_s: number | null;
  sum_worker_s: number;
  parallel_efficiency: number | null;
  workers_total: number;
  workers_finished: number;
  by_status: Record<string, number>;
  resolved: { done: number; found: number; missed: number };
  sources: Record<string, { ok: number; fail: number; ratelimited: number }>;
  workers: RunWorker[];
  blackboard: { at: string; worker: string; lesson: string }[];
};

export async function loadRun(): Promise<RunSnapshot> {
  const snap = await celebornJson<RunSnapshot>(["run", "status", "--json"]);
  // The CLI stamps `updated_at`; the poller dedupes on `generated_at`.
  return { ...snap, generated_at: snap.generated_at || snap.updated_at };
}
