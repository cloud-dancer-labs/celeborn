// Shared board-ui types. Phase 0 (CELE-t112) deliberately starts with ONLY the types that are
// byte-identical across both apps today, to keep the pipe-proof low-risk. `TaskState` is exactly
// that — both board/lib/tasks.ts and web/lib/tasks.ts declared the identical string-literal union.
//
// The richer per-app shapes (`Task`, `TaskBoard`, `ProjectSavings`) still DIVERGE in optionality
// (board: `created: string`, `stop?` optional, has `agent_protocol`; web: `created: string | null`,
// `stop`/`progress`/`subtasks` required, no `agent_protocol`). Reconciling them into one canonical
// shape is deferred to Phase 3, where merging Card/Column forces a single shape anyway — moving them
// now would drag Phase-3 work into Phase 0 and balloon the blast radius (against the plan's
// smallest-blast-radius-first ordering). See plan/cele-t98-shared-board-ui.md §1/§8.

/** The three kanban columns. Identical across both boards. The "blocked" state was retired
 *  (CELE-t135) — kanban discipline discourages a Blocked column, so DOING reclaims its width.
 *  Dependency tracking lives on independently in each card's `blocked_by` list. */
export type TaskState = "todo" | "doing" | "done";
