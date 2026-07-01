import { NextResponse } from "next/server";
import { celebornJson } from "@/lib/cli";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const report = await celebornJson<Record<string, unknown>>(["jira", "reconcile", "--json"]);
    return NextResponse.json(report, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}

/** Apply Celeborn → Jira (no orphan import). Celeborn state wins on drift. */
export async function POST(req: Request) {
  let apply = true;
  try {
    const body = await req.json();
    apply = body.apply !== false;
  } catch {
    // default apply
  }
  if (!apply) {
    return GET();
  }

  try {
    const report = await celebornJson<Record<string, unknown>>(
      ["jira", "reconcile", "--apply", "--json"],
      { timeoutMs: 120_000 },
    );
    return NextResponse.json(report, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}