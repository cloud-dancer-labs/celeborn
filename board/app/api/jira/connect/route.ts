import { NextResponse } from "next/server";
import { celebornJson } from "@/lib/cli";

export const dynamic = "force-dynamic";
export const revalidate = 0;

/**
 * Connect Jira from the board UI. Token travels in the POST body only (never in argv — the CLI reads
 * CELEBORN_JIRA_TOKEN). On first connect, the response includes a reconcile preview so the UI can
 * surface stale Jira orphans without importing them (Celeborn stays source of truth).
 */
export async function POST(req: Request) {
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "invalid JSON body" }, { status: 400 });
  }

  const site = String(body.site || "").trim();
  const email = String(body.email || "").trim();
  const project = String(body.project || "").trim().toUpperCase();
  const token = String(body.token || "").trim();

  if (!site || !email || !project || !token) {
    return NextResponse.json(
      { ok: false, error: "site, email, project, and token are all required" },
      { status: 400 },
    );
  }

  try {
    const result = await celebornJson<Record<string, unknown>>(
      ["jira", "connect", "--site", site, "--email", email, "--project", project, "--json"],
      { env: { ...process.env, CELEBORN_JIRA_TOKEN: token }, timeoutMs: 45_000 },
    );
    if (!result.ok) {
      return NextResponse.json(result, { status: 400 });
    }
    return NextResponse.json(result, { headers: { "Cache-Control": "no-store" } });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ ok: false, error: msg }, { status: 500 });
  }
}