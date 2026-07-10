"""celeborn_jira — bidirectional Jira Cloud integration (Phase 10/11, card t7).

Stdlib-only (urllib + base64). Transport is the **Jira Cloud REST API v3 with an API token**
(HTTP Basic: `email:token`), deliberately NOT the Atlassian MCP server: Celeborn pushes status from
hooks and the ~10-min cron fleet report, which run headless with no MCP runtime and no browser to
OAuth through. REST over urllib works everywhere the CLI runs. (MCP stays available as a future
interactive, in-chat door — it complements this, it isn't the engine.)

Decision (PLAN.md §9, card-assignment.md): integrate, don't clone. Celeborn = real-time work (claims,
owners, doing); Jira = secondary reporting for humans outside the agent loop. Mapping: Jira
epic ↔ phase card, story ↔ task.

Secrets follow the existing `celeborn_sync` convention:
  • connection secret bundle (email + token) → ~/.config/celeborn/credentials.json (0600, outside any repo)
  • non-secret config (site URL, project key) → .celebornrc "jira" object

This module is imported lazily by celeborn.py so the free, local core stays network-free.
`connect` and `status` are the first stage; `pull`/`push` land once a real project is connected.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import time
from pathlib import Path

import celeborn as cb

AUTOPUSH_STATE = ".jira-autopush.json"


# --------------------------------------------------------------------------- credential storage

def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "celeborn"


def _creds_path() -> Path:
    return _config_dir() / "credentials.json"


def _load_creds() -> dict:
    p = _creds_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_creds(creds: dict) -> None:
    """Write credentials.json with 0600 perms — the Jira API token is a live secret, stored OUTSIDE
    any repo or .context/. Merges into the same file celeborn_sync uses (under a "jira" key) so the
    two integrations share one secret store without clobbering each other."""
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = _creds_path()
    p.write_text(json.dumps(creds, indent=2) + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load_jira_creds() -> dict:
    """The {email, token} bundle, or {} if not connected."""
    return _load_creds().get("jira", {}) or {}


def save_jira_creds(email: str, token: str) -> None:
    creds = _load_creds()
    creds["jira"] = {"email": email, "token": token}
    _save_creds(creds)


def _set_rc_jira(ctx: Path, key: str, value) -> None:
    """Persist a non-secret value under the "jira" object in .celebornrc (e.g. site, project_key)."""
    rc = ctx / cb.RC_NAME
    data = {}
    if rc.is_file():
        try:
            data = json.loads(rc.read_text())
        except json.JSONDecodeError:
            data = {}
    data.setdefault("jira", {})[key] = value
    rc.write_text(json.dumps(data, indent=2) + "\n")


def jira_config(ctx: Path) -> dict:
    """Merge non-secret rc config (site, project_key) with the secret bundle (email, token)."""
    cfg = dict(cb.load_config(ctx).get("jira", {}) or {})
    cfg.update(load_jira_creds())
    return cfg


# --------------------------------------------------------------------------- Jira REST client

def _normalize_site(raw: str) -> str:
    """Accept 'myco', 'myco.atlassian.net', or a full URL → canonical 'https://myco.atlassian.net'."""
    s = (raw or "").strip().rstrip("/")
    if not s:
        return ""
    s = s.removeprefix("https://").removeprefix("http://")
    if "." not in s:
        s = f"{s}.atlassian.net"
    return f"https://{s}"


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


def _auth_header(email: str, token: str) -> str:
    raw = f"{email}:{token}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def jira_request(site: str, email: str, token: str, method: str, path: str, body=None):
    """Authenticated Jira Cloud REST call. `path` is like '/rest/api/3/myself'."""
    headers = {
        "Authorization": _auth_header(email, token),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return _http(method, f"{site}{path}", headers=headers, body=body)


# --------------------------------------------------------------------------- commands

def cmd_jira(args):
    action = getattr(args, "jira_cmd", None)
    if action == "connect":
        return _cmd_connect(args)
    if action == "status":
        return _cmd_status(args)
    if action == "pull":
        return _cmd_pull(args)
    if action == "push":
        return _cmd_push(args)
    if action == "flush":
        return _cmd_flush(args)
    if action == "reconcile":
        return _cmd_reconcile(args)
    cb.die(f"unknown jira command: {action!r}")


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def _resolve_token(args) -> str:
    """API token: --token flag, CELEBORN_JIRA_TOKEN env, else hidden prompt (CLI only)."""
    raw = (getattr(args, "token", None) or os.environ.get("CELEBORN_JIRA_TOKEN") or "").strip()
    if raw:
        return raw
    return getpass.getpass(
        "Jira API token (input hidden; create at "
        "https://id.atlassian.com/manage-profile/security/api-tokens): ").strip()


def jira_status_doc(ctx: Path, *, live: bool = True) -> dict:
    """JSON-shaped connection status for the board API (`celeborn jira status --json`)."""
    cfg = jira_config(ctx)
    site, email, token, pk = cfg.get("site"), cfg.get("email"), cfg.get("token"), cfg.get("project_key")
    if not (site and email and token):
        return {"connected": False, "reason": "not_configured"}
    if not live:
        return {
            "connected": True,
            "site": site,
            "project_key": pk or "",
            "email": email,
            "display_name": email.split("@")[0],
            "live": False,
        }
    status, payload = jira_request(site, email, token, "GET", "/rest/api/3/myself")
    if status != 200 or not isinstance(payload, dict):
        return {
            "connected": False,
            "reason": "auth_failed",
            "http_status": status,
            "site": site,
            "project_key": pk or "",
        }
    return {
        "connected": True,
        "site": site,
        "project_key": pk or "",
        "email": email,
        "display_name": payload.get("displayName") or payload.get("emailAddress") or email,
        "live": True,
    }


def analyze_reconcile(ctx: Path) -> dict:
    """Compare Jira issues to Celeborn tasks. Celeborn is the source of truth — orphans are reported,
    never imported on reconcile."""
    cfg = jira_config(ctx)
    site, email, token, pk = cfg.get("site"), cfg.get("email"), cfg.get("token"), cfg.get("project_key")
    if not (site and email and token and pk):
        return {"error": "not_connected"}

    issues = _search_issues(site, email, token, f"project={pk} ORDER BY created ASC",
                            ["summary", "status", "issuetype", "parent"])
    tasks = cb._load_tasks(ctx)
    jira_by_key: dict[str, dict] = {}
    skipped_epics = 0
    for it in issues:
        f = it["fields"]
        if (f.get("issuetype") or {}).get("name") == "Epic":
            skipped_epics += 1
            continue
        jira_by_key[it["key"]] = {
            "key": it["key"],
            "title": f.get("summary", it["key"]),
            "state": _issue_state(f),
        }

    celeborn_by_jira = {t["jira"]: t for t in tasks if t.get("jira")}
    linked, celeborn_unlinked, jira_orphans, state_drift, stale_links = [], [], [], [], []

    for key, ji in jira_by_key.items():
        if key not in celeborn_by_jira:
            jira_orphans.append(ji)

    for t in tasks:
        if not t.get("jira"):
            celeborn_unlinked.append({"id": t["id"], "title": t["title"], "state": t["state"]})
        elif t["jira"] not in jira_by_key:
            stale_links.append({"id": t["id"], "jira": t["jira"], "title": t["title"], "state": t["state"]})
        else:
            ji = jira_by_key[t["jira"]]
            entry = {"id": t["id"], "jira": t["jira"], "title": t["title"], "celeborn_state": t["state"]}
            linked.append(entry)
            if t["state"] != ji["state"]:
                state_drift.append({**entry, "jira_state": ji["state"]})

    return {
        "celeborn_truth": True,
        "project_key": pk,
        "jira_issue_count": len(jira_by_key),
        "celeborn_task_count": len(tasks),
        "skipped_epics": skipped_epics,
        "linked_count": len(linked),
        "linked": linked,
        "celeborn_unlinked": celeborn_unlinked,
        "jira_orphans": jira_orphans,
        "state_drift": state_drift,
        "stale_links": stale_links,
        "apply_hint": "Run reconcile --apply to push Celeborn → Jira (creates missing issues, "
                       "updates linked cards). Jira-only orphans are NOT imported.",
    }


def _reconcile_applied(ctx: Path) -> bool:
    return bool(cb.load_config(ctx).get("jira", {}).get("reconcile_applied"))


def _mark_reconcile_applied(ctx: Path) -> None:
    _set_rc_jira(ctx, "reconcile_applied", True)


def perform_connect(ctx: Path, site: str, email: str, project_key: str, token: str) -> dict:
    """Validate credentials, persist secrets + rc config. Returns a JSON-shaped result dict."""
    site = _normalize_site(site)
    project_key = (project_key or "").upper()
    if not site:
        return {"ok": False, "error": "a site URL is required (e.g. https://yourname.atlassian.net)"}
    if not email:
        return {"ok": False, "error": "an account email is required"}
    if not project_key:
        return {"ok": False, "error": "a project key is required"}
    if not token:
        return {"ok": False, "error": "an API token is required"}

    was_connected = jira_connected(ctx)
    status, payload = jira_request(site, email, token, "GET", "/rest/api/3/myself")
    if status == 401:
        return {"ok": False, "error": "401 Unauthorized — check the email + API token pair."}
    if status == 404:
        return {"ok": False, "error": f"404 from {site} — that site URL doesn't look like a Jira Cloud instance."}
    if status != 200 or not isinstance(payload, dict):
        return {"ok": False, "error": f"Jira returned HTTP {status}", "detail": payload}

    display = payload.get("displayName") or payload.get("emailAddress") or email
    ps, pp = jira_request(site, email, token, "GET", f"/rest/api/3/project/{project_key}")
    project_name = pp.get("name", project_key) if ps == 200 and isinstance(pp, dict) else ""

    save_jira_creds(email, token)
    _set_rc_jira(ctx, "site", site)
    _set_rc_jira(ctx, "project_key", project_key)

    result = {
        "ok": True,
        "connected": True,
        "live": True,
        "first_connect": not was_connected,
        "display_name": display,
        "email": email,
        "site": site,
        "project_key": project_key,
        "project_name": project_name,
        "token_path": str(_creds_path()),
    }
    if not was_connected or not _reconcile_applied(ctx):
        result["reconcile"] = analyze_reconcile(ctx)
    return result


def _cmd_connect(args):
    """Interactive one-time setup. Reads the API token via a HIDDEN prompt (getpass) so it never lands
    in the terminal scrollback, shell history, or this transcript. Validates against /myself before
    persisting, so a bad credential is caught immediately rather than on first sync."""
    ctx = cb.require_context(args)
    existing = jira_config(ctx)
    json_out = getattr(args, "json", False)

    site = _normalize_site(getattr(args, "site", None) or _prompt("Jira site URL", existing.get("site", "")))
    email = getattr(args, "email", None) or _prompt("Atlassian account email", existing.get("email", ""))
    project_key = (getattr(args, "project", None)
                   or _prompt("Project key (e.g. CEL)", existing.get("project_key", ""))).upper()
    token = _resolve_token(args)

    result = perform_connect(ctx, site, email, project_key, token)
    if not result.get("ok"):
        cb.die(result.get("error", "connect failed"))

    if json_out:
        print(json.dumps(result, indent=2))
        return 0

    cb.ok(f"Connected to Jira as {result['display_name']}")
    print(f"  site:    {result['site']}")
    if result.get("project_name"):
        print(f"  project: {result['project_key']} — “{result['project_name']}”")
    else:
        print(f"  project: {result['project_key']} (⚠ not found yet — set it later with `celeborn jira connect`)")
    print(f"  token:   stored in {result['token_path']} (0600, outside the repo)")
    print(f"  config:  site + project_key in {ctx / cb.RC_NAME} (\"jira\")")
    print("\nNext: `celeborn jira reconcile` to compare boards (Celeborn wins), then auto-push keeps Jira current.")
    return 0


def _cmd_status(args):
    ctx = cb.require_context(args)
    doc = jira_status_doc(ctx)
    if getattr(args, "json", False):
        print(json.dumps(doc, indent=2))
        return 0 if doc.get("connected") else 1
    if not doc.get("connected"):
        cb.warn("Jira not connected. Run `celeborn jira connect`.")
        return 1
    cb.ok(f"Jira OK — {doc.get('display_name')} @ {doc.get('site')}")
    print(f"  project_key: {doc.get('project_key', '—')}")
    return 0


def _cmd_reconcile(args):
    """Celeborn-first sync audit. Preview drift between boards; --apply pushes Celeborn → Jira without
    importing Jira-only orphans (stakeholder leftovers stay in Jira, not cloned into tasks.md)."""
    ctx = cb.require_context(args)
    if not jira_connected(ctx):
        cb.die("Jira not connected. Run `celeborn jira connect` first.")
    report = analyze_reconcile(ctx)
    if report.get("error"):
        cb.die(report["error"])

    apply = getattr(args, "apply", False)
    if getattr(args, "json", False) and not apply:
        print(json.dumps(report, indent=2))
        return 0

    print(f"Jira reconcile — project {report['project_key']} · Celeborn is source of truth")
    print(f"  linked: {report['linked_count']} · unlinked Celeborn cards: {len(report['celeborn_unlinked'])}")
    print(f"  Jira-only (orphan/stale): {len(report['jira_orphans'])} · state drift: {len(report['state_drift'])}")
    print(f"  broken jira: links: {len(report['stale_links'])}")

    for row in report["jira_orphans"][:12]:
        print(f"  ⚠ Jira-only {row['key']} — {row['title'][:50]} (not imported)")
    if len(report["jira_orphans"]) > 12:
        print(f"  … and {len(report['jira_orphans']) - 12} more Jira-only issue(s)")

    for row in report["state_drift"][:8]:
        print(f"  ↔ [{row['id']}] {row['jira']}: Celeborn {row['celeborn_state']} vs Jira {row['jira_state']} "
              f"(Celeborn wins on --apply)")

    if not apply:
        print("\n(preview — run with --apply to push Celeborn → Jira)")
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2))
        return 0

    tasks = cb._load_tasks(ctx)
    tasks, result = _push_tasks_apply(ctx, tasks, tasks, quiet=False)
    cb._save_tasks(ctx, tasks, autopush_ids=None)
    pushed = len(result.get("pushed") or [])
    report["applied"] = True
    report["pushed_count"] = pushed
    _mark_reconcile_applied(ctx)
    cb.ok(f"Reconcile applied — pushed {pushed} Celeborn card(s) to Jira.")
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
    return 0


# --------------------------------------------------------------------------- pull (Jira → tasks)

# Jira's three status CATEGORIES are stable across every workflow (custom status *names* aren't), so we
# map on category, not name. The three categories line up 1:1 with Celeborn's three states
# (the "blocked" state was retired in CELE-t135; dependencies live in each card's blocked_by list).
_CAT_TO_STATE = {"new": "todo", "indeterminate": "doing", "done": "done"}


def _issue_state(fields: dict) -> str:
    cat = ((fields.get("status") or {}).get("statusCategory") or {}).get("key", "")
    return _CAT_TO_STATE.get(cat, "todo")


def _search_issues(site, email, token, jql, fields):
    """Page through the (next-gen) /search/jql endpoint, gathering every issue for the JQL."""
    out = []
    page = ""
    base = f"/rest/api/3/search/jql?jql={urllib.parse.quote(jql)}&maxResults=100&fields={','.join(fields)}"
    while True:
        s, p = jira_request(site, email, token, "GET", base + page)
        if s != 200 or not isinstance(p, dict):
            cb.die(f"Jira search failed (HTTP {s}): {json.dumps(p)[:300]}")
        out.extend(p.get("issues", []))
        nxt = p.get("nextPageToken")
        if not nxt or p.get("isLast", True):
            break
        page = f"&nextPageToken={urllib.parse.quote(nxt)}"
    return out


def _cmd_pull(args):
    """Jira → Celeborn, read-only against Jira (GET only; writes solely to local tasks.md). Idempotent:
    each task carries its Jira key in the `jira` field, so a re-pull UPDATES the linked card instead of
    duplicating. Jira is authoritative for an issue's title + workflow state on pull; local
    notes/owner (live claims) are preserved — Celeborn owns real-time work. Epics are skipped
    here — epic→phase mapping is the next stage. `--dry-run` previews."""
    ctx = cb.require_context(args)
    cfg = jira_config(ctx)
    site, email, token, pk = cfg.get("site"), cfg.get("email"), cfg.get("token"), cfg.get("project_key")
    if not (site and email and token and pk):
        cb.die("Jira not connected. Run `celeborn jira connect` first.")

    issues = _search_issues(site, email, token, f"project={pk} ORDER BY created ASC",
                            ["summary", "status", "issuetype", "parent"])
    tasks = cb._load_tasks(ctx)
    by_key = {t["jira"]: t for t in tasks if t.get("jira")}
    created, updated, skipped_epics = [], [], 0

    for it in issues:
        key, f = it["key"], it["fields"]
        itype = (f.get("issuetype") or {}).get("name", "Task")
        if itype == "Epic":
            skipped_epics += 1
            continue
        state, title = _issue_state(f), f.get("summary", key)
        existing = by_key.get(key)
        if existing:
            changes = []
            if existing["title"] != title:
                existing["title"] = title
                changes.append("title")
            if existing["state"] != state:
                changes.append(f"{existing['state']}→{state}")
                existing["state"] = state
            if changes:
                existing["updated"] = cb.now_iso()
                updated.append((key, existing["id"], ", ".join(changes)))
        else:
            tid, stamp = cb._next_task_id(tasks), cb.now_iso()
            parent = (f.get("parent") or {}).get("key", "")
            note = f"Synced from Jira {key} ({itype})" + (f" · parent {parent}" if parent else "")
            nt = {"id": tid, "title": title, "state": state, "owner": "",
                  "tags": [itype.lower()], "blocked_by": [], "phase": "", "jira": key,
                  "created": stamp, "updated": stamp, "notes": note}
            tasks.append(nt)
            by_key[key] = nt
            created.append((key, tid, state, title))

    epic_note = f", {skipped_epics} epic(s) skipped (epic→phase: next stage)" if skipped_epics else ""
    print(f"Jira pull — project {pk}: {len(issues)} issue(s){epic_note}")
    for key, tid, state, title in created:
        print(f"  + [{tid}] ← {key}  ({state})  {title[:48]}")
    for key, tid, ch in updated:
        print(f"  ~ [{tid}] ← {key}  ({ch})")
    if not created and not updated:
        print("  (everything already in sync)")

    if getattr(args, "dry_run", False):
        print("\n(dry-run — nothing written)")
        return 0
    if created or updated:
        cb._save_tasks(ctx, tasks)
        cb.ok(f"Wrote {len(created)} new + {len(updated)} updated card(s) to tasks.md")
        # CELE-t216: a real pull delta means Jira moved cards under us — wake the PM to re-march.
        try:
            cb._pm_wake_enqueue(ctx, "jira", f"{len(created)} new + {len(updated)} updated")
        except Exception:  # noqa: BLE001
            pass
    return 0


# --------------------------------------------------------------------------- push (tasks → Jira)

# Celeborn state → Jira status CATEGORY (the stable axis). The three states map 1:1 onto Jira's three
# categories (the "blocked" state was retired in CELE-t135).
_STATE_TO_CAT = {"todo": "new", "doing": "indeterminate", "done": "done"}


def _adf(text: str) -> dict:
    """Minimal Atlassian Document Format doc — API v3 requires ADF, not plain strings, for description."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def _active_sprint_id(site, email, token, pk):
    """Resolve the project's active sprint id (for --sprint active). Returns None if there's no Scrum
    board or no active sprint — caller then leaves the issue in the backlog."""
    s, p = jira_request(site, email, token, "GET", f"/rest/agile/1.0/board?projectKeyOrId={pk}")
    boards = p.get("values", []) if isinstance(p, dict) else []
    for b in boards:
        s, sp = jira_request(site, email, token, "GET", f"/rest/agile/1.0/board/{b['id']}/sprint?state=active")
        vals = sp.get("values", []) if isinstance(sp, dict) else []
        if vals:
            return vals[0]["id"]
    return None


