import { loadBoard } from "@/lib/tasks";
import Board from "./Board";

// Read fresh on every request; the client component then polls for live updates.
export const dynamic = "force-dynamic";

export default async function Page({
  searchParams,
}: {
  searchParams: Promise<{ view?: string }>;
}) {
  const board = await loadBoard();
  // Lets other routes (e.g. Settings) deep-link back to a specific view: "/?view=run".
  const { view } = await searchParams;
  return <Board initialTasks={board} initialView={view === "run" ? "run" : "tasks"} />;
}
