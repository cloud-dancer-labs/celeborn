import type { Task, TaskState } from "@/lib/tasks";

/** Column display order — list order from tasks.json (top = highest priority / newest).
 *  Done also falls back to updated desc so legacy boards stay correct. */
export function sortByColumnRules(tasks: Task[], state: TaskState): Task[] {
  const inCol = tasks.filter((t) => t.state === state);
  if (state !== "done") return inCol;
  return [...inCol].sort((a, b) => {
    const au = Date.parse(a.updated) || 0;
    const bu = Date.parse(b.updated) || 0;
    return bu - au;
  });
}