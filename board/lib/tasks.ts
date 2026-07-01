import { promises as fs } from "node:fs";
import path from "node:path";
import type { TaskState } from "@celeborn/board-ui";

// `TaskState` is now sourced from the shared package (CELE-t98 / t112). Re-exported so the board's
// existing `import { TaskState } from "@/lib/tasks"` sites keep working unchanged.
export type { TaskState };

export interface Task {
  id: string;
  /** Presentation id (project-qualified `SLUG-tN` when the project opts in, else bare `tN`). The
   *  canonical key for mutations stays `id`; show this. May be absent on older tasks.json. */
  display_id?: string;
  title: string;
  state: TaskState;
  owner: string;
  tags: string[];
  blocked_by: string[];
  phase: string;
  /** Logical Stop condition — the clean `/clear` point for this card (CELE-t81). May be empty on
   *  legacy tasks.json that predate the field. */
  stop?: string;
  /** Percent complete 0-100 — drives the In-Progress card sand-fill bar (CELE-t106). Absent/0 on
   *  legacy tasks.json. */
  progress?: number;
  /** Subtask checklist (CELE-t106). Checking items auto-derives `progress`. Absent on legacy cards. */
  subtasks?: { text: string; weight: number; done: boolean }[];
  created: string;
  updated: string;
  notes: string;
  /** Agent-only protocol block (from tasks.json). Hidden in the board UI; included in copy prompts. */
  agent_protocol?: string;
  /** Owner's agent family (e.g. "Claude"), joined from the local agent registry. May be empty. */
  owner_family?: string;
  /** Owner's specific model (e.g. "Opus 4.8"), joined from the local agent registry. May be empty. */
  owner_model?: string;
}

export interface TaskBoard {
  generated_at: string;
  /** Per-repo slug for project-qualified card markers (⟨celeborn:slug/tN⟩). */
  project_slug?: string;
  /** True when this project opts into project-qualified card ids (render Task.display_id). */
  qualified_task_ids?: boolean;
  /** Upper-cased slug prefix used in qualified ids (SLUG-tN); empty unless qualified_task_ids. */
  id_prefix?: string;
  /** Human project title shown in the board header so you can tell repos apart. */
  project_name?: string;
  states: TaskState[];
  tasks: Task[];
  /** Absolute path the data was read from (for the empty-state hint). */
  source: string;
  /** True when tasks.json was missing — render the "run celeborn tasks" hint. */
  missing: boolean;
}

const DEFAULT_STATES: TaskState[] = ["todo", "doing", "done"];

/**
 * Resolve the derived tasks.json the CLI writes. Override with CELEBORN_TASKS_JSON
 * (absolute path) to point the board at any repo's `.context/`. By default the board
 * assumes it lives at `<repo>/board/`, so the data is one level up in `../.context/`.
 */
export function tasksJsonPath(): string {
  const override = process.env.CELEBORN_TASKS_JSON;
  if (override) return path.resolve(override);
  return path.resolve(process.cwd(), "..", ".context", "tasks.json");
}

export async function loadBoard(): Promise<TaskBoard> {
  const source = tasksJsonPath();
  try {
    const raw = await fs.readFile(source, "utf8");
    const doc = JSON.parse(raw);
    return {
      generated_at: doc.generated_at ?? "",
      project_slug: typeof doc.project_slug === "string" ? doc.project_slug : "",
      project_name: typeof doc.project_name === "string" ? doc.project_name : "",
      states: Array.isArray(doc.states) && doc.states.length ? doc.states : DEFAULT_STATES,
      tasks: Array.isArray(doc.tasks) ? doc.tasks : [],
      source,
      missing: false,
    };
  } catch {
    return { generated_at: "", states: DEFAULT_STATES, tasks: [], source, missing: true };
  }
}
