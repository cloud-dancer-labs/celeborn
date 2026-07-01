#!/usr/bin/env python3
"""Grok Build adapter for Celeborn — bridges Grok hooks/transcripts to the stock CLI.

Does not modify Celeborn core. Converts Grok session files into the Claude Code JSONL
shape that `celeborn capture` expects, reads token usage from signals.json, and
dispatches hook events without $CELEBORN_HOME or hookSpecificOutput.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

MODULE_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = MODULE_ROOT / "cache"
ORIENT_FILE = ".grok-orient-pending.md"

_SKIP = frozenset({"system", "reasoning"})
_TOOL_MAP = {
    "Shell": "Bash",
    "run_terminal_cmd": "Bash",
    "Bash": "Bash",
    "Read": "Read",
    "read_file": "Read",
    "Glob": "Glob",
    "Grep": "Grep",
    "Edit": "Edit",
    "search_replace": "Edit",
    "StrReplace": "Edit",
    "Write": "Write",
    "NotebookEdit": "NotebookEdit",
    "Task": "Task",
    "WebFetch": "WebFetch",
    "WebSearch": "WebSearch",
}

# Grok renewal commands (see ~/.grok/docs/user-guide/04-slash-commands.md):
#   /clear  — alias for /new; wipes the conversation; SessionStart re-orients via this adapter
#   /compact — compresses history in-place (lighter; same session continues)
GROK_RENEW_CMD = "/clear"

ORIENT_NUDGE = (
    "[Celeborn orient — REQUIRED before you reply; tagged SURFACE AND OBEY]\n"
    "Grok does not inject SessionStart output into the model. Read `.context/.grok-orient-pending.md` "
    "once (full `celeborn status` Hot tier), orient from it, then delete it. If missing, run "
    "`celeborn status` from the project root below.\n"
    "Project root: {root}\n"
    "Kanban shorthand HERE: `wire tN` / `claim tN` = take card tN; `ship tN` = close it; "
    "`hydrate` = orient. Cards: `.context/tasks.md`."
)

PRECOMPACT_MSG = (
    "[celeborn] Compaction imminent. CHECKPOINT now before context is summarized:\n"
    "  1. Rewrite .context/state.md in place (Now / Next action / Open threads).\n"
    "  2. Append one entry to the bottom of .context/journal.md (what + evidence + next).\n"
    "  3. Update .context/session.json (focus, next_action, branch, status, updated_at).\n"
    "Anything not written to .context/ will be lost on compaction."
)


def grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME", Path.home() / ".grok"))


def encode_cwd(path: Path) -> str:
    return quote(str(path.resolve()), safe="")


def read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def session_id(payload: dict) -> str:
    return (
        payload.get("session_id")
        or payload.get("sessionId")
        or os.environ.get("GROK_SESSION_ID")
        or ""
    )


def workspace_root(payload: dict) -> Path:
    for key in ("workspaceRoot", "workspace_root", "cwd"):
        val = payload.get(key)
        if val:
            return Path(val).resolve()
    env = os.environ.get("GROK_WORKSPACE_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()


def find_session_dir(sid: str, root: Path | None = None) -> Path | None:
    if not sid:
        return None
    base = grok_home() / "sessions"
    if root is not None:
        candidate = base / encode_cwd(root) / sid
        if candidate.is_dir():
            return candidate
    matches = sorted(base.glob(f"*/{sid}"))
    return matches[-1] if matches else None


def find_context_root(start: Path) -> Path | None:
    cur = start.resolve()
    for _ in range(32):
        if (cur / ".context").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def active_session_for(root: Path) -> dict | None:
    """Return the active Grok session dict for `root`, if any."""
    active_file = grok_home() / "active_sessions.json"
    if not active_file.is_file():
        return None
    try:
        sessions = json.loads(active_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(sessions, list):
        return None
    target = root.resolve()
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        cwd = entry.get("cwd")
        if not cwd:
            continue
        try:
            if Path(cwd).resolve() == target:
                return entry
        except OSError:
            continue
    return None


def bootstrap_payload(root: Path) -> dict:
    payload: dict = {
        "cwd": str(root),
        "workspaceRoot": str(root),
        "workspace_root": str(root),
    }
    active = active_session_for(root)
    if active:
        sid = active.get("session_id") or active.get("sessionId")
        if sid:
            payload["session_id"] = sid
            payload["sessionId"] = sid
    return payload


def run_celeborn(
    *args: str,
    project: Path | None = None,
    cwd: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Invoke celeborn. Global --path must precede the subcommand."""
    cmd = ["celeborn"]
    if project is not None:
        cmd.extend(["--path", str(project)])
    cmd.extend(args)
    # Tell core which harness is driving so active_adapter() resolves GrokAdapter (Grok-flavored
    # advisor renders, no Claude slash commands) instead of falling back to Claude.
    env = {**os.environ, "CELEBORN_HARNESS": "grok"}
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def grok_user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and block.get("text"):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def map_tool_name(name: str) -> str:
    return _TOOL_MAP.get(name or "", name or "Tool")