def _transition_to_state(site, email, token, key, state):
    """Move an issue to the status category matching `state`. Returns the transition name applied, ""
    if already there / blocked, or a "⚠ …" string if no matching transition exists in the workflow."""
    cat = _STATE_TO_CAT.get(state)
    if not cat:
        return ""  # blocked/unknown — leave Jira status as-is
    s, p = jira_request(site, email, token, "GET", f"/rest/api/3/issue/{key}?fields=status")
    cur = ((p.get("fields", {}) if isinstance(p, dict) else {}).get("status", {}).get("statusCategory", {}) or {}).get("key")
    if cur == cat:
        return ""  # already in the right column
    s, p = jira_request(site, email, token, "GET", f"/rest/api/3/issue/{key}/transitions")
    for tr in (p.get("transitions", []) if isinstance(p, dict) else []):
        if (tr.get("to", {}).get("statusCategory", {}) or {}).get("key") == cat:
            jira_request(site, email, token, "POST", f"/rest/api/3/issue/{key}/transitions",
                         {"transition": {"id": tr["id"]}})
            return tr["name"]
    return f"⚠ no transition to {cat}"


def jira_connected(ctx: Path) -> bool:
    """True when site + credentials + project_key are configured (does not ping the API)."""
    cfg = jira_config(ctx)
    return bool(cfg.get("site") and cfg.get("email") and cfg.get("token") and cfg.get("project_key"))


