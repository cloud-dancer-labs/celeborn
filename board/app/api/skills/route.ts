import { NextResponse } from "next/server";
import { celeborn, celebornJson } from "@/lib/cli";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export async function GET() {
  try {
    const state = await celebornJson(["skills", "--json"]);
    return NextResponse.json(state, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `celeborn skills --json failed: ${msg}` }, { status: 500 });
  }
}

/**
 * install-mattpocock → shell out to `celeborn skills install-mattpocock` (which runs the community
 * `skills` CLI via npx — needs Node + network). Generous timeout since npx may download. Returns the
 * freshly-read skills state so the page reflects the new install state.
 */
export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const action = String(body.action || "");
  const scope = String(body.scope || "local");

  try {
    if (action === "install-mattpocock" || action === "update-mattpocock") {
      // install and update are the same op (re-pull @latest); `update` just messages differently + stamps.
      const verb = action === "update-mattpocock" ? "update" : "install-mattpocock";
      const args = ["skills", verb];
      if (scope === "global") args.push("--global");
      await celeborn(args, { timeoutMs: 300_000 });
    } else {
      return NextResponse.json({ error: `unknown action: ${action}` }, { status: 400 });
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `celeborn CLI failed: ${msg}` }, { status: 500 });
  }

  const state = await celebornJson(["skills", "--json"]);
  return NextResponse.json(state, { headers: { "Cache-Control": "no-store" } });
}