def parse_tool_input(name: str, arguments) -> dict:
    if isinstance(arguments, dict):
        inp = dict(arguments)
    elif isinstance(arguments, str) and arguments.strip():
        try:
            parsed = json.loads(arguments)
            inp = parsed if isinstance(parsed, dict) else {"value": parsed}
        except json.JSONDecodeError:
            inp = {"raw": arguments}
    else:
        inp = {}
    if name in ("Shell", "run_terminal_cmd", "Bash") and "command" not in inp:
        for key in ("command", "cmd"):
            if key in inp:
                inp["command"] = inp[key]
                break
    if name in ("Read", "read_file") and "file_path" not in inp and "path" in inp:
        inp["file_path"] = inp["path"]
    if name in ("Edit", "search_replace", "StrReplace") and "file_path" not in inp:
        for key in ("path", "filePath"):
            if key in inp:
                inp["file_path"] = inp[key]
                break
    return inp


def convert_grok_transcript(src: Path, sid: str, dest: Path) -> int:
    """Convert Grok chat_history.jsonl → Claude Code JSONL. Returns line count."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with src.open("r", encoding="utf-8", errors="ignore") as fin, dest.open("w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t in _SKIP:
                continue

            if t == "user":
                text = grok_user_text(obj.get("content")).strip()
                if not text:
                    continue
                out = {
                    "type": "user",
                    "uuid": str(uuid.uuid4()),
                    "sessionId": sid,
                    "message": {"role": "user", "content": text},
                }
                fout.write(json.dumps(out, ensure_ascii=False) + "\n")
                count += 1
                continue

            if t == "assistant":
                blocks = []
                text = obj.get("content")
                if isinstance(text, str) and text.strip():
                    blocks.append({"type": "text", "text": text.strip()})
                for tc in obj.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    name = map_tool_name(tc.get("name") or "")
                    inp = parse_tool_input(tc.get("name") or name, tc.get("arguments"))
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or str(uuid.uuid4()),
                        "name": name,
                        "input": inp,
                    })
                if not blocks:
                    continue
                out = {
                    "type": "assistant",
                    "uuid": str(uuid.uuid4()),
                    "sessionId": sid,
                    "message": {"role": "assistant", "content": blocks},
                }
                fout.write(json.dumps(out, ensure_ascii=False) + "\n")
                count += 1
                continue

            if t == "tool_result":
                tid = obj.get("tool_call_id") or ""
                content = obj.get("content")
                if isinstance(content, list):
                    content = "\n".join(
                        b.get("text", "") if isinstance(b, dict) else str(b) for b in content
                    )
                content = str(content or "")
                out = {
                    "type": "user",
                    "uuid": str(uuid.uuid4()),
                    "sessionId": sid,
                    "toolUseResult": {"stdout": content, "stderr": ""},
                    "message": {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}],
                    },
                }
                fout.write(json.dumps(out, ensure_ascii=False) + "\n")
                count += 1
    return count


def read_context_tokens(session_dir: Path | None) -> int | None:
    if session_dir is None:
        return None
    signals = session_dir / "signals.json"
    if not signals.is_file():
        return None
    try:
        data = json.loads(signals.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for key in ("contextTokensUsed", "context_tokens_used", "totalTokensBeforeCompaction"):
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return None


def claude_transcript_path(sid: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{sid}.claude.jsonl"


def advisor_block(ctx_root: Path) -> str:
    """The advisor recommendation (if any) for this project, formatted to ride the SAME orient
    injection channel as the Hot tier (plan deliverable #6: the pending-file injection carries an
    advisor notice as well as the orient load). Reads the harness-neutral `celeborn advise --json`
    and degrades to "" when there's nothing to recommend. Best-effort — never raises into the hook."""
    try:
        res = run_celeborn("advise", "--json", project=ctx_root)
        data = json.loads((res.stdout or "").strip() or "{}")
    except (json.JSONDecodeError, OSError, ValueError):
        return ""
    recs = data.get("recommendations") or []
    if not recs:
        return ""
    lines = "\n".join(f"- {r.get('text', '')}" for r in recs if r.get("text"))
    return "\n\n## Celeborn advisor\n\n" + lines if lines else ""


