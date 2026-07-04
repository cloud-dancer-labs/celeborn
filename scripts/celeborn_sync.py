"""celeborn_sync — account + premium Supabase-backed sync for `.context/` (Phase 8b).

Stdlib-only (urllib). Identity is **Supabase Auth (GoTrue)**: email+password, TOTP MFA (Google
Authenticator), and GitHub OAuth (PKCE). The free account is OPTIONAL — the local core never needs it.
Push/pull `.context/*.md` to a Supabase project over PostgREST; hosted sync is gated by an active
Celeborn entitlement (Stripe) enforced server-side by RLS, exactly as before — only the identity path
changed (GoTrue mints the JWT; no more GitHub device flow + hand-signed token).

Boundary recap (see references/sync-design.md, references/supabase-auth-setup.md, freemium-billing.md):
  • The local SQLite index is NEVER synced — only markdown rows move.
  • Secrets are redacted out of every uploaded copy; the local file is left intact.
  • A free/logged-in user with no active entitlement gets ZERO synced rows (RLS), refused with a hint.

This module is imported lazily by `celeborn.py` so the free, local core stays dependency- and
network-free. It reuses celeborn's helpers (config, secret patterns, ok/warn/die)."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

import celeborn as cb  # already fully loaded by the time login/sync run

# --- Placeholders for the hosted instance. Override via env or .celebornrc "sync". --------------
DEFAULT_SUPABASE_URL = "https://REPLACE-with-your-project.supabase.co"
DEFAULT_ANON_KEY = "REPLACE-with-your-anon-key"
DEFAULT_GH_CLIENT_ID = "REPLACE-with-your-github-oauth-client-id"
# Public pricing/checkout page — the fallback when the server doesn't return a checkout URL.
# Override via env or .celebornrc; the 403 payload's `checkout_url`/`upgrade_url` takes precedence.
UPGRADE_URL = "https://celeborn.dev/pricing"

SYNCABLE_SUFFIXES = (".md", ".json")
# The board (`tasks.md` + its derived `tasks.json`) is synced through the structured `tasks` TABLE
# channel (build_task_rows/_push_tasks ⇄ reconcile_tasks/_pull_tasks), NOT the raw-file channel. If
# the raw-file channel also carried them, the two would fight: a file-pull of a stale tasks.md would
# clobber a table-level reconcile (web edit), so every sync would thrash. Keep the table authoritative.
TASK_TABLE_FILES = {cb.TASKS_FILE, cb.TASKS_JSON}
# The architecture diagram (CELE-t187) syncs through the structured `project_architecture` TABLE
# channel (build_architecture_row/_push_architecture), with its `credentials` block STRIPPED. It must
# NEVER go through the raw-file channel — that would upload the file verbatim, credentials and all.
INFRA_LOCAL_NAME = "infra-local.json"
# never sync these as raw files even though they're .json/.md: derived, local, rc, table-owned, or
# credential-bearing (.alerts.json is transient blocked-progress state that rides the tasks
# projection, CELE-t169; infra-local.json is credential-bearing and table-owned, CELE-t187)
SYNC_SKIP_NAMES = {cb.INDEX_NAME, cb.RC_NAME, cb.ALERTS_NAME, INFRA_LOCAL_NAME,
                   cb.ARCH_TRACE_STATE_NAME} | TASK_TABLE_FILES


# --------------------------------------------------------------------------- config + credentials

def sync_config(ctx: Path) -> dict:
    """Resolve the remote: env > .celebornrc "sync" > hosted defaults. Not secret (anon key is
    public; RLS protects the data)."""
    rc = cb.load_config(ctx).get("sync", {}) if ctx else {}
    return {
        "url": (os.environ.get("CELEBORN_SUPABASE_URL") or rc.get("url") or DEFAULT_SUPABASE_URL).rstrip("/"),
        "anon": os.environ.get("CELEBORN_SUPABASE_ANON_KEY") or rc.get("anon_key") or DEFAULT_ANON_KEY,
        "gh_client_id": os.environ.get("CELEBORN_GITHUB_CLIENT_ID") or rc.get("github_client_id") or DEFAULT_GH_CLIENT_ID,
        "project_id": rc.get("project_id"),
        "project_name": rc.get("project_name"),
        "github_repo": rc.get("github_repo"),          # set by `celeborn github link`
        "ingested_cursor": rc.get("ingested_cursor"),  # per-device high-water for ingested pull
    }


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "celeborn"


def _creds_path() -> Path:
    return _config_dir() / "credentials.json"


def load_creds() -> dict:
    p = _creds_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_creds(creds: dict) -> None:
    """Write credentials with 0600 perms — these ARE secrets (GitHub token + session JWT),
    deliberately stored OUTSIDE any repo or .context/."""
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = _creds_path()
    p.write_text(json.dumps(creds, indent=2) + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _set_rc_value(ctx: Path, key: str, value) -> None:
    """Persist a non-secret value under the "sync" object in .celebornrc (e.g. project_id)."""
    rc = ctx / cb.RC_NAME
    data = {}
    if rc.is_file():
        try:
            data = json.loads(rc.read_text())
        except json.JSONDecodeError:
            data = {}
    data.setdefault("sync", {})[key] = value
    rc.write_text(json.dumps(data, indent=2) + "\n")


# --------------------------------------------------------------------------- secret redaction

_LABELS = [
    (re.compile(r"^ghp_|^github_pat_"), "github_pat"),
    (re.compile(r"^xox[baprs]-"), "slack_token"),
    (re.compile(r"^AKIA"), "aws_key"),
    (re.compile(r"^AIza"), "google_key"),
    (re.compile(r"^xai-"), "xai_key"),
    (re.compile(r"^sbp_"), "supabase_token"),
    (re.compile(r"^sk-"), "api_key"),
    (re.compile(r"PRIVATE KEY"), "private_key"),
]


def _label_for(match: str) -> str:
    for rx, label in _LABELS:
        if rx.search(match):
            return label
    return "secret"


def redact(text: str, patterns: list[str]) -> tuple[str, list[str]]:
    """Replace every secret match with `[REDACTED:<type>]`. Returns (redacted_text, types_found).
    This runs on the OUTBOUND copy only — the caller never writes it back to disk."""
    found: list[str] = []
    out = text
    for pat in patterns:
        try:
            rx = re.compile(pat)
        except re.error:
            continue

        def _sub(m, _found=found):
            _found.append(_label_for(m.group(0)))
            return f"[REDACTED:{_label_for(m.group(0))}]"

        out = rx.sub(_sub, out)
    return out, found


# --------------------------------------------------------------------------- file <-> row mapping

def _iter_syncable(ctx: Path):
    for path in sorted(ctx.rglob("*")):
        if not path.is_file() or path.name in SYNC_SKIP_NAMES:
            continue
        if path.suffix not in SYNCABLE_SUFFIXES:
            continue
        yield path


def _json_intact(name: str, text: str) -> bool:
    """Guard: a `.json` payload must parse. Non-JSON files always pass. Stops a corrupt
    session.json from being pushed up or pulled down and clobbering a good local copy."""
    if not name.endswith(".json"):
        return True
    try:
        json.loads(text)
        return True
    except json.JSONDecodeError:
        return False


def build_push_rows(ctx: Path, project_id: str, patterns: list[str]) -> tuple[list[dict], int]:
    """Build PostgREST upsert rows from local markdown, REDACTED. Pure + testable.
    Returns (rows, total_redactions)."""
    rows: list[dict] = []
    redactions = 0
    for path in _iter_syncable(ctx):
        raw = path.read_text(errors="ignore")
        if not _json_intact(path.name, raw):
            cb.warn(f"skipping push of {path.name}: invalid JSON (fix with `celeborn checkpoint`)")
            continue
        clean, hits = redact(raw, patterns)
        redactions += len(hits)
        rows.append({
            "project_id": project_id,
            "path": str(path.relative_to(ctx)),
            "content": clean,
            "version": str(int(path.stat().st_mtime_ns)),
            "updated_at": cb.now_iso(),
        })
    return rows, redactions


def build_task_rows(ctx: Path, project_id: str, patterns: list[str]) -> list[dict]:
    """Build upsert rows for the hosted `tasks` table (0006, data-model 4b) — the synced projection
    of `.context/tasks.md`. One row per local task, keyed on (project_id, task_id). Pure + testable.

    Reuses the CLI's own enriched JSON projection (`_tasks_doc`) so display_id / owner family+model
    match the local board exactly. Free-text fields (title/notes/stop) are REDACTED with the same
    secret patterns as the file push — a secret pasted into a card title never leaves the machine.
    Returns [] when there is no tasks.md (callers skip the push + prune, so an empty/missing board
    never wipes the hosted table)."""
    if not cb._tasks_path(ctx).is_file():
        return []
    tasks = cb._load_tasks(ctx)
    doc = cb._tasks_doc(ctx, tasks)

    def _clean(text: str) -> str:
        cleaned, _ = redact(text or "", patterns)
        return cleaned

    rows: list[dict] = []
    for t in doc.get("tasks", []):
        rows.append({
            "project_id": project_id,
            "task_id": t["id"],
            "title": _clean(t.get("title", "")),
            "state": t.get("state") or "todo",
            "owner": t.get("owner") or "",
            "tags": t.get("tags") or [],
            "blocked_by": t.get("blocked_by") or [],
            "phase": t.get("phase") or "",
            "stop": _clean(t.get("stop", "")),
            "progress": max(0, min(100, int(t.get("progress") or 0))),  # CELE-t106 sand-fill bar
            "subtasks": [  # checklist (CELE-t106); subtask text is redacted like every other free-text field
                {"text": _clean(s.get("text", "")), "weight": max(1, int(s.get("weight", 1))), "done": bool(s.get("done"))}
                for s in (t.get("subtasks") or [])
            ],
            "notes": _clean(t.get("notes", "")),
            "display_id": t.get("display_id") or t["id"],
            "owner_family": t.get("owner_family") or "",
            "owner_model": t.get("owner_model") or "",
            # Live blocked-progress alert (CELE-t169) — the hosted board renders the same badge. Message
            # is redacted like every other free-text field. Null columns when the card isn't blocked.
            "alert_kind": (t.get("alert") or {}).get("kind") or None,
            "alert_message": _clean((t.get("alert") or {}).get("message", "")) or None,
            "alert_at": (t.get("alert") or {}).get("at") or None,
            "created": t.get("created") or None,
            "updated": t.get("updated") or None,
        })
    return rows


# --------------------------------------------------------------------------- Task reconcile (pure)

# Free-text card fields that build_task_rows redacts on push. The pull path must NOT let a value
# that still carries a [REDACTED:…] marker overwrite the real local text (see reconcile_tasks).
_RECONCILE_FREE_TEXT = ("title", "notes", "stop")
_REDACTION_RE = re.compile(r"\[REDACTED:[^\]]+\]")


def _norm_updated(value) -> str:
    """Normalize an `updated`/`created` value to a directly-comparable wall-clock key
    `YYYY-MM-DDTHH:MM:SS`. The hosted column is `timestamptz`, so PostgREST returns e.g.
    '2026-06-16T03:00:57+00:00' (or with fractional seconds), while tasks.md stores the tz-naive
    'YYYY-MM-DDTHH:MM:SS' that `now_iso()` writes. We compare (and store back) the wall-clock prefix
    ONLY, so a clean push→pull round-trip stays a tie (local wins, no phantom conflict). Web writes
    use the same local wall-clock convention (see web/lib/tasks.ts `nowStamp`), so a genuine web edit
    sorts strictly newer. Empty/garbage → '' (sorts oldest, i.e. loses). Comparing tz-naive local time
    across machines in different timezones is the known residual limitation of `now_iso()` (pre-existing;
    not introduced here)."""
    return str(value or "").strip().replace(" ", "T")[:19]


def _row_to_task(row: dict) -> dict:
    """Inverse of `build_task_rows`: a hosted `tasks` table row → a local tasks.md task dict.
    Drops the board-only enrichment (`display_id` / `owner_family` / `owner_model`) that `_tasks_doc`
    adds and tasks.md never stores, normalizes nulls to the empty shapes `_parse_tasks` produces, and
    normalizes the timestamptz `created`/`updated` back to the tz-naive form tasks.md uses."""
    return {
        "id": row.get("task_id") or row.get("id") or "",
        "title": row.get("title") or "",
        "state": (row.get("state") or "todo").lower(),
        "owner": row.get("owner") or "",
        "tags": list(row.get("tags") or []),
        "blocked_by": list(row.get("blocked_by") or []),
        "phase": row.get("phase") or "",
        "stop": row.get("stop") or "",
        "progress": max(0, min(100, int(row.get("progress") or 0))),  # CELE-t106
        "subtasks": [
            {"text": str(s.get("text", "")), "weight": max(1, int(s.get("weight", 1))), "done": bool(s.get("done"))}
            for s in (row.get("subtasks") or [])
        ],
        "jira": row.get("jira") or "",   # not synced; preserved if a remote row ever carries it
        "created": _norm_updated(row.get("created")),
        "updated": _norm_updated(row.get("updated")),
        "notes": row.get("notes") or "",
    }


def reconcile_tasks(local: list[dict], remote: list[dict],
                    archived_ids: set | None = None) -> tuple[list[dict], list[dict]]:
    """LWW merge of the local tasks.md model and the hosted `tasks` rows, keyed on `task_id` and
    resolved on the `updated` ISO-8601 timestamp. Returns (merged_tasks, conflicts). Pure — no IO,
    no HTTP — so it is unit-tested in isolation (mirrors `build_task_rows`).

    Rules:
      - only-local  → keep. A pull NEVER deletes a local card (it may be unpushed, or pruned remotely).
      - only-remote → adopt (created on the hosted board); the row is mapped back to a local task —
        UNLESS its id is in `archived_ids` (done-archive.md), i.e. it was intentionally archived off the
        board locally. Those are NOT re-adopted (done-archive is the tombstone), else every archive
        overflow would bounce the card back on the next sync. The subsequent `_push_tasks` prune then
        removes the stale row from the hub. (General local deletes still don't propagate — that needs a
        real tombstone column; see the t61 Phase-2 reconcile rules / decisions.md.)
      - in both     → newer `updated` wins the WHOLE task; tie → local wins (tasks.md is canonical).
      - missing/empty `updated` sorts as oldest (loses) on either side; never crashes.
      - redaction asymmetry (the sharp edge): when a remote win replaces an existing local task, a
        free-text field (title/notes/stop) whose remote value still carries a `[REDACTED:…]` marker
        does NOT clobber the real local text — the local value is kept for that field while the rest
        of the remote win (e.g. a `state` drag) still applies.

    `conflicts` lists every task the merge changed because of remote (remote wins + adoptions), as
    `{"id", "winner", "changed"}`, so the caller can surface "N task(s) changed on the hosted board"."""
    archived_ids = archived_ids or set()
    remote_tasks = [_row_to_task(r) for r in remote]
    by_id_remote = {t["id"]: t for t in remote_tasks if t["id"]}
    local_ids = {t.get("id") for t in local}

    merged: list[dict] = []
    conflicts: list[dict] = []

    # Walk local first so tasks.md ordering is preserved for cards that exist locally.
    for lt in local:
        rt = by_id_remote.get(lt.get("id"))
        if rt is None:
            merged.append(lt)                                   # only-local → keep
            continue
        if _norm_updated(rt.get("updated")) > _norm_updated(lt.get("updated")):
            winner = dict(rt)                                   # remote newer → remote wins the task
            for f in _RECONCILE_FREE_TEXT:                      # …but never let redacted text clobber
                if _REDACTION_RE.search(winner.get(f) or ""):
                    winner[f] = lt.get(f, "")
            merged.append(winner)
            changed = sorted(k for k in (set(lt) | set(winner)) if lt.get(k) != winner.get(k))
            if changed:
                conflicts.append({"id": lt["id"], "winner": "remote", "changed": changed})
        else:
            merged.append(lt)                                   # local newer or tie → local wins

    # Append cards that exist only on the hosted board (created on the web), in remote order — but
    # never re-adopt a card that was archived off the board locally (done-archive is the tombstone).
    for rt in remote_tasks:
        if rt["id"] and rt["id"] not in local_ids and rt["id"] not in archived_ids:
            merged.append(rt)
            conflicts.append({"id": rt["id"], "winner": "remote", "changed": ["*new*"]})

    return merged, conflicts


# --------------------------------------------------------------------------- HTTP (injectable)

def _http(method: str, url: str, headers: dict | None = None, body=None, timeout: int = 30):
    """Tiny JSON HTTP. Returns (status_code, parsed_json_or_None). Tests monkeypatch this."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="ignore")
        try:
            payload = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return e.code, payload
    except urllib.error.URLError as e:
        cb.die(f"network error talking to {url}: {e.reason}")


