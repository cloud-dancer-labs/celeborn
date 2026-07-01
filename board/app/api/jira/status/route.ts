import { NextResponse } from "next/server";
import { celebornJson } from "@/lib/cli";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const doc = await celebornJson<Record<string, unknown>>(["jira", "status", "--json"]);
    return NextResponse.json(doc, {
      headers: { "Cache-Control": "no-store" },
      status: doc.connected ? 200 : 200,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ connected: false, reason: "error", error: msg }, { status: 500 });
  }
}