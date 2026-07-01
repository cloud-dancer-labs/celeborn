import { celebornJson } from "@/lib/cli";

/** One live context window — a Claude session whose transcript was touched recently — as reported by
 *  `celeborn agents --json`. `tokens` is the REAL live window (latest transcript `usage`), not a
 *  proxy; `task` is the DOING card that session claimed (null when unattributed). */
export type ActiveAgent = {
  agent: string;        // owner handle ("Claude (Opus 4.8)") or "session abc123" when unattributed
  task: string | null;  // display id of the DOING card it owns (CELE-tN), or null
  task_id: string | null;
  tokens: number;       // live context window in tokens (board renders k)
  session: string;      // short session id
  last_active: string;
  age_min: number;
  owned: boolean;
  project: string;
};

export type ActiveAgents = {
  generated_at: string;
  project: string;
  window_min: number;
  count: number;
  agents: ActiveAgent[];
};

export async function loadAgents(): Promise<ActiveAgents> {
  return celebornJson<ActiveAgents>(["agents", "--json"]);
}
