import { NextResponse } from "next/server";
import { celeborn, celebornJson } from "@/lib/cli";

// Read fresh every time — the page reflects the live ~/.claude + project settings without a rebuild.
export const dynamic = "force-dynamic";
export const revalidate = 0;

// The exact phrase the Danger Zone POST must carry before this route will arm the full unsafe spectrum.
// Mirrors DANGER_CONFIRM_PHRASE in scripts/celeborn.py.
const DANGER_CONFIRM = "DISABLE ALL SAFETY";

/** Map a UI scope to the CLI's scope flag. "local" → project settings.local.json (no flag). */
function scopeFlags(scope: string): string[] {
  if (scope === "global") return ["--global"];
  if (scope === "shared") return ["--shared"];
  return [];
}

export async function GET() {
  try {
    const state = await celebornJson(["permissions", "--json"]);
    return NextResponse.json(state, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `celeborn permissions --json failed: ${msg}` }, { status: 500 });
  }
}

/**
 * Mutations. Every write rides the audited CLI (which backs up the file + refuses invalid JSON):
 *   baseline-on   → apply the safe t100 baseline           {scope: "global"|"shared"}
 *   baseline-off  → remove it
 *   danger-arm    → enable the FULL unsafe spectrum         {scope, confirm:"DISABLE ALL SAFETY"}
 *   danger-disarm → remove it + restore safe defaults
 * Returns the freshly-read state so the page updates immediately.
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
  const flags = scopeFlags(scope);

  try {
    if (action === "baseline-on") {
      await celeborn(["permissions", "--baseline", ...flags]);
    } else if (action === "baseline-off") {
      await celeborn(["permissions", "--baseline", "--remove", ...flags]);
    } else if (action === "danger-arm") {
      if (String(body.confirm || "") !== DANGER_CONFIRM) {
        return NextResponse.json(
          { error: `Danger Zone requires the exact confirmation phrase "${DANGER_CONFIRM}".` },
          { status: 400 },
        );
      }
      // The CLI demands --yes to arm; the route supplies it ONLY after the phrase matches above.
      await celeborn(["permissions", "--danger-zone", "--yes", ...flags]);
    } else if (action === "danger-disarm") {
      await celeborn(["permissions", "--danger-zone", "--disarm", ...flags]);
    } else {
      return NextResponse.json({ error: `unknown action: ${action}` }, { status: 400 });
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `celeborn CLI failed: ${msg}` }, { status: 500 });
  }

  const state = await celebornJson(["permissions", "--json"]);
  return NextResponse.json(state, { headers: { "Cache-Control": "no-store" } });
}
