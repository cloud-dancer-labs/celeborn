import { NextResponse } from "next/server";
import { loadBoard } from "@/lib/tasks";
import { celeborn } from "@/lib/cli";

// Always read fresh from disk so the board reflects `celeborn tasks` edits without a rebuild.
export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  const board = await loadBoard();
  return NextResponse.json(board, {
    headers: { "Cache-Control": "no-store" },
  });
}

/**
 * Mutations from the board UI. Each action shells out to the Celeborn CLI (tasks.md stays the single
 * source of truth), then returns the freshly-loaded board so the client updates without waiting for
 * the next poll.
 *   add      → create a To Do card
 *   reorder  → reprioritize within a column (up | down | top | bottom)
 *   move     → move a card to another column (state)
 *   handoff  → queue the card as a prompt for the live session (drained by the hook each turn)
 *   edit     → update a card's title and/or notes
 *   delete   → permanently remove the card from tasks.md
 */
export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const action = String(body.action || "");

  try {
    if (action === "add") {
      const title = String(body.title || "").trim();
      if (!title) return NextResponse.json({ error: "title required" }, { status: 400 });
      const args = ["tasks", "add", title];
      const note = String(body.note || "").trim();
      if (note) args.push("--note", note);
      await celeborn(args);
    } else if (action === "reorder") {
      const id = String(body.id || "");
      const dir = String(body.dir || "");
      if (!id || !["up", "down", "top", "bottom"].includes(dir)) {
        return NextResponse.json({ error: "id and dir (up|down|top|bottom) required" }, { status: 400 });
      }
      await celeborn(["tasks", "reorder", id, dir]);
    } else if (action === "move") {
      const id = String(body.id || "");
      const state = String(body.state || "");
      if (!id || !["todo", "doing", "done"].includes(state)) {
        return NextResponse.json({ error: "id and a valid state required" }, { status: 400 });
      }
      await celeborn(["tasks", "move", id, state]);
    } else if (action === "handoff") {
      const id = String(body.id || "");
      if (!id) return NextResponse.json({ error: "id required" }, { status: 400 });
      await celeborn(["outbox", "push", "--task", id]);
    } else if (action === "edit") {
      const id = String(body.id || "");
      if (!id) return NextResponse.json({ error: "id required" }, { status: 400 });
      const hasTitle = body.title !== undefined;
      const hasNote = body.note !== undefined;
      if (!hasTitle && !hasNote) {
        return NextResponse.json({ error: "title or note required" }, { status: 400 });
      }
      const args = ["tasks", "edit", id];
      if (hasTitle) {
        const title = String(body.title).trim();
        if (!title) return NextResponse.json({ error: "title required" }, { status: 400 });
        args.push("--title", title);
      }
      if (hasNote) args.push("--note", String(body.note));
      await celeborn(args);
    } else if (action === "delete") {
      const id = String(body.id || "");
      if (!id) return NextResponse.json({ error: "id required" }, { status: 400 });
      await celeborn(["tasks", "rm", id]);
    } else {
      return NextResponse.json({ error: `unknown action: ${action}` }, { status: 400 });
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `celeborn CLI failed: ${msg}` }, { status: 500 });
  }

  const board = await loadBoard();
  return NextResponse.json(board, { headers: { "Cache-Control": "no-store" } });
}
