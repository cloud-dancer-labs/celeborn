import { celebornJson } from "@/lib/cli";

/** One scope's running savings totals (this project, or the whole fleet). Mirrors the CLI's
 *  `_savings_figures` blocks — the same numbers `celeborn flex` reports, summed since start. */
export type SavingsScope = {
  dollars_saved: number;
  tokens_saved: number;
  restarts_avoided: number;
  sessions_resumed: number;
  compactions_bridged: number;
  load_events: number;
  // Permission prompts the user never had to click: the advisor's generalized allow-list rules plus
  // CMM's per-call pre-clear of structural-query tools (CELE-t92), summed by the CLI's
  // `_prompts_auto_allowed`. The "🔓 prompts auto-allowed" stat reads this.
  prompts_auto_allowed: number;
};

/** The permission-friction ledger the advisor (t70) keeps: rules auto-generalized into wildcards, and
 *  the aggregate bottlenecks (un-widenable literals) that still re-prompt for approval. */
export type AdvisorFigures = {
  permission_rules_generalized: number;
  skipped_bottlenecks_total: number;
};

export type Savings = {
  generated_at: string;
  project: SavingsScope & { project: string; usd_per_mtok: number; advisor?: AdvisorFigures };
  fleet: SavingsScope & { projects: number; advisor?: AdvisorFigures };
};

export async function loadSavings(): Promise<Savings> {
  return celebornJson<Savings>(["savings", "--json"]);
}
