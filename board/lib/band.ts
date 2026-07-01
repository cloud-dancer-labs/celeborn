/** The /clear-nudge band for a live context window, by size in k tokens. Single source of truth for
 *  both the active-agents chips and the per-card band pill (CELE-t131): fresh → mid → clear soon →
 *  clear now → clear urgent, mapped onto the board palette plus amber/yellow mid-steps.
 *
 *  Pure (no imports) on purpose: client components import it, so it must not drag in any server-only
 *  module (e.g. lib/agents → lib/cli → node:child_process, which breaks the client bundle). */
export function band(k: number): { color: string; word: string } {
  if (k < 50) return { color: "#22c55e", word: "fresh" };
  if (k < 75) return { color: "#3b82f6", word: "mid" };
  if (k < 100) return { color: "#f59e0b", word: "clear soon" };
  if (k < 125) return { color: "#facc15", word: "clear now" };
  return { color: "#ef4444", word: "clear urgent" };
}