def hook_session_start(payload: dict) -> int:
    root = workspace_root(payload)
    sid = session_id(payload)
    ctx_root = find_context_root(root)
    if ctx_root is None:
        return 0

    run_celeborn("grok", "sync-rules", project=ctx_root)

    if sid:
        run_celeborn("record", "orient", "--session", sid, project=ctx_root)

    result = run_celeborn("status", project=ctx_root)
    status = (result.stdout or "").strip()
    if status:
        advice = advisor_block(ctx_root)
        pending = ctx_root / ".context" / ORIENT_FILE
        pending.write_text(
            "# Celeborn Orient (Grok session start)\n\n"
            f"**Project root:** `{ctx_root}`\n\n"
            "Grok does not inject SessionStart hook output into the model. "
            "Read this file once, orient from it, then delete it.\n\n"
            "Kanban shorthand in this project: `wire tN` / `claim tN` = take card tN; "
            "`ship tN` = close it; `hydrate` = you are here.\n\n" + status + advice + "\n",
            encoding="utf-8",
        )
        print("## Celeborn memory (Orient load)\n")
        print(status)
        if advice:
            print(advice)
    return 0


def hook_user_prompt_submit(payload: dict) -> int:
    root = workspace_root(payload)
    sid = session_id(payload)
    ctx_root = find_context_root(root)
    if ctx_root is None:
        return 0

    session_dir = find_session_dir(sid, root)
    tokens = read_context_tokens(session_dir)
    lines = []

    pending = ctx_root / ".context" / ORIENT_FILE
    if pending.is_file():
        lines.append(ORIENT_NUDGE.format(root=ctx_root))

    if tokens is not None:
        remind = run_celeborn(
            "remind",
            "--tokens", str(tokens),
            "--every", "50000",
            "--soft-limit", "150000",
            "--alarm-limit", "200000",
            "--alarm-every", "100000",
            "--clear-cmd", GROK_RENEW_CMD,
            project=ctx_root,
        )
        msg = (remind.stdout or remind.stderr or "").strip()
        if msg:
            lines.append(msg)

    if sid:
        beat = run_celeborn("heartbeat", "--session", sid, project=ctx_root)
        beat_line = (beat.stdout or "").strip()
        if beat_line:
            lines.append(beat_line)

    if session_dir is not None:
        hist = session_dir / "chat_history.jsonl"
        if hist.is_file():
            dest = claude_transcript_path(sid or "session")
            convert_grok_transcript(hist, sid or "session", dest)
            if tokens is not None:
                run_celeborn("record", "turn", "--tokens", str(tokens), project=ctx_root)

    if lines:
        print("\n".join(lines))
    return 0