def _rest_headers(cfg: dict, jwt: str, extra: dict | None = None) -> dict:
    h = {
        "apikey": cfg["anon"],
        "Authorization": f"Bearer {jwt}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if extra:
        h.update(extra)
    return h


# --------------------------------------------------------------------------- Supabase Auth (GoTrue)

def _auth(method: str, cfg: dict, path: str, body=None, bearer: str | None = None, params: str = ""):
    """Call a GoTrue endpoint. `apikey` is always the anon key; `Authorization` is the user's access
    token when acting on a session (factors/user/logout), else the anon key. Returns (status, json)."""
    url = f"{cfg['url']}/auth/v1/{path}{params}"
    headers = {
        "apikey": cfg["anon"],
        "Authorization": f"Bearer {bearer or cfg['anon']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return _http(method, url, headers=headers, body=body)


def _auth_err(d: dict) -> str:
    d = d or {}
    return d.get("error_description") or d.get("msg") or d.get("error") or str(d)


def _store_session(d: dict, extra: dict | None = None) -> dict:
    """Persist a GoTrue session (access + refresh tokens, derived expiry, user fields) to the 0600
    credentials file. Refresh tokens let us re-mint access tokens without re-prompting."""
    creds = load_creds()
    if d.get("expires_in"):
        exp = time.time() + int(d["expires_in"])
    else:
        exp = float(d.get("expires_at") or (time.time() + 3600))
    creds["access_token"] = d["access_token"]
    if d.get("refresh_token"):
        creds["refresh_token"] = d["refresh_token"]
    creds["expires_at"] = exp
    u = d.get("user") or {}
    if u:
        creds["email"] = u.get("email")
        creds["user_id"] = u.get("id")
        md = u.get("user_metadata") or {}
        if md.get("username"):
            creds["username"] = md["username"]
    if extra:
        creds.update(extra)
    # Drop any legacy GitHub-device-flow token; identity is GoTrue now.
    creds.pop("github_token", None)
    save_creds(creds)
    return creds


def _upgrade_hint(payload: dict) -> None:
    p = payload or {}
    url = p.get("checkout_url") or p.get("upgrade_url") or p.get("sponsor_url") or UPGRADE_URL
    print("\n  Hosted sync is part of Celeborn Pro.")
    print("  Start a plan (Pro / Team) to sync your context across devices:")
    print(f"      {url}")
    print("  Local memory and the free git-daemon sync remain yours regardless.\n")


def _ensure_session(cfg: dict) -> str:
    """Return a valid Supabase JWT, refreshing via the stored GoTrue refresh token if expired."""
    creds = load_creds()
    tok = creds.get("access_token")
    exp = creds.get("expires_at", 0)
    if tok and time.time() < (exp - 60):
        return tok
    rt = creds.get("refresh_token")
    if not rt:
        cb.die("not logged in. Run `celeborn login` (or `celeborn register`) first.")
    s, d = _auth("POST", cfg, "token", params="?grant_type=refresh_token", body={"refresh_token": rt})
    if s != 200 or not d or not d.get("access_token"):
        cb.die("session expired — run `celeborn login` again.", code=2)
    return _store_session(d)["access_token"]


def _fn(cfg: dict, name: str, bearer: str, body: dict | None = None):
    """Call a Celeborn Edge Function (Supabase Functions) with the user's JWT. Returns (status, json)."""
    url = f"{cfg['url']}/functions/v1/{name}"
    headers = {
        "apikey": cfg["anon"],
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return _http("POST", url, headers=headers, body=body or {})


# --------------------------------------------------------------------------- account helpers

def _resolve_cfg(args) -> dict:
    """Resolve the remote (env > .celebornrc > defaults) for account commands; fail clearly if the
    hosted instance isn't configured. Account commands don't require a .context/ root."""
    ctx = cb.find_context_root(Path(getattr(args, "path", ".") or ".")) or Path(".")
    rc_ctx = ctx if isinstance(ctx, Path) and (ctx / cb.RC_NAME).exists() else None
    cfg = sync_config(rc_ctx)
    if cfg["url"].startswith("https://REPLACE") or cfg["anon"].startswith("REPLACE"):
        cb.die("hosted account not configured. Set CELEBORN_SUPABASE_URL / CELEBORN_SUPABASE_ANON_KEY "
               "(or .celebornrc sync.{url,anon_key}); see references/supabase-auth-setup.md.")
    return cfg


def _prompt(value, label: str) -> str:
    return (value or input(f"{label}: ")).strip()


def _password(provided=None) -> str:
    """Password from (in order): explicit arg, CELEBORN_PASSWORD env (automation), or a hidden prompt.
    Never accepted as a CLI flag — it would leak into shell history."""
    return provided or os.environ.get("CELEBORN_PASSWORD") or getpass.getpass("Password: ")


def _positioning() -> None:
    print("\n  Celeborn free = the local CLI you own: offline, no account required.")
    print("  Your account is cloud-ready — add hosted sync anytime with `celeborn upgrade`.\n")


def _tier_line(cfg: dict, tok: str | None = None) -> None:
    """Print the caller's tier. Free = no active entitlement row (RLS denies synced tables)."""
    tok = tok or _ensure_session(cfg)
    s, rows = _http("GET", f"{cfg['url']}/rest/v1/entitlements?select=*",
                    headers=_rest_headers(cfg, tok))
    tier = "free"
    if s == 200 and isinstance(rows, list) and rows:
        r = rows[0]
        active = r.get("active", True) and (r.get("status") in (None, "active", "trialing"))
        if active:
            tier = r.get("tier") or "paid"
    print(f"  tier:     {tier}")
    if tier == "free":
        print("  (hosted sync is part of Celeborn Pro — run `celeborn upgrade` when you're ready.)")


def _verified_totp_factor(login_resp: dict) -> str | None:
    for f in ((login_resp or {}).get("user") or {}).get("factors") or []:
        if f.get("factor_type") == "totp" and f.get("status") == "verified":
            return f.get("id")
    return None


def _maybe_elevate_mfa(cfg: dict, login_resp: dict) -> None:
    """If the account has a verified TOTP factor, raise the session AAL1 → AAL2 by verifying a code."""
    fid = _verified_totp_factor(login_resp)
    if not fid:
        return
    tok = login_resp["access_token"]
    s, ch = _auth("POST", cfg, f"factors/{fid}/challenge", bearer=tok)
    if s != 200 or not ch or not ch.get("id"):
        cb.warn("could not start the MFA challenge; continuing without elevation.")
        return
    code = input("MFA 6-digit code (Google Authenticator): ").strip()
    s, d = _auth("POST", cfg, f"factors/{fid}/verify", bearer=tok,
                 body={"challenge_id": ch["id"], "code": code})
    if s != 200 or not d or not d.get("access_token"):
        cb.die(f"MFA verification failed ({s}): {_auth_err(d)}", code=2)
    _store_session(d)


def _callback_page(ok: bool, error: str | None = None) -> bytes:
    """The single HTML page served on the loopback redirect after GitHub sign-in.

    Self-contained (inline CSS, no network) — this is the only face the browser shows during
    `celeborn login --github`, so it carries the Celeborn brand and clearly signals success vs.
    failure, then auto-closes the tab.
    """
    accent = "#7ee787" if ok else "#ff7b72"
    title = "Signed in to Celeborn" if ok else "Sign-in failed"
    if ok:
        body = "You're authenticated. Return to your terminal — the CLI now has your session."
    else:
        detail = (error or "no authorization code was returned").strip()
        body = f"GitHub didn't complete the sign-in: {detail}. Close this tab and try again."
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Celeborn</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  html,body{{height:100%}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:#0b0f14;color:#e6edf3;display:flex;align-items:center;justify-content:center;
    min-height:100vh;padding:24px}}
  .card{{text-align:center;padding:48px 56px;max-width:440px;border:1px solid #1f2630;
    border-radius:16px;background:#0f141b;box-shadow:0 8px 40px rgba(0,0,0,.45)}}
  .bow{{font-size:44px;line-height:1;margin-bottom:18px}}
  h1{{font-size:20px;font-weight:600;letter-spacing:-.01em;margin-bottom:10px;color:{accent}}}
  p{{font-size:14px;color:#8b98a5;line-height:1.55}}
  .hint{{margin-top:18px;font-size:12px;color:#5b6673}}
</style></head>
<body><div class="card">
  <div class="bow">🏹</div>
  <h1>{title}</h1>
  <p>{body}</p>
  <p class="hint">You can close this tab.</p>
</div>
<script>setTimeout(function(){{try{{window.close()}}catch(e){{}}}}, 2500)</script>
</body></html>"""
    return html.encode("utf-8")


def _login_github(cfg: dict) -> None:
    """Browser PKCE login via a one-shot loopback redirect (no client secret on the device)."""
    import base64
    import http.server
    import secrets as _secrets
    import urllib.parse
    import webbrowser

    verifier = base64.urlsafe_b64encode(_secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    holder: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            holder["code"] = (q.get("code") or [None])[0]
            holder["error"] = (q.get("error_description") or q.get("error") or [None])[0]
            ok = bool(holder["code"]) and not holder["error"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_callback_page(ok, holder["error"]))

        def log_message(self, *a):  # silence the default stderr logging
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    redirect_to = f"http://127.0.0.1:{port}/callback"
    authorize = (f"{cfg['url']}/auth/v1/authorize?provider=github"
                 f"&redirect_to={urllib.parse.quote(redirect_to, safe='')}"
                 f"&code_challenge={challenge}&code_challenge_method=s256")
    print(f"\n  Opening GitHub sign-in… if your browser doesn't open, visit:\n      {authorize}\n")
    try:
        webbrowser.open(authorize)
    except Exception:
        pass
    srv.handle_request()  # serve exactly the one callback request
    srv.server_close()
    if holder.get("error") or not holder.get("code"):
        cb.die(f"GitHub sign-in failed: {holder.get('error') or 'no authorization code returned'}")
    s, d = _auth("POST", cfg, "token", params="?grant_type=pkce",
                 body={"auth_code": holder["code"], "code_verifier": verifier})
    if s != 200 or not d or not d.get("access_token"):
        cb.die(f"GitHub token exchange failed ({s}): {_auth_err(d)}")
    _store_session(d)
    _maybe_elevate_mfa(cfg, d)
    creds = load_creds()
    cb.ok(f"logged in with GitHub as {creds.get('email', '?')}. Credentials in {_creds_path()}")
    _tier_line(cfg)


# --------------------------------------------------------------------------- project + push/pull

def _ensure_project(ctx: Path, cfg: dict, jwt: str) -> str:
    if cfg.get("project_id"):
        return cfg["project_id"]
    # Prefer an explicit project_name (the global ~/.context sink seeds "global" so it has a stable
    # identity across the user's devices); else derive from the repo folder name.
    name = cfg.get("project_name") or ctx.parent.name or "celeborn-project"
    s, d = _http("POST", f"{cfg['url']}/rest/v1/projects",
                 headers=_rest_headers(cfg, jwt, {"Prefer": "return=representation"}),
                 body={"name": name})
    if s in (401, 403):
        # RLS denied the insert: a logged-in but unentitled (free) user. Point them at the upgrade.
        _upgrade_hint(d)
        cb.die("hosted sync requires a Celeborn Pro subscription.", code=2)
    if s not in (200, 201) or not d:
        cb.die(f"could not create project ({s}): {d}")
    pid = d[0]["id"] if isinstance(d, list) else d["id"]
    _set_rc_value(ctx, "project_id", pid)
    cb.ok(f"registered hosted project {pid}")
    return pid


# --------------------------------------------------------------------------- project list / remove (t97)

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _list_projects(cfg: dict, jwt: str) -> list[dict]:
    """The signed-in user's hosted projects (RLS scopes to owner + entitled), ordered by name."""
    s, rows = _http("GET",
                    f"{cfg['url']}/rest/v1/projects?select=id,name,created_at&order=name.asc",
                    headers=_rest_headers(cfg, jwt))
    if s == 403:
        _upgrade_hint(rows)
        cb.die("hosted projects are part of Celeborn Pro.", code=2)
    if s != 200 or rows is None:
        cb.die(f"could not list projects ({s}): {rows}")
    return rows


def _resolve_project(rows: list[dict], selector: str) -> dict:
    """Pick the project the user named — by exact id (uuid) else exact name. Dies clearly when there is
    no match, or when a name is ambiguous (asks them to disambiguate by id)."""
    sel = (selector or "").strip()
    if not sel:
        cb.die("which project? pass a name or id (see `celeborn project list`).")
    if _UUID_RE.match(sel):
        hits = [r for r in rows if str(r.get("id")) == sel]
    else:
        hits = [r for r in rows if str(r.get("name")) == sel]
    if not hits:
        cb.die(f"no hosted project matches {sel!r}. Run `celeborn project list` to see them.")
    if len(hits) > 1:
        ids = ", ".join(str(r["id"]) for r in hits)
        cb.die(f"{len(hits)} projects are named {sel!r}; remove by id instead: {ids}")
    return hits[0]


def _delete_project(cfg: dict, jwt: str, project_id: str) -> None:
    """DELETE the project row. RLS gates it to owner + entitled; the FK cascade clears its context
    files, tasks, GitHub links/ingests, and Jira connection in one shot."""
    s, d = _http("DELETE",
                 f"{cfg['url']}/rest/v1/projects?id=eq.{project_id}",
                 headers=_rest_headers(cfg, jwt, {"Prefer": "return=minimal"}))
    if s == 403:
        _upgrade_hint(d)
        cb.die("removing a hosted project requires a Celeborn Pro subscription.", code=2)
    if s not in (200, 204):
        cb.die(f"could not remove project ({s}): {d}")


def cmd_project(args):
    """Manage hosted projects on celeborn.thot.ai: `list` them, or `rm <name|id>` to remove one. Removal
    cascades to the project's context files, tasks, GitHub links, and Jira connection. Operates purely
    against the hosted API (no local .context/ required), so a project whose repo was deleted can still
    be removed."""
    cfg = _resolve_cfg(args)
    jwt = _ensure_session(cfg)
    action = getattr(args, "project_cmd", None) or "list"
    rows = _list_projects(cfg, jwt)
    if action == "list":
        if not rows:
            print("  No hosted projects yet. Run `celeborn sync` (Pro) from a repo to create one.")
            return
        print(f"  {len(rows)} hosted project(s):")
        for r in rows:
            print(f"    {r.get('name')}  ·  {r.get('id')}")
        return
    # action == "rm"
    proj = _resolve_project(rows, getattr(args, "name", None))
    pid, pname = str(proj["id"]), str(proj.get("name"))
    if not getattr(args, "yes", False):
        print(f"  About to PERMANENTLY remove hosted project {pname!r} ({pid}).")
        print("  This deletes its context files, tasks, GitHub links, and Jira connection from")
        print("  the hosted board. Your local .context/ is untouched.")
        try:
            resp = input(f"  Type the project name to confirm: ").strip()
        except (EOFError, KeyboardInterrupt):
            cb.die("aborted.")
        if resp != pname:
            cb.die("name did not match — aborted.")
    _delete_project(cfg, jwt, pid)
    # If the current repo's .celebornrc pointed at this project, clear the now-stale id so a later
    # `celeborn sync` registers a fresh project instead of pushing into a deleted one.
    try:
        ctx = cb.find_context_root(Path(getattr(args, "path", ".") or "."))
        if ctx and (ctx / cb.RC_NAME).exists() and sync_config(ctx).get("project_id") == pid:
            _set_rc_value(ctx, "project_id", None)
            cb.warn("cleared the stale project_id in this repo's .celebornrc.")
    except Exception:
        pass
    cb.ok(f"removed hosted project {pname!r} ({pid}).")


def _push(ctx: Path, cfg: dict, jwt: str, project_id: str, patterns: list[str]) -> tuple[int, int]:
    rows, redactions = build_push_rows(ctx, project_id, patterns)
    if not rows:
        return 0, 0
    s, d = _http("POST", f"{cfg['url']}/rest/v1/context_files",
                 headers=_rest_headers(cfg, jwt, {"Prefer": "resolution=merge-duplicates,return=minimal"}),
                 body=rows)
    if s == 403:
        _upgrade_hint(d)
        cb.die("hosted sync requires a Celeborn Pro subscription.", code=2)
    if s not in (200, 201, 204):
        cb.die(f"push failed ({s}): {d}")
    return len(rows), redactions


def _pull(ctx: Path, cfg: dict, jwt: str, project_id: str) -> int:
    url = (f"{cfg['url']}/rest/v1/context_files?project_id=eq.{project_id}"
           "&select=path,content,version")
    s, rows = _http("GET", url, headers=_rest_headers(cfg, jwt))
    if s == 403:
        _upgrade_hint(rows)
        cb.die("hosted sync requires a Celeborn Pro subscription.", code=2)
    if s != 200 or rows is None:
        cb.die(f"pull failed ({s}): {rows}")
    written = 0
    for r in rows:
        dest = ctx / r["path"]
        if dest.name in SYNC_SKIP_NAMES:
            continue
        remote = r["content"]
        if not _json_intact(dest.name, remote):
            cb.warn(f"skipping pull of {dest.name}: remote copy is invalid JSON (kept local)")
            continue
        if not dest.is_file() or dest.read_text(errors="ignore") != remote:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(remote)
            written += 1
    return written


def _push_tasks(ctx: Path, cfg: dict, jwt: str, project_id: str, patterns: list[str]) -> int:
    """Push the local task board to the hosted `tasks` table (0006) so it renders on celeborn.thot.ai
    (t61 Phase 1). Upserts a row per local task, then PRUNES rows whose task no longer exists locally
    so the hosted board mirrors `.context/tasks.md` (the local truth). No tasks.md → no-op (never
    wipes the table). RLS gates this Pro, exactly like the file push."""
    rows = build_task_rows(ctx, project_id, patterns)
    if not rows:
        return 0
    s, d = _http("POST", f"{cfg['url']}/rest/v1/tasks",
                 headers=_rest_headers(cfg, jwt, {"Prefer": "resolution=merge-duplicates,return=minimal"}),
                 body=rows)
    if s == 403:
        _upgrade_hint(d)
        cb.die("hosted board sync requires a Celeborn Pro subscription.", code=2)
    if s not in (200, 201, 204):
        cb.die(f"task push failed ({s}): {d}")
    # Prune server rows that are gone locally. task_id matches [A-Za-z0-9_-]+ so it is URL-safe inside
    # PostgREST's in-list; quote each to be safe. Best-effort — a stale prune never fails the sync.
    keep = ",".join('"%s"' % r["task_id"] for r in rows)
    _http("DELETE",
          f"{cfg['url']}/rest/v1/tasks?project_id=eq.{project_id}&task_id=not.in.({keep})",
          headers=_rest_headers(cfg, jwt, {"Prefer": "return=minimal"}))
    return len(rows)


# --------------------------------------------------------------------------- architecture diagram (CELE-t187)

def _strip_credentials(doc: dict) -> dict:
    """Return a copy of the architecture doc with the ENTIRE `credentials` block removed. Credentials
    never sync (references/sync-design.md §9) — only schema/updated/nodes/flows survive. This is the
    single chokepoint the diagram push goes through, so a credential value can never reach the cloud."""
    return {k: v for k, v in doc.items() if k != "credentials"}


def build_architecture_row(ctx: Path, project_id: str, patterns: list[str]) -> dict | None:
    """Build the single `project_architecture` upsert row from `.context/infra-local.json`, with the
    `credentials` block stripped AND a defense-in-depth secret-redaction pass over the remaining doc.
    Pure + testable. Returns None when there's no (valid) infra-local.json — sync then no-ops."""
    p = ctx / INFRA_LOCAL_NAME
    if not p.is_file():
        return None
    raw = p.read_text(errors="ignore")
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        cb.warn(f"skipping architecture push: {INFRA_LOCAL_NAME} is not valid JSON")
        return None
    if not isinstance(doc, dict):
        cb.warn(f"skipping architecture push: {INFRA_LOCAL_NAME} is not a JSON object")
        return None
    clean = _strip_credentials(doc)
    # Belt-and-suspenders: run the same secret patterns over the (credential-free) doc in case a token
    # was pasted into a node's notes/endpoint. redact() works on text, so round-trip through JSON.
    redacted, _hits = redact(json.dumps(clean), patterns)
    try:
        clean = json.loads(redacted)
    except json.JSONDecodeError:
        pass  # redaction never breaks JSON structure, but never fail the sync if it somehow did
    return {
        "project_id": project_id,
        "doc": clean,
        "version": int(p.stat().st_mtime_ns),
        "updated": cb.now_iso(),
    }


def _push_architecture(ctx: Path, cfg: dict, jwt: str, project_id: str, patterns: list[str]) -> int:
    """Push the per-project architecture diagram to the hosted `project_architecture` table (0013) so it
    renders on celeborn.thot.ai (CELE-t187). One row per project (upsert on project_id). No
    infra-local.json → no-op. RLS gates this Pro, exactly like the file/task pushes."""
    row = build_architecture_row(ctx, project_id, patterns)
    if not row:
        return 0
    s, d = _http("POST", f"{cfg['url']}/rest/v1/project_architecture",
                 headers=_rest_headers(cfg, jwt, {"Prefer": "resolution=merge-duplicates,return=minimal"}),
                 body=[row])
    if s == 403:
        _upgrade_hint(d)
        cb.die("the hosted architecture diagram requires a Celeborn Pro subscription.", code=2)
    if s not in (200, 201, 204):
        cb.die(f"architecture push failed ({s}): {d}")
    return 1


def cmd_architecture_sync(args):
    """`celeborn architecture sync` — push the (credential-stripped) architecture topology to the
    hosted board. Mirrors cmd_sync's session/project bootstrap, then pushes only the diagram."""
    ctx = cb.require_context(args)
    cfg = sync_config(ctx)
    if cfg["url"].startswith("https://REPLACE"):
        cb.die("hosted sync not configured. Set CELEBORN_SUPABASE_URL / _ANON_KEY or .celebornrc "
               "sync.{url,anon_key} (see references/supabase-setup.md).")
    patterns = cb.load_config(ctx).get("secret_patterns", [])
    jwt = _ensure_session(cfg)
    project_id = _ensure_project(ctx, cfg, jwt)
    n = _push_architecture(ctx, cfg, jwt, project_id, patterns)
    if n:
        cb.ok("architecture diagram pushed to the hosted board (credentials stripped).")
    else:
        print(f"no {INFRA_LOCAL_NAME} to push — run `celeborn architecture init` first.")


# --------------------------------------------------------------------------- active agents (live windows)

def build_agent_rows(ctx: Path, project_id: str) -> list[dict]:
    """PostgREST upsert rows for the hosted `active_agents` table (0011) — the live per-session context
    windows `celeborn agents` reports locally. One row per active session for this project. Pure +
    testable (no network); the hosted board reads these because it can't see a transcript itself."""
    rows = []
    for a in cb._active_agents(ctx, cb.AGENT_ACTIVE_WINDOW_MIN, show_all=False):
        rows.append({
            "project_id": project_id,
            "session_id": a["session"],
            "owner": a["agent"],
            "task": a["task"] or "",
            "task_id": a["task_id"] or "",
            "tokens": int(a["tokens"]),
            "owned": bool(a["owned"]),
            "last_active": a["last_active"],
        })
    return rows


def _push_agents(ctx: Path, cfg: dict, jwt: str, project_id: str) -> int:
    """Push the live active-agents windows to the hosted `active_agents` table (0011) so the web board
    shows the same /clear-nudge chips. Upserts a row per active session, then PRUNES the project's rows
    whose session is no longer live (went idle) so the hosted panel only shows who's working now. No
    live sessions → prune everything (the panel empties). Best-effort; RLS gates it Pro like tasks."""
    rows = build_agent_rows(ctx, project_id)
    if rows:
        s, d = _http("POST", f"{cfg['url']}/rest/v1/active_agents",
                     headers=_rest_headers(cfg, jwt, {"Prefer": "resolution=merge-duplicates,return=minimal"}),
                     body=rows)
        if s == 403:
            return 0  # not Pro / not entitled — silently skip (never blocks the sync)
        if s not in (200, 201, 204):
            return 0
    # Prune sessions that are gone (idle past the window). session_id is URL-safe (uuid); quote each.
    keep = ",".join('"%s"' % r["session_id"] for r in rows)
    filt = f"&session_id=not.in.({keep})" if rows else ""
    _http("DELETE",
          f"{cfg['url']}/rest/v1/active_agents?project_id=eq.{project_id}{filt}",
          headers=_rest_headers(cfg, jwt, {"Prefer": "return=minimal"}))
    return len(rows)


# --------------------------------------------------------------------------- live push (board ~realtime)

def _session_quiet(cfg: dict) -> str | None:
    """A non-interactive `_ensure_session`: return a valid JWT (refreshing via the stored refresh token
    if needed) or None — never prompts, never exits. For the background live push, which must stay
    silent when the user isn't signed in."""
    try:
        return _ensure_session(cfg)
    except SystemExit:
        return None
    except Exception:
        return None


def schedule_hosted_push(ctx: Path, task_ids: list[str]) -> None:
    """Best-effort: after a local task mutation (claim/move/ship/edit), push the changed cards to the
    hosted `tasks` table so celeborn.thot.ai reflects them in ~realtime (the Supabase Realtime channel
    delivers the row change to every open board). Spawns a DETACHED `celeborn hosted-push` so the CLI
    never blocks or hangs on the network. Cheap local gates first — spawns nothing when hosted sync
    isn't configured or there's no stored session, so free / offline users pay ~nothing."""
    if not task_ids:
        return
    try:
        cfg = sync_config(ctx)
        if cfg["url"].startswith("https://REPLACE"):
            return  # hosted sync not configured → nowhere to push
        creds = load_creds()
        if not creds.get("refresh_token") and not creds.get("access_token"):
            return  # not signed in
    except Exception:
        return
    import sys, subprocess
    try:
        cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "celeborn.py")
        repo_root = ctx.parent if ctx.name == ".context" else ctx
        subprocess.Popen(
            [sys.executable, cli, "hosted-push", "--ids", ",".join(task_ids)],
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # outlive the parent CLI invocation
        )
    except Exception:
        pass  # liveness is best-effort — a failed spawn never breaks the task write


def cmd_hosted_push(args) -> None:
    """Hidden command: live-push specific task ids to the hosted board, spawned detached by a local
    task mutation. Upserts the changed rows (and drops rows whose task was deleted locally) so the
    realtime channel carries the change to open boards. Never prompts, never fails loudly — if hosted
    sync isn't configured / the user isn't signed in / not Pro, it exits silently."""
    try:
        ctx = cb.require_context(args)
        cfg = sync_config(ctx)
        if cfg["url"].startswith("https://REPLACE"):
            return
        ids = [i.strip() for i in (getattr(args, "ids", "") or "").split(",") if i.strip()]
        if not ids:
            return
        jwt = _session_quiet(cfg)
        if not jwt:
            return
        project_id = _ensure_project(ctx, cfg, jwt)
        patterns = cb.load_config(ctx).get("secret_patterns", [])
        rows = [r for r in build_task_rows(ctx, project_id, patterns) if r["task_id"] in ids]
        present = {r["task_id"] for r in rows}
        if rows:
            _http("POST", f"{cfg['url']}/rest/v1/tasks",
                  headers=_rest_headers(cfg, jwt, {"Prefer": "resolution=merge-duplicates,return=minimal"}),
                  body=rows)
        # An id that's gone locally (delete / ship-then-archive) → drop the hosted row so realtime
        # removes the card on open boards (the one deletion path that DOES propagate live).
        for gone in [i for i in ids if i not in present]:
            _http("DELETE",
                  f"{cfg['url']}/rest/v1/tasks?project_id=eq.{project_id}&task_id=eq.{gone}",
                  headers=_rest_headers(cfg, jwt, {"Prefer": "return=minimal"}))
        # A claim/move is also a strong "this session is live now" signal — refresh the hosted active
        # agents so the chips track the same change the realtime channel carries for the cards (t131).
        _push_agents(ctx, cfg, jwt, project_id)
    except SystemExit:
        return
    except Exception:
        return


def schedule_agents_push(ctx: Path, min_interval_s: int = 90) -> None:
    """Best-effort: keep the hosted `active_agents` chips tracking the live local windows BETWEEN card
    mutations (CELE-t131). Card pushes already refresh agents on claim/move/ship/edit, but a session
    burns tokens every turn with no card change — so without this, the hosted token counts freeze at the
    last push while the LOCAL board (which recomputes live every poll) climbs, and the two diverge and
    stay diverged (the 275k-vs-238k bug). The per-turn capture calls this; we throttle to at most once
    per `min_interval_s` via a metrics timestamp (spawn ~once a minute, not every prompt) and spawn a
    DETACHED `celeborn hosted-push-agents` so the turn never blocks on the network. Cheap gates first —
    nothing spawns when hosted sync isn't configured or there's no stored session."""
    try:
        cfg = sync_config(ctx)
        if cfg["url"].startswith("https://REPLACE"):
            return  # hosted sync not configured → nowhere to push
        creds = load_creds()
        if not creds.get("refresh_token") and not creds.get("access_token"):
            return  # not signed in
        m = cb._load_metrics(ctx)
        last = float(m.get("last_agents_push_ts") or 0)
        now = time.time()
        if last and (now - last) < max(0, min_interval_s):
            return  # throttled — a recent push already refreshed the chips
        m["last_agents_push_ts"] = now
        cb._save_metrics(ctx, m)
    except Exception:
        return
    import sys, subprocess
    try:
        cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "celeborn.py")
        repo_root = ctx.parent if ctx.name == ".context" else ctx
        subprocess.Popen(
            [sys.executable, cli, "hosted-push-agents"],
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # outlive the parent CLI invocation
        )
    except Exception:
        pass  # liveness is best-effort — a failed spawn never breaks the turn


def schedule_architecture_push(ctx: Path) -> None:
    """Best-effort detached remap (CELE-t201): after the auto-trace additively merges a new stack piece
    into infra-local.json, push the (credential-stripped) topology to the hosted Stack WITHOUT blocking
    the turn. Cheap gates first — nothing spawns unless hosted sync is configured and the user is signed
    in; a failed spawn (or an unentitled/offline machine) is a silent no-op, exactly like the local
    capture that already landed. No throttle: the trace itself only fires when something actually changed."""
    try:
        cfg = sync_config(ctx)
        if cfg["url"].startswith("https://REPLACE"):
            return  # hosted sync not configured → nowhere to push
        creds = load_creds()
        if not creds.get("refresh_token") and not creds.get("access_token"):
            return  # not signed in
    except Exception:
        return
    import sys, subprocess
    try:
        cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "celeborn.py")
        repo_root = ctx.parent if ctx.name == ".context" else ctx
        subprocess.Popen(
            [sys.executable, cli, "architecture", "sync"],
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # outlive the parent CLI invocation
        )
    except Exception:
        pass  # the remap is best-effort — a failed spawn never breaks the turn


def cmd_hosted_push_agents(args) -> None:
    """Hidden command: live-push the active-agents windows to the hosted board, spawned detached by the
    per-turn capture (CELE-t131) so hosted token counts track the live local windows between card
    mutations. Never prompts, never fails loudly — a silent no-op when hosted sync isn't configured or
    the user isn't signed in."""
    try:
        ctx = cb.require_context(args)
        cfg = sync_config(ctx)
        if cfg["url"].startswith("https://REPLACE"):
            return
        jwt = _session_quiet(cfg)
        if not jwt:
            return
        project_id = _ensure_project(ctx, cfg, jwt)
        _push_agents(ctx, cfg, jwt, project_id)
    except SystemExit:
        return
    except Exception:
        return


_TASK_PULL_SELECT = "task_id,title,state,owner,tags,blocked_by,phase,stop,progress,subtasks,notes,created,updated"


def _pull_tasks(ctx: Path, cfg: dict, jwt: str, project_id: str) -> tuple[int, list[dict]]:
    """Pull the hosted `tasks` rows and reconcile them back into `.context/tasks.md` — the inverse of
    `_push_tasks` (t61 Phase 2 write-back). A drag/edit/create made on celeborn.thot.ai lands in the
    LOCAL source of truth via the existing `_load_tasks`/`_save_tasks` round-trip (LWW on `updated`,
    keyed on task_id; see `reconcile_tasks`).

    Because the board is now synced ONLY through this table (not the raw-file channel, see
    TASK_TABLE_FILES), this is also the replication path: a fresh device with no local tasks.md but a
    non-empty hosted board materializes the board here. Only a genuinely empty pair (no local board AND
    no remote rows) is a no-op, so a "no board here" project stays empty. Returns (changed_count,
    conflicts) — changed_count is the number of cards remote added or won."""
    has_local = cb._tasks_path(ctx).is_file()
    s, rows = _http("GET",
                    f"{cfg['url']}/rest/v1/tasks?project_id=eq.{project_id}&select={_TASK_PULL_SELECT}",
                    headers=_rest_headers(cfg, jwt))
    if s == 403:
        _upgrade_hint(rows)
        cb.die("hosted board sync requires a Celeborn Pro subscription.", code=2)
    if s != 200 or rows is None:
        cb.die(f"task pull failed ({s}): {rows}")
    if not has_local and not rows:
        return 0, []                  # nothing on either side → leave "no board here" empty
    local = cb._load_tasks(ctx)       # [] when no local tasks.md (fresh device materializes the board)
    # done-archive.md is the tombstone for cards aged off the board: don't let the hub re-adopt them.
    arch = ctx / cb.DONE_ARCHIVE_FILE
    archived_ids = {t["id"] for t in cb._parse_tasks(arch.read_text())} if arch.is_file() else set()
    merged, conflicts = reconcile_tasks(local, rows, archived_ids)
    if merged != local:
        cb._save_tasks(ctx, merged)   # re-renders tasks.md + JSON; honors archive overflow
    return len(conflicts), conflicts


# --------------------------------------------------------------------------- GitHub App link + ingest pull

def cmd_github_link(args):
    """`celeborn github link <owner/repo> --installation <id>` — bind a repo (under an App
    installation) to this project's hosted store, so the GitHub App can ingest its PR/issue threads.

    Requires login + Pro (creating the hosted project already requires Pro). The actual link row is
    written server-side by the gh-link Edge Function, which verifies the caller owns the project AND
    the installation's account owns the repo (clients have no write policy on github_repo_links)."""
    ctx = cb.require_context(args)
    cfg = sync_config(ctx)
    if cfg["url"].startswith("https://REPLACE"):
        cb.die("hosted account not configured. Set CELEBORN_SUPABASE_URL / _ANON_KEY (see "
               "references/supabase-auth-setup.md).")
    repo = (getattr(args, "repo", None) or "").strip()
    if not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", repo):
        cb.die("pass the repo as <owner>/<repo>, e.g. `celeborn github link octo/myrepo --installation 12345`.")
    installation = getattr(args, "installation", None)
    if not installation:
        cb.die("pass --installation <id> (the App installation id, shown on the App's post-install "
               "setup page or at https://github.com/settings/installations).")
    jwt = _ensure_session(cfg)
    project_id = _ensure_project(ctx, cfg, jwt)  # gates Pro via RLS on project creation
    s, d = _fn(cfg, "gh-link", jwt,
               {"installation_id": int(installation), "repo_full_name": repo, "project_id": project_id})
    if s in (401, 403):
        cb.die(f"link refused ({s}): {(d or {}).get('error', d)}. You must own the project and the "
               "installation's account must own the repo.", code=2)
    if s == 404:
        cb.die("unknown installation — is the Celeborn App installed on that account? "
               "(install it, then retry with the installation id).")
    if s not in (200, 201):
        cb.die(f"link failed ({s}): {d}")
    _set_rc_value(ctx, "github_repo", repo)
    cb.ok(f"linked {repo} → hosted project {project_id}")
    cb.ok("PR/issue threads will be captured; `celeborn sync` pulls them into .context/journal.md (Pro).")


def _ingest_entry(row: dict) -> str:
    """Render one ingested GitHub thread as a journal entry (markdown). Inbound metadata + body —
    never anything from local memory."""
    when = (row.get("occurred_at") or row.get("created_at") or "")[:10]
    who = row.get("author_login") or "unknown"
    event = row.get("gh_event") or "thread"
    src = row.get("source_url")
    lines = [f"\n## {when} — ingested {event} by {who}"]
    if src:
        lines.append(f"- **Source:** {src}")
    body = (row.get("body") or "").strip()
    if body:
        lines.append(body)
    return "\n".join(lines) + "\n"


def _pull_ingested(ctx: Path, cfg: dict, jwt: str, project_id: str) -> int:
    """Pull captured GitHub threads (ingested_events) newer than this DEVICE's high-water cursor and
    append them to .context/journal.md. Uses a per-device cursor (not the server `pulled_at`) so the
    threads reach EVERY linked member's local .context/, not just the first device to sync. Free users
    are filtered to zero rows by RLS, so this is a quiet no-op until they upgrade."""
    cursor = cfg.get("ingested_cursor") or "1970-01-01T00:00:00Z"
    url = (f"{cfg['url']}/rest/v1/ingested_events?project_id=eq.{project_id}"
           f"&created_at=gt.{cursor}"
           "&select=gh_event,author_login,body,source_url,occurred_at,created_at"
           "&order=created_at.asc")
    s, rows = _http("GET", url, headers=_rest_headers(cfg, jwt))
    if s != 200 or not isinstance(rows, list) or not rows:
        return 0
    journal = ctx / "journal.md"
    existing = journal.read_text() if journal.is_file() else "# Journal\n"
    additions = "".join(_ingest_entry(r) for r in rows)
    journal.write_text(existing + additions)
    newest = max(r.get("created_at", "") for r in rows)
    if newest:
        _set_rc_value(ctx, "ingested_cursor", newest)
    return len(rows)


# --------------------------------------------------------------------------- usage metrics (per-user total)

def _push_metrics(ctx: Path, cfg: dict, jwt: str, project_id: str) -> None:
    """Write this project's CURRENT cumulative counters onto its hosted row, so the server can sum a
    per-user running total (the `user_savings` view). Best-effort: never blocks a sync on failure."""
    m = cb._load_metrics(ctx)
    body = {
        "tokens_saved": int(m.get("tokens_saved_estimate", 0)),
        "restarts_avoided": int(cb.restarts_avoided(m)),
        "sessions_resumed": int(m.get("sessions_resumed", 0)),
        "dollars_saved": round(cb.dollars_saved(ctx), 2),
        "metrics_updated_at": cb.now_iso(),
    }
    _http("PATCH", f"{cfg['url']}/rest/v1/projects?id=eq.{project_id}",
          headers=_rest_headers(cfg, jwt, {"Prefer": "return=minimal"}), body=body)


def _fetch_user_total(cfg: dict, jwt: str):
    """The signed-in user's running total across all their projects, or None. Best-effort."""
    s, rows = _http("GET", f"{cfg['url']}/rest/v1/user_savings"
                    "?select=tokens_saved,restarts_avoided,projects", headers=_rest_headers(cfg, jwt))
    if s == 200 and isinstance(rows, list) and rows:
        return rows[0]
    return None


# --------------------------------------------------------------------------- command entrypoints

def cmd_register(args):
    """Create a free Supabase Auth account (email + password + username)."""
    cfg = _resolve_cfg(args)
    email = _prompt(getattr(args, "email", None), "Email")
    username = _prompt(getattr(args, "username", None), "Username")
    pw = _password(getattr(args, "password", None))
    if not getattr(args, "password", None) and not os.environ.get("CELEBORN_PASSWORD"):
        if pw != getpass.getpass("Confirm password: "):
            cb.die("passwords do not match.")
    s, d = _auth("POST", cfg, "signup",
                 body={"email": email, "password": pw, "data": {"username": username}})
    if s not in (200, 201) or not d:
        cb.die(f"registration failed ({s}): {_auth_err(d)}")
    if d.get("access_token"):  # email confirmation disabled → signed in immediately
        _store_session(d, {"username": username})
        cb.ok(f"registered and signed in as {email}.")
        cb.ok("Consider enabling MFA now: `celeborn mfa enroll`.")
    else:  # confirmation required
        cb.ok(f"registered {email}. Check your inbox to confirm your email, then run `celeborn login`.")
    _positioning()


def cmd_login(args):
    """Sign in via Supabase Auth: email+password (+ TOTP if enrolled), or `--github` (browser PKCE)."""
    cfg = _resolve_cfg(args)
    if getattr(args, "github", False):
        return _login_github(cfg)
    email = _prompt(getattr(args, "email", None), "Email")
    pw = _password(getattr(args, "password", None))
    s, d = _auth("POST", cfg, "token", params="?grant_type=password",
                 body={"email": email, "password": pw})
    if s != 200 or not d or not d.get("access_token"):
        cb.die(f"login failed ({s}): {_auth_err(d)}", code=2)
    _store_session(d)
    _maybe_elevate_mfa(cfg, d)
    creds = load_creds()
    cb.ok(f"logged in as {creds.get('email', email)}. Credentials in {_creds_path()}")
    _tier_line(cfg)


def cmd_logout(args):
    """Revoke the session server-side and delete the local credentials file."""
    cfg = _resolve_cfg(args)
    tok = load_creds().get("access_token")
    if tok:
        _auth("POST", cfg, "logout", bearer=tok)
    p = _creds_path()
    if p.is_file():
        p.unlink()
    cb.ok("logged out.")


def _provider_of(user: dict) -> str:
    """The sign-in method behind a GoTrue /user payload: 'github', 'email', … Tolerant of shape: prefers
    app_metadata.provider, falls back to the first linked identity, else 'email'."""
    am = user.get("app_metadata") or {}
    if am.get("provider"):
        return am["provider"]
    ids = user.get("identities") or []
    if ids and ids[0].get("provider"):
        return ids[0]["provider"]
    return "email"


def _warn_identity_split(provider: str) -> None:
    """The heart of CELE-t107's *detection*: if you're signed in with email+password, the website (and
    `celeborn login --github`) use a DIFFERENT GitHub identity, so your web board can look empty even
    though your projects synced fine. Print the exact, copy-pasteable fix. No-op for a GitHub session."""
    if provider == "github":
        return
    cb.warn("you're signed in with email + password — but celeborn.thot.ai signs in with GitHub.")
    print("  These can be two separate accounts, so your web board may look EMPTY even though sync worked.")
    print("  To use the same identity the website does:")
    print("      celeborn login --github")
    print("  If you already pushed projects under THIS email account, move them across afterward with:")
    print("      celeborn account migrate")


def cmd_whoami(args):
    """Show the signed-in account: email, username, sign-in provider, MFA status, and tier."""
    cfg = _resolve_cfg(args)
    creds = load_creds()
    if not creds.get("access_token") and not creds.get("refresh_token"):
        cb.die("not logged in. Run `celeborn login` or `celeborn register`.")
    tok = _ensure_session(cfg)
    s, u = _auth("GET", cfg, "user", bearer=tok)
    if s != 200 or not u:
        cb.die(f"could not fetch account ({s}): {_auth_err(u)}")
    factors = [f for f in (u.get("factors") or []) if f.get("status") == "verified"]
    md = u.get("user_metadata") or {}
    provider = _provider_of(u)
    print(f"  email:    {u.get('email')}")
    print(f"  user id:  {u.get('id')}")
    if md.get("username"):
        print(f"  username: {md['username']}")
    print(f"  sign-in:  {provider}")
    print(f"  MFA:      {'on (' + str(len(factors)) + ' factor)' if factors else 'off — enable with `celeborn mfa enroll`'}")
    _tier_line(cfg, tok)
    _warn_identity_split(provider)


def cmd_account_migrate(args):
    """`celeborn account migrate` — heal the CLI-email-vs-GitHub-login split (CELE-t107).

    Reassigns hosted projects from an OLD account into the one you're signed in as right now (the
    *keeper* — normally your GitHub identity, since that's what the website uses). You prove control of
    the old account by signing in to it once here; both sessions go to the `account-migrate` Edge
    Function, which moves `projects.owner` (tasks/context follow via RLS) and copies the entitlement so
    the keeper session can actually read what moved."""
    cfg = _resolve_cfg(args)
    keeper_tok = _ensure_session(cfg)
    s, ku = _auth("GET", cfg, "user", bearer=keeper_tok)
    if s != 200 or not ku:
        cb.die(f"could not read your current account ({s}): {_auth_err(ku)}")
    keeper_provider = _provider_of(ku)
    print(f"\n  Keeper (projects move INTO this account): {ku.get('email')}  [{keeper_provider}, {ku.get('id')}]")
    if keeper_provider != "github" and not getattr(args, "yes", False):
        cb.warn("you're NOT signed in with GitHub — projects would move onto this email identity, not the "
                "one the website uses.")
        print("  Recommended: run `celeborn login --github` first, then re-run `celeborn account migrate`.")
        if input("  Continue moving into THIS account anyway? [y/N]: ").strip().lower() not in ("y", "yes"):
            cb.die("migrate cancelled — no projects moved.")

    print("\n  Now sign in to the OLD account whose projects you want to move (its session is used once and"
          " never stored):")
    src_email = _prompt(getattr(args, "email", None), "  Old account email")
    src_pw = _password(None)
    s, sd = _auth("POST", cfg, "token", params="?grant_type=password",
                  body={"email": src_email, "password": src_pw})
    if s != 200 or not sd or not sd.get("access_token"):
        cb.die(f"could not sign in to the old account ({s}): {_auth_err(sd)}", code=2)
    from_token = sd["access_token"]  # NOT stored — _store_session would clobber the keeper session

    s, d = _fn(cfg, "account-migrate", keeper_tok, {"from_token": from_token})
    if s == 400 and (d or {}).get("error") == "same_identity":
        cb.die("that's the same account you're already signed in as — nothing to migrate.")
    if s != 200 or not d:
        cb.die(f"migrate failed ({s}): {_auth_err(d)}")
    moved = d.get("moved", 0)
    if moved:
        cb.ok(f"moved {moved} project(s) into {ku.get('email')} [{keeper_provider}].")
        print("  Refresh celeborn.thot.ai — your board now shows them.")
    else:
        cb.ok("no projects were owned by the old account — nothing to move.")
        print("  (If your board is still empty, the projects may be under a third identity — run "
              "`celeborn whoami` on each login to compare user ids.)")


def cmd_upgrade(args):
    """Open Stripe Checkout to start (or change) a Celeborn Pro/Team subscription."""
    cfg = _resolve_cfg(args)
    tok = _ensure_session(cfg)
    tier = (getattr(args, "tier", None) or "pro").lower()
    interval = "year" if getattr(args, "annual", False) else "month"
    seats = max(1, int(getattr(args, "seats", 1) or 1))
    s, d = _fn(cfg, "create-checkout", tok, {"tier": tier, "interval": interval, "seats": seats})
    if s == 401:
        cb.die("session expired — run `celeborn login` again.", code=2)
    if s != 200 or not d or not d.get("url"):
        cb.die(f"could not start checkout ({s}): {_auth_err(d)}")
    url = d["url"]
    plural = "s" if seats != 1 else ""
    print(f"\n  Opening Stripe Checkout — Celeborn {tier.title()} "
          f"({'annual' if interval == 'year' else 'monthly'}, {seats} seat{plural})…")
    print(f"  If your browser doesn't open, visit:\n      {url}\n")
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("  After paying, run `celeborn sync` — your subscription unlocks hosted sync automatically.")


def cmd_billing(args):
    """Open the Stripe Billing Portal to manage seats / payment method / cancel."""
    cfg = _resolve_cfg(args)
    tok = _ensure_session(cfg)
    s, d = _fn(cfg, "billing-portal", tok)
    if s == 409:
        _upgrade_hint(d)
        cb.die("no subscription yet — run `celeborn upgrade` to start one.", code=2)
    if s == 401:
        cb.die("session expired — run `celeborn login` again.", code=2)
    if s != 200 or not d or not d.get("url"):
        cb.die(f"could not open the billing portal ({s}): {_auth_err(d)}")
    url = d["url"]
    print(f"\n  Opening your Celeborn billing portal…\n      {url}\n")
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


def cmd_mfa(args):
    """Manage TOTP MFA (Google Authenticator). action ∈ {enroll, status, disable}."""
    cfg = _resolve_cfg(args)
    tok = _ensure_session(cfg)
    action = getattr(args, "action", "status")
    if action == "enroll":
        s, d = _auth("POST", cfg, "factors", bearer=tok,
                     body={"factor_type": "totp", "friendly_name": "celeborn-cli"})
        if s not in (200, 201) or not d or not d.get("id"):
            cb.die(f"could not start MFA enrollment ({s}): {_auth_err(d)}")
        totp = d.get("totp") or {}
        print("\n  Add Celeborn to Google Authenticator / Authy — scan the QR or enter the key:")
        print(f"      secret:      {totp.get('secret')}")
        print(f"      otpauth URI: {totp.get('uri')}\n")
        code = input("Enter the current 6-digit code to activate MFA: ").strip()
        fid = d["id"]
        s, ch = _auth("POST", cfg, f"factors/{fid}/challenge", bearer=tok)
        if s != 200 or not ch or not ch.get("id"):
            cb.die(f"MFA challenge failed ({s}): {_auth_err(ch)}")
        s, v = _auth("POST", cfg, f"factors/{fid}/verify", bearer=tok,
                     body={"challenge_id": ch["id"], "code": code})
        if s != 200 or not v or not v.get("access_token"):
            cb.die(f"MFA verification failed ({s}): {_auth_err(v)}")
        _store_session(v)
        cb.ok("MFA enabled. You'll be asked for a code at next login.")
    elif action == "disable":
        s, u = _auth("GET", cfg, "user", bearer=tok)
        factors = [f for f in ((u or {}).get("factors") or []) if f.get("factor_type") == "totp"]
        if not factors:
            cb.die("no TOTP factor to disable.")
        for f in factors:
            _auth("DELETE", cfg, f"factors/{f['id']}", bearer=tok)
        cb.ok(f"disabled {len(factors)} TOTP factor(s).")
    else:  # status
        s, u = _auth("GET", cfg, "user", bearer=tok)
        factors = [f for f in ((u or {}).get("factors") or []) if f.get("status") == "verified"]
        print(f"  MFA: {'on (' + str(len(factors)) + ' factor)' if factors else 'off'}")
        if not factors:
            print("  Enable with: celeborn mfa enroll")


def cmd_sync(args):
    ctx = cb.require_context(args)
    cfg = sync_config(ctx)
    if cfg["url"].startswith("https://REPLACE"):
        cb.die("hosted sync not configured. Set CELEBORN_SUPABASE_URL / _ANON_KEY or .celebornrc "
               "sync.{url,anon_key} (see references/supabase-setup.md). Free git-daemon sync (8a) "
               "needs no setup.")
    patterns = cb.load_config(ctx).get("secret_patterns", [])
    interval = getattr(args, "interval", None) or 5
    once = not getattr(args, "watch", False)
    warned_split = False
    while True:
        jwt = _ensure_session(cfg)
        # CELE-t107 detection: once per run, flag an email-vs-GitHub identity split so a synced-but-empty
        # web board reads as 'wrong identity', not 'lost data'. Best-effort; never blocks the sync.
        if not warned_split:
            warned_split = True
            try:
                s, u = _auth("GET", cfg, "user", bearer=jwt)
                if s == 200 and u and _provider_of(u) != "github":
                    _warn_identity_split(_provider_of(u))
            except Exception:
                pass
        project_id = _ensure_project(ctx, cfg, jwt)
        pushed, redactions = _push(ctx, cfg, jwt, project_id, patterns)
        # Reconcile the hosted board back into tasks.md BEFORE pushing tasks (pull/merge → push), so a
        # web edit isn't clobbered by a stale local push and a web-created card survives the prune.
        task_changes, conflicts = _pull_tasks(ctx, cfg, jwt, project_id)
        pushed_tasks = _push_tasks(ctx, cfg, jwt, project_id, patterns)
        pushed_arch = _push_architecture(ctx, cfg, jwt, project_id, patterns)  # CELE-t187 (creds stripped)
        pulled = _pull(ctx, cfg, jwt, project_id)
        note = f"  ↑ pushed {pushed} file(s)"
        if redactions:
            note += f" ({redactions} secret(s) redacted out)"
        if pushed_tasks:
            note += f"  ⤴ {pushed_tasks} task(s) → hosted board"
        if pushed_arch:
            note += "  🗺 architecture → hosted board"
        if task_changes:
            note += f"  ⚠ {task_changes} task(s) changed on the hosted board → merged into tasks.md"
        note += f"  ↓ pulled {pulled} change(s)"
        # If this repo is linked to the GitHub App, also pull captured PR/issue threads (Pro; RLS
        # filters free users to zero). Re-read cfg so the cursor advanced in a prior loop is honored.
        if sync_config(ctx).get("github_repo"):
            ingested = _pull_ingested(ctx, sync_config(ctx), jwt, project_id)
            if ingested:
                note += f"  ⬇ {ingested} GitHub thread(s)"
        print(note)
        # Report this project's counters and surface the user's running cross-device total.
        try:
            _push_metrics(ctx, cfg, jwt, project_id)
            _push_agents(ctx, cfg, jwt, project_id)  # live per-session windows → hosted chips (t131)
            total = _fetch_user_total(cfg, jwt)
            if total:
                print(f"  Σ {int(total.get('tokens_saved', 0)):,} tokens saved across "
                      f"{total.get('projects', 0)} project(s) — your running total on Celeborn")
        except Exception:
            pass  # metrics are best-effort; never fail a sync over them
        if pulled or task_changes:
            cb.cmd_index(args)  # rebuild the local index from refreshed markdown (files or tasks.md)
        if once:
            return
        time.sleep(interval)
