import { NextResponse } from "next/server";
import { loadRun } from "@/lib/run";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const run = await loadRun();
    return NextResponse.json(run, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "run load failed";
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
