"""celeborn_github — bidirectional GitHub Issues mirror for the board (CELE-t214).

Twin of `celeborn_jira`, same shape and guarantees. Transport is the **GitHub REST API v3 over
urllib** with the *operator's own* token (Bearer). This is deliberately NOT the hosted `Celeborn
Memory` GitHub App: that App is read-only by contract (`references/github-app-listing.md`) —
"structurally incapable of pushing … issues", no Issues-write scope — so it can ingest PR/issue
threads but can never mirror cards outward. The board→Issues mirror is therefore a *local* feature
run under the operator's credential, exactly as `celeborn jira` runs under the operator's Jira token.
The App's guarantee is untouched: this is the operator writing their own issues, a separate path.

Celeborn is the source of truth. `push`/`reconcile` write cards → GitHub Issues; `pull` reads Issues
→ tasks.md (idempotent via each card's `github` field + a `<!-- celeborn:tN -->` body marker).
Mapping: card `todo/doing` → an OPEN issue carrying a `celeborn:{state}` label; card `done` → a
CLOSED issue. State is authoritative from Celeborn on push (push wins); pull is opt-in + dry-run-first
so a two-way loop can't thrash.

Token resolution (never committed): `--token` → `CELEBORN_GITHUB_TOKEN` env → `gh auth token`.
Explicit tokens are stored 0600 in ~/.config/celeborn/credentials.json (under "github"); a gh-sourced
token is re-resolved live each call (source marker only), so rotating `gh` login just works.

Imported lazily by celeborn.py so the free, local core stays network-free.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import celeborn as cb

AUTOPUSH_STATE = ".github-autopush.json"
GH_API = "https://api.github.com"
API_VERSION = "2022-11-28"

# Card state → the open-issue label that carries it (done has no label — the issue is CLOSED instead).
_STATE_LABEL = {"todo": "celeborn:todo", "doing": "celeborn:doing"}
_LABEL_COLOR = {"celeborn:todo": "ededed", "celeborn:doing": "1d76db"}
_LABEL_DESC = {"celeborn:todo": "Celeborn card in TODO", "celeborn:doing": "Celeborn card in DOING"}


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
    """Write credentials.json 0600 — shares the file celeborn_jira/celeborn_sync use, under a
    "github" key, so the integrations don't clobber each other."""
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = _creds_path()
    p.write_text(json.dumps(creds, indent=2) + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def load_github_creds() -> dict:
    """The {token?, source?} bundle for the mirror, or {} if not connected."""
    return _load_creds().get("github", {}) or {}


def save_github_creds(*, token: str = "", source: str = "gh") -> None:
    creds = _load_creds()
    # Store an explicit token, else only a source marker so a rotating `gh` login is re-resolved live.
    creds["github"] = {"token": token} if token else {"source": source}
    _save_creds(creds)


def _set_rc_github(ctx: Path, key: str, value) -> None:
    """Persist a non-secret value under the "github" object in .celebornrc (e.g. mirror repo). Kept
    distinct from the top-level `github_repo` key that the App-link (`github link`) writes."""
    rc = ctx / cb.RC_NAME
    data = {}
    if rc.is_file():
        try:
            data = json.loads(rc.read_text())
        except json.JSONDecodeError:
            data = {}
    data.setdefault("github", {})[key] = value
    rc.write_text(json.dumps(data, indent=2) + "\n")


def github_config(ctx: Path) -> dict:
    """Merge non-secret rc config (repo) with the secret bundle (token/source)."""
    cfg = dict(cb.load_config(ctx).get("github", {}) or {})
    cfg.update(load_github_creds())
    return cfg


# --------------------------------------------------------------------------- GitHub REST client

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


def gh_request(token: str, method: str, path: str, body=None):
    """Authenticated GitHub REST call. `path` is like '/repos/o/r/issues' (or a full URL for paging)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "celeborn",
        "Content-Type": "application/json",
    }
    url = path if path.startswith("http") else f"{GH_API}{path}"
    return _http(method, url, headers=headers, body=body)


def _gh_cli_token() -> str:
    """Reuse an existing `gh` login: `gh auth token`. "" if gh is absent or not logged in."""
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _resolve_token(ctx: Path | None = None, args=None) -> str:
    """Token priority: --token flag → stored explicit token → CELEBORN_GITHUB_TOKEN → `gh auth token`."""
    if args is not None:
        raw = (getattr(args, "token", None) or "").strip()
        if raw:
            return raw
    stored = (load_github_creds().get("token") or "").strip()
    if stored:
        return stored
    env = (os.environ.get("CELEBORN_GITHUB_TOKEN") or "").strip()
    if env:
        return env
    return _gh_cli_token()


def _repo(ctx: Path) -> str:
    return (github_config(ctx).get("repo") or "").strip()


def _split_repo(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def github_connected(ctx: Path) -> bool:
    """True when a mirror repo is configured and a token resolves (does not ping the API)."""
    return bool(_repo(ctx) and _resolve_token(ctx))


# --------------------------------------------------------------------------- card ⇄ issue mapping

def _marker(tid: str) -> str:
    return f"<!-- celeborn:{tid} -->"


def _marker_id(body: str) -> str:
    """Extract the bare card id from an issue body marker, or "" if absent."""
    if not body:
        return ""
    start = body.find("<!-- celeborn:")
    if start < 0:
        return ""
    end = body.find("-->", start)
    if end < 0:
        return ""
    return body[start + len("<!-- celeborn:"):end].strip()


def _display_id(ctx: Path, tid: str) -> str:
    slug = cb.project_slug(ctx)
    return f"{slug.upper()}-{tid}" if slug else tid


def _issue_body(ctx: Path, t: dict) -> str:
    """Issue body: hidden round-trip marker + provenance header + the card's notes."""
    lines = [_marker(t["id"]),
             f"**Celeborn card {_display_id(ctx, t['id'])}** — mirrored from the board (state: {t['state']})."]
    notes = (t.get("notes") or "").strip()
    if notes:
        lines += ["", notes]
    return "\n".join(lines)


def _desired_labels(t: dict) -> list[str]:
    """Open cards carry exactly one state label; done cards are closed and carry none."""
    lbl = _STATE_LABEL.get(t["state"])
    return [lbl] if lbl else []


def _issue_to_state(issue: dict) -> str:
    """Reverse map: closed → done; open + celeborn:doing → doing; else todo."""
    if (issue.get("state") or "").lower() == "closed":
        return "done"
    names = {(l.get("name") if isinstance(l, dict) else l) for l in (issue.get("labels") or [])}
    return "doing" if "celeborn:doing" in names else "todo"


# --------------------------------------------------------------------------- issue listing / labels

def _list_mirror_issues(token: str, owner: str, repo: str) -> list[dict]:
    """Every mirror issue (open+closed) that carries a `<!-- celeborn:tN -->` marker. Paged."""
    out, page = [], 1
    while True:
        s, rows = gh_request(token, "GET",
                             f"/repos/{owner}/{repo}/issues?state=all&per_page=100&page={page}")
        if s != 200 or not isinstance(rows, list) or not rows:
            break
        for it in rows:
            if "pull_request" in it:  # /issues returns PRs too — skip them
                continue
            if _marker_id(it.get("body") or ""):
                out.append(it)
        if len(rows) < 100:
            break
        page += 1
    return out


def _ensure_labels(token: str, owner: str, repo: str) -> None:
    """Create the celeborn:* state labels if missing (GitHub won't auto-create on issue write)."""
    for name, color in _LABEL_COLOR.items():
        s, _ = gh_request(token, "GET", f"/repos/{owner}/{repo}/labels/{urllib.parse.quote(name)}")
        if s == 404:
            gh_request(token, "POST", f"/repos/{owner}/{repo}/labels",
                       {"name": name, "color": color, "description": _LABEL_DESC.get(name, "")})


# --------------------------------------------------------------------------- commands

def cmd_github(args):
    action = getattr(args, "github_cmd", None)
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
    cb.die(f"unknown github command: {action!r}")


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    return val or default


def github_status_doc(ctx: Path, *, live: bool = True) -> dict:
    """JSON-shaped connection status for the board API (`celeborn github status --json`)."""
    repo = _repo(ctx)
    token = _resolve_token(ctx)
    if not (repo and token):
        return {"connected": False, "reason": "not_configured"}
    if not live:
        return {"connected": True, "repo": repo, "live": False}
    s, p = gh_request(token, "GET", "/user")
    if s != 200 or not isinstance(p, dict):
        return {"connected": False, "reason": "auth_failed", "http_status": s, "repo": repo}
    return {"connected": True, "repo": repo, "login": p.get("login"), "live": True}


def perform_connect(ctx: Path, repo: str, token: str, *, token_explicit: bool) -> dict:
    """Validate the repo + token, ensure labels, persist rc repo + creds. Returns a result dict."""
    repo = (repo or "").strip()
    if not repo or repo.count("/") != 1:
        return {"ok": False, "error": "pass the repo as <owner>/<repo>, e.g. cloud-dancer-labs/celeborn"}
    if not token:
        return {"ok": False, "error": "no token — pass --token, set CELEBORN_GITHUB_TOKEN, or `gh auth login`"}

    was_connected = github_connected(ctx)
    s, who = gh_request(token, "GET", "/user")
    if s == 401:
        return {"ok": False, "error": "401 Unauthorized — the token is invalid or expired."}
    if s != 200 or not isinstance(who, dict):
        return {"ok": False, "error": f"GitHub /user returned HTTP {s}", "detail": who}

    owner, name = _split_repo(repo)
    rs, rp = gh_request(token, "GET", f"/repos/{owner}/{name}")
    if rs == 404:
        return {"ok": False, "error": f"repo {repo} not found (or the token can't see it)."}
    if rs != 200 or not isinstance(rp, dict):
        return {"ok": False, "error": f"GitHub repo lookup returned HTTP {rs}", "detail": rp}
    if not rp.get("has_issues", True):
        return {"ok": False, "error": f"Issues are disabled on {repo} — enable them to mirror the board."}
    perms = rp.get("permissions") or {}
    if not (perms.get("push") or perms.get("admin") or perms.get("maintain")):
        return {"ok": False, "error": f"the token lacks write access to {repo} (need push/maintain/admin)."}

    _ensure_labels(token, owner, name)
    save_github_creds(token=token if token_explicit else "", source="gh")
    _set_rc_github(ctx, "repo", repo)

    result = {
        "ok": True, "connected": True, "live": True,
        "first_connect": not was_connected,
        "login": who.get("login"), "repo": repo,
        "token_source": "stored" if token_explicit else "gh/env",
    }
    if not was_connected:
        result["reconcile"] = analyze_reconcile(ctx)
    return result


def _cmd_connect(args):
    """One-time setup. Resolves the token from --token/env/`gh` (never echoed), validates repo +
    write scope, creates the celeborn:* labels, and persists the mirror config."""
    ctx = cb.require_context(args)
    json_out = getattr(args, "json", False)
    repo = getattr(args, "repo", None) or _prompt("Mirror repo (owner/repo)", _repo(ctx))
    token_explicit = bool((getattr(args, "token", None) or "").strip())
    token = _resolve_token(ctx, args)

    result = perform_connect(ctx, repo, token, token_explicit=token_explicit)
    if not result.get("ok"):
        cb.die(result.get("error", "connect failed"))

    if json_out:
        print(json.dumps(result, indent=2))
        return 0
    cb.ok(f"Connected GitHub mirror as {result['login']}")
    print(f"  repo:   {result['repo']}")
    print(f"  token:  {result['token_source']} (explicit tokens stored 0600 outside the repo)")
    print(f"  labels: celeborn:todo, celeborn:doing ensured")
    print("\nNext: `celeborn github reconcile` to preview, then `--apply` to mirror the board → Issues.")
    return 0


def _cmd_status(args):
    ctx = cb.require_context(args)
    doc = github_status_doc(ctx)
    if getattr(args, "json", False):
        print(json.dumps(doc, indent=2))
        return 0 if doc.get("connected") else 1
    if not doc.get("connected"):
        cb.warn("GitHub mirror not connected. Run `celeborn github connect <owner/repo>`.")
        return 1
    cb.ok(f"GitHub mirror OK — {doc.get('login')} → {doc.get('repo')}")
    return 0


def analyze_reconcile(ctx: Path) -> dict:
    """Compare GitHub mirror issues to Celeborn cards. Celeborn is source of truth — orphans reported,
    never imported on reconcile."""
    repo, token = _repo(ctx), _resolve_token(ctx)
    if not (repo and token):
        return {"error": "not_connected"}
    owner, name = _split_repo(repo)
    issues = _list_mirror_issues(token, owner, name)
    tasks = cb._load_tasks(ctx)

    gh_by_num: dict[str, dict] = {}
    for it in issues:
        gh_by_num[str(it["number"])] = {
            "number": str(it["number"]),
            "marker_id": _marker_id(it.get("body") or ""),
            "title": it.get("title", ""),
            "state": _issue_to_state(it),
        }

    linked_ids = {t["github"] for t in tasks if t.get("github")}
    linked, celeborn_unlinked, gh_orphans, state_drift, stale_links = [], [], [], [], []

    for num, gi in gh_by_num.items():
        if num not in linked_ids:
            gh_orphans.append(gi)

    for t in tasks:
        if not t.get("github"):
            celeborn_unlinked.append({"id": t["id"], "title": t["title"], "state": t["state"]})
        elif t["github"] not in gh_by_num:
            stale_links.append({"id": t["id"], "github": t["github"], "title": t["title"], "state": t["state"]})
        else:
            gi = gh_by_num[t["github"]]
            entry = {"id": t["id"], "github": t["github"], "title": t["title"], "celeborn_state": t["state"]}
            linked.append(entry)
            if t["state"] != gi["state"]:
                state_drift.append({**entry, "github_state": gi["state"]})

    return {
        "celeborn_truth": True, "repo": repo,
        "github_issue_count": len(gh_by_num), "celeborn_task_count": len(tasks),
        "linked_count": len(linked), "linked": linked,
        "celeborn_unlinked": celeborn_unlinked, "github_orphans": gh_orphans,
        "state_drift": state_drift, "stale_links": stale_links,
        "apply_hint": "Run reconcile --apply to push Celeborn → GitHub (creates missing issues, "
                      "updates linked cards). GitHub-only orphans are NOT imported.",
    }


def _cmd_reconcile(args):
    ctx = cb.require_context(args)
    if not github_connected(ctx):
        cb.die("GitHub mirror not connected. Run `celeborn github connect <owner/repo>` first.")
    report = analyze_reconcile(ctx)
    if report.get("error"):
        cb.die(report["error"])

    apply = getattr(args, "apply", False)
    if getattr(args, "json", False) and not apply:
        print(json.dumps(report, indent=2))
        return 0

    print(f"GitHub reconcile — repo {report['repo']} · Celeborn is source of truth")
    print(f"  linked: {report['linked_count']} · unlinked Celeborn cards: {len(report['celeborn_unlinked'])}")
    print(f"  GitHub-only (orphan): {len(report['github_orphans'])} · state drift: {len(report['state_drift'])}")
    print(f"  broken github: links: {len(report['stale_links'])}")
    for row in report["github_orphans"][:12]:
        print(f"  ⚠ GitHub-only #{row['number']} — {row['title'][:50]} (not imported)")
    if len(report["github_orphans"]) > 12:
        print(f"  … and {len(report['github_orphans']) - 12} more GitHub-only issue(s)")
    for row in report["state_drift"][:8]:
        print(f"  ↔ [{row['id']}] #{row['github']}: Celeborn {row['celeborn_state']} vs "
              f"GitHub {row['github_state']} (Celeborn wins on --apply)")

    if not apply:
        print("\n(preview — run with --apply to push Celeborn → GitHub)")
        return 0

    tasks = cb._load_tasks(ctx)
    tasks, result = _push_tasks_apply(ctx, tasks, tasks, quiet=False)
    cb._save_tasks(ctx, tasks, autopush_ids=None)
    pushed = len(result.get("pushed") or [])
    cb.ok(f"Reconcile applied — pushed {pushed} Celeborn card(s) to GitHub.")
    if getattr(args, "json", False):
        report["applied"] = True
        report["pushed_count"] = pushed
        print(json.dumps(report, indent=2))
    return 0


# --------------------------------------------------------------------------- pull (issues → tasks)

def _cmd_pull(args):
    """GitHub → Celeborn, read-only against GitHub (GET only; writes solely to local tasks.md).
    Idempotent: each card carries its issue number in `github`, so a re-pull UPDATES the linked card.
    Links first by the stored number, else by the `<!-- celeborn:tN -->` body marker. GitHub is
    authoritative for title + open/closed state on pull; local notes/owner (live claims) are
    preserved. Unmarked issues are ignored (never imported). `--dry-run` previews."""
    ctx = cb.require_context(args)
    repo, token = _repo(ctx), _resolve_token(ctx)
    if not (repo and token):
        cb.die("GitHub mirror not connected. Run `celeborn github connect <owner/repo>` first.")
    owner, name = _split_repo(repo)
    issues = _list_mirror_issues(token, owner, name)
    tasks = cb._load_tasks(ctx)
    by_num = {t["github"]: t for t in tasks if t.get("github")}
    by_id = {t["id"]: t for t in tasks}
    updated = []

    for it in issues:
        num, title, state = str(it["number"]), it.get("title", ""), _issue_to_state(it)
        card = by_num.get(num) or by_id.get(_marker_id(it.get("body") or ""))
        if not card:
            continue  # marked issue with no matching card — orphan, not imported (Celeborn wins)
        changes = []
        if not card.get("github"):
            card["github"] = num
            changes.append("linked")
        if card["title"] != title and title:
            card["title"] = title
            changes.append("title")
        if card["state"] != state:
            changes.append(f"{card['state']}→{state}")
            card["state"] = state
        if changes:
            card["updated"] = cb.now_iso()
            updated.append((num, card["id"], ", ".join(changes)))

    print(f"GitHub pull — repo {repo}: {len(issues)} mirror issue(s)")
    for num, tid, ch in updated:
        print(f"  ~ [{tid}] ← #{num}  ({ch})")
    if not updated:
        print("  (everything already in sync)")

    if getattr(args, "dry_run", False):
        print("\n(dry-run — nothing written)")
        return 0
    if updated:
        cb._save_tasks(ctx, tasks, autopush_ids=None)
        cb.ok(f"Updated {len(updated)} card(s) from GitHub.")
        # CELE-t216: a real pull delta means GitHub moved cards under us — wake the PM to re-march.
        try:
            cb._pm_wake_enqueue(ctx, "github", f"{len(updated)} card(s) pulled")
        except Exception:  # noqa: BLE001
            pass
    return 0


# --------------------------------------------------------------------------- push (tasks → issues)

def _push_tasks_apply(ctx: Path, tasks: list[dict], targets: list[dict], *, quiet: bool = False):
    """Push `targets` to GitHub Issues (always applies). Returns (mutated tasks, result summary)."""
    repo, token = _repo(ctx), _resolve_token(ctx)
    if not (repo and token):
        return tasks, {"skipped": "not connected"}
    owner, name = _split_repo(repo)
    _ensure_labels(token, owner, name)

    # Self-heal against a stripped `github` field (CELE-t214): any older `celeborn` that writes
    # tasks.md without knowing this field drops the mirror link, which would make a re-push CREATE a
    # duplicate. Re-link by the immutable body marker before deciding create-vs-update, so a lost
    # field is silently recovered instead of duplicated. Only scan when some target looks unlinked.
    relinked = 0
    if any(not t.get("github") for t in targets):
        by_marker = {mid: str(it["number"]) for it in _list_mirror_issues(token, owner, name)
                     if (mid := _marker_id(it.get("body") or ""))}
        for t in targets:
            if not t.get("github") and t["id"] in by_marker:
                t["github"] = by_marker[t["id"]]
                t["updated"] = cb.now_iso()
                relinked += 1

    creates = [t for t in targets if not t.get("github")]
    updates = [t for t in targets if t.get("github")]
    pushed, errors = [], []

    for t in creates:
        payload = {"title": t["title"][:250], "body": _issue_body(ctx, t), "labels": _desired_labels(t)}
        s, p = gh_request(token, "POST", f"/repos/{owner}/{name}/issues", payload)
        if s not in (200, 201) or not isinstance(p, dict) or "number" not in p:
            errors.append(f"[{t['id']}] create HTTP {s}")
            continue
        num = str(p["number"])
        t["github"] = num
        t["updated"] = cb.now_iso()
        extra = ""
        if t["state"] == "done":  # POST can't open-as-closed — close in a follow-up PATCH
            gh_request(token, "PATCH", f"/repos/{owner}/{name}/issues/{num}", {"state": "closed"})
            extra = " · closed"
        pushed.append(t["id"])
        if not quiet:
            print(f"  + [{t['id']}] → #{num}{extra}")

    for t in updates:
        num = t["github"]
        payload = {"title": t["title"][:250], "body": _issue_body(ctx, t),
                   "labels": _desired_labels(t), "state": "closed" if t["state"] == "done" else "open"}
        s, _ = gh_request(token, "PATCH", f"/repos/{owner}/{name}/issues/{num}", payload)
        if s != 200:
            errors.append(f"[{t['id']}] update #{num} HTTP {s}")
            continue
        pushed.append(t["id"])
        if not quiet:
            print(f"  ~ [{t['id']}] → #{num} ({t['state']})")

    return tasks, {"pushed": pushed, "errors": errors, "created": len(creates),
                   "updated": len(updates), "relinked": relinked}


def _cmd_push(args):
    """Celeborn → GitHub. PREVIEW BY DEFAULT (writes only with --apply). Per card:
      • no `github` link  → create the issue (labels for its state; closed if done), link it back
      • has a `github` link → update title/body/labels + open/closed to match the card's column
    Pass task ids to push a subset; default is every card."""
    ctx = cb.require_context(args)
    if not github_connected(ctx):
        cb.die("GitHub mirror not connected. Run `celeborn github connect <owner/repo>` first.")
    repo = _repo(ctx)
    apply = getattr(args, "apply", False)
    only = set(getattr(args, "ids", []) or [])

    tasks = cb._load_tasks(ctx)
    targets = [t for t in tasks if (not only or t["id"] in only)]
    if only:
        missing = only - {t["id"] for t in targets}
        if missing:
            cb.die(f"no such task id(s): {', '.join(sorted(missing))}")

    print(f"GitHub push — repo {repo} · {'APPLY' if apply else 'PREVIEW'} · {len(targets)} card(s)")
    creates = [t for t in targets if not t.get("github")]
    updates = [t for t in targets if t.get("github")]

    if not apply:
        for t in creates:
            closed = " (closed)" if t["state"] == "done" else f" ({t['state']})"
            print(f"  + create  ← [{t['id']}] {t['title'][:50]}{closed}")
        for t in updates:
            print(f"  ~ update  #{t['github']} ← [{t['id']}] (title/body + → {t['state']})")
        print("\n(preview — nothing written to GitHub. Re-run with --apply to execute.)")
        return 0

    tasks, result = _push_tasks_apply(ctx, tasks, targets, quiet=False)
    cb._save_tasks(ctx, tasks, autopush_ids=None)
    if result.get("errors"):
        for e in result["errors"]:
            cb.warn(f"  ! {e}")
    cb.ok(f"Pushed {result['created']} created + {result['updated']} updated to GitHub; local links saved.")
    return 0


# --------------------------------------------------------------------------- auto-push (mirror of jira)

def _autopush_enabled(ctx: Path) -> bool:
    return bool(cb.load_config(ctx).get("github_autopush", True))


def _autopush_debounce(ctx: Path) -> int:
    return max(0, int(cb.load_config(ctx).get("github_autopush_debounce_seconds", 90)))


def _autopush_path(ctx: Path) -> Path:
    return ctx / AUTOPUSH_STATE


def _task_fingerprint(t: dict) -> str:
    return json.dumps(
        {"title": t.get("title", ""), "state": t.get("state", ""), "github": t.get("github", "")},
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


def schedule_auto_push(ctx: Path, tasks: list[dict], task_ids: list[str]) -> None:
    """Queue task ids for GitHub push after a board mutation; flush immediately when debounce allows."""
    if not _autopush_enabled(ctx) or not github_connected(ctx) or not task_ids:
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
    if not _autopush_enabled(ctx) or not github_connected(ctx):
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

    for tid in list(pending.keys()):
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
    """Drain the auto-push queue now (also runs after capture)."""
    ctx = cb.require_context(args)
    if not github_connected(ctx):
        cb.warn("GitHub mirror not connected — auto-push queue left intact.")
        return 1
    result = flush_auto_push(ctx, force=getattr(args, "force", False), quiet=False)
    n = int(result.get("flushed") or 0)
    if n:
        cb.ok(f"Auto-pushed {n} card(s) to GitHub.")
    else:
        print("GitHub auto-push queue empty or debounced (nothing to push).")
    return 0