def hook_stop(payload: dict) -> int:
    root = workspace_root(payload)
    sid = session_id(payload) or "session"
    session_dir = find_session_dir(sid if sid != "session" else session_id(payload), root)
    if session_dir is None:
        return 0

    hist = session_dir / "chat_history.jsonl"
    if not hist.is_file():
        return 0

    dest = claude_transcript_path(sid)
    if convert_grok_transcript(hist, sid, dest) == 0:
        return 0

    ctx_root = find_context_root(root)
    cap_args = [
        "capture",
        "--transcript", str(dest),
        "--session", sid,
        "--quiet",
        "--note",
    ]
    if ctx_root is None:
        cap_args.append("--global")
    capture = run_celeborn(*cap_args, project=ctx_root)
    note = (capture.stdout or "").strip()
    if note:
        if note.startswith("{"):
            try:
                env = json.loads(note)
                msg = env.get("systemMessage")
                if msg:
                    print(msg)
                    return 0
            except json.JSONDecodeError:
                pass
        print(note)
    return 0


def hook_pre_compact(payload: dict) -> int:
    root = workspace_root(payload)
    ctx_root = find_context_root(root)
    if ctx_root is not None:
        run_celeborn("record", "compaction", project=ctx_root)
    print(PRECOMPACT_MSG)
    return 0


