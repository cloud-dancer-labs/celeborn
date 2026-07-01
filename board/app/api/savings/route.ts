import { NextResponse } from "next/server";
import { loadSavings } from "@/lib/savings";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const savings = await loadSavings();
    return NextResponse.json(savings, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch (e) {
    const msg = e instanceof Error ? e.message : "savings load failed";
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
