import { NextResponse } from "next/server";
import { loadAgents } from "@/lib/agents";

// Always read fresh: active-agents is a live snapshot of which sessions are working right now.
export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const agents = await loadAgents();
    return NextResponse.json(agents, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "agents load failed";
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