def hook_session_end(payload: dict) -> int:
    root = workspace_root(payload)
    ctx_root = find_context_root(root)
    if ctx_root is None:
        return 0
    run_celeborn("handoff", project=ctx_root)
    pending = ctx_root / ".context" / ORIENT_FILE
    if pending.is_file():
        pending.unlink(missing_ok=True)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Prime Celeborn memory without waiting for Grok hooks (orient file + optional capture)."""
    root = Path(args.path or ".").resolve()
    ctx_root = find_context_root(root)
    if ctx_root is None:
        print("No .context/ found — run `celeborn init` in your project first.", file=sys.stderr)
        return 1

    # Pin the harness in .celebornrc so any direct `celeborn` call in this repo (not just our hooks,
    # which already export CELEBORN_HARNESS) resolves GrokAdapter. Idempotent + best-effort. SKIPPED
    # when core's own `celeborn init` wires Grok speculatively (--no-harness-pin): that path fires on
    # any machine with Grok installed, including Claude-primary repos, so it must NOT override the
    # default-claude resolution. A deliberate grok install (no flag) pins; speculative wiring doesn't.
    if not getattr(args, "no_harness_pin", False):
        run_celeborn("harness", "grok", project=ctx_root)

    payload = bootstrap_payload(ctx_root)
    hook_session_start(payload)

    active = active_session_for(ctx_root)
    if active:
        hook_user_prompt_submit(payload)
        hook_stop(payload)
        print(f"✓ bootstrapped memory for active Grok session {payload.get('session_id', '')[:8]}")
    else:
        pending = ctx_root / ".context" / ORIENT_FILE
        if pending.is_file():
            print(f"✓ wrote {pending.relative_to(ctx_root)} (read once on next Grok turn)")
        print("· no active Grok session — hooks will run automatically on session start")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """One-shot Grok wiring: copy hooks, optionally init memory, bootstrap orient."""
    install_sh = MODULE_ROOT / "scripts" / "install.sh"
    if not install_sh.is_file():
        print(f"install script missing: {install_sh}", file=sys.stderr)
        return 1

    cmd = ["bash", str(install_sh)]
    project = getattr(args, "project", None)
    if project:
        cmd.extend(["--project", str(Path(project).resolve())])
    if getattr(args, "private", False):
        cmd.append("--private")
    if getattr(args, "public", False):
        cmd.append("--public")
    if getattr(args, "no_init", False):
        cmd.append("--no-init")

    result = subprocess.run(cmd, check=False)
    return result.returncode


def cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True
    which = subprocess.run(["which", "celeborn"], capture_output=True, text=True)
    if which.returncode != 0:
        print("✗ celeborn not on PATH — run: uv tool install --editable <celeborn-clone>")
        ok = False
    else:
        print(f"✓ celeborn at {which.stdout.strip()}")
        ver = run_celeborn("version")
        print(f"  {ver.stdout.strip()}")

    hooks = grok_home() / "hooks" / "celeborn.json"
    if hooks.is_file():
        print(f"✓ Grok hooks installed at {hooks}")
    else:
        print(f"✗ Grok hooks missing — run: {MODULE_ROOT}/scripts/install.sh")
        ok = False

    sid = os.environ.get("GROK_SESSION_ID", "")
    root = Path(os.environ.get("GROK_WORKSPACE_ROOT", Path.cwd()))
    session_dir = find_session_dir(sid, root) if sid else None
    if session_dir:
        print(f"✓ session dir {session_dir}")
        tokens = read_context_tokens(session_dir)
        if tokens is not None:
            print(f"  context tokens: {tokens:,}")
        hist = session_dir / "chat_history.jsonl"
        if hist.is_file():
            dest = claude_transcript_path(sid)
            n = convert_grok_transcript(hist, sid, dest)
            print(f"  transcript bridge: {n} Claude-shaped line(s) in {dest}")
    else:
        print("· no active GROK_SESSION_ID (run from a Grok hook to test session resolution)")

    return 0 if ok else 1


def cmd_convert(args: argparse.Namespace) -> int:
    src = Path(args.source)
    if not src.is_file():
        print(f"source not found: {src}", file=sys.stderr)
        return 1
    dest = Path(args.dest) if args.dest else claude_transcript_path(args.session)
    n = convert_grok_transcript(src, args.session, dest)
    print(f"converted {n} line(s) → {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Grok Build adapter for Celeborn")
    sub = parser.add_subparsers(dest="cmd")

    for event in (
        "session-start",
        "user-prompt-submit",
        "stop",
        "pre-compact",
        "session-end",
    ):
        sub.add_parser(event, help=f"Grok {event} hook entry point")

    sub.add_parser("doctor", help="verify celeborn + grok bridge health")
    boot = sub.add_parser(
        "bootstrap",
        help="write orient-pending and prime capture without Grok hook reload",
    )
    boot.add_argument("--path", default=".", help="project root (default: cwd)")
    boot.add_argument("--no-harness-pin", dest="no_harness_pin", action="store_true",
                      help="don't pin harness=grok in .celebornrc (used by core's speculative `celeborn init` wiring)")
    inst = sub.add_parser(
        "install",
        help="install Grok hooks + optional celeborn init + bootstrap (one command)",
    )
    inst.add_argument("--project", help="project root to init/bootstrap (default: cwd if --init)")
    inst.add_argument("--private", action="store_true", help="pass --private to celeborn init")
    inst.add_argument("--public", action="store_true", help="pass --public to celeborn init")
    inst.add_argument("--no-init", action="store_true", help="skip celeborn init even if no .context/")
    cvt = sub.add_parser("convert", help="convert a Grok chat_history.jsonl for celeborn capture")
    cvt.add_argument("source")
    cvt.add_argument("--session", default="manual")
    cvt.add_argument("--dest")

    args = parser.parse_args(argv)
    hook_cmds = frozenset({
        "session-start", "user-prompt-submit", "stop", "pre-compact", "session-end",
    })
    if not args.cmd:
        parser.print_help()
        return 2
    if args.cmd in hook_cmds:
        return {
            "session-start": hook_session_start,
            "user-prompt-submit": hook_user_prompt_submit,
            "stop": hook_stop,
            "pre-compact": hook_pre_compact,
            "session-end": hook_session_end,
        }[args.cmd](read_stdin_json())
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "bootstrap":
        return cmd_bootstrap(args)
    if args.cmd == "install":
        return cmd_install(args)
    if args.cmd == "convert":
        return cmd_convert(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())