def _autopush_enabled(ctx: Path) -> bool:
    return bool(cb.load_config(ctx).get("jira_autopush", True))


def _autopush_debounce(ctx: Path) -> int:
    return max(0, int(cb.load_config(ctx).get("jira_autopush_debounce_seconds", 90)))


def _autopush_path(ctx: Path) -> Path:
    return ctx / AUTOPUSH_STATE


def _task_fingerprint(t: dict) -> str:
    return json.dumps(
        {"title": t.get("title", ""), "state": t.get("state", ""), "jira": t.get("jira", "")},
        sort_keys=True,
    )


def _load_autopush_state(ctx: Path) -> dict:
    p = _autopush_path(ctx)
    if not p.is_file():
        return {"pending": {}, "last": {}}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {"pending": {}, "last": {}}
    data.setdefault("pending", {})
    data.setdefault("last", {})
    return data


def _save_autopush_state(ctx: Path, state: dict) -> None:
    _autopush_path(ctx).write_text(json.dumps(state, indent=2) + "\n")


def _push_tasks_apply(
    ctx: Path,
    tasks: list[dict],
    targets: list[dict],
    *,
    itype: str = "Task",
    sprint_opt: str = "active",
    quiet: bool = False,
) -> tuple[list[dict], dict]:
    """Push `targets` to Jira (always applies). Returns (possibly-mutated tasks, result summary)."""
    cfg = jira_config(ctx)
    site, email, token, pk = cfg.get("site"), cfg.get("email"), cfg.get("token"), cfg.get("project_key")
    if not (site and email and token and pk):
        return tasks, {"skipped": "not connected"}

    sprint_id = None
    if sprint_opt == "active":
        sprint_id = _active_sprint_id(site, email, token, pk)
    elif sprint_opt.isdigit():
        sprint_id = int(sprint_opt)

    creates = [t for t in targets if not t.get("jira")]
    updates = [t for t in targets if t.get("jira")]
    pushed, errors = [], []

    for t in creates:
        payload = {"fields": {"project": {"key": pk}, "summary": t["title"][:240],
                              "issuetype": {"name": itype}, "description": _adf(f"Pushed from Celeborn [{t['id']}].")}}
        s, p = jira_request(site, email, token, "POST", "/rest/api/3/issue", payload)
        if s not in (200, 201) or not isinstance(p, dict) or "key" not in p:
            errors.append(f"[{t['id']}] create HTTP {s}")
            continue
        key = p["key"]
        t["jira"] = key
        t["updated"] = cb.now_iso()
        extra = ""
        if sprint_id:
            ss, _ = jira_request(site, email, token, "POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue", {"issues": [key]})
            extra = " +sprint" if ss in (200, 204) else ""
        tr = _transition_to_state(site, email, token, key, t["state"])
        if tr and not tr.startswith("⚠"):
            extra += f" · {tr}"
        pushed.append(t["id"])
        if not quiet:
            print(f"  + [{t['id']}] → {key}{extra}")

    for t in updates:
        key = t["jira"]
        jira_request(site, email, token, "PUT", f"/rest/api/3/issue/{key}", {"fields": {"summary": t["title"][:240]}})
        tr = _transition_to_state(site, email, token, key, t["state"])
        pushed.append(t["id"])
        if not quiet:
            print(f"  ~ [{t['id']}] → {key}" + (f" · {tr}" if tr else ""))

    return tasks, {"pushed": pushed, "errors": errors, "created": len(creates), "updated": len(updates)}


def schedule_auto_push(ctx: Path, tasks: list[dict], task_ids: list[str]) -> None:
    """Queue task ids for Jira push after a board mutation; flush immediately when debounce allows."""
    if not _autopush_enabled(ctx) or not jira_connected(ctx) or not task_ids:
        return
    state = _load_autopush_state(ctx)
    now = time.time()
    by_id = {t["id"]: t for t in tasks}
    for tid in task_ids:
        t = by_id.get(tid)
        if not t:
            continue
        state["pending"][tid] = {"fingerprint": _task_fingerprint(t), "queued_at": now}
    _save_autopush_state(ctx, state)
    flush_auto_push(ctx, tasks, quiet=True)


def flush_auto_push(ctx: Path, tasks: list[dict] | None = None, *, quiet: bool = False, force: bool = False) -> dict:
    """Drain the auto-push queue. Debounces per task unless `force`. Skips when disconnected."""
    if not _autopush_enabled(ctx) or not jira_connected(ctx):
        return {"flushed": 0, "skipped": "disabled or not connected"}
    state = _load_autopush_state(ctx)
    pending = dict(state.get("pending") or {})
    if not pending:
        return {"flushed": 0}
    tasks = tasks if tasks is not None else cb._load_tasks(ctx)
    by_id = {t["id"]: t for t in tasks}
    debounce = _autopush_debounce(ctx)
    now = time.time()
    to_push: list[dict] = []

    for tid, meta in list(pending.items()):
        t = by_id.get(tid)
        if not t:
            pending.pop(tid, None)
            continue
        fp = _task_fingerprint(t)
        last = (state.get("last") or {}).get(tid) or {}
        if not force and debounce > 0:
            if last.get("fingerprint") == fp and (now - float(last.get("at") or 0)) < debounce:
                continue
        to_push.append(t)

    if not to_push:
        return {"flushed": 0, "debounced": len(pending)}

    tasks, result = _push_tasks_apply(ctx, tasks, to_push, quiet=quiet)
    if result.get("pushed"):
        cb._save_tasks(ctx, tasks, autopush_ids=None)
        now = time.time()
        for tid in result["pushed"]:
            t = by_id.get(tid) or next((x for x in tasks if x["id"] == tid), None)
            if t:
                state.setdefault("last", {})[tid] = {"fingerprint": _task_fingerprint(t), "at": now}
            pending.pop(tid, None)
        state["pending"] = pending
        _save_autopush_state(ctx, state)
    return {"flushed": len(result.get("pushed") or []), **result}


def _cmd_flush(args):
    """Drain the auto-push queue now (used by capture/cron hooks and manual `celeborn jira flush`)."""
    ctx = cb.require_context(args)
    if not jira_connected(ctx):
        cb.warn("Jira not connected — auto-push queue left intact.")
        return 1
    result = flush_auto_push(ctx, force=getattr(args, "force", False), quiet=False)
    n = int(result.get("flushed") or 0)
    if n:
        cb.ok(f"Auto-pushed {n} card(s) to Jira.")
    else:
        print("Jira auto-push queue empty or debounced (nothing to push).")
    return 0


def _cmd_push(args):
    """Celeborn → Jira. PREVIEW BY DEFAULT (writes to your real Jira only with --apply). For each card:
      • no `jira:` link  → create the issue, link it back, add to the active sprint, transition to match state
      • has a `jira:` link → update summary + transition to match the card's column
    Blocked cards keep their Jira status untouched (no Jira equivalent). Pass task ids to push a subset;
    default is every card. `--type` sets the issue type for NEW issues (default Task); `--sprint`
    chooses active|backlog|<id>."""
    ctx = cb.require_context(args)
    if not jira_connected(ctx):
        cb.die("Jira not connected. Run `celeborn jira connect` first.")
    cfg = jira_config(ctx)
    pk = cfg.get("project_key")

    apply = getattr(args, "apply", False)
    itype = getattr(args, "type", None) or "Task"
    sprint_opt = (getattr(args, "sprint", None) or "active").lower()
    only = set(getattr(args, "ids", []) or [])

    tasks = cb._load_tasks(ctx)
    targets = [t for t in tasks if (not only or t["id"] in only)]
    if only:
        missing = only - {t["id"] for t in targets}
        if missing:
            cb.die(f"no such task id(s): {', '.join(sorted(missing))}")

    print(f"Jira push — project {pk} · {'APPLY' if apply else 'PREVIEW'} · "
          f"{len(targets)} card(s) · new issues → {sprint_opt} · type={itype}")
    creates = [t for t in targets if not t.get("jira")]
    updates = [t for t in targets if t.get("jira")]

    if not apply:
        for t in creates:
            print(f"  + create  {itype:5} ← [{t['id']}] {t['title'][:50]}  (then → {t['state']}, sprint={sprint_opt})")
        for t in updates:
            print(f"  ~ update  {t['jira']} ← [{t['id']}] (summary + → {t['state']})")
        print("\n(preview — nothing written to Jira. Re-run with --apply to execute.)")
        return 0

    tasks, result = _push_tasks_apply(ctx, tasks, targets, itype=itype, sprint_opt=sprint_opt, quiet=False)
    cb._save_tasks(ctx, tasks, autopush_ids=None)
    cb.ok(f"Pushed {len(creates)} created + {len(updates)} updated to Jira; local links saved.")
    return 0
