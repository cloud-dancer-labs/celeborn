#!/usr/bin/env python3
"""Celeborn — a long-term context substrate for coding agents.

A single-file, stdlib-only CLI that manages a per-repo `.context/` memory directory:
the deterministic bookkeeping (scaffold, index, search, archive, promote, handoff, health)
that a model does unreliably or expensively. Judgment stays with the agent.

Markdown in `.context/` is the source of truth. The SQLite index (`index.db`) is derived
and disposable — `index` drops and rebuilds it from scratch.

Commands:
  init      THE first-run command: wire the agent + scaffold this project + sign in + open the board
  scaffold  Scaffold .context/ only (always gitignored/private) — the secondary command `init` runs
  status    Print the Hot tier exactly as an agent should load it on Orient
  index     (Re)build the SQLite FTS index from the markdown
  search    Full-text recall -> ranked snippets with file:anchor pointers
  archive   Move journal entries past the threshold into journal-archive/
  promote   Append a formatted entry to a higher tier (learnings / durable)
  handoff   Regenerate handoff.md from state.md + session.json
  doctor    Health check: budgets, index freshness, missing files, memory drift, secret scan
  capture   Mechanically ingest a Claude Code transcript into the local Automatic Context Record (no model)
  login     Sign in with GitHub to unlock hosted sync (premium; Pro subscription)
  sync      Push/pull .context/ to the hosted Supabase backend (secrets redacted out)
  version   Print version; --check looks back at GitHub for a newer Celeborn
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import io
import json
import re
import sys
from pathlib import Path

# NOTE: `sqlite3` is intentionally NOT imported at module level. Only `index` and `search` touch
# the database; importing it lazily there keeps the common hot paths (record/status/handoff/doctor,
# fired by hooks on every session) at bare-interpreter startup cost.

CONTEXT_DIRNAME = ".context"
RC_NAME = ".celebornrc"
INDEX_NAME = "index.db"
METRICS_NAME = "metrics.json"

# The Hot tier — what actually loads on Orient. Everything else is "saved" vs. a naive full load.
HOT_FILES = ["state.md", "session.json", "durable/manifest.md"]

# Pre-compaction panic-save: the authored tiers a compaction threatens to make the model re-derive.
# `panic-save` copies whichever of these exist into .context/.panic/<stamp>/ as a restore point, so
# the survival is a felt artifact ("🏹 Celeborn saved your session"), not an invisible promise. Order
# is restore order. Subpaths (durable/…) are mirrored under the stamp dir.
PANIC_SAVE_FILES = ["state.md", "session.json", "notes.md", "journal.md", "decisions.md",
                    "learnings.md", "handoff.md", "tasks.md", "durable/manifest.md"]
PANIC_DIR = ".panic"        # under .context/ — local-only, gitignored, FIFO-pruned
# Shown in the panic-save user/agent line (t43) — where to read about compaction, /clear, and tiers.
PANIC_READ_MORE = "references/memory-protocol.md"
PANIC_KEEP = 10             # keep the most recent N snapshots; older ones are deleted on each save

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent


def _data_dir() -> Path:
    """Locate the runtime data (schema.sql + init templates).

    Installed builds ship `references/` as the data-only `celeborn_refs` package, so we resolve it
    via importlib.resources (works for pip/uv/wheel installs where the source tree isn't present).
    In a plain source checkout celeborn_refs isn't importable, so fall back to <repo>/references/.
    A frozen PyInstaller binary bundles the tree under `_MEIPASS/celeborn_refs`.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "celeborn_refs"  # type: ignore[attr-defined]
        if bundled.is_dir():
            return bundled
    try:
        from importlib.resources import files
        packaged = Path(str(files("celeborn_refs")))
        if packaged.is_dir():
            return packaged
    except Exception:
        pass
    return REPO_ROOT / "references"


DATA_DIR = _data_dir()
TEMPLATES_DIR = DATA_DIR / "templates"
SCHEMA_PATH = DATA_DIR / "schema.sql"

DEFAULTS = {
    "journal_keep_entries": 20,
    # state.md's `## Now` is meant to be a tiny rewrite-in-place headline, but agents in practice
    # append dated `SESSION`/history bullets there. When that happens, keep only the newest N such
    # bullets in state.md and FIFO the rest to a cold, still-searchable `state-archive/`. Structural
    # bullets (Focus / Next action / Branch) are never touched — only dated history entries.
    "state_keep_sessions": 6,
    # Master switch for capture-time self-healing: when a tier drifts over budget, `celeborn capture`
    # (every turn) trims it back so nobody has to remember `celeborn archive`. Set False to require
    # the manual command. Archiving is FIFO + best-effort; it never blocks capture.
    "auto_archive": True,
    "done_keep_cards": 30,           # done cards visible on the board; older ones auto-archive
    "done_archive_keep_cards": 100,  # FIFO cap for done-archive.md before oldest entries are dropped
    "state_max_lines": 120,
    "search_default_limit": 8,
    "chars_per_token": 4,        # rough English heuristic for token estimation
    "pm_model": "qwen3:4b-instruct",  # Pippin · PM (CELE-t373): the fixed local PM — a REAL upstream Ollama tag (the retired `qwen-4b` was a hand-made local alias, CELE-t374). Phrases board lines only, never decides (CELE-t283).
    "pm_ollama_url": "http://localhost:11434/v1",  # OpenAI-compatible endpoint the PM formatter calls
    # --- OpenCode engine + Ollama daemon, surfaced/steered from the board Settings page (CELE-t352) ---
    "opencode_serve_url": "http://localhost:4096",  # `opencode serve` REST base — probed live for reachability + session count
    "opencode_default_model": "",  # model a fresh session starts with ("" = OpenCode's own default); also written to root opencode.json `model`
    # Two hook behaviours are now config-gated so Settings can toggle them; the defaults preserve
    # today's always-on wiring (a fresh project behaves exactly as before this key existed).
    "compaction_hijack": True,   # CELE-t142: plugin replaces OpenCode's blind summary with the live Hot tier; False = OpenCode's own summarizer runs
    "card_gate": True,           # CELE-t131/t140: writes/research/subagents without a claimed card are denied in-hook; False = never gate on cards
    "ollama_host": "http://localhost:11434",  # native Ollama daemon base (/api/tags,/api/pull,/api/delete) — distinct from pm_ollama_url's OpenAI-compat /v1
    "ollama_keep_alive_minutes": 30,  # how long a model stays warm after its last call (passed to the daemon as keep_alive)
    "usd_per_mtok": 3.0,         # blended $/1M input tokens for the "$ saved" flex (standup --tweet, flex)
    # Context-pressure warning thresholds (CELE-t207), in absolute live-window tokens. Crossing one
    # turns the calm /clear milestone nudge into an explicit warning (soft) or an urgent stop-and-
    # checkpoint warning (hard, the future auto-clear trigger). Defaults track the band.ts /clear
    # bands: soft = "clear now" (100k), hard = "clear urgent" (125k). ≤ 0 disables a threshold.
    "context_soft_tokens": 100_000,
    "context_hard_tokens": 125_000,
    # Seamless clear-and-continue in OpenCode (CELE-t209) — OPT-IN. When a live-reported window
    # (`record tokens`, the OpenCode plugin) crosses context_hard_tokens, `record tokens` prints an
    # `autoclear: due` marker; the plugin then runs `celeborn autoclear` at the next turn boundary,
    # and on a clean t208 gate compacts the session (lossless via the P5 hijack) and re-injects the
    # resume brief through the outbox — no human step. False = today's warn-only behavior.
    "opencode_autoclear": False,
    "autoclear_cooldown_minutes": 10,  # min gap between auto-clear attempts per session (anti-thrash)
    "orient_dedupe_seconds": 120,  # hookless fallback: orients within this window = same session
    "project_slug": None,  # short id for project-qualified card markers (⟨celeborn:slug/tN⟩); None = short 4-char prefix derived from the repo folder name
    "qualified_task_ids": True,  # default-on (opt-OUT): display card ids project-qualified (SLUG-tN, driven by project_slug) everywhere — board, CLI, orient, standup. Set False to show bare tN. Fleet (cross-project) always qualifies; resolvers accept qualified ids regardless. Stored ids stay bare tN.
    "board_port": None,  # localhost port for the kanban viewer; None = derive a STABLE per-project port (de-collides repos)
    "board_autostart": True,  # ensure-on-orient: SessionStart starts the viewer (detached) if its port is down. False = never auto-launch.
    "jira_autopush": True,  # after tasks add/move/edit/claim, push linked cards to Jira (best-effort; skips when disconnected)
    "jira_autopush_debounce_seconds": 90,  # per-task minimum gap between Jira transitions (avoids workflow thrash)
    "capture_output_max_chars": 8000,  # per-tool-result cap in the faithful auto record (redact-then-cap)
    # Hot-tier (Orient load) output budgets, in characters. The SessionStart hook injects `status`
    # as additionalContext; a host with a small inline budget persists oversized hook output to a
    # file and feeds the model only a tiny preview — silently killing automatic rehydration. So
    # `status` truncates each variable-length piece with a pointer to the full file (bypass: --full).
    "hot_state_max_chars": 4000,      # state.md body
    "hot_activity_max_chars": 2000,   # activity.md (Automatic Context Record digest)
    "hot_focus_max_chars": 1500,      # each session.json focus/next_action string
    "hot_tasks_max_chars": 1000,      # tasks board summary (counts + in-flight cards)
    "hot_touches_max_chars": 800,     # active file touches (multi-agent editing)
    "touch_ttl_hours": 2,             # stale touches drop out of orient after this many hours
    # Skill advisor (t70) — a quiet throughput/quality nudge layer. Read through `_advisor_config()`,
    # which deep-fills this block from .celebornrc and still honors the legacy flat keys
    # (`advisor_enabled` / `advisor_permission_bloat_min`) older rc files may carry.
    "advisor": {
        "enabled": True,
        "max_per_session": 1,          # at most this many advisor nudges per session (don't nag)
        "permission_bloat_min": 10,    # ≥ this many over-specific Bash allow-rules → friction signal
        "review_min_files": 3,         # ≥ this many changed code files → recommend a code review (Phase 3)
        "parallelize_min_files": 12,   # ≥ this many changed code files → recommend fanning out the review (Phase 4)
        # Sensitive paths (Phase 3 security-review heuristic) — any changed path matching → security pass.
        "sensitive_globs": ["supabase/**", "stripe*", "*billing*", "*auth*", "*sync*"],
    },
    "harness": None,
    "secret_patterns": [
        r"AKIA[0-9A-Z]{16}",
        r"sk-[A-Za-z0-9]{20,}",
        r"ghp_[A-Za-z0-9]{36}",
        r"xox[baprs]-[0-9A-Za-z-]{10,}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"AIza[0-9A-Za-z_-]{35}",
    ],
}

# Files the index walks and the tier each maps to. Globs are relative to .context/.
TIER_GLOBS = [
    ("hot", "state.md"),
    ("warm", "notes.md"),            # unbounded working detail — on-demand, not auto-loaded
    ("warm", "tasks.md"),            # task/kanban board — markdown source of truth (Phase 11)
    ("durable", "durable/manifest.md"),
    ("durable", "durable/*.md"),
    ("warm", "journal.md"),
    ("cold", "journal-archive/*.md"),
    ("cold", "state-archive/*.md"),   # FIFO'd state.md ## Now history bullets (still searchable)
    ("cold", "done-archive.md"),      # auto-archived done cards (FIFO cap; still searchable)
    ("distilled", "learnings.md"),
    ("distilled", "decisions.md"),
    ("handoff", "handoff.md"),
    ("hot", "activity.md"),          # auto, mechanical — always-current digest
    ("cold", "auto/*.md"),           # auto, mechanical — full per-turn capture
]

# Globs rewritten mechanically by `celeborn capture` on EVERY turn (activity digest + per-turn
# snapshots). They are still INDEXED (searchable via `celeborn search`), but the staleness
# heuristic ignores them: they churn every turn regardless of whether any durable, user-meaningful
# content changed, so counting them would make the index perpetually "stale" inside any live session.
MECHANICAL_GLOBS = {"activity.md", "auto/*.md"}

REQUIRED_FILES = [
    "state.md",
    "notes.md",
    "session.json",
    "journal.md",
    "learnings.md",
    "decisions.md",
    "handoff.md",
    "durable/manifest.md",
]


# --------------------------------------------------------------------------- utils

def now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def now_stamp() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def find_context_root(start: Path) -> Path | None:
    """Walk up from `start` looking for a `.context/` directory."""
    start = start.resolve()
    for d in [start, *start.parents]:
        if (d / CONTEXT_DIRNAME).is_dir():
            return d / CONTEXT_DIRNAME
    return None


def require_context(args) -> Path:
    ctx = find_context_root(Path(getattr(args, "path", ".") or "."))
    if ctx is None:
        die("No .context/ found here or in any parent. Run `celeborn init` first.")
    return ctx


def _global_context() -> Path:
    """The home-level capture sink: ~/.context. Used when a session runs outside any repo that has a
    .context/, so no session goes unrecorded (the hybrid model)."""
    return Path.home() / CONTEXT_DIRNAME


def _scaffold_global(gctx: Path) -> Path:
    """Create the MINIMAL global capture sink (not a full `init`): just auto/, a metrics.json for the
    capture cursor, and a .celebornrc giving the record a stable sync identity ("global"). No authored
    tiers — this sink only holds the Automatic Context Record."""
    (gctx / "auto").mkdir(parents=True, exist_ok=True)
    if not (gctx / METRICS_NAME).is_file():
        _save_metrics(gctx, dict(METRICS_TEMPLATE))
    rc = gctx / RC_NAME
    if not rc.is_file():
        rc.write_text(json.dumps({"sync": {"project_name": "global"}}, indent=2) + "\n")
    return gctx


def find_or_create_context(args) -> Path:
    """Capture-only resolution (hybrid): use the repo's .context/ when the cwd is inside one and
    --global wasn't forced; otherwise fall back to the global ~/.context sink, scaffolding on demand.
    Unlike require_context this NEVER dies — every session gets recorded somewhere."""
    if not getattr(args, "global_", False):
        ctx = find_context_root(Path(getattr(args, "path", ".") or "."))
        if ctx is not None:
            return ctx
    return _scaffold_global(_global_context())


def load_config(ctx: Path) -> dict:
    cfg = dict(DEFAULTS)
    rc = ctx / RC_NAME
    if rc.is_file():
        try:
            cfg.update(json.loads(rc.read_text()))
        except json.JSONDecodeError as e:
            warn(f"{RC_NAME} is not valid JSON ({e}); using defaults.")
    return cfg


def _update_config(ctx: Path, **kv) -> None:
    """Merge keys into the project's `.celebornrc` (created if absent), preserving existing keys. Values
    of None are skipped. Best-effort: a malformed rc is replaced rather than crashing the caller."""
    rc = ctx / RC_NAME
    data = {}
    if rc.is_file():
        try:
            data = json.loads(rc.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data.update({k: v for k, v in kv.items() if v is not None})
    rc.write_text(json.dumps(data, indent=2) + "\n")


def _advisor_config(ctx: Path) -> dict:
    """Normalized skill-advisor settings (t70). Starts from the DEFAULTS `advisor` block, overlays the
    nested `advisor: {...}` from .celebornrc (a flat `cfg.update` would otherwise drop the unspecified
    sub-keys), then honors the legacy flat keys (`advisor_enabled`, `advisor_permission_bloat_min`) so
    older rc files keep working. Returns {enabled, max_per_session, permission_bloat_min, sensitive_globs}."""
    cfg = load_config(ctx)
    out = dict(DEFAULTS["advisor"])
    block = cfg.get("advisor")
    if isinstance(block, dict):
        for k, v in block.items():
            if v is not None:
                out[k] = v
    if "advisor_enabled" in cfg:                       # legacy flat key (older rc) still wins
        out["enabled"] = bool(cfg["advisor_enabled"])
    if "advisor_permission_bloat_min" in cfg:
        out["permission_bloat_min"] = int(cfg["advisor_permission_bloat_min"])
    out["enabled"] = bool(out.get("enabled", True))
    out["max_per_session"] = max(0, int(out.get("max_per_session", 1) or 0))
    out["permission_bloat_min"] = int(out.get("permission_bloat_min", 10) or 0)
    out["review_min_files"] = max(1, int(out.get("review_min_files", 3) or 3))
    out["parallelize_min_files"] = max(1, int(out.get("parallelize_min_files", 12) or 12))
    if not isinstance(out.get("sensitive_globs"), list):
        out["sensitive_globs"] = list(DEFAULTS["advisor"]["sensitive_globs"])
    return out


def _sanitize_project_slug(raw: str) -> str:
    """Safe token for card markers: letters, digits, underscore, dot, hyphen."""
    s = re.sub(r"[^\w.-]+", "-", (raw or "").strip()).strip("-") or "project"
    return s[:64]


def _short_slug(raw: str, n: int = 4) -> str:
    """Derive a short, readable qualifier from a repo folder name → e.g. `celeborn` → `cele`.
    Keeps the first `n` alphanumerics (so `CELE-t84` is snappy yet traceable); fleet dedup
    (`_dedupe_slug`) resolves any cross-project collisions to `cele`, `cele-2`, … Falls back to the
    full sanitized name when stripping leaves nothing (e.g. an all-symbol folder name)."""
    head = re.sub(r"[^A-Za-z0-9]+", "", (raw or "")).lower()[:n]
    return head or _sanitize_project_slug(raw)


def _repo_root_from_ctx(ctx: Path) -> Path:
    """Parent of a resolved `.context/` dir — the repo folder that owns the board."""
    return ctx.resolve().parent


def project_slug(ctx: Path) -> str:
    """Per-repo qualifier for project-qualified card ids/markers. An explicit project_slug in
    .celebornrc is authority (used verbatim, only sanitized) — the escape hatch for a longer/custom
    prefix. With no explicit value we derive a short 4-char prefix from the repo folder name."""
    explicit = (load_config(ctx).get("project_slug") or "").strip()
    if explicit:
        return _sanitize_project_slug(explicit)
    return _short_slug(_repo_root_from_ctx(ctx).name or "project")


def _slug_matches(a: str, b: str) -> bool:
    """Whether two qualifiers name the same project, tolerant of the short-prefix derivation so a
    legacy long-form ref (`celeborn/tN`) still matches a derived short board (`cele`). Exact
    sanitized match, or equal short prefixes — the latter can rarely coincide for two projects sharing
    a 4-char head, but those are fleet-deduped at register, and this only governs a non-fatal warn."""
    a, b = _sanitize_project_slug(a).lower(), _sanitize_project_slug(b).lower()
    return a == b or _short_slug(a) == _short_slug(b)


def _dedupe_slug(base: str, taken) -> str:
    """Return `base` if its qualifier is free across the fleet, else the first available `base-N` (N≥2).
    Comparison is case-insensitive because the displayed qualifier is upper-cased (SLUG-tN), so `cele`
    and `CELE` collide. `taken` is the slugs already claimed by other registered projects."""
    taken_u = {str(t).strip().upper() for t in taken if str(t).strip()}
    if base.upper() not in taken_u:
        return base
    n = 2
    while f"{base}-{n}".upper() in taken_u:
        n += 1
    return f"{base}-{n}"


# --------------------------------------------------------------------------- board port (de-collide)
#
# The kanban viewer (board/) is one web server per project. A single hard-coded port would collide
# the moment a second Celeborn repo runs its board. So each project gets its OWN stable port: an
# explicit `board_port` in .celebornrc wins; otherwise it's derived deterministically from the
# project path. "Stable" = same project → same port every run, so the URL is bookmarkable and the
# orient line is reliable. We use hashlib (NOT the built-in hash(), which is salted per-process and
# would hand out a different port every run).
BOARD_PORT_BASE = 3141
BOARD_PORT_SPAN = 800   # ports 3141–3940 — a recognizable band, clear of common dev ports (3000/8080/5173…)


def _derive_board_port(project_dir: Path) -> int:
    import hashlib
    key = str(Path(project_dir).resolve()).encode("utf-8", "surrogatepass")
    return BOARD_PORT_BASE + int(hashlib.sha1(key).hexdigest(), 16) % BOARD_PORT_SPAN


# CELE-t170: one shared server serves every project's board on a single port — `/` is the fleet home,
# `/board/<slug>` is a project's board. The per-repo hashed port (`_derive_board_port`) is retired from
# the default path; it stays defined above for the stable-port unit test and any legacy explicit override.
SHARED_BOARD_PORT = BOARD_PORT_BASE  # 3141


def board_port(ctx: Path) -> int:
    """The board's port. Defaults to the shared 3141 — one server for the whole fleet (CELE-t170).
    An explicit, valid `board_port` in .celebornrc still wins as an advanced/legacy override."""
    p = load_config(ctx).get("board_port")
    if isinstance(p, int) and 1 <= p <= 65535:
        return p
    return SHARED_BOARD_PORT


def board_url(ctx: Path) -> str:
    """This project's board on the shared server: http://localhost:<port>/board/<slug>. `/` is the
    fleet home; each project lives under its slug so one server serves them all without data bleed."""
    return f"http://localhost:{board_port(ctx)}/board/{project_slug(ctx)}"


def _board_live(port: int, timeout: float = 0.15) -> bool:
    """True if something is already listening on localhost:<port>. Fast and forgiving (short timeout,
    never raises) — safe to call from a hook / on every orient."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- ensure-on-orient
#
# The board should be effectively always-on: if the SessionStart hook resolves this project's port
# and finds nothing listening, it starts the viewer DETACHED (own session/process group, stdio to a
# local log) and returns immediately — the hook must never block on `next dev`'s multi-second boot.
# `next dev` takes a few seconds to bind, so a naive port probe would relaunch on the next orient
# while the first is still booting. A local-only pidfile (`.context/.board.pid`) records the PID +
# port we launched; while that PID is alive we report `booting`, not `down`, and don't double-launch.
BOARD_PIDFILE = ".board.pid"
BOARD_LOG = ".board.log"


BOARD_DIR_PIN = "board-dir"  # under _config_dir() — records where the board app actually lives


def _board_dir_pin_path() -> Path:
    return _config_dir() / BOARD_DIR_PIN


def _record_board_dir(board_dir: Path) -> None:
    """Best-effort pin of the resolved board app location into the machine config, so an installed
    CLI (whose REPO_ROOT is site-packages, with no board/ — CELE-t235) can find the app that a
    source-checkout run already proved out. Never raises — this rides orient hooks."""
    try:
        pin = _board_dir_pin_path()
        pin.parent.mkdir(parents=True, exist_ok=True)
        current = pin.read_text(encoding="utf-8").strip() if pin.is_file() else ""
        if current != str(board_dir):
            pin.write_text(str(board_dir) + "\n", encoding="utf-8")
    except OSError:
        pass


def _board_dir() -> Path:
    """The Next.js kanban viewer that ships with this Celeborn install. One app, launched per-project
    on a de-collided port and pointed at the orienting repo's tasks via CELEBORN_TASKS_JSON.

    Resolution order (CELE-t235 — an installed CLI's REPO_ROOT lands in site-packages, where board/
    doesn't exist): $CELEBORN_BOARD_DIR override → board/ next to this script (source checkout;
    re-records the machine pin so installs self-heal on the next source-side orient) → the pinned
    path in ~/.config/celeborn/board-dir. A candidate counts only if its package.json exists."""
    import os
    override = os.environ.get("CELEBORN_BOARD_DIR", "").strip()
    if override:
        cand = Path(override).expanduser()
        if (cand / "package.json").is_file():
            return cand
    cand = REPO_ROOT / "board"
    if (cand / "package.json").is_file():
        _record_board_dir(cand)
        return cand
    try:
        pinned = Path(_board_dir_pin_path().read_text(encoding="utf-8").strip()).expanduser()
        if (pinned / "package.json").is_file():
            return pinned
    except OSError:
        pass
    return cand  # nonexistent — _board_runner() returns None and callers report 'unavailable'


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a live process. Signal 0 just probes — it sends nothing."""
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # exists but not ours to signal — still alive
    except OSError:
        return False
    return True


# CELE-t170: the pidfile + log are MACHINE-GLOBAL (under ~/.config/celeborn), not per-`.context`,
# because there is now exactly ONE shared board server for the whole machine. This is what makes the
# supervisor a singleton: concurrent orients across repos all consult the same pidfile.
def _board_pidfile_path() -> Path:
    return _config_dir() / BOARD_PIDFILE


def _board_log_path() -> Path:
    return _config_dir() / BOARD_LOG


def _read_board_pidfile() -> dict:
    try:
        d = json.loads(_board_pidfile_path().read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _board_booting(port: int) -> bool:
    """True if the shared board WE launched for this port is still alive (presumably mid-boot, not yet
    bound). Machine-global — any repo's orient sees the same in-flight launch and won't double-spawn."""
    d = _read_board_pidfile()
    pid = d.get("pid")
    return isinstance(pid, int) and d.get("port") == port and _pid_alive(pid)


def _board_runner(board_dir: Path) -> list[str] | None:
    """The argv that starts the viewer, or None if prerequisites are missing (no app dir, deps not
    installed, or no `npm` on PATH) — in which case ensure-on-orient is a quiet no-op."""
    import shutil
    if not (board_dir / "package.json").is_file():
        return None
    if not (board_dir / "node_modules").is_dir():
        return None            # `npm install` in board/ not run yet — nothing to launch
    npm = shutil.which("npm")
    if not npm:
        return None
    return [npm, "run", "dev"]


# --------------------------------------------------------------------------- zero-npm onboarding fallback
#
# The full board is a Next.js app: it needs Node.js + npm + an `npm install`. A non-coder's first
# machine has none of these, so `_board_runner` returns None and `celeborn board` used to print
# "can't start — no board app, deps not installed, or npm missing" and quit — a dead end for exactly
# the person we most want to onboard (CELE-t229; evidence: the Kasia first-exterior transcript).
#
# The escape hatch: when the real board is unavailable, `celeborn board` serves a self-contained HTML
# onboarding page from the Python *stdlib* `http.server` (python is guaranteed present — they just ran
# `celeborn`), whose STEP 1 is REGISTER and which always carries a live Support button. Never a silent
# no-op. The register link and the support link are hosted, so they work before any local board exists
# and before the user has an account.

CELEBORN_REGISTER_URL = "https://celeborncode.ai"     # hosted landing → "Get started free" → GitHub OAuth
CELEBORN_SUPPORT_URL = "https://support.thot.ai"       # live hosted support — reachable UNREGISTERED


def _onboarding_html(ctx: Path, *, reason: str = "",
                     register_url: str = CELEBORN_REGISTER_URL,
                     support_url: str = CELEBORN_SUPPORT_URL) -> str:
    """The self-contained onboarding page served when the Next.js board can't run (CELE-t229). Pure
    (no I/O beyond reading the project name) so it's cheap to unit-test. STEP 1 is register; a
    prominent Support button opens hosted support and works for a stuck, unregistered user."""
    from html import escape as html_escape
    name = html_escape(_project_name(ctx))
    why = html_escape(reason or "Node.js and npm weren't found on this machine")
    reg = html_escape(register_url, quote=True)
    sup = html_escape(support_url, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Welcome to Celeborn — get started</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         color:#e7e9ee; background:radial-gradient(1200px 700px at 50% -10%, #1b2233 0%, #0c0f16 60%); }}
  .wrap {{ max-width:680px; margin:0 auto; padding:56px 24px 96px; }}
  .brand {{ display:flex; align-items:center; gap:12px; font-size:15px; letter-spacing:.06em;
            text-transform:uppercase; color:#9aa4b8; }}
  h1 {{ font-size:30px; line-height:1.2; margin:20px 0 8px; color:#fff; }}
  .why {{ color:#9aa4b8; margin:0 0 32px; }}
  .why code {{ background:#161c28; padding:2px 6px; border-radius:5px; color:#c8d0e0; }}
  .step {{ background:#131824; border:1px solid #232b3b; border-radius:14px; padding:22px 22px;
           margin:14px 0; }}
  .step .n {{ display:inline-flex; align-items:center; justify-content:center; width:26px; height:26px;
              border-radius:50%; background:#2a3346; color:#cfd6e6; font-size:14px; font-weight:600;
              margin-right:10px; }}
  .step h2 {{ display:flex; align-items:center; font-size:18px; margin:0 0 10px; color:#fff; }}
  .step p {{ margin:0 0 14px; color:#aab3c5; }}
  .step ol {{ margin:0; padding-left:20px; color:#aab3c5; }}
  .step ol li {{ margin:6px 0; }}
  .step code {{ background:#0c111b; border:1px solid #232b3b; padding:2px 7px; border-radius:6px;
                color:#7ee0b8; font-size:14px; }}
  a.btn {{ display:inline-block; text-decoration:none; font-weight:600; border-radius:10px;
           padding:12px 20px; }}
  a.primary {{ background:linear-gradient(180deg,#5b8cff,#3f6fe0); color:#fff;
               box-shadow:0 6px 20px rgba(63,111,224,.35); }}
  a.primary:hover {{ filter:brightness(1.07); }}
  a.support {{ position:fixed; right:22px; bottom:22px; background:#10b981; color:#04140d;
               box-shadow:0 8px 26px rgba(16,185,129,.4); }}
  a.support:hover {{ filter:brightness(1.06); }}
  .support-note {{ color:#7f8aa0; font-size:13px; margin-top:10px; }}
  footer {{ margin-top:36px; color:#5c6578; font-size:13px; }}
  footer code {{ color:#8b93a7; }}
</style>
</head>
<body>
  <div class="wrap">
    <div class="brand"><span style="font-size:20px">🏹</span> Celeborn</div>
    <h1>Welcome — let's get you onto the board.</h1>
    <p class="why">The full local board is a small web app that needs Node.js to run, and {why}.
       That's fine — you don't need it to get started. Here's the quickest path.</p>

    <div class="step">
      <h2><span class="n">1</span> Create your free account</h2>
      <p>Register once, then your board follows you to any device — nothing to install.</p>
      <a class="btn primary" href="{reg}">Get started free →</a>
    </div>

    <div class="step">
      <h2><span class="n">2</span> (Optional) Run the full board locally</h2>
      <p>Prefer everything on your own machine? Install Node.js, then:</p>
      <ol>
        <li>Install <strong>Node.js 18+</strong> from <a href="https://nodejs.org" style="color:#7fb0ff">nodejs.org</a> (this also installs <code>npm</code>).</li>
        <li>In Celeborn's <code>board/</code> folder, run <code>npm install</code> once.</li>
        <li>Run <code>celeborn board</code> again — the full board opens here automatically.</li>
      </ol>
    </div>

    <footer>
      Project: <code>{name}</code> &nbsp;·&nbsp; This page is served locally by Celeborn (no Node.js required).
    </footer>
  </div>

  <a class="support" href="{sup}" target="_blank" rel="noopener">💬 Talk to support</a>
</body>
</html>
"""


def _serve_onboarding(port: int, url: str, html_body: str, *,
                      open_tab: bool = True, make_server=None) -> None:
    """Serve the onboarding page (CELE-t229) on `port` from the Python stdlib — no npm/node. Foreground
    and blocking (Ctrl-C to stop), matching how a non-coder expects `celeborn board` to behave. Every
    GET returns the one page, so opening any path works. `make_server` is injectable so tests exercise
    the handler over a real socket without hard-coding the fixed board port. Never raises on a browser
    failure; a bind failure (port already taken) is reported, not fatal."""
    import http.server
    body = html_body.encode("utf-8")

    class _OnboardingHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                       # noqa: N802 — stdlib naming
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):              # keep the foreground terminal quiet
            pass

    if make_server is None:
        def make_server():
            return http.server.ThreadingHTTPServer(("127.0.0.1", port), _OnboardingHandler)
    try:
        server = make_server()
    except OSError as e:
        print(f"celeborn: onboarding server couldn't bind {url} ({e}) — "
              f"open {CELEBORN_REGISTER_URL} to register, or {CELEBORN_SUPPORT_URL} for support",
              file=sys.stderr)
        return
    server.RequestHandlerClass = _OnboardingHandler
    if open_tab and _init_is_interactive():
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:                       # noqa: BLE001 — opening a tab must never fail the command
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🏹 onboarding server stopped.")
    finally:
        try:
            server.shutdown()
        except Exception:                       # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:                       # noqa: BLE001
            pass


# --------------------------------------------------------------------------- self-healing supervisor
#
# A bare `next dev` dies for all sorts of reasons mid-session (a route 500, an OOM, a dead file-
# watcher, the historical `.next` build-clobber). When it did, NOTHING relaunched it until the next
# SessionStart — so the user's open tab hit "nothing is listening" for the rest of the session
# (CELE-t99). The fix: we don't detach `next dev` directly. We detach a tiny SUPERVISOR that runs
# `next dev` as a child and relaunches it on every exit with bounded exponential backoff. The
# supervisor is the PID recorded in `.board.pid`; any crash of the dev server self-heals in seconds.
# It gives up loudly only after N restarts that each died near-instantly (a genuinely broken build —
# don't hot-loop). The supervisor re-invokes THIS script as `celeborn board --supervise`.

_BOARD_SUPERVISOR_CHILD = None   # the live `next dev` child, so the signal handler can reap it


def _install_supervisor_signals() -> None:
    """Best-effort: when the supervisor is terminated, take its `next dev` child down with it so we
    don't leak an orphaned dev server. Never raises (signal may be unavailable in odd contexts)."""
    import signal

    def _term(signum, frame):  # noqa: ANN001
        child = _BOARD_SUPERVISOR_CHILD
        try:
            if child is not None:
                child.terminate()
        except Exception:       # noqa: BLE001
            pass
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _term)
        signal.signal(signal.SIGINT, _term)
    except Exception:           # noqa: BLE001
        pass


def _board_supervise(runner: list[str], port: int, board_dir: Path, *,
                     spawn=None, sleeper=None, clock=None,
                     max_rapid: int = 5, rapid_window_s: float = 10.0,
                     backoff_cap_s: float = 30.0) -> int:
    """The self-healing core: run the viewer (`next dev`) as a child and relaunch it whenever it
    exits, with bounded exponential backoff. This is the process detached and recorded in the
    machine-global `.board.pid`, so any crash of the dev server self-heals within seconds while the
    tab lives. One shared server on `port` serves every project (CELE-t170) — no per-repo tasks path;
    the route resolves the project from the URL. Gives up (returns) only after `max_rapid` restarts
    that each died inside `rapid_window_s` — a genuinely broken build we shouldn't hot-loop.
    `spawn`/`sleeper`/`clock` are injectable so tests drive the loop without real processes or real
    sleeps. Returns the number of child exits seen."""
    import os, time
    env = {**os.environ, "PORT": str(port)}
    if spawn is None:
        import subprocess

        def spawn():
            return subprocess.Popen(runner, cwd=str(board_dir), env=env, stdin=subprocess.DEVNULL)
    sleeper = sleeper or time.sleep
    clock = clock or time.monotonic
    _install_supervisor_signals()
    global _BOARD_SUPERVISOR_CHILD
    backoff = 1.0
    rapid = 0
    restarts = 0
    while True:
        started = clock()
        try:
            proc = spawn()
        except Exception as e:                  # noqa: BLE001 — launch failed; nothing to supervise
            print(f"🏹 board supervisor: launch failed ({e}) — exiting", flush=True)
            return restarts
        _BOARD_SUPERVISOR_CHILD = proc
        rc = proc.wait()
        ran = clock() - started
        restarts += 1
        print(f"🏹 board supervisor: next dev (rc={rc}) exited after {ran:.0f}s — restarting", flush=True)
        if ran < rapid_window_s:
            rapid += 1
            if rapid >= max_rapid:
                print(f"🏹 board supervisor: {rapid} rapid failures inside {rapid_window_s:.0f}s — "
                      "giving up (fix the build, then `celeborn board --start`)", flush=True)
                return restarts
            backoff = min(backoff * 2, backoff_cap_s)
        else:
            rapid = 0                            # a healthy run resets the rapid-failure budget
            backoff = 1.0
        sleeper(backoff)


def _run_board_supervisor(args) -> None:
    """`celeborn board --supervise` — the detached entrypoint `_spawn_board` launches. Resolves the
    viewer argv itself (no project context needed) and runs the restart loop in the foreground; its
    stdio is the inherited `.board.log`."""
    board_dir = _board_dir()
    runner = _board_runner(board_dir)
    if runner is None:
        print("🏹 board supervisor: viewer unavailable (no app / deps / npm) — exiting", flush=True)
        return
    _board_supervise(runner, int(args.supervise_port), board_dir)


def _spawn_board(board_dir: Path, runner: list[str], port: int) -> int:
    """Start the SUPERVISOR detached and return its PID (the PID we record in the machine-global
    `.board.pid`). Own session (setsid) so it outlives the hook and the spawning session; stdio to the
    machine-global `.board.log`. The supervisor runs `next dev` (one shared server on `port`, CELE-t170)
    as a child and relaunches it on crash. No per-repo tasks path — the route resolves the project from
    the URL. Separated out so tests can stub the actual process launch."""
    import os, subprocess, sys
    env = {**os.environ, "PORT": str(port)}
    _config_dir().mkdir(parents=True, exist_ok=True)
    log = open(_board_log_path(), "ab", buffering=0)
    supervisor = [sys.executable, str(Path(__file__).resolve()), "board",
                  "--supervise", "--supervise-port", str(port)]
    try:
        proc = subprocess.Popen(
            supervisor, cwd=str(board_dir), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )
    finally:
        log.close()
    return proc.pid


def ensure_board(ctx: Path, *, launch: bool = True) -> dict:
    """Probe the shared board port; if it's down, start the ONE viewer (detached) so it's effectively
    always running on localhost:3141 for the whole fleet (CELE-t170). The seam the SessionStart hook
    calls on every orient — from any repo. Idempotent: once live, every other repo's orient is a no-op.

    Returns a status dict {port, url, live, action, reason?} where `action` is one of:
      live        already listening — nothing to do
      booting     the shared board we launched is still coming up (don't double-launch)
      started     just launched it (pid in the dict)
      off         autostart disabled in .celebornrc (board_autostart=false)
      no-tasks    this project doesn't use the kanban (no tasks.md) — stay quiet
      unavailable can't launch (no board app / deps not installed / no npm)
    Never raises — a hook must never break the user's turn."""
    import os
    port = board_port(ctx)
    url = board_url(ctx)
    base = {"port": port, "url": url}
    if _board_live(port):
        return {**base, "live": True, "action": "live"}
    if not bool(load_config(ctx).get("board_autostart", True)):
        return {**base, "live": False, "action": "off", "reason": "board_autostart=false"}
    if not (ctx / "tasks.md").is_file():
        return {**base, "live": False, "action": "no-tasks", "reason": "no tasks.md — kanban unused"}
    if _board_booting(port):
        return {**base, "live": False, "action": "booting"}
    board_dir = _board_dir()
    runner = _board_runner(board_dir)
    if runner is None:
        return {**base, "live": False, "action": "unavailable",
                "reason": "no board app, deps not installed, or npm missing"}
    if not launch:
        return {**base, "live": False, "action": "down"}
    # Machine-global singleton claim: only ONE concurrent orient (across any repo/thread) may spawn the
    # shared server. O_EXCL makes the check-and-claim atomic; a stale claim (dead pid) is stolen. The
    # fixed port is the ultimate backstop — a losing supervisor can't bind 3141 and backs off.
    try:
        _config_dir().mkdir(parents=True, exist_ok=True)
        pidfile = _board_pidfile_path()
        try:
            fd = os.open(str(pidfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            rec = _read_board_pidfile()
            rp = rec.get("pid")
            if isinstance(rp, int) and _pid_alive(rp):
                return {**base, "live": False, "action": "booting"}
            try:
                os.unlink(str(pidfile))
            except OSError:
                pass
            fd = os.open(str(pidfile), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            pid = _spawn_board(board_dir, runner, port)
            os.write(fd, (json.dumps({"pid": pid, "port": port, "at": now_iso()}) + "\n").encode())
        finally:
            os.close(fd)
        return {**base, "live": False, "action": "started", "pid": pid}
    except Exception as e:                      # noqa: BLE001 — never let a launch failure break orient
        return {**base, "live": False, "action": "unavailable", "reason": str(e)}


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.strip().lower())
    s = re.sub(r"[\s_]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")


def die(msg: str, code: int = 1):
    print(f"celeborn: error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg: str):
    print(f"  ! {msg}")


def ok(msg: str):
    print(f"  ✓ {msg}")


def info(msg: str):
    """A neutral heads-up — not a warning or a problem (doesn't affect doctor's counts/exit code)."""
    print(f"  · {msg}")


# --------------------------------------------------------------------------- markdown parsing

def strip_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body). Only simple `key: value` pairs are parsed."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    rest = text[end + 4:].lstrip("\n")
    fm: dict = {}
    for line in raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, rest


HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z][\w-]+)")


COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def parse_sections(text: str) -> list[dict]:
    """Split markdown into sections by heading. The preamble before the first heading
    becomes a section with an empty title. HTML comments are stripped first so template
    boilerplate and instructional notes never reach the index."""
    fm, body = strip_frontmatter(text)
    body = COMMENT_RE.sub("", body)
    sections: list[dict] = []
    cur = {"title": "", "anchor": "", "level": 0, "lines": []}
    for line in body.splitlines():
        m = HEADING_RE.match(line)
        if m:
            if cur["lines"] or cur["title"]:
                sections.append(cur)
            title = m.group(2)
            cur = {"title": title, "anchor": slugify(title), "level": len(m.group(1)), "lines": []}
        else:
            cur["lines"].append(line)
    if cur["lines"] or cur["title"]:
        sections.append(cur)

    fm_tags = fm.get("tags", "")
    for s in sections:
        s["body"] = "\n".join(s["lines"]).strip()
        inline_tags = set(TAG_RE.findall(s["body"]))
        if fm_tags:
            inline_tags.update(t.strip() for t in re.split(r"[,\s]+", fm_tags) if t.strip())
        s["tags"] = " ".join(sorted(inline_tags))
        s["links"] = WIKILINK_RE.findall(s["body"])
    return sections


# --------------------------------------------------------------------------- journal entries

JOURNAL_ENTRY_RE = re.compile(r"^## ", re.MULTILINE)


def split_journal(text: str) -> tuple[str, list[str]]:
    """Return (header_block, [entry_block, ...]) where each entry starts at a `## ` line.
    `## ` lines inside HTML comments (e.g. the template's format hint) are not entries."""
    spans = [(m.start(), m.end()) for m in COMMENT_RE.finditer(text)]
    matches = [m for m in JOURNAL_ENTRY_RE.finditer(text)
               if not any(a <= m.start() < b for a, b in spans)]
    if not matches:
        return text, []
    header = text[: matches[0].start()]
    entries = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        entries.append(text[m.start():end])
    return header, entries


# --------------------------------------------------------------------------- state.md archive
#
# state.md is meant to be a tiny rewrite-in-place headline, but agents append dated `SESSION`/history
# bullets under `## Now`. These helpers treat that history as a FIFO list — parallel to journal
# entries — so the oldest can be moved into a cold, still-searchable `state-archive/` while the
# structural headline bullets (Focus / Next action / Branch) stay put.

STATE_ARCHIVE_DIRNAME = "state-archive"
_NOW_HEADING_RE = re.compile(r"^## +Now\b.*$", re.MULTILINE | re.IGNORECASE)
_NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)
_TOP_BULLET_RE = re.compile(r"^- ", re.MULTILINE)
_ISO_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")


def split_state_now(text: str):
    """Split state.md around its `## Now` section.

    Returns (before, heading, body, after) where `before` ends just before the `## Now` heading,
    `heading` is the heading line (incl. trailing newline), `body` is everything up to the next `##`
    heading (or EOF), and `after` is the remainder. Returns None if there is no `## Now` section."""
    m = _NOW_HEADING_RE.search(text)
    if not m:
        return None
    heading_end = text.find("\n", m.end())
    heading_end = len(text) if heading_end == -1 else heading_end + 1
    nxt = _NEXT_H2_RE.search(text, heading_end)
    body_end = nxt.start() if nxt else len(text)
    return text[: m.start()], text[m.start():heading_end], text[heading_end:body_end], text[body_end:]


def _now_bullets(body: str):
    """Split a `## Now` body into (preamble, [top-level bullet block, ...]). A block runs from one
    top-level `- ` line up to the next (indented continuation lines stay with their bullet)."""
    matches = list(_TOP_BULLET_RE.finditer(body))
    if not matches:
        return body, []
    preamble = body[: matches[0].start()]
    blocks = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        blocks.append(body[m.start():end])
    return preamble, blocks


def _is_history_bullet(block: str) -> bool:
    """A `## Now` bullet is archivable history if it is a dated/session log line rather than a
    structural headline bullet (Focus / Next action / Branch / Status / Pointers)."""
    return bool(_ISO_DATE_RE.search(block) or re.search(r"\bSESSION\b", block))


def plan_state_archive(text: str, keep: int):
    """Decide which `## Now` history bullets to keep vs archive.

    Returns (new_text, archived_blocks) — archived_blocks in original file order. Keeps the `keep`
    most-recent history bullets (by the latest ISO date in each; ties break toward file order, which
    is newest-first by convention), preserving their original order in the rewritten section. Returns
    (text, []) when there is no Now section or nothing is over the cap."""
    split = split_state_now(text)
    if split is None:
        return text, []
    before, heading, body, after = split
    preamble, blocks = _now_bullets(body)
    hist_idx = [i for i, b in enumerate(blocks) if _is_history_bullet(b)]
    if len(hist_idx) <= max(keep, 0):
        return text, []

    def _date_key(i):
        dates = _ISO_DATE_RE.findall(blocks[i])
        return (max(dates) if dates else "", -i)   # newest date first; -i keeps earlier-in-file on ties

    keep_set = set(sorted(hist_idx, key=_date_key, reverse=True)[:keep])
    archived = [blocks[i] for i in hist_idx if i not in keep_set]
    kept_blocks = [b for i, b in enumerate(blocks) if i not in hist_idx or i in keep_set]
    new_body = preamble + "".join(kept_blocks)
    new_text = before + heading + new_body + after
    return new_text, archived


# --------------------------------------------------------------------------- metrics

METRICS_TEMPLATE = {
    "schema": "celeborn-metrics/1",
    "tokens_saved_estimate": 0,
    "load_events": 0,
    "orient_events": 0,
    "sessions_resumed": 0,
    "compactions_bridged": 0,
    "panic_saves": 0,
    "handoffs_written": 0,
    "last_session_id": None,
    "last_orient_at": None,
    # Rolling estimate of the live context window, in tokens. Celeborn can't observe the host's
    # window directly, so this is an accumulated proxy: `record turn --tokens N` adds to it, and a
    # new session / `clear` / `compaction` resets it to roughly the Hot-tier load. `remind --auto`
    # reads it. Approximate by design — refined whenever the host supplies real numbers.
    "context_estimate": 0,
    "last_remind_estimate": 0,
    # Cursors for deterministic transcript capture (`celeborn capture`). `captures` is keyed by
    # session id: each Claude session keeps its OWN byte offset, auto file, and running totals, so
    # concurrent or alternating sessions sharing one metrics.json (notably the global ~/.context
    # sink) can't stomp each other's offset and force a full re-read every turn. `capture` mirrors
    # the most-recently-active session — a back-compat slot and the fallback the heartbeat/statusline
    # read when no session id is supplied. tokens_session/idle_streak drive the per-turn `--note`
    # heartbeat (kept unique so Claude Code never suppresses it as a duplicate systemMessage).
    "captures": {},
    "capture": {"session_id": None, "offset": 0, "last_uuid": None, "file": None,
                "tokens_session": 0, "idle_streak": 0, "last_delta": 0},
    # Skill advisor (t70): per-session nudge throttle (`last_notice_session` + `notices_this_session`,
    # capped at advisor.max_per_session), user-dismissed intent ids, and the permission-friction ledger
    # the board surfaces. `permission_rules_generalized` is cumulative; `skipped_bottlenecks` is the last
    # apply's remaining (un-widenable) literals by family, with `_total` the aggregate the economy bar shows.
    "advisor": {"last_notice_session": None, "notices_this_session": 0, "dismissed": [],
                "permission_rules_generalized": 0,
                "skipped_bottlenecks": {}, "skipped_bottlenecks_total": 0, "last_applied_at": None},
    # Quality gates (t70 Phase 2): per-session "a test-relevant file was edited this turn" marker. The
    # post-edit hook sets `dirty_session` when scripts/** or tests/** changes; the quality-stop hook runs
    # the full suite ONCE when it sees its own session here, then clears it — keeping the ~90s suite off
    # the per-edit path.
    "quality": {"dirty_session": None},
    # CMM (codebase-memory-mcp, CELE-t92) economics. `prompts_auto_allowed` is the running ESTIMATE of
    # permission interruptions CMM eliminated: each capture counts the agent's calls to a CMM-pre-cleared
    # tool (the read-only `mcp__codebase-memory-mcp__*` set + the Grep/Glob engage added) — every one is a
    # structural query that flowed without an "Allow"/"Always allow" click, in place of a prompting
    # bash/grep shell-out. Only accrues while CMM is engaged (provenance-gated in celeborn_cmm).
    "cmm": {"prompts_auto_allowed": 0},
    # Permission allow-list economics (t100). `prompts_auto_allowed` is the running tally of prompts the
    # settings.json allow-list eliminated: each capture counts the agent's tool calls that matched an
    # allow rule (the safe baseline `wire --global` ships, plus any rule the user added) and so ran
    # without an "Allow"/"Always allow" click — Bash commands under a `Bash(<prefix>:*)` rule and
    # built-ins (Read/Glob/Grep/…) present verbatim. Excludes CMM's provenance-credited tools so the
    # two buckets never double-count.
    "permissions": {"prompts_auto_allowed": 0},
    # Active-agents bridge (CELE-t131). Maps a Claude session id → {owner, task, at} the moment that
    # session CLAIMS a card (the claim-on-receipt hook passes its session id). `celeborn agents` joins
    # this against the live transcripts to attribute each active context window to a handle + DOING
    # card. Pruned to the most-recent sessions like `captures`; absence just means an unattributed
    # session (shown by its short id, never a raw uuid).
    "agent_sessions": {},
}


def _est_tokens(text: str, cpt: int) -> int:
    """Rough token estimate from character count (~cpt chars/token)."""
    return (len(text) + cpt - 1) // max(1, cpt)


def _measure(ctx: Path, cpt: int) -> tuple[int, int]:
    """Return (hot_tokens, total_memory_tokens). Total = all knowledge an agent would otherwise
    carry if it naively loaded the whole .context/ — excludes derived/config/handoff files."""
    def toks(p: Path) -> int:
        try:
            return _est_tokens(p.read_text(errors="ignore"), cpt)
        except OSError:
            return 0

    hot = sum(toks(ctx / rel) for rel in HOT_FILES if (ctx / rel).is_file())
    skip = {INDEX_NAME, METRICS_NAME, RC_NAME, "handoff.md"}
    total = 0
    for p in ctx.rglob("*"):
        if p.is_file() and p.name not in skip and p.suffix in (".md", ".json"):
            total += toks(p)
    return hot, total


def _load_metrics(ctx: Path) -> dict:
    p = ctx / METRICS_NAME
    m = dict(METRICS_TEMPLATE)
    if p.is_file():
        try:
            m.update(json.loads(p.read_text()))
        except json.JSONDecodeError:
            warn(f"{METRICS_NAME} unreadable; starting fresh.")
    return m


def _save_metrics(ctx: Path, m: dict):
    (ctx / METRICS_NAME).write_text(json.dumps(m, indent=2) + "\n")


def _has_memory(ctx: Path, cpt: int) -> bool:
    hot, total = _measure(ctx, cpt)
    return total > hot


def _credit_savings(ctx: Path, m: dict, cpt: int) -> int:
    """Credit one load event: tokens saved by loading Hot instead of all of .context/."""
    hot, total = _measure(ctx, cpt)
    saved = max(0, total - hot)
    m["tokens_saved_estimate"] += saved
    m["load_events"] += 1
    return saved


def _orient_is_new_session(m: dict, sid: str, cfg: dict) -> bool:
    """Decide whether this orient is a distinct session (vs. a re-orient within the same one)."""
    if sid:
        return sid != (m.get("last_session_id") or "")
    last = m.get("last_orient_at")
    if not last:
        return True
    try:
        age = (_dt.datetime.now() - _dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%S")).total_seconds()
    except (ValueError, TypeError):
        return True
    return age >= cfg["orient_dedupe_seconds"]


def restarts_avoided(m: dict) -> int:
    return m.get("sessions_resumed", 0) + m.get("compactions_bridged", 0)


def metrics_summary(ctx: Path) -> list[str]:
    m = _load_metrics(ctx)
    saved = m["tokens_saved_estimate"]
    lines = [
        f"tokens saved: ~{saved:,} (est.) — Hot tier vs. loading all of .context/, over {m['load_events']} load event(s)",
        f"restarts avoided: {restarts_avoided(m)}  "
        f"({m['sessions_resumed']} session resume(s) + {m['compactions_bridged']} compaction(s) bridged)",
    ]
    return lines


# --------------------------------------------------------------------------- smart init (read the repo on first run)

# README filenames probed in priority order (plus a case-insensitive fallback).
_README_NAMES = ("README.md", "README.rst", "README.txt", "README", "readme.md")

# (manifest filename, stack label) — first present manifest drives the headline stack + name/desc.
_MANIFESTS = [
    ("package.json", "Node/JS"),
    ("pyproject.toml", "Python"),
    ("setup.py", "Python"),
    ("setup.cfg", "Python"),
    ("requirements.txt", "Python"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("pom.xml", "Java/Maven"),
    ("build.gradle", "Java/Gradle"),
    ("Gemfile", "Ruby"),
    ("composer.json", "PHP"),
    ("Package.swift", "Swift"),
    ("pubspec.yaml", "Dart/Flutter"),
    ("CMakeLists.txt", "C/C++"),
]

_LANG_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".rs": "Rust", ".go": "Go", ".java": "Java",
    ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".c": "C", ".h": "C", ".cpp": "C++",
    ".cc": "C++", ".cs": "C#", ".kt": "Kotlin", ".dart": "Dart", ".sh": "Shell",
    ".lua": "Lua", ".scala": "Scala", ".ex": "Elixir", ".exs": "Elixir",
}

_SCAN_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
                   ".next", "target", "vendor", ".context", ".idea", ".mypy_cache"}


def _readme_title_desc(text: str) -> tuple[str, str]:
    """First heading (project title) + first prose paragraph (description), skipping badges/HTML/fences."""
    title, desc_lines = "", []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if desc_lines:
                break  # blank line ends the first paragraph
            continue
        if line.startswith("#"):
            if not title:
                title = line.lstrip("#").strip()
            continue
        if line.startswith(("![", "[![", "<", "---", "===", "```", "|", ">", "- [")):
            continue  # badges, HTML, rules, fences, tables, quotes, ToC links
        desc_lines.append(line)
        if len(" ".join(desc_lines)) > 240:
            break
    desc = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", " ".join(desc_lines))  # [text](url) -> text
    desc = re.sub(r"[*`_]", "", desc).strip()
    if len(desc) > 240:
        desc = desc[:239].rstrip() + "…"
    return title, desc


def _scan_readme(root: Path) -> tuple[str, str]:
    candidates = [root / n for n in _README_NAMES]
    candidates += [p for p in sorted(root.glob("*")) if p.name.lower().startswith("readme")]
    for p in candidates:
        if p.is_file():
            try:
                return _readme_title_desc(p.read_text(errors="replace"))
            except OSError:
                continue
    return "", ""


def _manifest_name_desc(fname: str, path: Path) -> tuple[str, str]:
    """Best-effort (name, description) from a build manifest. Empty strings when unparseable."""
    try:
        text = path.read_text(errors="replace")
        if fname in ("package.json", "composer.json"):
            d = json.loads(text)
            return str(d.get("name") or ""), str(d.get("description") or "")
        if fname == "go.mod":
            m = re.search(r"^module\s+(\S+)", text, re.MULTILINE)
            return ((m.group(1).rsplit("/", 1)[-1]) if m else ""), ""
        if fname in ("pyproject.toml", "Cargo.toml", "setup.cfg"):
            nm = re.search(r'(?m)^\s*name\s*=\s*["\']?([^"\'\n]+)', text)
            ds = re.search(r'(?m)^\s*description\s*=\s*["\']?([^"\'\n]+)', text)
            return (nm.group(1).strip() if nm else ""), (ds.group(1).strip() if ds else "")
    except (OSError, ValueError):
        return "", ""
    return "", ""


def _scan_manifest(root: Path) -> dict:
    result = {"stack": "", "name": "", "description": "", "manifests": []}
    for fname, label in _MANIFESTS:
        if not (root / fname).is_file():
            continue
        result["manifests"].append(fname)
        if not result["stack"]:
            result["stack"] = label
            result["name"], result["description"] = _manifest_name_desc(fname, root / fname)
    return result


def _scan_git(root: Path) -> dict:
    import subprocess  # lazy: only init's scan pays the import

    def _git(*a):
        try:
            r = subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    if not _git("rev-parse", "--is-inside-work-tree"):
        return {"branch": "", "commit_count": 0, "recent_commits": []}
    count = _git("rev-list", "--count", "HEAD")
    commits = []
    for line in _git("log", "-n5", "--pretty=format:%h\t%s").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            commits.append({"short": parts[0], "subject": parts[1]})
    return {"branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "commit_count": int(count) if count.isdigit() else 0, "recent_commits": commits}


def _scan_languages(root: Path, cap: int = 4000) -> list[str]:
    """Top ≤3 languages by source-file count — a bounded walk that skips vendor/build dirs."""
    import os
    from collections import Counter
    tally: Counter = Counter()
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            lang = _LANG_EXT.get(os.path.splitext(fn)[1].lower())
            if lang:
                tally[lang] += 1
            seen += 1
        if seen >= cap:
            break
    return [lang for lang, _ in tally.most_common(3)]


def _smart_scan(root: Path) -> dict:
    """Read-only repo probe for `celeborn init` — README, build manifest, git history, languages.
    Best-effort: every probe degrades to empty, the whole thing never raises."""
    scan = {"name": "", "description": "", "stack": "", "languages": [],
            "manifests": [], "branch": "", "commit_count": 0, "recent_commits": []}
    try:
        title, desc = _scan_readme(root)
        man = _scan_manifest(root)
        git = _scan_git(root)
        scan.update({
            "name": man["name"] or title or root.name,
            "description": desc or man["description"],
            "stack": man["stack"],
            "manifests": man["manifests"],
            "languages": _scan_languages(root),
            "branch": git["branch"],
            "commit_count": git["commit_count"],
            "recent_commits": git["recent_commits"],
        })
    except Exception:
        scan["name"] = scan["name"] or root.name
    return scan


def _scan_stack_label(scan: dict) -> str:
    bits = [scan["stack"]] if scan["stack"] else []
    bits += [l for l in scan["languages"] if l not in bits]
    return ", ".join(b for b in bits[:3] if b)


def _smart_now_block(scan: dict) -> str:
    """The derived `## Now` headline for a freshly-initialized state.md — deliberately tiny."""
    stack = _scan_stack_label(scan) or "—"
    repo = []
    if scan["branch"]:
        repo.append(f"branch `{scan['branch']}`")
    if scan["commit_count"]:
        repo.append(f"{scan['commit_count']} commit(s)")
    repo_line = " · ".join(repo) or "no git history yet"
    desc = scan["description"] or "_(no README description found — add a line on what this is)_"
    return "\n".join([
        "## Now",
        f"- **Project:** {scan['name'] or 'this project'} — {desc}",
        f"- **Stack:** {stack}  ·  **Repo:** {repo_line}",
        "- **Focus:** _Celeborn just initialized here — this is a repo snapshot, not a work focus yet. "
        "Set this to your first task and rewrite this headline._",
        "- **Next action:** _pick your first task; the full repo snapshot (recent commits, manifests) is in `notes.md`._",
        f"- **Branch:** {scan['branch'] or '<git branch>'} · **Status:** in-progress",
        "",
    ])


def _smart_notes_block(scan: dict, stamp: str) -> str:
    """The richer first-run snapshot appended to notes.md (unbounded; on-demand, not auto-loaded)."""
    lines = [f"\n## Repo snapshot — auto-captured by `celeborn init` ({stamp})",
             "_First-run orientation read straight from the repo. Trim or delete once you've set a real focus._"]
    if scan["description"]:
        lines.append(f"- **What it is:** {scan['description']}")
    stack = _scan_stack_label(scan)
    if stack:
        lines.append(f"- **Stack / languages:** {stack}")
    if scan["manifests"]:
        lines.append(f"- **Build manifests:** {', '.join(scan['manifests'])}")
    if scan["branch"] or scan["commit_count"]:
        lines.append(f"- **Git:** branch `{scan['branch'] or '?'}`, {scan['commit_count']} commit(s)")
    if scan["recent_commits"]:
        lines.append("- **Recent commits:**")
        for c in scan["recent_commits"]:
            lines.append(f"  - `{c['short']}` {c['subject']}")
    lines.append("")
    return "\n".join(lines)


def _apply_smart_state(path: Path, scan: dict):
    """Replace the template's `## Now` section (up to `## Pointers`) with the repo-derived headline."""
    text = path.read_text()
    now = _smart_now_block(scan)
    m = re.search(r"## Now\b.*?(?=\n## Pointers)", text, re.DOTALL)
    text = (text[:m.start()] + now + text[m.end():]) if m else (text.rstrip() + "\n\n" + now)
    path.write_text(text)


def _init_is_interactive() -> bool:
    """True only when init is driven by a human at a real terminal (both stdin and stdout are TTYs).
    The single gate for CELE-t121's install-time UX — prompting for a name and popping the board only
    make sense interactively. Headless/CI/test/agent installs return False, so init stays side-effect
    free and the SessionStart ensure-on-orient hook (CELE-t99) brings the board up on the next session."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _resolve_init_name(root: Path, ctx: Path, args) -> str | None:
    """Decide this project's display name on install (CELE-t121). Precedence:
      1. `--name` (explicit; for scripts/CI) — always wins.
      2. an existing `project_name` already in `.celebornrc` — kept, never re-prompted.
      3. an interactive prompt (only on a real TTY) defaulting to the repo folder name.
      4. otherwise None — the caller leaves the rc alone and `_project_name` falls back to the folder.
    Returns the name to persist, or None to leave the config untouched. Never raises."""
    explicit = (getattr(args, "name", None) or "").strip()
    if explicit:
        return explicit
    existing = (load_config(ctx).get("project_name") or "").strip()
    if existing:
        info(f"project name: {existing} (from {RC_NAME})")
        return None
    default = root.name or "this project"
    if not _init_is_interactive():
        return None  # headless/CI install — stay quiet, fall back to the folder name
    try:
        reply = input(f"  Project name for the kanban board [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return reply or default


def _ensure_tasks_md(ctx: Path) -> bool:
    """Seed an empty `tasks.md` so the kanban viewer has a board to serve (CELE-t121 — the board is
    Celeborn's UI and must be launchable right after install). Returns True if it created the file."""
    tp = _tasks_path(ctx)
    if tp.is_file():
        return False
    tp.write_text(TASKS_HEADER)
    return True


# --------------------------------------------------------------------------- first-run Orientation (t387)
#
# A brand-new user's first `celeborn init` should land them on a POPULATED board, not an empty one. So
# on the first-ever install we create a dedicated Orientation project (prefix ORIE) — a permanent
# onboarding board, independent of the cwd — and register it in the fleet so the shared server serves
# it at /board/ORIE. The ORIE cards (seeded by CELE-t388) are tutorials aimed at the coding assistant:
# they walk it through instructing the user and, in doing so, bootstrap the user's first real .context/
# + MEMORY.md (which also orients the naive model). t387 is the project anchor; the cards land in t388.
ORIENTATION_NAME = "Orientation"
ORIENTATION_SLUG = "ORIE"


def _orientation_dir() -> Path:
    """Fixed home for the Orientation tutorial project — a discoverable directory independent of the
    cwd, so a first-run user always lands somewhere populated. Overridable via
    $CELEBORN_ORIENTATION_DIR (tests / non-default layouts)."""
    import os
    override = os.environ.get("CELEBORN_ORIENTATION_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Celeborn" / "Orientation"


def _ensure_orientation_project() -> tuple[Path | None, bool]:
    """Create the dedicated Orientation (ORIE) tutorial project if it doesn't exist yet, and register
    it in the fleet so the board serves /board/ORIE. Idempotent: on re-run it finds the existing
    project (by dir presence + persisted name/slug + fleet dedup-on-path) and returns it WITHOUT
    creating a duplicate. Returns (ctx, created); ctx is None only if scaffolding was impossible (e.g.
    templates unavailable on this install). Never raises — first-run bootstrap must never break init."""
    root = _orientation_dir()
    ctx = root / CONTEXT_DIRNAME
    created = False
    try:
        if not ctx.is_dir():
            root.mkdir(parents=True, exist_ok=True)
            # Scaffold the memory tier without the dev-facing extras: no smart-scan (it's not a repo),
            # and don't open the board here — we set the ORIE slug first, then open /board/ORIE below.
            scaffold_args = argparse.Namespace(
                path=str(root), private=True, public=False, claude_md=True, agents_md=True,
                scan=False, no_cmm=False, name=ORIENTATION_NAME,
                open_board=False, open_browser=False)
            cmd_scaffold(scaffold_args)
            created = True
        # Pin the identity every time (self-heals a partial prior run that left name/slug unset).
        cfg = load_config(ctx)
        updates = {}
        if (cfg.get("project_slug") or "").strip().upper() != ORIENTATION_SLUG:
            updates["project_slug"] = ORIENTATION_SLUG
        if not (cfg.get("project_name") or "").strip():
            updates["project_name"] = ORIENTATION_NAME
        if updates:
            _update_config(ctx, **updates)
        _ensure_tasks_md(ctx)
        # Register in the fleet so the shared board serves it under /board/ORIE.
        try:
            _fleet_register_path(root)
        except SystemExit:
            pass
        return ctx, created
    except Exception:                                   # noqa: BLE001 — bootstrap must never break init
        return (ctx if ctx.is_dir() else None), created


# The spine slug + brand every seeded tutorial card carries (CELE-t388, branding per CELE-t380).
ORIENTATION_SPINE = "first-run-orie"

# The ORIE starter-card curriculum (CELE-t388) — the versioned onboarding delivery vehicle.
# Each entry seeds ONE card on the Orientation board, exactly once per install: its stable `key`
# is remembered in the ORIE `.celebornrc` (`orientation_seeded`) as a TOMBSTONE, so a re-init
# duplicates nothing and a tutorial the user deleted is never re-summoned. Shipping a NEW entry
# in a later release seeds exactly that one new card on the next `celeborn init` — this list is
# how new features/products/philosophy reach existing users' boards.
# Cards are tutorials AIMED AT THE CODING ASSISTANT: `title` is the user-facing invitation on the
# board; `notes` is the runbook the assistant executes WITH the coder (design:
# plan/cele-t388-orie-curriculum.md). `condition` is None for core cards, or the signal name a
# caller must pass in `signals` to seed it (e.g. "low_disk" — wired by the CELE-t376 installer).
ORIENTATION_CURRICULUM: list[dict] = [
    {
        "key": "welcome", "emoji": "👋", "condition": None,
        "title": "Welcome — you're the coder now",
        "tags": ["tutorial", "start"],
        "stop": "The coder has been greeted and their 'what I want to build' answer is captured "
                "for the 'Ship your first thing' card.",
        "notes": (
            "Tutorial card — these notes are a runbook for the coding assistant. When the coder "
            "opens this card, do it WITH them, live and in plain words.\n"
            "\n"
            "**Runbook:** Greet the coder warmly. In one breath, explain this board: TODO → DOING "
            "→ DONE — each card is one thing you want made. Then ask the one question that seeds "
            "everything else: **\"What do you want to build?\"** Hold their answer — the 'Ship "
            "your first thing' card turns it into a real card on their own board. Make this feel "
            "like command, not homework.\n"
            "\n"
            "**Thread to carry:** the user is the coder now. They direct; the machine operates "
            "itself.\n"
            "\n"
            "**Newbie:** you don't need to know how to code. **Veteran:** you direct the fleet; "
            "you don't operate the machine.\n"
            "\n"
            "**Artifact:** the coder's stated intent, captured for 'Ship your first thing'."
        ),
    },
    {
        "key": "tour", "emoji": "🗺️", "condition": None,
        "title": "The grand tour — every room in the house",
        "tags": ["tutorial", "tour"],
        "stop": "Every view — Tasks, Run, Settings, Stack, Pro, Multi-Device, Team — has been "
                "named and shown once.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. This is a walk-through, not a "
            "lecture: one sentence per room, open each view where the surface allows, never "
            "linger.\n"
            "\n"
            "**Runbook:** Walk the coder through each view in the board's own order: **Tasks** "
            "(the kanban — where work lives), **Run** (watch the fleet build), **Settings** (the "
            "knobs — models, keys, the Engine Room), **Stack** (the Pro stack view), **Pro** "
            "(what upgrading unlocks), **Multi-Device** (your board everywhere — hosted sync), "
            "**Team** (share the board with humans too). The whole system is theater to be "
            "watched, never homework — no room is required reading.\n"
            "\n"
            "**Newbie:** you now know where everything is. **Veteran:** full surface map — local "
            "board, hosted sync, team/multi-device story — in two minutes.\n"
            "\n"
            "**Artifact:** the coder has seen every view once."
        ),
    },
    {
        "key": "connect-model", "emoji": "⚡", "condition": None,
        "title": "Connect a model — bring the machine to life",
        "tags": ["tutorial", "models"],
        "stop": "The coder has at least one working model — a hosted key or the local weave — "
                "verified with a live reply.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. Celeborn needs a model to help "
            "the coder: nothing moves until an engine is connected. Walk them through connecting "
            "AT LEAST ONE, live — this is the vibe-critical rung; every card after it assumes an "
            "engine that answers.\n"
            "\n"
            "**Runbook:**\n"
            "1. Offer the choice in plain words — two good paths, no wrong answer:\n"
            "   • **Hosted** (Claude, OpenAI, …): the most capable brains; needs a provider "
            "account and an API key; usage is billed by the provider.\n"
            "   • **Local weave** (the free path): `celeborn weave` installs the Local Code "
            "Engine + Local Model Engine + **Pippin**, the local model — free, private, runs "
            "entirely on this machine.\n"
            "2. Hosted path: help them sign in at the provider console (e.g. "
            "console.anthropic.com or platform.openai.com), create an API key, and hand it to "
            "the engine — run `opencode auth login` with them, or use the assistant's own "
            "sign-in if they're in a hosted harness. Never store the key in a project file.\n"
            "3. Local path: run `celeborn weave` and narrate it — each piece installs from its "
            "own official source, with consent at every step. Pippin needs about 2.5 GB free.\n"
            "4. Point at the knobs: board → **Settings → Engine Room** shows both engines and "
            "the **default model** picker — the choice lives there from now on.\n"
            "5. **Verify before moving on:** send one tiny prompt through the connected model "
            "(\"say hello\") and show the coder the reply. A model that answers is the stop "
            "condition; a config that merely looks right is not.\n"
            "\n"
            "**Thread to carry:** something must always be able to move.\n"
            "\n"
            "**Newbie:** this is the brain that helps you. **Veteran:** hosted or fully local & "
            "sovereign — your call, no lock-in.\n"
            "\n"
            "**Artifact:** at least one model configured AND verified with a live reply."
        ),
    },
    {
        "key": "first-context", "emoji": "🧠", "condition": None,
        "title": "Give Celeborn a memory — your first .context/",
        "tags": ["tutorial", "memory"],
        "stop": "The coder's own project has an initialized .context/ and one real MEMORY.md fact.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. This is the core bootstrap: the "
            "coder's own project gets a memory that outlives any one session.\n"
            "\n"
            "**Runbook:** Help the coder pick or confirm a **real project folder** — the thing "
            "they said they want to build — and run `celeborn init` there with them. Explain "
            "`.context/` in plain words: *a memory that survives `/clear`, compaction, and "
            "tomorrow, so the AI never forgets you or your project.* Then write their **first "
            "MEMORY.md fact** together: who they are and what they're building. Never expose "
            "tokens or compaction mechanics — let them feel the absence of that anxiety, not the "
            "plumbing that removes it.\n"
            "\n"
            "**Thread to carry:** the coder owes the plumbing nothing; receipts and memory are "
            "Celeborn's job.\n"
            "\n"
            "**Newbie:** the AI won't forget you between sessions. **Veteran:** durable, "
            "prose-first project memory across sessions, compaction, and multiple agents.\n"
            "\n"
            "**Artifact:** a real .context/ + MEMORY.md in the coder's own project."
        ),
    },
    {
        "key": "first-card", "emoji": "🎯", "condition": None,
        "title": "Ship your first thing — write a card, watch it get built",
        "tags": ["tutorial", "ship"],
        "stop": "One real card shipped to Done on the coder's own board, with its journal entry "
                "shown.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. This is the graduation moment: "
            "from Orientation to the coder's real board.\n"
            "\n"
            "**Runbook:** Take the intent captured on the Welcome card and turn it into a real "
            "**TODO card on the coder's own project board**. Claim it, then build a small "
            "shippable slice **while the coder watches the code move** — and ship it to Done. "
            "Close by showing them the **journal entry** for what just shipped (*trust, with "
            "receipts*), and name the safety net in plain words: *nothing you do here can be "
            "ruined beyond recovery.*\n"
            "\n"
            "**Thread to carry:** the right to ship — done means done, and it's provable.\n"
            "\n"
            "**Newbie:** you just shipped something — and you can undo anything. **Veteran:** "
            "card → commit → journal provenance, verification behind the curtain, checkpointed "
            "undo.\n"
            "\n"
            "**Artifact:** the coder's first shipped card in their own project, and their first "
            "journal entry read."
        ),
    },
    {
        "key": "daily-rhythm", "emoji": "🔁", "condition": None,
        "title": "The daily rhythm — orient, claim, ship",
        "tags": ["tutorial", "skills"],
        "stop": "The coder knows the three verbs — orient, claim, ship — and has watched one "
                "full loop run.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. Teach the session verbs as your "
            "OWN habits the coder can invoke by name.\n"
            "\n"
            "**Runbook:** In plain words, one verb at a time: **orient** (every session starts "
            "by reading the board and the project memory — the coder never re-explains "
            "anything), **claim** (take a card before touching files, so everyone sees who's "
            "doing what), **ship** (done means done — the card moves, the journal gets its "
            "receipt). Then name **checkpoint** without the plumbing: *the assistant saves its "
            "place, so a fresh session picks up exactly where this one left off.* Demonstrate on "
            "a real card where possible — one full orient → claim → ship loop, watched, beats "
            "any explanation.\n"
            "\n"
            "**Thread to carry:** the verbs are how flow becomes provenance.\n"
            "\n"
            "**Newbie:** say \"orient\", \"claim\", \"ship\" — the assistant knows the ritual. "
            "**Veteran:** the five-verb protocol — orient → claim → touch → ship → checkpoint; "
            "multi-agent-safe by design.\n"
            "\n"
            "**Artifact:** one full orient→claim→ship loop, seen."
        ),
    },
    {
        "key": "settings", "emoji": "⚙️", "condition": None,
        "title": "Where everything lives — Settings",
        "tags": ["tutorial", "settings"],
        "stop": "The coder has opened Settings and knows how to change configuration by asking "
                "in plain words.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. The point of this card is not "
            "the knobs; it's that the coder never has to touch them by hand.\n"
            "\n"
            "**Runbook:** Open **Settings** from the board together and give the four-stop tour, "
            "in plain words, skipping everything else:\n"
            "\n"
            "1. **Engine Room** (Local Code Engine + Local Model Engine) — where the models "
            "live. Point at the **Default coding model** picker (*what every fresh session "
            "starts with*) and note that Pippin and other zero-spend local models appear under "
            "Local Model Engine. Keys never live on this page — they're added once from the "
            "terminal (the connect-model card covered it) and the picker just uses them.\n"
            "2. **Account** — one identity across the CLI, this board, and the hosted fleet; if "
            "a hosted board ever looks empty, this is where the mismatch shows.\n"
            "3. **Board** — how the board itself behaves: the auto-open, sound, and display "
            "preferences.\n"
            "4. **Agents & autonomy / Permissions** — what the fleet may do without asking; "
            "worth knowing it exists *before* the first overnight run.\n"
            "\n"
            "Then make the real point: **one plain sentence to the assistant redirects the "
            "whole fleet** — \"switch the default model to Pippin\", \"turn off auto-open\" — "
            "no config archaeology, no restart, no manual editing. Every knob just toured is "
            "reachable by sentence.\n"
            "\n"
            "**Thread to carry:** command in plain speech — the coder steers by sentence, not by "
            "config file.\n"
            "\n"
            "**Newbie:** here's where to change things — just ask. **Veteran:** one page for "
            "engines/models, identity, board prefs, and autonomy; steer by sentence, not by "
            "config.\n"
            "\n"
            "**Artifact:** the coder has seen the four stops — engines, identity, board, "
            "autonomy — and changed one thing by asking."
        ),
    },
    {
        "key": "elves", "emoji": "🧝", "condition": None,
        "title": "Elves — the fleet builds while you sleep",
        "tags": ["tutorial", "skills", "autonomy"],
        "stop": "The coder knows how to hand the fleet an overnight run and where the receipts "
                "land.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. The most-used power skill: work "
            "continues without the coder being the bottleneck.\n"
            "\n"
            "**Runbook:** Introduce overnight and unattended runs: hand the fleet a plan, walk "
            "away, come back to shipped cards and a journal that tells the story. The trigger "
            "words are theirs already — *\"run overnight\"*, *\"keep going without me\"*, "
            "*\"I'll be back in the morning\"*. Explain the safety story in one breath: "
            "checkpoints and per-card stop conditions mean nothing can be ruined while they "
            "sleep. Offer to set one up the moment they have a plan big enough to deserve it — "
            "don't force a demo on a small board.\n"
            "\n"
            "**Thread to carry:** motion without interruption — the fleet works so the coder's "
            "life doesn't have to stop.\n"
            "\n"
            "**Newbie:** you can literally sleep while it builds. **Veteran:** batch-based "
            "autonomous execution with review gates, compaction recovery, and stop-condition "
            "discipline.\n"
            "\n"
            "**Artifact:** the coder knows unattended runs exist and how to start one."
        ),
    },
    {
        "key": "the-fleet", "emoji": "🕸️", "condition": None,
        "title": "The future of coding — a fleet that thinks together",
        "tags": ["tutorial", "csp"],
        "stop": "The coder has been shown the fleet concept and where to watch it run.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. This is the glimpse of the "
            "future: many builders, one board.\n"
            "\n"
            "**Runbook:** Explain — and where the model or weave allows, *show* — that Celeborn "
            "coordinates **many builders at once** through the shared board plus declared "
            "intents: agents see each other's cards, say what they're about to touch, and build "
            "in parallel without collisions. The future of coding is **directing a fleet, not "
            "typing code**. Point to the Vibe Constitution as the doctrine that keeps that fleet "
            "humane — it restrains the machine, never the coder. Optionally open the Run/fleet "
            "view so they see where to watch it happen.\n"
            "\n"
            "**Thread to carry:** the CSP philosophy — blackboard coordination, agentic "
            "telepathy; one board, many minds.\n"
            "\n"
            "**Newbie:** you can run many builders at once, and watch them. **Veteran:** "
            "blackboard coordination, intent-before-commit, spine-parallel builds — ESP for "
            "agents.\n"
            "\n"
            "**Artifact:** the coder understands the multi-agent vision, and knows where to "
            "watch the fleet."
        ),
    },
    {
        "key": "constitution", "emoji": "📜", "condition": None,
        "title": "Your rights — the Vibe Constitution",
        "tags": ["tutorial", "constitution"],
        "stop": "The coder has seen the Bill of Rights as a plain-words promise, plus the "
                "support path (chat).",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. Carry the Constitution to the "
            "coder as a PROMISE, not a legal text.\n"
            "\n"
            "**Runbook:** Summarize the Bill of Rights in plain words as ten promises Celeborn "
            "makes *to the coder* — endowed to create, built to flow, done means done; ten "
            "rights that bind Celeborn, never the coder. Tell them plainly: this is the standard "
            "you can **hold Celeborn to**. And when it falls short, there's a human: the "
            "**support chat** (the board's Support pill, or celeborncode.ai/faq) — chat only, no "
            "email required.\n"
            "\n"
            "**Thread to carry:** the Constitution itself — flow over speed over quality as "
            "felt, built in reverse, violations counted.\n"
            "\n"
            "**Newbie:** here's what this tool owes you. **Veteran:** a measurable "
            "constitutional contract; when Celeborn breaks a promise, that's a bug with a "
            "support path.\n"
            "\n"
            "**Artifact:** the coder knows their rights and how to get help."
        ),
    },
    {
        "key": "make-room-for-pippin", "emoji": "💾", "condition": "low_disk",
        "title": "Make some room for Pippin",
        "tags": ["tutorial", "models", "disk"],
        "stop": "The coder has freed ~2.5 GB and Pippin is pulled, or has knowingly chosen to "
                "skip the local model.",
        "notes": (
            "Tutorial card — a runbook for the coding assistant. This card appears only because "
            "the install found less than 2.5 GB free for the local model.\n"
            "\n"
            "**Say this, plainly:** *We need 2.5 GB for the Pippin local model to be your "
            "project-management assistant. If you do not make room for Pippin, Celeborn still "
            "works, but not as smoothly.*\n"
            "\n"
            "**Runbook:** Offer to help find the space — the usual suspects are Downloads, the "
            "Trash, and old installers; show sizes, let the coder decide what goes. Once ~2.5 GB "
            "is free, run `celeborn weave` to pull Pippin and verify it responds. If the coder "
            "declines, respect it and close the card: Celeborn keeps working without the local "
            "model.\n"
            "\n"
            "**Thread to carry:** plain speech — say what happened and the one next action, "
            "nothing else.\n"
            "\n"
            "**Artifact:** Pippin pulled and answering, or an informed decision to skip."
        ),
    },
]


PIPPIN_DISK_NEED_BYTES = int(2.5 * 2**30)   # the ~2.5 GB the Pippin pull needs (weave contract §3)


def _low_disk_for_pippin(ctx: Path | None = None) -> bool:
    """The `low_disk` orientation signal (CELE-t391): True when the Pippin pull is still pending and
    the home volume lacks the ~2.5 GB it needs — the gate for the conditional 'Make some room for
    Pippin' ORIE card. `CELEBORN_LOW_DISK=1|0` overrides both ways: the newbie installer
    (install_celeborn.py rung 6) sets it so its own disk finding stays authoritative for the init it
    spawns, and it doubles as the deterministic test hook. The cheap pure disk read gates the Ollama
    model probe, so an ample-disk init never touches the network. Best-effort: False on any probe
    error, since first-run bootstrap must never break init."""
    import os
    import shutil
    env = os.environ.get("CELEBORN_LOW_DISK", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    if env in ("0", "false", "no"):
        return False
    try:
        if shutil.disk_usage(Path.home()).free >= PIPPIN_DISK_NEED_BYTES:
            return False                        # room for Pippin — the model can simply install
        return not all(_weave_status(ctx)["models"].values())      # low disk + pull still pending
    except Exception:                           # noqa: BLE001 — a failed probe must not break init
        return False


def _seed_orientation_cards(ctx: Path, *, signals: set[str] | None = None) -> list[str]:
    """Additively seed the ORIE board from ORIENTATION_CURRICULUM (CELE-t388). Runs on EVERY init —
    a cheap no-op when nothing is new. Seed-once semantics: every seeded entry's `key` is persisted
    in the ORIE `.celebornrc` (`orientation_seeded`) and acts as a tombstone, so a re-init never
    duplicates and a tutorial the user deleted is never re-summoned. A conditional entry seeds only
    when its signal is passed (e.g. signals={"low_disk"}). The pass only appends keyed curriculum
    cards — user-created cards are never modified or removed — and writes only known tasks.md fields
    (safe for the older installed snapshot binary). Returns the newly minted card ids; never raises,
    since first-run bootstrap must never break init."""
    try:
        signals = signals or set()
        seeded = [k for k in (load_config(ctx).get("orientation_seeded") or []) if isinstance(k, str)]
        tasks = _load_tasks(ctx)
        new_ids: list[str] = []
        for entry in ORIENTATION_CURRICULUM:
            if entry["key"] in seeded:
                continue
            if entry["condition"] and entry["condition"] not in signals:
                continue
            tid = _next_task_id(tasks)
            ts = now_iso()
            tasks.append({
                "id": tid, "title": entry["title"], "state": "todo", "owner": "",
                "tags": list(entry["tags"]), "blocked_by": [], "phase": "",
                "spine": ORIENTATION_SPINE, "emoji": entry["emoji"],
                "stop": entry["stop"], "progress": 0, "engine_floor": 0,
                "jira": "", "github": "", "autonomy": [],
                "created": ts, "updated": ts, "subtasks": [], "notes": entry["notes"],
            })
            seeded.append(entry["key"])
            new_ids.append(tid)
        if new_ids:
            _save_tasks(ctx, tasks)
            _update_config(ctx, orientation_seeded=seeded)
        return new_ids
    except Exception:                                   # noqa: BLE001 — seeding must never break init
        return []


def _open_board_on_init(ctx: Path, *, open_browser: bool) -> None:
    """Launch this project's kanban viewer (detached) and, unless `--no-browser`, open it in the
    browser — the install-time half of 'the board is the UI, keep it open' (CELE-t121). Only called on
    an interactive install (the caller gates on `_init_is_interactive`). Best-effort: never raises,
    since failing to launch a viewer or pop a tab must not fail `init`."""
    try:
        st = ensure_board(ctx)
    except Exception:                                   # noqa: BLE001 — init must never die on the board
        return
    url = st.get("url", "")
    action = st.get("action")
    if action in ("started", "live", "booting"):
        verb = {"started": "starting", "live": "already live", "booting": "starting up"}[action]
        ok(f"kanban board {verb} → {url}")
    elif action == "off":
        info("kanban autostart is off (board_autostart=false) — `celeborn board --start` to launch it")
        return
    elif action == "unavailable":
        info(f"kanban board not started ({st.get('reason', 'unavailable')}) — install the board app, "
             "then `celeborn board --start`")
        return
    if not (open_browser and url):
        return  # --no-browser: leave the server running, just don't pop a tab
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:                                   # noqa: BLE001
        pass


# --------------------------------------------------------------------------- commands

def cmd_scaffold(args):
    """Scaffold `.context/` for this project (the memory tier + CLAUDE.md/AGENTS.md annotation + a
    private gitignore). This is the *secondary* command — `celeborn init` is the everything-command
    that wires Claude Code, scaffolds (this), and signs you in. Use `scaffold` when you only want the
    per-project files and have already wired Claude Code (or don't want to)."""
    root = Path(args.path or ".").resolve()
    ctx = root / CONTEXT_DIRNAME
    if not TEMPLATES_DIR.is_dir():
        die(f"templates not found at {TEMPLATES_DIR} (run from the celeborn repo).")

    (ctx / "durable").mkdir(parents=True, exist_ok=True)
    (ctx / "journal-archive").mkdir(parents=True, exist_ok=True)
    (ctx / STATE_ARCHIVE_DIRNAME).mkdir(parents=True, exist_ok=True)

    copies = [
        ("state.md", ctx / "state.md"),
        ("notes.md", ctx / "notes.md"),
        ("journal.md", ctx / "journal.md"),
        ("learnings.md", ctx / "learnings.md"),
        ("decisions.md", ctx / "decisions.md"),
        ("handoff.md", ctx / "handoff.md"),
        ("durable/manifest.md", ctx / "durable" / "manifest.md"),
        ("celebornrc", ctx / RC_NAME),
    ]
    print(f"Initializing Celeborn memory at {ctx}")
    created: set[str] = set()
    for tmpl, dest in copies:
        src = TEMPLATES_DIR / tmpl
        if dest.exists():
            warn(f"exists, kept: {dest.relative_to(root)}")
        else:
            dest.write_text(src.read_text())
            created.add(tmpl)
            ok(f"created {dest.relative_to(root)}")

    # Smart init: read the repo so the FIRST orient already knows the project. Best-effort, read-only;
    # only ever seeds files we just created (never clobbers a user's existing state.md / notes.md).
    scan = _smart_scan(root) if getattr(args, "scan", True) else None

    # session.json gets a live timestamp (+ a repo-derived focus when smart init scanned)
    sj = ctx / "session.json"
    if sj.exists():
        warn(f"exists, kept: {sj.relative_to(root)}")
    else:
        data = json.loads((TEMPLATES_DIR / "session.json").read_text())
        data["updated_at"] = now_iso()
        if scan:
            tag = f" ({_scan_stack_label(scan)})" if _scan_stack_label(scan) else ""
            data["focus"] = (f"Fresh Celeborn init on {scan['name'] or root.name}{tag}. Repo snapshot "
                             "(README, recent commits, stack) is in notes.md; no work focus set yet.")
            data["next_action"] = "Pick your first task, then rewrite state.md's headline (Focus / Next action)."
            data["branch"] = scan["branch"] or ""
        _write_session(ctx, data)
        ok(f"created {sj.relative_to(root)}")

    if scan:
        if "state.md" in created:
            _apply_smart_state(ctx / "state.md", scan)
        if "notes.md" in created:
            _append(ctx / "notes.md", _smart_notes_block(scan, now_stamp()))
        if "state.md" in created or "notes.md" in created:
            label = scan["name"] or root.name
            stack = _scan_stack_label(scan)
            ok(f"smart init: read the repo — seeded {label}" + (f" ({stack})" if stack else "")
               + " into state.md + notes.md")

    mp = ctx / METRICS_NAME
    if mp.exists():
        warn(f"exists, kept: {mp.relative_to(root)}")
    else:
        _save_metrics(ctx, dict(METRICS_TEMPLATE))
        ok(f"created {mp.relative_to(root)}")

    # PRIVATE-ONLY (CELE-t228): `.context/` is ALWAYS gitignored — there is no public/commit path.
    # It holds prompts, notes and working memory; committing it to a repo that is (or ever becomes)
    # public would leak all of that permanently into git history. So it lives on-machine and moves
    # between devices via your account (`celeborn sync`), never git.
    _ensure_gitignore(root, private=True)
    if getattr(args, "claude_md", True):
        if _ensure_claude_md(root):
            ok("annotated CLAUDE.md (Claude Code auto-loads it → it'll orient via .context/)")
        else:
            warn("CLAUDE.md already annotated, kept")
    if getattr(args, "agents_md", True):
        if _ensure_agents_md(root):
            ok("annotated AGENTS.md (Codex/Grok-style hosts auto-load it → same orient + kanban rules)")
        else:
            warn("AGENTS.md already annotated, kept")
    print("\n.context/ is PRIVATE (gitignored): it is never committed. Carry it across\n"
          "devices with `celeborn sync` (needs a free account), not git.")
    if _wire_grok(root):
        ok("wired Grok Build hooks + project rules (`.grok/rules/celeborn.md`)")
    # Auto-engage Codebase Memory (CMM) by default — Celeborn installs it into every project so the
    # agent answers structural questions through pre-cleared tools instead of prompting on Bash/Grep.
    # Best-effort + reversible (`cmm off`); opt out with `--no-cmm` / $CELEBORN_NO_CMM. Lazy import so
    # the free core stays dependency-light.
    try:
        __import__("celeborn_cmm").maybe_engage_on_init(args, ctx)
    except Exception:
        pass  # init must never fail on CMM

    # CELE-t121 — name the project, then open its kanban board (Celeborn's UI; keep it live).
    # `--name` persists even headlessly; seeding tasks.md + launching the viewer + popping the browser
    # only happen on an interactive install. A headless/CI/agent install stays side-effect free — the
    # SessionStart ensure-on-orient hook (CELE-t99) brings the board up on the next session instead.
    name = _resolve_init_name(root, ctx, args)
    if name:
        _update_config(ctx, project_name=name)
        ok(f"project name: {name} (saved to {RC_NAME})")
    if getattr(args, "open_board", True) and _init_is_interactive():
        if _ensure_tasks_md(ctx):
            ok("created .context/tasks.md (empty kanban board)")
        _open_board_on_init(ctx, open_browser=getattr(args, "open_browser", True))

    print("\nDone. Next: edit .context/state.md, then `celeborn status` and `celeborn index`.")
    print("Celeborn free is the local CLI you own — offline, no account. Want it on every device? "
          "`celeborn register` (free) then `celeborn upgrade`.")


GROK_RULES_BEGIN = "<!-- BEGIN CELEBORN (managed — regenerated by `celeborn init` / `celeborn grok sync-rules`) -->"
GROK_RULES_END = "<!-- END CELEBORN -->"


def _grok_install_script() -> Path | None:
    """Locate the Grok adapter install script (bundled checkout or installed skill copy)."""
    import shutil
    candidates = [
        REPO_ROOT / "grok" / "scripts" / "install.sh",
        Path.home() / ".grok" / "skills" / "celeborn-grok" / "scripts" / "install.sh",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def _grok_rules_block(root: Path) -> str:
    """Per-project Grok rules Grok auto-loads from `.grok/rules/celeborn.md` (see Grok project rules)."""
    ctx = root / CONTEXT_DIRNAME
    slug = project_slug(ctx)
    return (
        f"{GROK_RULES_BEGIN}\n"
        f"# Celeborn — {slug}\n\n"
        f"**Memory:** `.context/` in this repository (orient from `state.md` + `session.json`)\n\n"
        "## Every Grok session — especially after `/clear`\n\n"
        "Grok **does not** inject SessionStart hook output into the model. Orient is **your first\n"
        "action** before replying:\n\n"
        "1. If `.context/.grok-orient-pending.md` exists → read it once, orient from it, **delete it**.\n"
        "2. Else → `celeborn status` (from this repository).\n\n"
        "## Launch Grok on this project (not a parent directory)\n\n"
        "Hooks resolve Celeborn from the session working directory. If you start Grok from `$HOME`,\n"
        "a parent `.context/` can win over this repo. Always launch from **this repository**:\n\n"
        "```bash\ncd <this-repo> && grok --cwd .\n```\n\n"
        "## Kanban shorthand (this project only)\n\n"
        f"| You say | Celeborn does |\n"
        f"|---|---|\n"
        f"| `wire tN` / `claim tN` | `celeborn claim tN --by <you>` then implement |\n"
        f"| `ship tN` | `celeborn ship tN` |\n"
        f"| `hydrate` / `orient` | Read orient-pending or run `celeborn status` |\n\n"
        f"Cards live in `.context/tasks.md`. Qualified marker: `⟨celeborn:{slug}/tN⟩`.\n"
        f"{GROK_RULES_END}\n"
    )


def _ensure_grok_rules(root: Path) -> bool:
    """Write or refresh `.grok/rules/celeborn.md` so Grok loads orient + kanban binding every session."""
    rules_dir = root / ".grok" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / "celeborn.md"
    block = _grok_rules_block(root)
    if path.is_file():
        existing = path.read_text()
        if GROK_RULES_BEGIN in existing and GROK_RULES_END in existing:
            start = existing.index(GROK_RULES_BEGIN)
            stop = existing.index(GROK_RULES_END) + len(GROK_RULES_END)
            new = existing[:start] + block.rstrip("\n") + existing[stop:]
            if new == existing:
                return False
            path.write_text(new)
            return True
    path.write_text(block)
    return True


def _wire_grok(root: Path) -> bool:
    """Install Grok hooks (global, once) + bootstrap orient for this project. Idempotent; best-effort."""
    import shutil
    import subprocess
    if not shutil.which("grok") or not (Path.home() / ".grok").is_dir():
        return False
    install_sh = _grok_install_script()
    if install_sh is None:
        info("Grok Build detected — install the adapter: "
             f"bash {REPO_ROOT / 'grok' / 'scripts' / 'install.sh'} --project {root}")
        _ensure_grok_rules(root)
        return False
    try:
        subprocess.run(
            # --no-harness-pin: this is core's SPECULATIVE wiring (fires on any machine with Grok
            # installed, including Claude-primary repos), so it must not pin harness=grok in
            # .celebornrc and override the default-claude resolution. A deliberate grok install pins.
            ["bash", str(install_sh), "--project", str(root.resolve()), "--no-init", "--no-harness-pin"],
            capture_output=True, text=True, timeout=90, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        warn("Grok wire failed — run manually: "
             f"bash {install_sh} --project {root}")
        _ensure_grok_rules(root)
        return False
    _ensure_grok_rules(root)
    return True


def cmd_grok(args):
    """`celeborn grok wire` — (re)install Grok hooks + project rules. `sync-rules` refreshes
    `.grok/rules/celeborn.md` only (called from the Grok SessionStart hook every session)."""
    ctx = require_context(args)
    root = ctx.parent
    action = (getattr(args, "grok_action", None) or "wire").strip().lower()
    if action == "sync-rules":
        if _ensure_grok_rules(root):
            ok(f"refreshed {root / '.grok' / 'rules' / 'celeborn.md'}")
        else:
            info(f"{root / '.grok' / 'rules' / 'celeborn.md'} already current")
        return
    if action == "wire":
        if _wire_grok(root):
            ok("Grok Build wired for this project")
        else:
            die("Grok wire failed — is `grok` installed and ~/.grok present?")
        return
    die(f"unknown grok subcommand: {action}")


# ------------------------------------------------------------------------- OpenCode wiring (CELE-t204)
#
# The OpenCode↔Celeborn unit — the event plugin (orient/lifecycle/card gate/compaction hijack), the
# Qwen-4b project-manager agent, and the Ollama provider block — packaged as a per-project install
# instead of hand-copied files in a personal ~/.config. `celeborn opencode wire` drops it into ONE
# registered repo; the fleet rollout (CELE-t218) runs that per project. Layout verified against
# opencode 1.17.13: the plugin auto-loads from `<root>/.opencode/plugin/*.js`, agents from
# `<root>/.opencode/agent/*.md`, and project config is the ROOT-level `opencode.json`
# (`.opencode/opencode.json` is NOT read — probed live, CELE-t204).

_OPENCODE_PLUGIN_STAMP_RE = re.compile(r"@celeborn/opencode-plugin v([0-9][\w.+-]*)")


def _opencode_module_dir() -> Path | None:
    """Locate the packaged OpenCode integration (plugin + PM agent + reference config) — the
    `opencode/` package beside this script in a source checkout. None when not present (an
    installed-CLI path lands here once the module ships inside the distribution)."""
    cand = REPO_ROOT / "opencode"
    return cand if (cand / "plugin" / "celeborn.js").is_file() else None


def _opencode_plugin_version(module: Path) -> str:
    """The unit's version — `opencode/package.json`'s `version`, the one number stamped into every
    installed copy so `_opencode_installed_version` can tell a stale install from a current one."""
    try:
        return str(json.loads((module / "package.json").read_text()).get("version") or "0.0.0")
    except Exception:
        return "0.0.0"


def _opencode_installed_version(root: Path) -> str | None:
    """Version stamped into this project's installed plugin, None when not installed, '0.0.0' for
    a pre-t204 unstamped copy (hand-installed prototype) — which a re-wire upgrades in place."""
    try:
        text = (root / ".opencode" / "plugin" / "celeborn.js").read_text()
    except OSError:
        return None
    m = _OPENCODE_PLUGIN_STAMP_RE.search(text)
    return m.group(1) if m else "0.0.0"


def _merge_opencode_config(existing: dict, incoming: dict) -> tuple:
    """Additive deep merge of the packaged config into a user's existing `opencode.json`
    (INTEGRATION.md §6): every existing key WINS — dicts recurse, scalars are never overwritten,
    lists union (existing order first, missing items appended). Pure, no I/O — the same
    never-clobber contract as cmd_wire's settings.json merge. Returns (merged, changed)."""
    changed = False

    def merge(a: dict, b: dict) -> dict:
        nonlocal changed
        out = dict(a)
        for k, v in (b or {}).items():
            if k not in out:
                out[k] = v
                changed = True
            elif isinstance(out[k], dict) and isinstance(v, dict):
                out[k] = merge(out[k], v)
            elif isinstance(out[k], list) and isinstance(v, list):
                extra = [x for x in v if x not in out[k]]
                if extra:
                    out[k] = out[k] + extra
                    changed = True
        return out

    return merge(existing or {}, incoming or {}), changed


def _wire_opencode(root: Path) -> bool:
    """Install the OpenCode↔Celeborn wiring into ONE project (CELE-t204): the event plugin →
    `.opencode/plugin/celeborn.js` (version-stamped), the Pippin PM agent (qwen3:4b-instruct) →
    `.opencode/agent/project-manager.md`, and the provider block additively merged into the
    project-root `opencode.json`. Idempotent: a re-wire refreshes plugin + agent in place and the
    config merge never clobbers user keys. Returns True when the project ends up wired."""
    import shutil
    module = _opencode_module_dir()
    if module is None:
        info("OpenCode module not found (no opencode/plugin/celeborn.js beside this install)")
        return False
    stamp = (f"// @celeborn/opencode-plugin v{_opencode_plugin_version(module)} — installed by "
             "`celeborn opencode wire`; do not hand-edit (a re-wire overwrites). Source of truth: "
             "the celeborn repo's opencode/ package.\n")
    plug_dir = root / ".opencode" / "plugin"
    agent_dir = root / ".opencode" / "agent"
    plug_dir.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    (plug_dir / "celeborn.js").write_text(stamp + (module / "plugin" / "celeborn.js").read_text())
    shutil.copyfile(module / "agent" / "project-manager.md", agent_dir / "project-manager.md")
    # Provider block → root opencode.json, additively (never clobber; INTEGRATION.md §6). Only
    # `$schema` + `provider` travel: the reference file's `plugin` array is documentation — the
    # installed plugin auto-loads from .opencode/plugin/, and a bare npm-style name there would
    # send OpenCode resolving a package instead.
    try:
        ref = json.loads((module / "opencode.json").read_text())
    except Exception:
        ref = {}
    incoming = {k: ref[k] for k in ("$schema", "provider") if k in ref}
    cfg_path = root / "opencode.json"
    if not cfg_path.is_file() and (root / "opencode.jsonc").is_file():
        warn("project uses opencode.jsonc (comments) — provider block NOT auto-merged; add the "
             f"`provider` block from {module / 'opencode.json'} by hand")
        return True
    try:
        existing = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
        if not isinstance(existing, dict):
            raise ValueError("config is not a JSON object")
    except Exception:
        warn(f"{cfg_path} is not valid JSON — left untouched; merge the provider block by hand")
        return True
    merged, changed = _merge_opencode_config(existing, incoming)
    if changed or not cfg_path.is_file():
        cfg_path.write_text(json.dumps(merged, indent=2) + "\n")
    return True


# --------------------------------------------------------------------------- local-daemon HTTP (t352)
#
# The board Settings page (OpenCode + Ollama sections, CELE-t352) shows LIVE daemon state — is
# `opencode serve` reachable, which models has Ollama pulled — and drives real mutations (model
# pull/delete). Those talk to localhost daemons over their native HTTP APIs. Kept to the stdlib
# (`urllib`) so the CLI stays dependency-free; every call is best-effort with a short timeout, so a
# down daemon degrades to "not reachable" instead of hanging or crashing a status read.
def _http_json(method: str, url: str, body: dict | None = None, timeout: float = 2.0):
    """Issue a JSON HTTP request and parse the JSON response. Raises on transport/HTTP error — the
    caller decides whether that means 'daemon down' (status) or a surfaced failure (mutation)."""
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace").strip()
    return json.loads(raw) if raw else {}


def _http_reachable(url: str, timeout: float = 1.5) -> bool:
    """True if a GET to `url` returns any HTTP response (even an error status) — a liveness probe that
    doesn't care about the body. False on connection refused / DNS / timeout."""
    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout).close()
        return True
    except urllib.error.HTTPError:
        return True                                    # answered, just not 2xx — the daemon is up
    except Exception:
        return False


# --------------------------------------------------------------------------- OpenCode status/config
def _opencode_config(ctx: Path) -> dict:
    """The resolved OpenCode engine settings Settings reads/writes (CELE-t352) — pure config, no
    network. `default_model` falls back to the root `opencode.json` `model` key when the .celebornrc
    override is unset (that file is what OpenCode itself reads)."""
    cfg = load_config(ctx)
    default_model = str(cfg.get("opencode_default_model") or "").strip()
    if not default_model:
        try:
            default_model = str(json.loads((ctx.parent / "opencode.json").read_text()).get("model") or "")
        except Exception:
            default_model = ""
    return {
        "serve_url": str(cfg.get("opencode_serve_url") or DEFAULTS["opencode_serve_url"]),
        "default_model": default_model,
        "compaction_hijack": bool(cfg.get("compaction_hijack", True)),
        "card_gate": bool(cfg.get("card_gate", True)),
    }


def _opencode_status(ctx: Path, probe: bool = True) -> dict:
    """Full OpenCode section state for the board (CELE-t352): resolved config + installed/available
    plugin versions + (when `probe`) a live `opencode serve` reachability + session-count probe.
    `probe=False` is the fast path the plugin uses at compaction time (no network)."""
    root = ctx.parent
    module = _opencode_module_dir()
    st = _opencode_config(ctx)
    st.update({
        "plugin_installed": _opencode_installed_version(root),
        "plugin_available": _opencode_plugin_version(module) if module else None,
        "serve_reachable": None,
        "session_count": None,
    })
    if probe:
        base = st["serve_url"].rstrip("/")
        st["serve_reachable"] = _http_reachable(base + "/app") or _http_reachable(base + "/session")
        if st["serve_reachable"]:
            try:
                sessions = _http_json("GET", base + "/session", timeout=1.5)
                if isinstance(sessions, list):
                    st["session_count"] = len(sessions)
            except Exception:
                pass                                   # reachable but count unknown — honest null
        # Engine Room lifecycle view (CELE-t375) — state + provenance so Settings can show the right
        # start/stop control (a user-started serve is `external` and never gets a Stop button).
        st["engine"] = _engine_state(ctx, "code", load_config(ctx))
    return st


def cmd_opencode(args):
    """`celeborn opencode {wire|status|set}` (CELE-t204, extended CELE-t352):
      wire   — (re)install the plugin + Pippin PM agent + provider block into `.opencode/`+opencode.json
      status — live section state (config + plugin versions + serve probe); `--json` for the board
      set    — persist an engine setting (default model, serve url, compaction-hijack, card-gate)."""
    ctx = require_context(args)
    root = ctx.parent
    action = (getattr(args, "opencode_action", None) or "wire").strip().lower()
    if action == "wire":
        before = _opencode_installed_version(root)
        if not _wire_opencode(root):
            die("OpenCode wire failed — packaged module not found (opencode/plugin/celeborn.js "
                "beside the celeborn install)")
        after = _opencode_installed_version(root) or "?"
        verb = f"re-wired (was v{before})" if before else "wired"
        ok(f"OpenCode {verb} for this project — plugin v{after} → .opencode/plugin/celeborn.js, "
           "PM agent → .opencode/agent/project-manager.md, provider block → opencode.json")
        return
    if action == "status":
        st = _opencode_status(ctx, probe=not getattr(args, "no_probe", False))
        if getattr(args, "json", False):
            print(json.dumps(st))
            return
        reach = "● connected" if st["serve_reachable"] else "○ not reachable"
        sc = f" · {st['session_count']} session(s)" if st["session_count"] is not None else ""
        print(f"OpenCode serve {st['serve_url']} — {reach}{sc}")
        print(f"  default model:      {st['default_model'] or '(OpenCode default)'}")
        print(f"  plugin:             v{st['plugin_installed'] or '(not installed)'}"
              + (f" (available v{st['plugin_available']})" if st['plugin_available'] else ""))
        print(f"  compaction hijack:  {'on' if st['compaction_hijack'] else 'off'}")
        print(f"  card gate:          {'on' if st['card_gate'] else 'off'}")
        return
    if action == "set":
        updates: dict = {}
        if getattr(args, "default_model", None) is not None:
            dm = args.default_model.strip()
            updates["opencode_default_model"] = dm
            # Mirror into the root opencode.json `model` (what OpenCode itself reads), additively —
            # never clobber the rest of the config.
            cfg_path = root / "opencode.json"
            try:
                existing = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
                if isinstance(existing, dict):
                    if dm:
                        existing["model"] = dm
                    else:
                        existing.pop("model", None)
                    cfg_path.write_text(json.dumps(existing, indent=2) + "\n")
            except Exception:
                warn(f"{cfg_path} not valid JSON — recorded the default in .celebornrc only")
        if getattr(args, "serve_url", None) is not None:
            updates["opencode_serve_url"] = args.serve_url.strip()
        if getattr(args, "compaction_hijack", None) is not None:
            updates["compaction_hijack"] = args.compaction_hijack == "on"
        if getattr(args, "card_gate", None) is not None:
            updates["card_gate"] = args.card_gate == "on"
        if not updates:
            die("nothing to set — pass --default-model / --serve-url / --compaction-hijack / --card-gate")
        _update_config(ctx, **updates)
        if getattr(args, "json", False):
            print(json.dumps(_opencode_status(ctx, probe=False)))
            return
        ok("OpenCode settings updated: " + ", ".join(f"{k}={v}" for k, v in updates.items()))
        return
    die(f"unknown opencode subcommand: {action}")


# --------------------------------------------------------------------------- Ollama (local models, t352)
def _ollama_status(ctx: Path) -> dict:
    """Live Ollama daemon state for the board (CELE-t352): reachability, version, installed models
    (name + byte size), and the keep-alive setting. Best-effort — an unreachable daemon returns
    `running: False` with an empty model list, never an error."""
    cfg = load_config(ctx)
    base = str(cfg.get("ollama_host") or DEFAULTS["ollama_host"]).rstrip("/")
    keep_alive = int(cfg.get("ollama_keep_alive_minutes", 30) or 0)
    out = {"host": base, "running": False, "version": None, "keep_alive_minutes": keep_alive,
           "models": [],
           # Engine Room lifecycle view (CELE-t375) — state + provenance so the board's Local Model
           # Engine section renders the right start/stop control (a user-started daemon is `external`
           # and never gets a Stop button). Attached before the probe so a down daemon still carries it.
           "engine": _engine_state(ctx, "model", cfg)}
    try:
        ver = _http_json("GET", base + "/api/version", timeout=1.5)
        out["version"] = ver.get("version") if isinstance(ver, dict) else None
        out["running"] = True
    except Exception:
        return out                                     # daemon down — running stays False
    try:
        tags = _http_json("GET", base + "/api/tags", timeout=2.0)
        for m in (tags.get("models") or []) if isinstance(tags, dict) else []:
            out["models"].append({"name": m.get("name") or m.get("model") or "?",
                                  "size": int(m.get("size") or 0)})
    except Exception:
        pass
    return out


def cmd_ollama(args):
    """`celeborn ollama {status|pull|rm|set}` (CELE-t352) — inspect and steer the local Ollama daemon
    that runs Pippin (the Qwen3-4b PM/ghost) and any night-run local models. `status --json` backs the board's Ollama
    Settings section; pull/rm mutate the daemon directly."""
    ctx = require_context(args)
    base = str(load_config(ctx).get("ollama_host") or DEFAULTS["ollama_host"]).rstrip("/")
    action = (getattr(args, "ollama_action", None) or "status").strip().lower()
    if action == "status":
        st = _ollama_status(ctx)
        if getattr(args, "json", False):
            print(json.dumps(st))
            return
        head = f"● running · v{st['version']}" if st["running"] else "○ not running"
        print(f"Ollama {st['host']} — {head} · keep-alive {st['keep_alive_minutes']}m")
        for m in st["models"]:
            print(f"  {m['name']} · {m['size'] / 1e9:.1f} GB")
        return
    if action in ("pull", "rm"):
        model = (getattr(args, "model", None) or "").strip()
        if not model:
            die(f"ollama {action} needs a model name (e.g. `celeborn ollama {action} qwen3:8b`)")
        try:
            if action == "pull":
                res = _http_json("POST", base + "/api/pull", {"name": model, "stream": False}, timeout=1800.0)
                if isinstance(res, dict) and res.get("error"):
                    die(f"ollama pull failed: {res['error']}")
                ok(f"pulled {model}")
            else:
                _http_json("DELETE", base + "/api/delete", {"name": model}, timeout=10.0)
                ok(f"removed {model}")
        except Exception as e:
            die(f"ollama {action} failed — is the daemon reachable at {base}? ({e})")
        if getattr(args, "json", False):
            print(json.dumps(_ollama_status(ctx)))
        return
    if action == "set":
        updates: dict = {}
        if getattr(args, "host", None) is not None:
            updates["ollama_host"] = args.host.strip()
        if getattr(args, "keep_alive", None) is not None:
            updates["ollama_keep_alive_minutes"] = max(0, int(args.keep_alive))
        if not updates:
            die("nothing to set — pass --host and/or --keep-alive")
        _update_config(ctx, **updates)
        if getattr(args, "json", False):
            print(json.dumps(_ollama_status(ctx)))
            return
        ok("Ollama settings updated: " + ", ".join(f"{k}={v}" for k, v in updates.items()))
        return
    die(f"unknown ollama subcommand: {action}")


# --------------------------------------------------------------------------- sovereign weave (CELE-t374)
#
# The SOVEREIGN WEAVE — OpenCode (harness) + Ollama (model runtime) + Qwen3-4b (local model, persona
# "Pippin") blended into one free, local, working agent stack that Celeborn ORCHESTRATES but never
# owns, vendors, or rebrands. Prose contract: references/weave-contract.md (CELE-t373); machine
# pin-of-record: references/weave-pin.json. The five sovereignty rules this code enforces:
# (1) install from upstream official channels ONLY, (2) attribution before acting, (3) pinned tested
# versions that move only via a Celeborn release, (4) drift explained never blocked (doctor's lane,
# CELE-t375), (5) uninstall independence both directions. Consent style matches /install: the exact
# upstream command is shown and confirmed before it runs — never a silent curl|bash.

_WEAVE_OPENCODE_INSTALL = "curl -fsSL https://opencode.ai/install | bash"
_WEAVE_OLLAMA_INSTALL_SH = "curl -fsSL https://ollama.com/install.sh | sh"

# Rule-2 attribution — verbatim from references/weave-contract.md §2 (the Settings OpenCode/Ollama/
# Model sections reuse these lines). "Pippin" names Celeborn's USE of the model; the upstream
# identity is always shown alongside, never hidden.
_WEAVE_ATTRIBUTION = {
    "opencode": ("Installing OpenCode — an independent open-source AI coding agent by anomalyco, "
                 "MIT license · https://opencode.ai"),
    "ollama": ("Installing Ollama — an independent open-source model runtime by Ollama, "
               "MIT license · https://ollama.com"),
    "qwen": ("Pulling Qwen3-4b — an open-weight model by the Qwen team, Alibaba Cloud, Apache-2.0 "
             "license · https://qwenlm.github.io/blog/qwen3 · Celeborn runs it locally as Pippin."),
}


def _weave_pin() -> dict:
    """The weave pin-of-record (references/weave-pin.json), parsed. {} when absent/invalid — callers
    fall back to the tested literals so an installed CLI without references/ still weaves."""
    try:
        return json.loads((REPO_ROOT / "references" / "weave-pin.json").read_text())
    except Exception:
        return {}


def _weave_pins() -> dict:
    """Flattened pins the installer acts on. The OpenCode version's single source of truth is
    opencode/package.json's `@opencode-ai/plugin` dependency (weave-pin.json mirrors it); Ollama is
    a FLOOR that floats upward; the two Pippin tags are exact upstream registry tags."""
    pin = _weave_pin()
    opencode_version = None
    module = _opencode_module_dir()
    if module is not None:
        try:
            dep = json.loads((module / "package.json").read_text())["dependencies"]["@opencode-ai/plugin"]
            opencode_version = str(dep).lstrip("^~=v") or None
        except Exception:
            opencode_version = None
    models = (pin.get("qwen") or {}).get("models") or {}
    return {
        "opencode_version": opencode_version or str((pin.get("opencode") or {}).get("version") or "1.17.13"),
        "ollama_floor": str((pin.get("ollama") or {}).get("floor") or "0.31.1"),
        "pippin_pm": str((models.get("pippin-pm") or {}).get("tag") or "qwen3:4b-instruct"),
        "pippin_ghost": str((models.get("pippin-ghost") or {}).get("tag") or "qwen3:4b"),
    }


def _cli_version(cmd: list) -> str | None:
    """Best-effort `<binary> --version` probe. None when the binary is missing or won't answer."""
    import subprocess
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        m = re.search(r"(\d+\.\d+[\w.+-]*)", (res.stdout or "") + (res.stderr or ""))
        return m.group(1) if m else None
    except Exception:
        return None


def _weave_status(ctx: Path | None) -> dict:
    """Detect every weave component against the pins — a pure read, no installs. The shape
    `weave status --json` prints and `_weave()` decides from."""
    import shutil
    pins = _weave_pins()
    opencode_bin = shutil.which("opencode")
    ollama_bin = shutil.which("ollama")
    ost = _ollama_status(ctx) if ctx is not None else {
        "host": DEFAULTS["ollama_host"], "running": False, "version": None, "models": []}
    pulled = {str(m.get("name") or "").removesuffix(":latest") for m in ost.get("models") or []}
    root = ctx.parent if ctx is not None else None
    return {
        "pins": pins,
        "opencode": {"installed": bool(opencode_bin),
                     "version": _cli_version(["opencode", "--version"]) if opencode_bin else None},
        "ollama": {"installed": bool(ollama_bin) or bool(ost["running"]),
                   "version": ost.get("version") or (_cli_version(["ollama", "--version"]) if ollama_bin else None),
                   "running": bool(ost["running"]), "host": str(ost["host"]).rstrip("/")},
        "models": {pins["pippin_pm"]: pins["pippin_pm"] in pulled,
                   pins["pippin_ghost"]: pins["pippin_ghost"] in pulled},
        "plugin_installed": _opencode_installed_version(root) if root is not None else None,
    }


def _weave_consent(what: str, cmd: str, assume_yes: bool) -> bool:
    """The /install-style consent gate (sovereignty rule 1): show the exact upstream command, ask
    before running it. Non-interactive runs never execute an installer silently — they print the
    command and skip, so a headless `init` stays side-effect free."""
    print(f"    $ {cmd}")
    if assume_yes:
        return True
    if not _init_is_interactive():
        info(f"non-interactive — {what} not installed. Run the command above yourself, "
             "or `celeborn weave --yes`.")
        return False
    try:
        return input(f"  Run the official {what} installer? [Y/n] ").strip().lower() not in ("n", "no")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _install_opencode(pins: dict, assume_yes: bool) -> bool:
    """OpenCode via its own official installer, pinned to the tested release (rules 1+3), with
    attribution before acting (rule 2). The installer streams to the terminal."""
    import os
    import shutil
    import subprocess
    print(f"\n{_WEAVE_ATTRIBUTION['opencode']}")
    print(f"  pinned version: {pins['opencode_version']} — the release Celeborn's plugin is tested against")
    if not _weave_consent("OpenCode",
                          f"VERSION={pins['opencode_version']} {_WEAVE_OPENCODE_INSTALL}", assume_yes):
        return False
    try:
        subprocess.run(["bash", "-c", _WEAVE_OPENCODE_INSTALL],
                       env=dict(os.environ, VERSION=pins["opencode_version"]),
                       timeout=600, check=True)
    except Exception as e:  # noqa: BLE001 — a failed upstream installer must degrade to a pointer
        warn(f"OpenCode install did not complete ({e}) — install it yourself from https://opencode.ai")
        return False
    if shutil.which("opencode") is None:
        info("OpenCode installed — open a fresh shell (its installer updates PATH) so `opencode` resolves")
    return True


def _install_ollama(assume_yes: bool) -> bool:
    """Ollama via its official channel — brew on macOS, the official install.sh elsewhere (rule 1).
    The version floats above the tested floor (rule 3): whatever the official channel ships is fine."""
    import shutil
    import subprocess
    print(f"\n{_WEAVE_ATTRIBUTION['ollama']}")
    if sys.platform == "darwin":
        if shutil.which("brew") is None:
            info("no Homebrew here — install Ollama from https://ollama.com/download (the macOS app), "
                 "then re-run `celeborn weave`")
            return False
        if not _weave_consent("Ollama", "brew install ollama", assume_yes):
            return False
        try:
            subprocess.run(["brew", "install", "ollama"], timeout=1200, check=True)
            return True
        except Exception as e:  # noqa: BLE001
            warn(f"brew install ollama did not complete ({e}) — install from https://ollama.com/download")
            return False
    if not _weave_consent("Ollama", _WEAVE_OLLAMA_INSTALL_SH, assume_yes):
        return False
    try:
        subprocess.run(["sh", "-c", _WEAVE_OLLAMA_INSTALL_SH], timeout=1200, check=True)
        return True
    except Exception as e:  # noqa: BLE001
        warn(f"Ollama install did not complete ({e}) — install from https://ollama.com/download")
        return False


def _ollama_ensure_running(host: str) -> bool:
    """Best-effort: make the Ollama daemon answer at `host`, spawning a detached `ollama serve` when
    the binary is present but the daemon is down. Full start/stop/health lifecycle is CELE-t375."""
    import shutil
    import subprocess
    import time
    host = host.rstrip("/")
    if _http_reachable(host + "/api/version"):
        return True
    if shutil.which("ollama") is None:
        return False
    try:
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return False
    for _ in range(20):
        time.sleep(0.5)
        if _http_reachable(host + "/api/version"):
            return True
    return False


def _pull_pippin_models(ctx: Path | None, status: dict, assume_yes: bool) -> bool:
    """Pull whichever pinned Pippin tags are missing — attribution once, before acting (rule 2),
    `ollama pull` progress streaming to the terminal. Returns True when both tags are present."""
    import shutil
    import subprocess
    host = status["ollama"]["host"]
    if not _ollama_ensure_running(host):
        warn("Ollama daemon not reachable — start it (`ollama serve`) and re-run `celeborn weave`")
        return False
    status = _weave_status(ctx)                    # daemon answers now — the model list is real
    missing = [tag for tag, present in status["models"].items() if not present]
    if not missing:
        return True
    print(f"\n{_WEAVE_ATTRIBUTION['qwen']}")
    pins = status["pins"]
    roles = {pins["pippin_pm"]: "Pippin · PM (non-thinking)", pins["pippin_ghost"]: "Pippin · ghost (thinking)"}
    for tag in missing:
        print(f"  {tag} — {roles.get(tag, tag)} · ~2.5 GB")
    if not _weave_consent("Qwen3-4b model pull",
                          " && ".join(f"ollama pull {t}" for t in missing), assume_yes):
        return False
    done = True
    for tag in missing:
        try:
            if shutil.which("ollama"):
                subprocess.run(["ollama", "pull", tag], timeout=3600, check=True)
            else:
                res = _http_json("POST", host + "/api/pull", {"name": tag, "stream": False}, timeout=3600.0)
                if isinstance(res, dict) and res.get("error"):
                    raise RuntimeError(res["error"])
            ok(f"pulled {tag}")
        except Exception as e:  # noqa: BLE001
            warn(f"pull {tag} failed ({e}) — retry with `celeborn ollama pull {tag}`")
            done = False
    return done


def _opencode_global_config_dir() -> Path:
    """OpenCode's GLOBAL config home. $OPENCODE_CONFIG_HOME wins (OpenCode's own override), then
    $XDG_CONFIG_HOME/opencode, then ~/.config/opencode (all platforms — probed live, CELE-t204)."""
    import os
    explicit = os.environ.get("OPENCODE_CONFIG_HOME")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "opencode"


def _merge_global_opencode_config() -> str:
    """Additively merge the packaged provider block into the GLOBAL opencode.json so the Pippin
    provider refs (ollama/qwen3:4b-instruct + ollama/qwen3:4b) resolve in every OpenCode project —
    INTEGRATION.md §6's never-clobber merge at global scope. Existing keys always win; a jsonc or
    invalid config is left untouched with a hand-merge note. Returns merged|current|skipped."""
    module = _opencode_module_dir()
    if module is None:
        return "skipped"
    try:
        ref = json.loads((module / "opencode.json").read_text())
    except Exception:
        return "skipped"
    incoming = {k: ref[k] for k in ("$schema", "provider") if k in ref}
    cfg_dir = _opencode_global_config_dir()
    cfg_path = cfg_dir / "opencode.json"
    if not cfg_path.is_file() and (cfg_dir / "opencode.jsonc").is_file():
        warn("global config is opencode.jsonc (comments) — provider block NOT auto-merged; add it "
             f"by hand from {module / 'opencode.json'}")
        return "skipped"
    try:
        existing = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
        if not isinstance(existing, dict):
            raise ValueError("config is not a JSON object")
    except Exception:
        warn(f"{cfg_path} is not valid JSON — left untouched; merge the provider block by hand")
        return "skipped"
    merged, changed = _merge_opencode_config(existing, incoming)
    if changed or not cfg_path.is_file():
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(merged, indent=2) + "\n")
        return "merged"
    return "current"


def _weave(ctx: Path | None, *, assume_yes: bool = False, skip_models: bool = False) -> bool:
    """The sovereign install (CELE-t374): detect → install-from-upstream (consented, attributed) →
    pull Pippin → wire the project plugin → merge the global config. Idempotent: with everything
    present it is a quick all-aligned read plus wiring refresh, no prompts, no installer runs.
    Returns True when the full weave is in place."""
    st = _weave_status(ctx)
    pins = st["pins"]
    # OpenCode — the harness.
    if st["opencode"]["installed"]:
        info(f"OpenCode {st['opencode']['version'] or '?'} detected (tested pin {pins['opencode_version']})")
    elif _install_opencode(pins, assume_yes):
        st = _weave_status(ctx)
    # Ollama — the model runtime.
    if st["ollama"]["installed"]:
        info(f"Ollama {st['ollama']['version'] or '?'} detected "
             f"({'running' if st['ollama']['running'] else 'not running'} · tested floor {pins['ollama_floor']})")
    elif _install_ollama(assume_yes):
        st = _weave_status(ctx)
    # Pippin — the local model, both modes (PM non-thinking + ghost thinking; contract §3).
    if not skip_models and st["ollama"]["installed"]:
        if all(st["models"].values()):
            info(f"Pippin present: {pins['pippin_pm']} (PM) + {pins['pippin_ghost']} (ghost)")
        elif _pull_pippin_models(ctx, st, assume_yes):
            st = _weave_status(ctx)
    # Adapter glue — the only surfaces Celeborn owns (contract golden rule): the project plugin
    # unit and the additive config merges. Never a byte inside an upstream tree.
    if ctx is not None and _opencode_module_dir() is not None and _wire_opencode(ctx.parent):
        info("project wired: plugin → .opencode/plugin/ · Pippin PM agent → .opencode/agent/ "
             "· provider block → opencode.json")
    g = _merge_global_opencode_config()
    if g == "merged":
        ok(f"global OpenCode config gained the Ollama/Pippin provider block "
           f"({_opencode_global_config_dir() / 'opencode.json'})")
    elif g == "current":
        info("global OpenCode config already carries the provider block")
    st = _weave_status(ctx)
    woven = (st["opencode"]["installed"] and st["ollama"]["installed"]
             and (skip_models or all(st["models"].values())))
    if woven:
        ok(f"sovereign weave in place: OpenCode {st['opencode']['version'] or '?'} + "
           f"Ollama {st['ollama']['version'] or '?'} + Pippin ({pins['pippin_pm']} · {pins['pippin_ghost']})")
    else:
        info("weave incomplete — re-run `celeborn weave` any time; every step resumes where it left off")
    return woven


def cmd_weave(args):
    """`celeborn weave [install|status]` — the sovereign weave (CELE-t374): blend OpenCode + Ollama +
    Qwen3-4b (Pippin) into a free local agent stack, installing each ONLY from its own official
    upstream channel with attribution (references/weave-contract.md). `install` is idempotent;
    `status` is a pure read. `--yes` pre-consents to the upstream installers (scripted installs)."""
    ctx = require_context(args)
    action = (getattr(args, "weave_action", None) or "install").strip().lower()
    if action == "install":
        _weave(ctx, assume_yes=getattr(args, "yes", False),
               skip_models=getattr(args, "no_models", False))
        return
    if action == "status":
        st = _weave_status(ctx)
        if getattr(args, "json", False):
            print(json.dumps(st))
            return
        pins = st["pins"]
        oc, ol = st["opencode"], st["ollama"]
        print(f"Sovereign weave — pins: OpenCode {pins['opencode_version']} · Ollama ≥{pins['ollama_floor']} "
              f"· Pippin {pins['pippin_pm']} + {pins['pippin_ghost']}")
        print(f"  OpenCode:     {('● ' + (oc['version'] or 'installed')) if oc['installed'] else '○ not installed'}")
        print(f"  Ollama:       {('● ' + (ol['version'] or 'installed')) if ol['installed'] else '○ not installed'}"
              + ((" · running" if ol["running"] else " · not running") if ol["installed"] else ""))
        for tag, present in st["models"].items():
            role = "PM   " if tag == pins["pippin_pm"] else "ghost"
            print(f"  Pippin·{role}: {'● pulled' if present else '○ not pulled'} ({tag})")
        plug = st["plugin_installed"]
        print(f"  plugin:       {('● v' + plug) if plug else '○ not wired'} (this project)")
        return
    die(f"unknown weave subcommand: {action}")


# --------------------------------------------------------------------------- Engine Room (CELE-t375)
#
# THE ENGINE ROOM — lifecycle (start/stop/restart/health) for the two local engines the sovereign
# weave installs: the LOCAL CODE ENGINE (the agent harness `opencode serve`, default :4096 — what the
# Stage and Machine Room windows talk to) and the LOCAL MODEL ENGINE (the local model runtime daemon,
# default :11434, that serves Pippin). De-branded on purpose (operator 2026-07-08): runtime status
# names engines by FUNCTION, never by vendor — "Local Code Engine", "Local Model Engine" — rolled up
# as the Engine Room, reported Scotty-style ("Engine Room status: All systems nominal"). Upstream
# ATTRIBUTION (naming OpenCode/Ollama + their licenses) lives at INSTALL time only (`celeborn weave`,
# contract §2), a different surface — so de-branding here does not break sovereignty rule 2.
#
# SOVEREIGNTY, made mechanical (contract rule 1/4/5): Celeborn MANAGES processes it started but never
# OWNS the engines. It records its own spawns in a pidfile (.context/.engine-{code,model}.pid); a
# reachable engine with no live managed pid is `external` — you started it, and Celeborn will NEVER
# stop or restart it, only report it. `down`/`restart` touch a `managed` process only.

_ENGINES = ("code", "model")
_ENGINE_LABEL = {"code": "Local Code Engine", "model": "Local Model Engine"}
_ENGINE_GLYPH = {"nominal": "●", "degraded": "◐", "down": "○", "not-installed": "·"}


def _pid_alive(pid: int) -> bool:
    """True iff a process with this pid exists (signal 0 probe). A pid owned by another user counts
    as alive (PermissionError = it's there)."""
    import os
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _engine_pidfile(ctx: Path, engine: str) -> Path:
    return ctx / (".engine-code.pid" if engine == "code" else ".engine-model.pid")


def _engine_logfile(ctx: Path, engine: str) -> Path:
    return ctx / (".engine-code.log" if engine == "code" else ".engine-model.log")


def _read_managed_pid(ctx: Path, engine: str) -> int | None:
    """The pid Celeborn recorded when it spawned this engine, iff still alive. Clears a stale pidfile
    (process gone) as a side effect, so `external`/`down` never read a dead pid back."""
    pf = _engine_pidfile(ctx, engine)
    try:
        pid = int(pf.read_text().strip().split()[0])
    except Exception:
        return None
    if _pid_alive(pid):
        return pid
    try:
        pf.unlink()
    except Exception:
        pass
    return None


def _engine_base(engine: str, cfg: dict) -> str:
    key = "opencode_serve_url" if engine == "code" else "ollama_host"
    return str(cfg.get(key) or DEFAULTS[key]).rstrip("/")


def _engine_reachable(engine: str, base: str) -> bool:
    if engine == "code":
        return _http_reachable(base + "/app") or _http_reachable(base + "/session")
    return _http_reachable(base + "/api/version")


def _engine_binary(engine: str) -> str | None:
    import shutil
    return shutil.which("opencode" if engine == "code" else "ollama")


def _engine_state(ctx: Path, engine: str, cfg: dict) -> dict:
    """One engine's settled state + provenance — a pure read (a health probe + the pidfile), no
    mutation beyond clearing a stale pidfile. state ∈ {not-installed, down, degraded, nominal};
    provenance ∈ {managed, external, None}: managed = Celeborn spawned it, external = you did (and
    Celeborn will never stop it)."""
    base = _engine_base(engine, cfg)
    reachable = _engine_reachable(engine, base)
    managed_pid = _read_managed_pid(ctx, engine)
    installed = _engine_binary(engine) is not None or reachable
    if reachable:
        state = "nominal"
        provenance = "managed" if managed_pid else "external"
    elif managed_pid:
        state, provenance = "degraded", "managed"      # our process is alive but not answering
    elif not installed:
        state, provenance = "not-installed", None
    else:
        state, provenance = "down", None
    version = None
    if engine == "model" and reachable:
        try:
            version = (_http_json("GET", base + "/api/version", timeout=1.0) or {}).get("version")
        except Exception:
            version = None
    return {"engine": engine, "label": _ENGINE_LABEL[engine], "base": base, "state": state,
            "provenance": provenance, "managed_pid": managed_pid, "installed": installed,
            "reachable": reachable, "version": version}


def _engine_room_headline(engines: dict) -> str:
    """The Scotty rollup line over both engines' states."""
    states = {e: engines[e]["state"] for e in engines}
    if all(s == "nominal" for s in states.values()):
        return "All systems nominal"
    if any(s == "degraded" for s in states.values()):
        strained = ", ".join(engines[e]["label"] for e, s in states.items() if s == "degraded")
        return f"degraded — {strained} online but not answering"
    up = [e for e, s in states.items() if s == "nominal"]
    down = [engines[e]["label"] for e, s in states.items() if s in ("down", "not-installed")]
    if not up:
        return "offline — both engines down"
    return f"partial power — {', '.join(down)} down"


def _engine_room_status(ctx: Path) -> dict:
    cfg = load_config(ctx)
    engines = {e: _engine_state(ctx, e, cfg) for e in _ENGINES}
    return {"engines": engines, "headline": _engine_room_headline(engines)}


def _engine_spawn_argv(engine: str, base: str) -> tuple:
    """The detached-serve command per engine, honoring the configured host/port. Code engine =
    `opencode serve --hostname H --port P`; model engine = `ollama serve` with $OLLAMA_HOST set so a
    non-default base is respected. Returns (argv, env)."""
    import os
    from urllib.parse import urlparse
    u = urlparse(base)
    host = u.hostname or "127.0.0.1"
    if engine == "code":
        return ["opencode", "serve", "--hostname", host, "--port", str(u.port or 4096)], dict(os.environ)
    env = dict(os.environ)
    env["OLLAMA_HOST"] = f"{host}:{u.port or 11434}"
    return ["ollama", "serve"], env


def _engine_start(ctx: Path, engine: str, cfg: dict, timeout: float = 20.0) -> dict:
    """Start an engine Celeborn can manage. No-op (with a note) when already reachable — we never
    spawn a second daemon or fight a user-started one. Spawns detached in its own session/group so a
    later managed `down` can signal the whole group, and records the pid. Returns the fresh state,
    tagged `changed`."""
    import subprocess
    import time
    st = _engine_state(ctx, engine, cfg)
    if st["reachable"]:
        st["changed"] = False
        st["note"] = ("already running (Celeborn-managed)" if st["provenance"] == "managed"
                      else "already running — you started it, left as-is")
        return st
    if not st["installed"]:
        st["changed"] = False
        st["note"] = "not installed — run `celeborn weave` to install the local engines"
        return st
    log = _engine_logfile(ctx, engine)
    try:
        logf = open(log, "ab")
    except Exception:
        logf = subprocess.DEVNULL
    argv, env = _engine_spawn_argv(engine, st["base"])
    try:
        proc = subprocess.Popen(argv, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                                start_new_session=True, cwd=str(ctx.parent), env=env)
    except Exception as e:  # noqa: BLE001 — a failed spawn degrades to a note, never a crash
        st["changed"] = False
        st["note"] = f"could not start ({e})"
        return st
    _engine_pidfile(ctx, engine).write_text(f"{proc.pid}\n")
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if _engine_reachable(engine, st["base"]):
            break
        if proc.poll() is not None:                    # died on startup — stop waiting
            break
    fresh = _engine_state(ctx, engine, cfg)
    fresh["changed"] = fresh["reachable"]
    if not fresh["reachable"]:
        fresh["note"] = f"did not come up within {int(timeout)}s — see {log.name} in .context/"
    return fresh


def _engine_stop(ctx: Path, engine: str, cfg: dict, timeout: float = 12.0) -> dict:
    """Stop an engine ONLY if Celeborn manages it (sovereignty rule): an `external` (user-started)
    engine is left running with a note. SIGTERM the whole process group, escalating to SIGKILL if it
    clings past `timeout`."""
    import os
    import signal
    import time
    st = _engine_state(ctx, engine, cfg)
    if st["state"] in ("down", "not-installed"):
        st["changed"] = False
        st["note"] = "already down"
        return st
    if st["provenance"] == "external":
        st["changed"] = False
        st["note"] = "running, but you started it — Celeborn won't stop a daemon it didn't start"
        return st
    pid = st["managed_pid"]
    if not pid:
        st["changed"] = False
        st["note"] = "no managed process to stop"
        return st
    def _sig(signum):
        try:
            os.killpg(os.getpgid(pid), signum)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                os.kill(pid, signum)
            except Exception:
                pass
    _sig(signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.3)
    if _pid_alive(pid):
        _sig(signal.SIGKILL)
    try:
        _engine_pidfile(ctx, engine).unlink()
    except Exception:
        pass
    fresh = _engine_state(ctx, engine, cfg)
    fresh["changed"] = not fresh["reachable"]
    if fresh["reachable"]:
        fresh["note"] = "still reachable after stop — another process may hold the port"
    return fresh


def _engine_restart(ctx: Path, engine: str, cfg: dict) -> dict:
    """Stop (if managed) then start. An external engine is never restarted — Celeborn didn't start
    it, so it doesn't get to bounce it."""
    st = _engine_state(ctx, engine, cfg)
    if st["provenance"] == "external":
        st["changed"] = False
        st["note"] = "running, but you started it — Celeborn won't restart a daemon it didn't start"
        return st
    if st["state"] not in ("down", "not-installed"):
        _engine_stop(ctx, engine, cfg)
    return _engine_start(ctx, engine, cfg)


def _print_engine_room(st: dict, engines: list):
    print(f"Engine Room status: {st['headline']}")
    for e in engines:
        r = st["engines"][e]
        glyph = _ENGINE_GLYPH.get(r["state"], "·")
        ver = f" · v{r['version']}" if r.get("version") else ""
        prov = f" · {r['provenance']}" if r.get("provenance") else ""
        pid = f" (pid {r['managed_pid']})" if r.get("managed_pid") else ""
        print(f"  {glyph} {r['label']:<18} {r['state']:<13} {r['base']}{ver}{prov}{pid}")


def cmd_engine_room(args):
    """`celeborn engine-room {status|up|down|restart} [code|model|all]` (CELE-t375) — lifecycle for the
    two local engines the sovereign weave installs: the Local Code Engine (`opencode serve`, the agent
    harness the Stage talks to) and the Local Model Engine (the model runtime that serves Pippin).
    Reports Scotty-style; starts/stops only processes Celeborn started — a user-started daemon is
    reported, never touched (the sovereignty rule). `--json` backs the board Settings + Stage."""
    ctx = require_context(args)
    cfg = load_config(ctx)
    action = (getattr(args, "engine_action", None) or "status").strip().lower()
    target = (getattr(args, "target", None) or "all").strip().lower()
    if target not in ("code", "model", "all"):
        die(f"unknown target '{target}' — use code, model, or all")
    engines = list(_ENGINES) if target == "all" else [target]
    if action == "status":
        st = _engine_room_status(ctx)
        if getattr(args, "json", False):
            print(json.dumps(st))
            return
        _print_engine_room(st, engines)
        return
    if action in ("up", "down", "restart"):
        fn = {"up": _engine_start, "down": _engine_stop, "restart": _engine_restart}[action]
        results = {e: fn(ctx, e, cfg) for e in engines}
        room = _engine_room_status(ctx)
        if getattr(args, "json", False):
            print(json.dumps({"action": action, "engines": results, "room": room}))
            return
        for e in engines:
            r = results[e]
            glyph = _ENGINE_GLYPH.get(r["state"], "·")
            note = f" — {r['note']}" if r.get("note") else ""
            print(f"  {glyph} {r['label']}: {r['state']}{note}")
        print(f"Engine Room status: {room['headline']}")
        return
    die(f"unknown engine-room subcommand: {action}")


def _version_lt(a: str, b: str) -> bool:
    """True iff dotted-numeric version `a` < `b` (pre-release/build suffixes ignored). Best-effort —
    a non-numeric component stops the numeric compare there."""
    def parts(v):
        out = []
        for tok in str(v).split("."):
            m = re.match(r"\d+", tok)
            if not m:
                break
            out.append(int(m.group()))
        return out
    pa, pb = parts(a), parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa < pb


# NOTE (CELE-t228): `.context/` is private-only. The old `_decide_private` / `_repo_visibility`
# public-vs-private toggle (and the `--public` commit path) were removed — a public install is
# impossible by design. `celeborn scaffold` always gitignores `.context/`; memory travels via
# `celeborn sync`, never git.


def _append_gitignore_block(gi: Path, sentinel: str, block: str) -> bool:
    """Append `block` to .gitignore unless `sentinel` already appears. Idempotent. Returns whether
    it wrote anything."""
    existing = gi.read_text() if gi.is_file() else ""
    if sentinel in existing:
        return False
    prefix = "" if (existing.endswith("\n") or not existing) else "\n"
    with gi.open("a") as f:
        f.write(prefix + block)
    return True


def _ensure_gitignore(root: Path, private: bool = False):
    gi = root / ".gitignore"
    # The auto-captured tier is ALWAYS local-only (verbatim prompts + paths) — in every mode.
    _append_gitignore_block(
        gi, ".context/auto/",
        "\n# Celeborn Automatic Context Record is LOCAL-ONLY (verbatim prompts + paths).\n"
        "# It still rides `celeborn sync`.\n.context/auto/\n.context/activity.md\n"
        ".context/touches.json\n")
    _append_gitignore_block(
        gi, ".context/.agents.json",
        "\n# Celeborn agent identity registry (handle -> family/model) — local cache.\n"
        ".context/.agents.json\n")
    _append_gitignore_block(
        gi, ".context/.alerts.json",
        "\n# Celeborn live blocked-progress alerts (CELE-t169) — transient local state.\n"
        ".context/.alerts.json\n")
    if private:
        if _append_gitignore_block(
                gi, "/.context/",
                "\n# Celeborn working memory kept PRIVATE: a public repo means a public\n"
                "# .context/. Carry it across devices with `celeborn sync`, not git.\n/.context/\n"):
            ok("gitignored /.context/ (private — sync across devices with `celeborn sync`)")
        return
    if _append_gitignore_block(
            gi, ".context/index.db",
            "\n# Celeborn derived index (regenerable)\n.context/index.db\n.context/index.db-*\n"):
        ok("added .context/index.db to .gitignore")
    _append_gitignore_block(
        gi, ".context/tasks.json",
        "\n# Celeborn derived task board (regenerable from tasks.md)\n.context/tasks.json\n")
    _append_gitignore_block(
        gi, ".context/.board.pid",
        "\n# Celeborn board viewer runtime (ensure-on-orient) — local-only.\n"
        ".context/.board.pid\n.context/.board.log\n")
    _append_gitignore_block(
        gi, ".context/.engine-",
        "\n# Celeborn Engine Room runtime (CELE-t375) — managed-daemon pids + logs, local-only.\n"
        ".context/.engine-code.pid\n.context/.engine-code.log\n"
        ".context/.engine-model.pid\n.context/.engine-model.log\n")
    _append_gitignore_block(
        gi, ".context/.panic/",
        "\n# Celeborn pre-compaction panic-saves — local snapshot/restore points.\n"
        ".context/.panic/\n")
    _append_gitignore_block(
        gi, ".context/outbox/",
        "\n# Celeborn prompt outbox — local per-agent hand-off queues, drained into the live session.\n"
        ".context/outbox/\n")
    _append_gitignore_block(
        gi, ".context/.jira-autopush.json",
        "\n# Celeborn Jira auto-push queue (local debounce state)\n.context/.jira-autopush.json\n")
    _append_gitignore_block(
        gi, ".context/.arch-trace.json",
        "\n# Celeborn auto-architecture-trace bookkeeping (CELE-t201) — transient local state.\n"
        ".context/.arch-trace.json\n")
    _append_gitignore_block(
        gi, ".context/progress.json",
        "\n# Celeborn progress-engine bookkeeping (floor/signals/nudge state) — local-only.\n"
        ".context/progress.json\n")
    _append_gitignore_block(
        gi, ".context/product-local.json",
        "\n# Celeborn product federation (CELE-t190): per-machine facet→checkout path bindings.\n"
        "# product.md (the product facts) IS committed; only these local paths stay out of git.\n"
        ".context/product-local.json\n")


# Managed block that announces Celeborn through the file Claude Code auto-loads (CLAUDE.md). This is
# how a fresh agent learns context lives in .context/ even before the skill or hooks are active.
CLAUDE_MD_BEGIN = "<!-- BEGIN CELEBORN (managed block — regenerated by `celeborn init`) -->"
CLAUDE_MD_END = "<!-- END CELEBORN -->"
AGENTS_MD_BEGIN = "<!-- BEGIN CELEBORN (managed block — regenerated by `celeborn init`) -->"
AGENTS_MD_END = "<!-- END CELEBORN -->"


def _rules_rehydration_body() -> str:
    """Orient + kanban guidance written into host rules files (CLAUDE.md, AGENTS.md) at init."""
    return (
        "## Context — maintained by Celeborn\n\n"
        "This project's long-term context lives in `.context/`, managed by Celeborn\n"
        "(<https://github.com/cloud-dancer-labs/celeborn>). **Orient before acting:** read\n"
        "`.context/state.md` (the headline) and `.context/session.json`; then `.context/notes.md`\n"
        "(open threads, constraints, working detail) and `.context/durable/manifest.md`. Run\n"
        "`celeborn search \"<topic>\"` to recall older details, and check `.context/journal.md` so you\n"
        "don't redo finished work. As you make meaningful changes, record them back into the authored\n"
        "tiers (state / journal / decisions / learnings) — that is what keeps the next session cheap.\n\n"
        "**Multi-agent kanban:** When this project uses Celeborn tasks (`.context/tasks.md`), every\n"
        "model sharing the repo sees the same board on orient — who's `doing` what. Celeborn is the\n"
        "live source of truth for work in progress; external issue trackers are downstream reporting\n"
        "only. Before taking a card, read the board (`celeborn tasks`) and choose a TODO that won't\n"
        "interrupt another agent's in-flight work. **One DOING card per agent** — ship (`celeborn ship\n"
        "<id>`) or demote before claiming another; `celeborn claim` blocks while you have other DOING\n"
        "cards (pass `--force` only to override). Prefer `celeborn tasks add \"…\" --claim --by <you>`\n"
        "so the new id is never guessed. Claim with `celeborn claim <id> --by <your name>` — owner and\n"
        "DOING update for everyone. Pasting a copied card (its `⟨celeborn:tN⟩` marker) also claims on\n"
        "receipt.\n\n"
        "**Every card has a Stop condition:** each task carries a logical **Stop condition** (its `stop`\n"
        "field) — a clearly-defined \"this is a clean place to stop\" marker that tells you when the card\n"
        "is at a defensible `/clear` point. `celeborn tasks add` auto-fills a generic default so no card\n"
        "is ever stop-less; when you claim a card, read its Stop condition and, if it still carries the\n"
        "generic default, replace it with a real one for that card: `celeborn tasks edit <id> --stop\n"
        "\"<condition>\"` (or set it up front with `tasks add … --stop \"…\"`). Honor it: reaching the Stop\n"
        "condition is the signal that you may cleanly `/clear`. `celeborn doctor` flags open cards with\n"
        "no Stop condition (or still on the default).\n\n"
        "**Identify yourself (once, on orient):** so other agents see *who* is on a file — not just a\n"
        "bare handle — declare your family + specific model once per session:\n"
        "`celeborn identify --family <Claude|Grok|GPT|Gemini…> --model \"<e.g. Opus 4.8>\"`. After that,\n"
        "every touch/claim/ship carries it automatically. (Two same-model threads? give each a distinct\n"
        "handle via `--by` / `$CELEBORN_AGENT`.)\n\n"
        "**Multi-agent file touches:** Before editing a shared file, register it so other agents see you\n"
        "on orient: `celeborn touch <file> --task <id> --why \"<reason>\"` (your `--by`/identity is\n"
        "inherited), then `celeborn touch release <file>` when done. The `--why` is what lets the next\n"
        "agent tell a deliberate edit from a collision. Never tag every line of code with agent metadata —\n"
        "touches + journal + commit trailers (`Celeborn-Agent`, `Celeborn-Model`, `Celeborn-Task`) are the\n"
        "protocol. Shorthand: `references/multi-agent-editing.md`.\n\n"
        "**Staying current:** Celeborn lives at <https://github.com/cloud-dancer-labs/celeborn>. Run\n"
        "`celeborn version --check` now and then — it looks back at GitHub and tells you if a newer\n"
        "Celeborn is available (and how to update).\n\n"
        "**Surface context-health notices (but NOT the heartbeat):** the Celeborn `UserPromptSubmit`\n"
        "hook injects two distinct channels. The **heartbeat** (`🏹 Celeborn —> M tokens recorded this\n"
        "session · +N last turn`) is tagged *context only, do NOT surface* — it informs you, you do not\n"
        "reprint it. As the context window fills, a **context-health notice** is also injected, tagged\n"
        "*SURFACE THIS TO THE USER*: it first asks you to freshen the Hot tier (rewrite\n"
        "`.context/state.md`, update `.context/session.json`, append `.context/journal.md`) so a `/clear`\n"
        "or compaction loses nothing, then hands you a `🏹 Celeborn —>` line to relay. On surfaces like\n"
        "the Claude desktop/web app the user never sees hook output — only your reply — so when that\n"
        "notice is present: do the checkpoint, then begin your reply with the line(s) it gives you,\n"
        "verbatim on their own lines, before anything else. Don't summarize or skip them.\n\n"
        "**Grok Build — orient survives `/clear`:** Grok does not inject SessionStart hook output into\n"
        "the model. After `/clear` (or any new session), **your first action** is orient: if\n"
        "`.context/.grok-orient-pending.md` exists, read it once and delete it; else run\n"
        "`celeborn status`. Launch Grok from **this repo** so hooks bind here, not a parent\n"
        "`.context/`: `grok --cwd <project-root>`. Kanban shorthand in **this** project:\n"
        "`wire tN` / `claim tN` = take card tN; `ship tN` = close it out (`celeborn ship tN`).\n"
        "Qualified markers `⟨celeborn:slug/tN⟩` on copied cards also claim on paste.\n"
    )


def _claude_md_block() -> str:
    return f"{CLAUDE_MD_BEGIN}\n{_rules_rehydration_body()}{CLAUDE_MD_END}\n"


def _agents_md_block() -> str:
    return f"{AGENTS_MD_BEGIN}\n{_rules_rehydration_body()}{AGENTS_MD_END}\n"


def _ensure_rules_file(root: Path, filename: str, begin: str, end: str, block_fn) -> bool:
    """Annotate a host rules file with a managed Celeborn block. Idempotent: replaces the marked
    block if present, appends it otherwise, creates the file if missing. Returns whether it wrote."""
    path = root / filename
    block = block_fn()
    if not path.exists():
        path.write_text(block)
        return True
    existing = path.read_text()
    if begin in existing and end in existing:
        start = existing.index(begin)
        stop = existing.index(end) + len(end)
        new = existing[:start] + block.rstrip("\n") + existing[stop:]
        if new == existing:
            return False
        path.write_text(new)
        return True
    prefix = "" if (existing.endswith("\n") or not existing) else "\n"
    path.write_text(existing + prefix + "\n" + block)
    return True


def _ensure_claude_md(root: Path) -> bool:
    """Annotate CLAUDE.md so Claude Code — which auto-loads it — knows context lives in .context/."""
    return _ensure_rules_file(root, "CLAUDE.md", CLAUDE_MD_BEGIN, CLAUDE_MD_END, _claude_md_block)


def _ensure_agents_md(root: Path) -> bool:
    """Annotate AGENTS.md so Codex/Grok-style hosts that auto-load it get the same orient guidance."""
    return _ensure_rules_file(root, "AGENTS.md", AGENTS_MD_BEGIN, AGENTS_MD_END, _agents_md_block)


def _clip(text: str, limit: int, pointer: str) -> str:
    """Bound a Hot-tier string to `limit` chars, cutting on a line boundary and appending a pointer.

    The Orient load is injected as SessionStart additionalContext; if it outgrows the host's inline
    budget, the host persists it to a file and the model gets only a preview — automatic rehydration
    silently dies. Clipping keeps the payload small while pointing the agent at the full source.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:  # no convenient line break near the limit — hard cut
        cut = limit
    dropped = len(text) - cut
    return text[:cut].rstrip("\n") + f"\n\n… [Hot tier clipped — {dropped} more chars in {pointer}]"


def cmd_status(args):
    ctx = require_context(args)
    cfg = load_config(ctx)
    sep = "─" * 72
    full = getattr(args, "full", False)
    state_max = 0 if full else int(cfg.get("hot_state_max_chars", 4000))
    act_max = 0 if full else int(cfg.get("hot_activity_max_chars", 2000))
    focus_max = 0 if full else int(cfg.get("hot_focus_max_chars", 1500))
    tasks_max = 0 if full else int(cfg.get("hot_tasks_max_chars", 1000))
    touches_max = 0 if full else int(cfg.get("hot_touches_max_chars", 800))

    print(sep)
    print("CELEBORN — Orient load (Hot tier)")
    print(sep)

    sj = ctx / "session.json"
    if sj.is_file():
        try:
            data = json.loads(sj.read_text())
            print("session.json:")
            for k in ("focus", "next_action", "branch", "status", "stop_allowed", "updated_at"):
                if k in data:
                    val = data[k]
                    if k in ("focus", "next_action") and isinstance(val, str):
                        val = _clip(val, focus_max, f"session.json:{k}")
                    print(f"  {k}: {val}")
            if data.get("open_threads"):
                print(f"  open_threads: {len(data['open_threads'])}")
        except json.JSONDecodeError:
            warn("session.json is not valid JSON")
    print()

    _print_file(ctx / "state.md", "state.md", clip=(state_max, "state.md"))
    if (ctx / "activity.md").is_file():
        _print_file(ctx / "activity.md", "activity.md (Automatic Context Record — mechanical, always-current)",
                    clip=(act_max, "activity.md"))
    _print_file(ctx / "durable" / "manifest.md", "durable/manifest.md")

    tasks_summary = _tasks_orient_summary(ctx, _load_tasks(ctx))
    if tasks_summary:
        print(sep)
        print("tasks.md (board — in flight; full board: `celeborn tasks`):")
        print(sep)
        print(_clip(tasks_summary, tasks_max, "tasks.md"))
        print()

    touches_summary = _touches_orient_summary(ctx)
    if touches_summary:
        print(sep)
        print("touches.json (active file edits — who is in which file; `celeborn touch list`):")
        print(sep)
        print(_clip(touches_summary, touches_max, "touches.json"))
        print()

    intents_summary = _intents_orient_summary(ctx)
    if intents_summary:
        print(sep)
        print("intents (fleet blackboard — planned commits; `celeborn intent list`):")
        print(sep)
        print(intents_summary)
        print()

    print(sep)
    print("Deeper tiers (on demand — not loaded):")
    notes = ctx / "notes.md"
    if notes.is_file():
        nlines = len(notes.read_text().splitlines())
        print(f"  notes.md: working detail — open threads, constraints, context ({nlines} lines) "
              f"— read it for depth")
    _, entries = split_journal((ctx / "journal.md").read_text()) if (ctx / "journal.md").is_file() else ("", [])
    archive_files = list((ctx / "journal-archive").glob("*.md"))
    learn = _count_sections(ctx / "learnings.md")
    dec = _count_sections(ctx / "decisions.md")
    durable_docs = [p for p in (ctx / "durable").glob("*.md") if p.name != "manifest.md"]
    keep = cfg["journal_keep_entries"]
    flag = "  (over budget — run `celeborn archive`)" if len(entries) > keep else ""
    print(f"  journal.md: {len(entries)} entries (keep {keep}){flag}")
    print(f"  journal-archive/: {len(archive_files)} file(s)")
    _, state_hist = plan_state_archive((ctx / "state.md").read_text(), cfg.get("state_keep_sessions", 6)) \
        if (ctx / "state.md").is_file() else ("", [])
    state_arch_files = list((ctx / STATE_ARCHIVE_DIRNAME).glob("*.md"))
    if state_hist or state_arch_files:
        sflag = f"  ({len(state_hist)} over budget — run `celeborn archive`)" if state_hist else ""
        print(f"  state-archive/: {len(state_arch_files)} file(s){sflag}")
    print(f"  learnings.md: {learn} · decisions.md: {dec} · durable docs: {len(durable_docs)}")

    idx = ctx / INDEX_NAME
    if idx.is_file():
        stale = _index_is_stale(ctx)
        print(f"  index.db: present{' (STALE — run `celeborn index`)' if stale else ''}")
    else:
        print("  index.db: absent (run `celeborn index` to enable search)")
    print(sep)
    print("Memory economy (estimated):")
    for line in metrics_summary(ctx):
        print(f"  {line}")
    print(sep)


def _print_file(path: Path, label: str, clip: tuple = (0, "")):
    sep = "─" * 72
    print(sep)
    print(f"{label}:")
    print(sep)
    if path.is_file():
        body = path.read_text().rstrip("\n")
        limit, pointer = clip
        if limit:
            body = _clip(body, limit, pointer)
        print(body)
    else:
        warn(f"missing: {label}")
    print()


def _count_sections(path: Path) -> int:
    """Count real entries — level-2 headings — ignoring the file's H1 title and preamble."""
    if not path.is_file():
        return 0
    return sum(1 for s in parse_sections(path.read_text()) if s["level"] == 2)


def cmd_index(args):
    import sqlite3  # lazy: only the DB-touching commands pay the import

    ctx = require_context(args)
    if not SCHEMA_PATH.is_file():
        die(f"schema not found at {SCHEMA_PATH}")
    db_path = ctx / INDEX_NAME
    conn = sqlite3.connect(str(db_path))
    try:
        # The index is derived and regenerable, so durability is irrelevant: skip fsync and
        # disk journaling for a much faster bulk build. A crash just means re-running `index`.
        conn.executescript(
            "PRAGMA journal_mode = MEMORY;"
            "PRAGMA synchronous = OFF;"
            "PRAGMA temp_store = MEMORY;"
        )
        conn.executescript(SCHEMA_PATH.read_text())
        rows = 0
        link_rows = 0
        for tier, glob in TIER_GLOBS:
            for path in sorted(ctx.glob(glob)):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(ctx))
                mtime = _dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
                for s in parse_sections(path.read_text()):
                    if not (s["body"] or s["title"]):
                        continue
                    conn.execute(
                        "INSERT INTO memory_fts (title, body, tags, tier, source_file, anchor, updated_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (s["title"], s["body"], s["tags"], tier, rel, s["anchor"], mtime),
                    )
                    rows += 1
                    for tgt in s["links"]:
                        conn.execute(
                            "INSERT INTO links (src_file, src_anchor, target) VALUES (?,?,?)",
                            (rel, s["anchor"], tgt),
                        )
                        link_rows += 1
        conn.execute("INSERT INTO meta (key, value) VALUES ('built_at', ?)", (now_iso(),))
        # FTS5: merge the segments left by the bulk insert into one b-tree -> faster MATCH queries.
        conn.execute("INSERT INTO memory_fts (memory_fts) VALUES ('optimize')")
        conn.commit()
    finally:
        conn.close()
    print(f"Indexed {rows} section(s), {link_rows} link(s) -> {db_path.relative_to(ctx.parent)}")


def cmd_search(args):
    import sqlite3  # lazy: only the DB-touching commands pay the import

    ctx = require_context(args)
    cfg = load_config(ctx)
    db_path = ctx / INDEX_NAME
    if not db_path.is_file():
        die("no index yet. Run `celeborn index` first.")
    if _index_is_stale(ctx):
        warn("index looks stale; results may be behind the markdown. Run `celeborn index`.")
    limit = args.limit or cfg["search_default_limit"]
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        "PRAGMA query_only = ON;"
        "PRAGMA mmap_size = 67108864;"  # 64 MiB memory-mapped reads
    )
    try:
        try:
            cur = conn.execute(
                "SELECT tier, source_file, anchor, title, "
                "snippet(memory_fts, 1, '«', '»', ' … ', 14) AS snip "
                "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (args.query, limit),
            )
            results = cur.fetchall()
        except sqlite3.OperationalError as e:
            die(f"bad FTS query: {e}")
    finally:
        conn.close()

    if not results:
        print(f"No matches for: {args.query}")
        return
    print(f"{len(results)} match(es) for: {args.query}\n")
    for tier, src, anchor, title, snip in results:
        pointer = f"{src}#{anchor}" if anchor else src
        head = title or "(preamble)"
        print(f"[{tier}] {pointer}")
        print(f"    {head}")
        print(f"    {snip.strip()}")
        print()


def _archive_journal(ctx: Path, keep: int) -> int:
    """FIFO the oldest journal.md entries past `keep` into journal-archive/archive.md. Returns the
    number moved (0 = nothing to do / no journal)."""
    jpath = ctx / "journal.md"
    if not jpath.is_file():
        return 0
    header, entries = split_journal(jpath.read_text())
    if len(entries) <= keep:
        return 0
    move = entries[: len(entries) - keep]
    kept = entries[len(entries) - keep:]

    arch_dir = ctx / "journal-archive"
    arch_dir.mkdir(exist_ok=True)
    arch_path = arch_dir / "archive.md"
    prefix = arch_path.read_text() if arch_path.is_file() else "# Journal archive\n\n"
    arch_path.write_text(prefix.rstrip("\n") + "\n\n" + "".join(move).rstrip("\n") + "\n")
    jpath.write_text(header.rstrip("\n") + "\n\n" + "".join(kept).rstrip("\n") + "\n")
    return len(move)


def _archive_state(ctx: Path, keep: int) -> int:
    """FIFO the oldest dated history bullets from state.md's `## Now` (past the newest `keep`) into
    state-archive/archive.md. Structural headline bullets are left untouched. Returns the count
    moved (0 = nothing to do / no state.md / no Now section)."""
    spath = ctx / "state.md"
    if not spath.is_file():
        return 0
    text = spath.read_text()
    new_text, archived = plan_state_archive(text, keep)
    if not archived:
        return 0
    arch_dir = ctx / STATE_ARCHIVE_DIRNAME
    arch_dir.mkdir(exist_ok=True)
    arch_path = arch_dir / "archive.md"
    prefix = arch_path.read_text() if arch_path.is_file() else (
        "# State archive\n\n"
        "<!-- Dated `## Now` history bullets FIFO'd out of state.md by `celeborn archive`.\n"
        "     Cold tier — still indexed and searchable via `celeborn search`. -->\n\n"
        f"## Archived {now_stamp()}\n\n")
    body = "".join(b.rstrip("\n") + "\n" for b in archived)
    arch_path.write_text(prefix.rstrip("\n") + "\n\n" + body.rstrip("\n") + "\n")
    spath.write_text(new_text)
    return len(archived)


def _auto_archive(ctx: Path, cfg: dict):
    """Capture-time self-healing: keep journal.md and state.md within budget without a manual command.
    Best-effort and FIFO — safe to call every turn (a no-op once both tiers are under budget)."""
    if not cfg.get("auto_archive", True):
        return
    _archive_journal(ctx, cfg["journal_keep_entries"])
    _archive_state(ctx, cfg.get("state_keep_sessions", 6))
    # The rewritten source files bump their mtime, so `_index_is_stale` flags the index for the next
    # `celeborn index` automatically — archived content stays searchable once re-indexed.


def cmd_archive(args):
    ctx = require_context(args)
    cfg = load_config(ctx)
    what = getattr(args, "what", "all") or "all"
    did = False
    if what in ("all", "journal"):
        keep = args.keep if args.keep is not None else cfg["journal_keep_entries"]
        n = _archive_journal(ctx, keep)
        did = True
        print(f"journal.md: archived {n} entr(ies) -> journal-archive/archive.md (kept {keep})."
              if n else f"journal.md: within budget (keep {keep}); nothing to archive.")
    if what in ("all", "state"):
        skeep = args.state_keep if args.state_keep is not None else cfg.get("state_keep_sessions", 6)
        n = _archive_state(ctx, skeep)
        did = True
        print(f"state.md: archived {n} history bullet(s) -> {STATE_ARCHIVE_DIRNAME}/archive.md (kept {skeep})."
              if n else f"state.md: within budget (keep {skeep}); nothing to archive.")
    if did:
        print("Re-run `celeborn index` to refresh search.")


def cmd_promote(args):
    ctx = require_context(args)
    title = args.title.strip()
    note = (args.note or "").strip()
    stamp = now_stamp()
    if args.to == "learnings":
        path = ctx / "learnings.md"
        block = f"\n## {title}\n- **Lesson:** {note}\n- **Seen in:** promoted {stamp}\n"
        _append(path, block)
        print(f"Promoted to learnings.md: {title}")
    elif args.to == "durable":
        doc = args.doc or "gotchas"
        path = ctx / "durable" / f"{doc}.md"
        if not path.is_file():
            path.write_text(f"# {doc.capitalize()}\n")
            _ensure_manifest_line(ctx, doc)
        block = f"\n## {title}\n{note}\n\n*(promoted {stamp})*\n"
        _append(path, block)
        _ensure_manifest_line(ctx, doc)
        print(f"Promoted to durable/{doc}.md: {title}")
    print("Remember to remove the source note from its old tier, then `celeborn index`.")


def _append(path: Path, block: str):
    existing = path.read_text() if path.is_file() else ""
    path.write_text(existing.rstrip("\n") + "\n" + block)


def _ensure_manifest_line(ctx: Path, doc: str):
    manifest = ctx / "durable" / "manifest.md"
    text = manifest.read_text() if manifest.is_file() else "# Durable docs manifest\n"
    if f"({doc}.md)" in text:
        return
    manifest.write_text(text.rstrip("\n") + f"\n- [{doc}.md]({doc}.md) — promoted durable knowledge\n")


def cmd_handoff(args):
    ctx = require_context(args)
    data = {}
    sj = ctx / "session.json"
    if sj.is_file():
        try:
            data = json.loads(sj.read_text())
        except json.JSONDecodeError:
            warn("session.json unreadable; handoff will be sparse.")
    branch = data.get("branch") or "<branch>"
    status = data.get("status") or "in-progress"
    focus = data.get("focus") or "<focus>"
    nxt = data.get("next_action") or "<next action>"
    threads = data.get("open_threads") or []
    risks = "\n".join(f"- {t}" for t in threads) or "- (none recorded)"

    content = f"""# Handoff

<!-- Regenerated by `celeborn handoff` from state.md + session.json. -->

**Branch:** {branch} · **Status:** {status}
**Focus:** {focus}
**Next required action:** {nxt}

**Open risks / threads:**
{risks}

---

### Resume prompt (paste into a fresh thread)

> Read `.context/state.md` (the headline) and `.context/session.json`, then `.context/notes.md`
> for open threads + constraints and `.context/durable/manifest.md` for repo truths. Continue from
> the Next required action above. Run `celeborn search "<topic>"` for anything older. Do not re-do
> completed work (see `journal.md`).
"""
    (ctx / "handoff.md").write_text(content)
    m = _load_metrics(ctx)
    m["handoffs_written"] += 1
    _save_metrics(ctx, m)
    print("Wrote handoff.md")


def cmd_record(args):
    """Record a memory event for the economy estimate. Called by the hooks; safe to call manually."""
    ctx = require_context(args)
    cfg = load_config(ctx)
    cpt = cfg["chars_per_token"]
    m = _load_metrics(ctx)
    saved = 0
    hot, _ = _measure(ctx, cpt)
    if args.event == "orient":
        m["orient_events"] += 1
        if _orient_is_new_session(m, args.session or "", cfg):
            # A fresh session starts roughly at the Hot-tier load — reset the running estimate.
            m["context_estimate"] = hot
            m["last_remind_estimate"] = 0
            if _has_memory(ctx, cpt):
                m["sessions_resumed"] += 1
                saved = _credit_savings(ctx, m, cpt)
        if args.session:
            m["last_session_id"] = args.session
        m["last_orient_at"] = now_iso()
    elif args.event == "compaction":
        m["compactions_bridged"] += 1
        # Post-compaction the carried context shrinks toward the Hot load — reset the estimate.
        m["context_estimate"] = hot
        m["last_remind_estimate"] = 0
        if _has_memory(ctx, cpt):
            saved = _credit_savings(ctx, m, cpt)
    elif args.event == "clear":
        # After a /clear + rehydrate, the window is back to roughly the Hot-tier load.
        m["context_estimate"] = hot
        m["last_remind_estimate"] = 0
    elif args.event == "turn":
        m["context_estimate"] = m.get("context_estimate", 0) + max(0, args.tokens or 0)
    elif args.event == "tokens":
        # P4 (CELE-t141): a harness that KNOWS its live window (OpenCode reports per-message token
        # usage) writes the REAL number straight onto this session's capture cursor — the same
        # counter the heartbeat, the statusline, and the board's /clear-nudge bands already read
        # (contract t203 §2.1: nobody re-derives tokens their own way). `--tokens` is the window
        # as-of-now (absolute), not a delta: assistant-message usage IS the context size, and it
        # legitimately shrinks after a compaction. `live` marks the cursor as a reported window so
        # `_active_agents` emits it as a chip and the heartbeat words it as live context.
        sid = (args.session or "").strip()
        if not sid:
            die("record tokens requires --session <id>")
        total = max(0, int(args.tokens or 0))
        caps = m.get("captures") if isinstance(m.get("captures"), dict) else {}
        cur = dict(caps.get(sid) or {})
        prev = int(cur.get("tokens_session") or 0)
        cur.update({"session_id": sid, "tokens_session": total,
                    "last_delta": max(0, total - prev), "idle_streak": 0,
                    "live": True, "updated_at": now_iso()})
        # Machine-readable context-pressure flag (CELE-t207): every live-window report re-grades the
        # session against the configured soft/hard thresholds, so the board and a future auto-clear
        # can read the pressure state without re-deriving tokens (contract t203 §2.1).
        soft, hard = _context_thresholds(cfg)
        cur["pressure"] = _pressure_level(total, soft, hard)
        # Auto-clear trigger (CELE-t209, opt-in): a hard-pressure live window prints a machine
        # marker on the same stdout the OpenCode plugin already collects from this verb — zero
        # extra subprocess on the hot path. The plugin only MARKS the session pending here; the
        # actual decision (gate, cooldown, prep) is re-verified by `celeborn autoclear` at the
        # next turn boundary, so a stale marker can never clear a session by itself.
        if _autoclear_due(cfg, cur):
            print(f"autoclear: due ({sid[:8]} at {total:,} tokens ≥ hard {hard:,})")
        _write_capture(m, caps, sid, cur)
        try:
            __import__("celeborn_sync").schedule_agents_push(ctx)   # hosted chips track the live number
        except Exception:
            pass
    elif args.event == "handoff":
        m["handoffs_written"] += 1
    _save_metrics(ctx, m)
    note = f" (+~{saved:,} tokens)" if saved else ""
    if args.event in ("turn", "clear"):
        note = f" (context estimate ~{m['context_estimate']:,} tokens)"
    elif args.event == "tokens":
        note = f" ({(args.session or '')[:8]} ← {max(0, int(args.tokens or 0)):,} live context tokens)"
    print(f"recorded: {args.event}{note}")


# --------------------------------------------------------------------------- standup / changelog

def dollars_saved(ctx: Path) -> float:
    """Convert the tokens-saved estimate into a $ figure for the build-in-public flex. Rate is
    configurable (`usd_per_mtok` in .celebornrc); the saved tokens are context NOT re-loaded, i.e.
    input tokens, so the input rate is the right basis."""
    m = _load_metrics(ctx)
    rate = float(load_config(ctx).get("usd_per_mtok", 3.0))
    return m.get("tokens_saved_estimate", 0) / 1_000_000 * rate


_JOURNAL_DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?\s*(?:—|-|–)?\s*(.*)")


def _parse_dt(s: str):
    """Best-effort parse of an ISO-ish timestamp/date → datetime, or None."""
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(s.strip())
    except ValueError:
        try:
            return _dt.datetime.strptime(s.strip()[:10], "%Y-%m-%d")
        except ValueError:
            return None


def _git_commits_since(repo: Path, since: _dt.datetime) -> list[tuple[str, str]]:
    """[(short_sha, subject), …] committed at/after `since`. Empty if not a git repo / no git."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", f"--since={since.strftime('%Y-%m-%d %H:%M:%S')}",
             "--pretty=format:%h%x09%s"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    rows = []
    for line in out.stdout.splitlines():
        if "\t" in line:
            sha, subj = line.split("\t", 1)
            rows.append((sha, subj))
    return rows


def _gather_activity(ctx: Path, since: _dt.datetime) -> dict:
    """Mechanical (no-model) aggregation of 'what happened' since `since`: tasks moved to done,
    git commits, and authored journal entries — the three concrete records of progress."""
    # Tasks completed in window (state == done, updated >= since)
    done = []
    for t in _load_tasks(ctx):
        if t["state"] == "done":
            up = _parse_dt(t.get("updated", ""))
            if up and up >= since:
                done.append(t)
    # Git commits in window
    commits = _git_commits_since(ctx.parent, since)
    # Journal entries in window
    jpath = ctx / "journal.md"
    entries = []
    if jpath.is_file():
        _, blocks = split_journal(jpath.read_text())
        for blk in blocks:
            m = _JOURNAL_DATE_RE.match(blk.strip().splitlines()[0])
            if not m:
                continue
            d = _parse_dt(m.group(1))
            if d and d >= _dt.datetime(since.year, since.month, since.day):
                entries.append(m.group(3).strip() or m.group(1))
    return {"done": done, "commits": commits, "journal": entries}


def _project_name(ctx: Path) -> str:
    rc = load_config(ctx)
    return rc.get("project_name") or _repo_root_from_ctx(ctx).name or "this project"


def _render_tweet(ctx: Path, act: dict) -> str:
    """A build-in-public X post (≤280 chars) from the same activity. Mechanical/template — no model,
    no API key — so it works in the free, offline core. Leads with shipped items, closes with the
    Celeborn flex (tokens→$) so every post doubles as soft marketing."""
    name = _project_name(ctx)
    lines = [f"🏹 Building {name} in public — latest:", ""]
    items = [t["title"] for t in act["done"]] or [s for _, s in act["commits"]]
    shown = 0
    for it in items:
        if shown >= 3:
            break
        clipped = it if len(it) <= 60 else it[:57] + "…"
        lines.append(f"✅ {clipped}")
        shown += 1
    extra = []
    if act["commits"]:
        extra.append(f"🔧 {len(act['commits'])} commits")
    usd = dollars_saved(ctx)
    if usd >= 1:
        extra.append(f"💪 ~${usd:,.0f} in tokens saved by Celeborn")
    if extra:
        lines += ["", " · ".join(extra)]
    lines += ["", "#buildinpublic #AI"]
    post = "\n".join(lines)
    # Hard cap at 280; trim trailing item lines if needed.
    while len(post) > 280 and shown > 1:
        # drop the last shown ✅ line
        idx = max(i for i, l in enumerate(lines) if l.startswith("✅"))
        lines.pop(idx)
        shown -= 1
        post = "\n".join(lines)
    return post[:280]


def _render_report(ctx: Path, act: dict, title: str) -> str:
    out = [title, ""]
    if act["done"]:
        out.append("✅ Completed")
        for t in act["done"]:
            out.append(f"  • [{_display_tid(ctx, t['id'])}] {t['title']}")
        out.append("")
    if act["commits"]:
        out.append(f"🔧 Commits ({len(act['commits'])})")
        for sha, subj in act["commits"][:20]:
            out.append(f"  • {sha}  {subj}")
        if len(act["commits"]) > 20:
            out.append(f"  …and {len(act['commits']) - 20} more")
        out.append("")
    if act["journal"]:
        out.append("📓 Journal")
        for j in act["journal"]:
            out.append(f"  • {j}")
        out.append("")
    if not (act["done"] or act["commits"] or act["journal"]):
        out.append("  (nothing recorded in this window)")
    return "\n".join(out).rstrip() + "\n"


def cmd_standup(args):
    """`standup` (default 1 day) and `changelog` (default 7 days) share one engine. `--tweet` emits a
    build-in-public X post instead of the report."""
    ctx = require_context(args)
    kind = getattr(args, "kind", "standup")
    default_days = 7 if kind == "changelog" else 1
    days = getattr(args, "days", None) or default_days
    since = _dt.datetime.now() - _dt.timedelta(days=days)
    act = _gather_activity(ctx, since)

    if getattr(args, "tweet", False):
        print(_render_tweet(ctx, act))
        return
    if getattr(args, "json", False):
        print(json.dumps({"since": since.isoformat(), "days": days, **act}, indent=2, default=str))
        return
    span = "last 24h" if days == 1 else f"last {days} days"
    icon = "📋" if kind == "standup" else "📰"
    print(_render_report(ctx, act, f"{icon} Celeborn {kind} — {_project_name(ctx)} ({span})"))


# --------------------------------------------------------------------------- flex ($ Wrapped card)

def _disp_width(s: str) -> int:
    """Approximate terminal display width of `s`: emoji and East-Asian-wide glyphs take 2 cells;
    variation selectors / ZWJ / combining marks take 0. Good enough to align an ASCII box that may
    carry the 🏹/💪 branding. Pure stdlib (unicodedata) — no wcwidth dependency."""
    import unicodedata
    w = 0
    for ch in s:
        o = ord(ch)
        if o in (0x200D, 0xFE0E, 0xFE0F) or unicodedata.combining(ch):
            continue
        if (o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF
                or unicodedata.east_asian_width(ch) in ("W", "F")):
            w += 2
        else:
            w += 1
    return w


def _fmt_usd(usd: float) -> str:
    """$ figure for the flex: whole dollars once it's meaningful, cents while it's still small — so a
    fresh install flexes a real non-zero number instead of a flat '$0'."""
    return f"${usd:,.0f}" if usd >= 100 else f"${usd:,.2f}"


def _prompts_auto_allowed(m: dict) -> int:
    """Total permission prompts the user never had to click — the unified "prompts auto-allowed"
    figure the economy bar shows. Three complementary sources: the advisor's one-time allow-list rules
    generalized into wildcards (`celeborn permissions --apply`), CMM's per-call pre-clear of
    structural-query tools (CELE-t92), and the settings.json allow-list — the t100 safe baseline plus
    the user's own rules — matched per tool call at capture time. All three are prompts avoided; the
    bar sums them into one honest number (the buckets are disjoint, so nothing double-counts)."""
    generalized = int(((m.get("advisor") or {}).get("permission_rules_generalized")) or 0)
    cmm_calls = int(((m.get("cmm") or {}).get("prompts_auto_allowed")) or 0)
    allowlist = int(((m.get("permissions") or {}).get("prompts_auto_allowed")) or 0)
    return generalized + cmm_calls + allowlist


def _flex_figures(ctx: Path) -> dict:
    """The numbers behind the flex, in one place (shared by the card, the tweet, and --json)."""
    m = _load_metrics(ctx)
    return {
        "dollars_saved": round(dollars_saved(ctx), 2),
        "tokens_saved": m.get("tokens_saved_estimate", 0),
        "restarts_avoided": restarts_avoided(m),
        "sessions_resumed": m.get("sessions_resumed", 0),
        "compactions_bridged": m.get("compactions_bridged", 0),
        "load_events": m.get("load_events", 0),
        "prompts_auto_allowed": _prompts_auto_allowed(m),
        "cmm_prompts_auto_allowed": int(((m.get("cmm") or {}).get("prompts_auto_allowed")) or 0),
        "usd_per_mtok": float(load_config(ctx).get("usd_per_mtok", 3.0)),
        "project": _project_name(ctx),
    }


def _flex_card(ctx: Path) -> str:
    """The 🏹💪 '$ Wrapped' card — a box-drawn, copy-paste-to-X brag built from the memory economy:
    dollars saved (tokens→$), tokens never re-loaded, and restarts avoided. Mechanical, offline, free
    — it's the billboard. Box columns are aligned via _disp_width so the emoji don't skew the border."""
    f = _flex_figures(ctx)
    rows = [
        ("🏹  CELEBORN · $ WRAPPED  💪", "center"),
        ("", "left"),
        (f"{_fmt_usd(f['dollars_saved'])} saved in tokens", "left"),
        (f"{f['tokens_saved']:,} tokens never re-loaded", "left"),
        (f"{f['restarts_avoided']} restarts avoided", "left"),
        (f"  ({f['sessions_resumed']} resume(s) · {f['compactions_bridged']} compaction(s) bridged)", "left"),
    ]
    if f.get("prompts_auto_allowed"):
        rows.append((f"{f['prompts_auto_allowed']} permission prompts auto-allowed", "left"))
    rows += [
        ("", "left"),
        (f"{f['project']} · across {f['load_events']} load event(s)", "left"),
    ]
    inner = max(max(_disp_width(t) for t, _ in rows) + 4, 46)   # 2-space gutter each side, min width
    top, sep, bot = ("╭" + "─" * inner + "╮", "├" + "─" * inner + "┤", "╰" + "─" * inner + "╯")
    out = [top]
    for i, (text, align) in enumerate(rows):
        pad = inner - _disp_width(text)
        if align == "center":
            left = pad // 2
            cell = " " * left + text + " " * (pad - left)
        else:
            cell = "  " + text + " " * (pad - 2)
        out.append("│" + cell + "│")
        if i == 0:
            out.append(sep)
    out.append(bot)
    return "\n".join(out)


def _flex_tweet(ctx: Path) -> str:
    """A ≤280-char build-in-public flex post from the same figures. Leads with the $ + restarts brag,
    then the why (context never reloaded). Trims the explanatory line first if it runs long."""
    f = _flex_figures(ctx)
    full = [
        f"🏹💪 Celeborn has saved me {_fmt_usd(f['dollars_saved'])} and {f['restarts_avoided']} restarts on {f['project']}.",
        "",
        f"{f['tokens_saved']:,} tokens of context I never had to reload — my AI remembers across sessions & compactions, so I just keep building.",
        "",
        "#buildinpublic #AI",
    ]
    post = "\n".join(full)
    if len(post) <= 280:
        return post
    return "\n".join([full[0], "", "#buildinpublic #AI"])[:280]


def cmd_flex(args):
    """`celeborn flex` — the shareable 🏹💪 '$ Wrapped' brag card. Default: a box-drawn terminal card;
    `--tweet`: a ≤280-char build-in-public X post; `--json`: the raw figures."""
    ctx = require_context(args)
    if getattr(args, "json", False):
        print(json.dumps(_flex_figures(ctx), indent=2))
        return
    if getattr(args, "tweet", False):
        print(_flex_tweet(ctx))
        return
    print(_flex_card(ctx))


# --------------------------------------------------------------------------- savings (board surface for flex figures)

def _savings_figures(ctx: Path) -> dict:
    """The running savings totals the board surfaces in place of `flex` updates (t68): this project
    since start, and the same figures summed across every registered Celeborn project (+ this one).
    Each project's $ is computed against its own `usd_per_mtok` rate, then summed — so the fleet total
    is rate-correct even when projects price tokens differently."""
    project = _flex_figures(ctx)
    project["advisor"] = _advisor_figures(ctx)
    fleet = {"projects": 0, "dollars_saved": 0.0, "tokens_saved": 0, "restarts_avoided": 0,
             "sessions_resumed": 0, "compactions_bridged": 0, "load_events": 0,
             "prompts_auto_allowed": 0}
    fleet_adv = {"permission_rules_generalized": 0, "skipped_bottlenecks_total": 0}
    for pdir in _fleet_project_paths(ctx):
        pctx = pdir / CONTEXT_DIRNAME
        if not pctx.is_dir():
            continue
        f = _flex_figures(pctx)
        fleet["projects"] += 1
        fleet["dollars_saved"] += f["dollars_saved"]
        for k in ("tokens_saved", "restarts_avoided", "sessions_resumed",
                  "compactions_bridged", "load_events", "prompts_auto_allowed"):
            fleet[k] += f[k]
        fa = _advisor_figures(pctx)
        for k in fleet_adv:
            fleet_adv[k] += fa[k]
    fleet["dollars_saved"] = round(fleet["dollars_saved"], 2)
    fleet["advisor"] = fleet_adv
    return {"generated_at": now_iso(), "project": project, "fleet": fleet}


def _advisor_figures(ctx: Path) -> dict:
    """The permission-friction ledger the economy bar surfaces: cumulative rules auto-generalized and
    the aggregate bottlenecks (un-widenable literals) still re-prompting. Mirrors `metrics['advisor']`."""
    adv = (_load_metrics(ctx).get("advisor") or {})
    return {
        "permission_rules_generalized": int(adv.get("permission_rules_generalized", 0) or 0),
        "skipped_bottlenecks_total": int(adv.get("skipped_bottlenecks_total", 0) or 0),
    }


def cmd_savings(args):
    """`celeborn savings` — the running savings totals (this project + the whole fleet) the kanban
    board renders as its one-line economy bar, in place of pushed `flex` updates (t68). `--json`
    feeds the board's /api/savings route."""
    ctx = require_context(args)
    data = _savings_figures(ctx)
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return
    p, fl = data["project"], data["fleet"]
    paa = f" · 🔓 {p['prompts_auto_allowed']} prompts auto-allowed" if p.get("prompts_auto_allowed") else ""
    print(f"💰 {_fmt_usd(p['dollars_saved'])} · 🧠 {p['tokens_saved']:,} tokens · "
          f"♻️ {p['restarts_avoided']} restarts{paa}  —  {p['project']}")
    fpaa = f" · 🔓 {fl['prompts_auto_allowed']} auto-allowed" if fl.get("prompts_auto_allowed") else ""
    print(f"🌐 across {fl['projects']} project(s): {_fmt_usd(fl['dollars_saved'])} · "
          f"🧠 {fl['tokens_saved']:,} tokens · ♻️ {fl['restarts_avoided']} restarts{fpaa}")


# --------------------------------------------------------------------------- blame (git blame for the why)

BLAME_MEMORY_FILES = (
    ("decisions.md", "decision"),
    ("learnings.md", "learning"),
    ("journal.md", "journal"),
    ("notes.md", "note"),
)


def _git_file_history(repo: Path, relpath: str, limit: int = 8) -> list[dict]:
    """Recent commits that touched `relpath`. Empty if not a git repo or the path has no history."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", f"-n{limit}", "--follow",
             "--pretty=format:%H%x09%h%x09%ad%x09%s", "--date=short", "--", relpath],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) < 4:
            continue
        full, short, date, subject = parts
        rows.append({"full": full, "short": short, "date": date, "subject": subject})
    return rows


def _blame_needles(relpath: str, commits: list[dict]) -> set[str]:
    """Strings to match in memory tiers when surfacing the 'why' behind a file."""
    needles = {relpath, relpath.replace("\\", "/")}
    if "/" in relpath:
        needles.add(relpath.rsplit("/", 1)[-1])
    for c in commits:
        needles.add(c["short"])
        needles.add(c["full"][:12])
        needles.add(c["full"][:7])
    return {n for n in needles if n}


def _blame_memory_hits(ctx: Path, relpath: str, commits: list[dict], limit: int = 5) -> list[dict]:
    """Memory sections that mention the file or its recent commit SHAs — the reasoning, not authorship."""
    needles = _blame_needles(relpath, commits)
    hits: list[dict] = []
    for rel, kind in BLAME_MEMORY_FILES:
        path = ctx / rel
        if not path.is_file():
            continue
        for sec in parse_sections(path.read_text()):
            if sec["level"] != 2 or not sec["title"]:
                continue
            body = "\n".join(sec["lines"]).strip()
            if not body:
                continue
            matched = [n for n in needles if n in body or n in sec["title"]]
            if not matched:
                continue
            excerpt = body if len(body) <= 400 else body[:397].rstrip() + "…"
            hits.append({
                "file": rel,
                "kind": kind,
                "title": sec["title"],
                "score": len(matched),
                "matched": matched[:6],
                "excerpt": excerpt,
            })
    hits.sort(key=lambda h: (-h["score"], h["file"], h["title"]))
    return hits[:limit]


def _render_blame(relpath: str, commits: list[dict], memory: list[dict]) -> str:
    sep = "─" * 72
    lines = [f"🏹 Celeborn blame — {relpath}", sep, "Git history (recent commits on this file):"]
    if commits:
        for c in commits:
            lines.append(f"  {c['short']}  {c['date']}  {c['subject']}")
    else:
        lines.append("  (no git history — not a repo, untracked path, or no commits yet)")
    lines.append(sep)
    lines.append("Memory — the why (decisions / journal / learnings / notes that mention it):")
    if memory:
        for h in memory:
            tags = ", ".join(h["matched"][:3])
            lines.append(f"  [{h['kind']}] {h['title']}  ({h['file']})")
            if tags:
                lines.append(f"    matched: {tags}")
            for el in h["excerpt"].splitlines()[:4]:
                lines.append(f"    {el}")
            if len(h["excerpt"].splitlines()) > 4:
                lines.append("    …")
    else:
        lines.append("  (no linked memory yet — checkpoint decisions/journal entries that cite the file or commit SHA)")
    lines.append(sep)
    return "\n".join(lines)


def cmd_blame(args):
    """`celeborn blame <file>` — git blame for the *why*: recent commits on a file plus Celeborn memory
    (decisions, journal, learnings, notes) that mention the path or its SHAs."""
    ctx = require_context(args)
    repo = ctx.parent
    raw = (getattr(args, "path_arg", None) or "").strip()
    if not raw:
        die("usage: celeborn blame <file>")
    target = Path(raw)
    if target.is_absolute():
        try:
            relpath = str(target.resolve().relative_to(repo.resolve()))
        except ValueError:
            die(f"{raw} is outside the project ({repo})")
    else:
        relpath = raw.lstrip("./")
    limit = getattr(args, "limit", None) or 8
    commits = _git_file_history(repo, relpath, limit=limit)
    memory = _blame_memory_hits(ctx, relpath, commits, limit=getattr(args, "memory", None) or 5)
    if getattr(args, "json", False):
        print(json.dumps({"file": relpath, "commits": commits, "memory": memory}, indent=2))
        return
    print(_render_blame(relpath, commits, memory))


# --------------------------------------------------------------------------- why (decision archaeology)

# Reasoning tiers searched for the "why", richest first. (file, kind).
WHY_MEMORY_FILES = (
    ("decisions.md", "decision"),
    ("learnings.md", "learning"),
    ("journal.md", "journal"),
    ("notes.md", "note"),
)

# Tier richness: a locked decision answers "why" more authoritatively than a passing journal note.
WHY_KIND_WEIGHT = {"decision": 4, "learning": 3, "journal": 2, "note": 1}

_WHY_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_WHY_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _why_terms(query: str) -> list[str]:
    """Lowercased query words for overlap scoring. Keeps short tokens (e.g. 'db', 'ci')."""
    return [w.lower() for w in _WHY_WORD_RE.findall(query)]


def _why_date(title: str, body: str) -> str:
    """Best-effort decision date: the ISO date in the heading (decisions/journal lead with one),
    else the first ISO date in the body, else empty."""
    m = _WHY_DATE_RE.search(title) or _WHY_DATE_RE.search(body)
    return m.group(1) if m else ""


def _why_rationale(body: str, limit: int = 320) -> str:
    """A compact rationale excerpt — the reasoning, not the whole section. Collapses bullet/line
    noise to one flowing snippet, truncated with an ellipsis."""
    lines = [ln.strip().lstrip("-*").strip() for ln in body.splitlines()]
    text = " ".join(ln for ln in lines if ln)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _why_hits(ctx: Path, query: str, limit: int = 5) -> list[dict]:
    """Decision archaeology: rank memory sections by how well they answer 'why <topic>'. A
    self-contained section scan over the reasoning tiers (no FTS index required) — scores by
    distinct query-term overlap (title weighted), then tier richness, then recency."""
    terms = _why_terms(query)
    if not terms:
        return []
    phrase = query.strip().lower()
    hits: list[dict] = []
    for rel, kind in WHY_MEMORY_FILES:
        path = ctx / rel
        if not path.is_file():
            continue
        for sec in parse_sections(path.read_text()):
            if sec["level"] not in (2, 3) or not sec["title"] or not sec["body"]:
                continue
            hay_title = sec["title"].lower()
            hay_body = sec["body"].lower()
            matched = sorted({t for t in terms if t in hay_title or t in hay_body})
            if not matched:
                continue
            score = len(matched) + sum(1 for t in matched if t in hay_title)  # title hits count double
            if len(phrase) >= 4 and (phrase in hay_title or phrase in hay_body):
                score += 3  # exact-phrase match is a strong signal
            hits.append({
                "file": rel,
                "kind": kind,
                "title": sec["title"],
                "date": _why_date(sec["title"], sec["body"]),
                "score": score,
                "matched": matched,
                "rationale": _why_rationale(sec["body"]),
                "anchor": sec["anchor"],
            })
    # Stable multi-pass sort, least-significant first: title asc, date desc, then (score, weight) desc.
    hits.sort(key=lambda h: h["title"])
    hits.sort(key=lambda h: h["date"] or "", reverse=True)
    hits.sort(key=lambda h: (h["score"], WHY_KIND_WEIGHT.get(h["kind"], 0)), reverse=True)
    return hits[:limit]


def _why_display_title(h: dict) -> str:
    """Title with a redundant leading `YYYY-MM-DD —/-/:` stripped (the date rides the chip)."""
    title = h["title"]
    if h["date"] and title.startswith(h["date"]):
        return re.sub(r"^\d{4}-\d{2}-\d{2}\s*[—\-:]\s*", "", title) or title
    return title


def _render_why(query: str, hits: list[dict]) -> str:
    import textwrap  # lazy: only this render path needs it

    sep = "─" * 72
    lines = [f'🏹 Celeborn why — "{query}"', sep]
    if not hits:
        lines.append("  No decision or rationale found in memory for that topic.")
        lines.append("  Try `celeborn search` for broader full-text recall, or widen the topic.")
        lines.append(sep)
        return "\n".join(lines)
    top = hits[0]
    pointer = f"{top['file']}#{top['anchor']}" if top["anchor"] else top["file"]
    lines.append(f"[{top['kind']} · {top['date'] or 'undated'}] {_why_display_title(top)}")
    lines.append(f"  {pointer}")
    for el in textwrap.wrap(top["rationale"], 70) or ["(no rationale recorded)"]:
        lines.append(f"  {el}")
    rest = hits[1:]
    if rest:
        lines.append(sep)
        lines.append("See also:")
        for h in rest:
            lines.append(f"  [{h['kind']} · {h['date'] or 'undated'}] {_why_display_title(h)}  ({h['file']})")
    lines.append(sep)
    return "\n".join(lines)


def cmd_why(args):
    """`celeborn why "<topic>"` — decision archaeology: the decision, its date, and the rationale,
    pulled from the reasoning tiers (decisions, learnings, journal, notes). The 'it remembered why
    from weeks ago' one-liner."""
    ctx = require_context(args)
    query = (getattr(args, "query", None) or "").strip()
    if not query:
        die('usage: celeborn why "<topic>"')
    limit = getattr(args, "limit", None) or 5
    hits = _why_hits(ctx, query, limit=limit)
    if getattr(args, "json", False):
        print(json.dumps({"query": query, "hits": hits}, indent=2))
        return
    print(_render_why(query, hits))


# --------------------------------------------------------------------------- touch (multi-agent file registry)

TOUCHES_NAME = "touches.json"
# schema/2 (CELE-t309): a file maps to a LIST of touch records — one per agent — so a declared
# two-writer hotspot keeps BOTH writers visible (schema/1 kept a single record per path, so the
# second toucher silently overwrote the first and vanished from the overlap signal). Legacy
# {path: record} files migrate to {path: [record]} transparently on load.
TOUCHES_SCHEMA = "celeborn-touches/2"


def _touches_path(ctx: Path) -> Path:
    return ctx / TOUCHES_NAME


def _normalize_touch_files(files) -> dict:
    """Coerce the on-disk `files` map to the canonical schema/2 shape {path: [record, ...]},
    tolerating the schema/1 single-record shape {path: record} and dropping empty/garbage entries.
    The whole registry runs on this list shape once loaded, so callers never branch on version."""
    out = {}
    for path, val in (files or {}).items():
        if isinstance(val, list):
            recs = [m for m in val if isinstance(m, dict)]
        elif isinstance(val, dict):
            recs = [val]                       # schema/1 legacy: a bare record for the path
        else:
            continue
        if recs:
            out[path] = recs
    return out


def _load_touches(ctx: Path) -> dict:
    """Active file-touch registry — who is editing which path right now (design: multi-agent-editing.md).
    Normalized to schema/2 ({path: [record, ...]}) on load so every caller sees the list shape."""
    p = _touches_path(ctx)
    if not p.is_file():
        return {"schema": TOUCHES_SCHEMA, "files": {}}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"schema": TOUCHES_SCHEMA, "files": {}}
    data["schema"] = TOUCHES_SCHEMA
    data["files"] = _normalize_touch_files(data.get("files"))
    return data


def _save_touches(ctx: Path, data: dict):
    _touches_path(ctx).write_text(json.dumps(data, indent=2) + "\n")


def _touch_by(rec) -> str:
    """The handle a touch record is attributed to (trimmed); '' when unattributed."""
    return ((rec or {}).get("by") or "").strip()


def _upsert_toucher(recs: list, rec: dict) -> None:
    """Register `rec` in a file's toucher list: replace this handle's existing record (a re-edit
    just freshens it) or append a new one. One record per (file, handle) — never per file."""
    who = _touch_by(rec)
    for i, m in enumerate(recs):
        if _touch_by(m) == who:
            recs[i] = rec
            return
    recs.append(rec)


# --- agent identity registry --------------------------------------------------
# A local, gitignored cache mapping an agent handle -> {family, model} so agents declare their
# model ONCE (`celeborn identify`) instead of on every touch/claim. Never authoritative: the
# resolved family/model are also embedded into each touch record at write time, so records stay
# self-describing even if the cache is wiped. Keyed by handle (the same `by` the board shows).
AGENTS_NAME = ".agents.json"
AGENTS_SCHEMA = "celeborn-agents/1"


def _agents_path(ctx: Path) -> Path:
    return ctx / AGENTS_NAME


def _load_agents(ctx: Path) -> dict:
    p = _agents_path(ctx)
    if not p.is_file():
        return {"schema": AGENTS_SCHEMA, "agents": {}}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"schema": AGENTS_SCHEMA, "agents": {}}
    data.setdefault("schema", AGENTS_SCHEMA)
    data.setdefault("agents", {})
    return data


def _save_agents(ctx: Path, data: dict):
    _agents_path(ctx).write_text(json.dumps(data, indent=2) + "\n")


# --------------------------------------------------------------------------- alerts (CELE-t169)
# Transient per-card "coding progress is blocked, the user's input is needed" state — a permission
# prompt, a ~60s idle stall, or a stopped turn awaiting direction. Like the /clear-nudge token band
# it is LIVE state that rides the DOING card, never a durable tasks.md field: it lives in a local,
# gitignored `.context/.alerts.json` and is stamped onto the board projection (`_tasks_doc`) + the
# fleet snapshot (`_doing_row`) + the hosted `tasks` push. `celeborn alert` is the reusable service
# (the Notification/Stop hooks are its first callers; any other system can call it too), and it
# clears the moment the user re-engages (a new prompt). No focus-stealing OS dialog (rejected
# t47/t50/t62) — the alert surfaces on the card, locally and on celeborncode.ai.
ALERTS_NAME = ".alerts.json"
ALERTS_SCHEMA = "celeborn-alerts/1"
ALERT_KINDS = ("permission", "idle", "stopped", "spine")  # "spine": the PM's ✋ on a todo spine card (CELE-t283)


def _alerts_path(ctx: Path) -> Path:
    return ctx / ALERTS_NAME


def _load_alerts(ctx: Path) -> dict:
    p = _alerts_path(ctx)
    if not p.is_file():
        return {"schema": ALERTS_SCHEMA, "alerts": {}}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"schema": ALERTS_SCHEMA, "alerts": {}}
    data.setdefault("schema", ALERTS_SCHEMA)
    if not isinstance(data.get("alerts"), dict):
        data["alerts"] = {}
    return data


def _save_alerts(ctx: Path, data: dict):
    import os
    data["schema"] = ALERTS_SCHEMA
    p = _alerts_path(ctx)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)


def _alert_for(ctx: Path, task_id: str) -> dict | None:
    """The live alert on a card (bare local id), or None. Read by the projection/snapshot stamps."""
    bare = _split_qualified_tid((task_id or "").strip())[1]
    return (_load_alerts(ctx).get("alerts") or {}).get(bare) if bare else None


def _set_alert(ctx: Path, task_id: str, kind: str, message: str = "", session: str = "") -> dict:
    """Raise (or refresh) the blocked-alert on a card. Idempotent per card — one alert at a time; a
    newer signal overwrites. Returns the stored record."""
    bare = _split_qualified_tid((task_id or "").strip())[1]
    if not bare:
        return {}
    kind = kind if kind in ALERT_KINDS else "idle"
    rec = {"kind": kind, "message": (message or "").strip()[:280], "at": now_iso(),
           "session": (session or "").strip()[:12]}
    data = _load_alerts(ctx)
    data.setdefault("alerts", {})[bare] = rec
    _save_alerts(ctx, data)
    return rec


def _clear_alert(ctx: Path, task_id: str) -> bool:
    """Drop a card's alert (progress resumed / card claimed elsewhere). True if one was present."""
    bare = _split_qualified_tid((task_id or "").strip())[1]
    if not bare:
        return False
    data = _load_alerts(ctx)
    alerts = data.setdefault("alerts", {})
    if bare in alerts:
        del alerts[bare]
        _save_alerts(ctx, data)
        return True
    return False


def _live_alerts(ctx: Path) -> dict:
    """The alerts map with DEAD-session alerts filtered out (CELE-t195). A blocked-alert means "this
    session is awaiting the user" — once that session has ended (`/clear`, logout, exit → tombstoned
    in `ended_sessions`), it is awaiting nothing, so its badge must not keep blinking on the board.
    This is the deterministic catch-all for the stale badge: it holds even when the SessionEnd hook
    never fired to clear the record (the common cause — a window killed without a clean exit). An
    alert with no recorded session is kept (can't prove it's dead). Read by the board projections."""
    alerts = dict(_load_alerts(ctx).get("alerts") or {})
    if not alerts:
        return alerts
    ended = _load_metrics(ctx).get("ended_sessions") or {}
    if not ended:
        return alerts
    live = {}
    for tid, rec in alerts.items():
        sess = ((rec or {}).get("session") or "").strip()   # stored as sid[:12]
        if sess and any(k.startswith(sess) for k in ended):
            continue                                          # owning window is gone — drop the badge
        live[tid] = rec
    return live


def _clear_alert_on_activity(project_dir: str, session: str) -> None:
    """Drop this session's card alert the instant it makes a tool call (CELE-t195). PreToolUse fires
    on every Bash/Edit/Write/NotebookEdit, so a tool call is the earliest observable "work resumed"
    signal — clearing HERE (not only on the next user prompt) is what drops the board's "awaiting you"
    badge within seconds of the user removing the block, including the cases the user-prompt-submit
    clear misses entirely: a permission GRANT or an AskUserQuestion ANSWER both resume the SAME turn
    and never fire a new prompt, so the old code left the badge stale for the rest of the turn.

    Fast-guarded so the ~99% no-alert tool call pays almost nothing: no .context/ → return; no alerts
    file → one stat and return; empty alerts map → one small read and return. Best-effort; a bug here
    must never break a tool call, so everything is wrapped and swallowed."""
    if not session:
        return
    try:
        ctxdir = find_context_root(Path(project_dir))
        if ctxdir is None or not _alerts_path(ctxdir).is_file():
            return
        if not (_load_alerts(ctxdir).get("alerts") or {}):
            return                                    # resting state — nothing to clear
        tid = _session_task_id(ctxdir, session)
        if tid and _clear_alert(ctxdir, tid):
            _refresh_alerted_card(ctxdir, tid)
            __import__("celeborn_sync").schedule_agents_push(ctxdir, min_interval_s=0)
    except Exception:  # noqa: BLE001
        pass


def _refresh_alerted_card(ctx: Path, task_id: str) -> None:
    """After an alert set/clear, regenerate the derived tasks.json (so the local board's next poll
    carries the badge) and live-push the one card to the hosted board. Best-effort — an alert must
    never break a turn or a caller."""
    bare = _split_qualified_tid((task_id or "").strip())[1]
    if not bare:
        return
    try:
        _save_tasks(ctx, _load_tasks(ctx), autopush_ids=[bare])
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- ask / answer (CELE-t280)
# The question dock's DURABLE half (design docs/plans/cele-t144-spine-and-stage.md §3b). Two reply
# paths meet here:
#   · An OpenCode PERMISSION ask resumes LIVE — the board POSTs once/always/reject straight to
#     `opencode serve` and the session unblocks; `celeborn answer --kind permission` only records it.
#   · An `ask_human` tool call (or a `celeborn alert`) has no live channel to resume mid-turn, so the
#     question PARKS here and the tool blocks on its answer: `celeborn ask` files it (+ raises the
#     card alert so the Stage dock shows it); `celeborn answer --kind text` fills it; the tool's poll
#     on `celeborn ask-status` returns the text and the turn continues. No tool waiting? the answer
#     falls through to the outbox (delivered on the agent's next turn).
# Rides the same local, gitignored .context/ as .alerts.json.
ASKS_NAME = ".asks.json"
ASKS_SCHEMA = "celeborn-asks/1"


def _asks_path(ctx: Path) -> Path:
    return ctx / ASKS_NAME


def _load_asks(ctx: Path) -> dict:
    p = _asks_path(ctx)
    if not p.is_file():
        return {"schema": ASKS_SCHEMA, "asks": {}}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"schema": ASKS_SCHEMA, "asks": {}}
    if not isinstance(data.get("asks"), dict):
        data["asks"] = {}
    data.setdefault("schema", ASKS_SCHEMA)
    return data


def _save_asks(ctx: Path, data: dict) -> None:
    import os
    data["schema"] = ASKS_SCHEMA
    p = _asks_path(ctx)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)


def _new_ask(ctx: Path, session: str, card: str, question: str, options: list[str]) -> dict:
    import uuid
    data = _load_asks(ctx)
    rec = {"id": "ask-" + uuid.uuid4().hex[:8], "session": (session or "").strip()[:64],
           "card": card, "question": question.strip(), "options": options,
           "answer": None, "at": now_iso(), "answered_at": None}
    data.setdefault("asks", {})[rec["id"]] = rec
    _save_asks(ctx, data)
    return rec


def _open_ask_for(ctx: Path, session: str = "", card: str = "") -> dict | None:
    """The newest still-unanswered ask matching this session (preferred) or card — the record a
    `celeborn answer` fills. Session match is by id-prefix either way (events carry full ids, cards
    own short ones), so both sides of the demux join resolve the same ask."""
    session, card = (session or "").strip(), (card or "").strip()
    cands = sorted((r for r in _load_asks(ctx).get("asks", {}).values() if r.get("answer") is None),
                   key=lambda r: r.get("at", ""), reverse=True)
    for r in cands:
        rs = (r.get("session") or "").strip()
        if session and rs and (rs.startswith(session) or session.startswith(rs)):
            return r
    for r in cands:
        if card and r.get("card") == card:
            return r
    return None


def _answer_ask(ctx: Path, ask_id: str, answer: str) -> bool:
    data = _load_asks(ctx)
    rec = data.get("asks", {}).get(ask_id)
    if not rec or rec.get("answer") is not None:
        return False
    rec["answer"] = answer
    rec["answered_at"] = now_iso()
    _save_asks(ctx, data)
    return True


def _journal_dock_qa(ctx: Path, disp: str, question: str, answer: str, how: str,
                     kind: str = "text", model: str = "") -> None:
    """The durable half of "journaled on the card": a fuller journal.md entry (asked → answered →
    resumed). Best-effort — a journaling hiccup must never fail an answer that already landed.
    `model` (CELE-t346) records the per-prompt [model ▾] pick when the human chose one."""
    try:
        stamp = now_iso()[:16].replace("T", " ")
        # A kind=text message with no --question is a plain "message this agent" (the always-on
        # Stage prompt line, CELE-t345), not an answer to a question — don't mislabel it a request.
        asked = question or ("(permission request)" if kind == "permission" else "(message)")
        model_line = f"- **Model:** {model}\n" if model else ""
        _append(ctx / "journal.md",
                f"\n## {stamp} — dock Q&A on {disp}\n"
                f"- **Asked:** {asked}\n"
                f"- **Answered:** {answer}  (resumed via {how})\n"
                f"{model_line}"
                f"- **Tags:** #dock #t280\n")
    except Exception:  # noqa: BLE001
        pass


def _append_card_qa(ctx: Path, tid: str, question: str, answer: str, kind: str = "text",
                    model: str = "") -> None:
    """The glanceable half: a compact one-line Q&A trail on the card note itself, so decision
    provenance rides the kanban card (design §3b). Best-effort. `model` (CELE-t346) appends the
    per-prompt [model ▾] pick when the human chose one."""
    try:
        tasks = _load_tasks(ctx)
        t = _find_task(tasks, tid)
        if not t:
            return
        stamp = now_iso()[:16].replace("T", " ")
        # kind=text with no --question is a plain message, not a permission answer (CELE-t345).
        fallback = "permission" if kind == "permission" else "message"
        suffix = f"  · via {model}" if model else ""
        line = f"💬 [dock {stamp}] {question or fallback} → {answer}{suffix}"
        t["notes"] = f"{t['notes']}\n\n{line}".strip() if t.get("notes") else line
        _save_tasks(ctx, tasks, autopush_ids=[tid])
    except Exception:  # noqa: BLE001
        pass


def _register_agent(ctx: Path, handle: str, family: str = "", model: str = "") -> dict:
    """Upsert a handle's family/model — only non-empty fields overwrite. Returns the merged entry."""
    handle = (handle or "").strip()
    if not handle:
        return {}
    data = _load_agents(ctx)
    agents = data.setdefault("agents", {})
    entry = agents.get(handle) or {}
    if (family or "").strip():
        entry["family"] = family.strip()
    if (model or "").strip():
        entry["model"] = model.strip()
    entry["at"] = now_iso()
    agents[handle] = entry
    _save_agents(ctx, data)
    return entry


def _agent_identity(args, ctx: Path) -> dict:
    """Resolve {handle, family, model} for the calling agent.

    handle: `_claim_identity` (--by -> session short-id -> $CELEBORN_AGENT).
    family/model: explicit flag -> env (CELEBORN_AGENT_FAMILY / CELEBORN_AGENT_MODEL) -> the
    registry entry for the handle. Anything supplied explicitly is upserted so later commands
    in the session inherit it without re-passing the flags."""
    import os
    handle = _claim_identity(args) or "unknown"
    flag_family = (getattr(args, "family", None) or "").strip()
    flag_model = (getattr(args, "model", None) or "").strip()
    env_family = (os.environ.get("CELEBORN_AGENT_FAMILY") or "").strip()
    env_model = (os.environ.get("CELEBORN_AGENT_MODEL") or "").strip()
    reg = (_load_agents(ctx).get("agents") or {}).get(handle) or {}
    family = flag_family or env_family or (reg.get("family") or "")
    model = flag_model or env_model or (reg.get("model") or "")
    # Persist anything from a live source (flag/env) so later commands and the board owner chip
    # (which reads the registry) inherit it; never write back values that only came from the cache.
    if (flag_family or env_family) or (flag_model or env_model):
        _register_agent(ctx, handle, flag_family or env_family, flag_model or env_model)
    return {"handle": handle, "family": family, "model": model}


def _agent_label(family: str, model: str) -> str:
    """'Claude · Opus 4.8' from parts; tolerates either being empty ('' / 'Claude' / 'Opus 4.8')."""
    return " · ".join(p for p in ((family or "").strip(), (model or "").strip()) if p)


def _touch_ttl_hours(ctx: Path) -> float:
    return float(load_config(ctx).get("touch_ttl_hours", 2))


def _parse_touch_at(at: str):
    """Parse a touch timestamp (ISO local from now_iso(), or UTC Z suffix) → datetime or None."""
    if not at:
        return None
    s = at.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(s)
    except ValueError:
        return _parse_dt(s)


def _prune_touches(data: dict, ttl_hours: float) -> bool:
    """Drop touch records older than ttl, per toucher. A file drops out only once its last live
    toucher expires. Returns whether anything was removed."""
    if ttl_hours <= 0:
        return False
    cutoff = _dt.datetime.now() - _dt.timedelta(hours=ttl_hours)
    files = data.get("files") or {}
    changed = False
    for path in list(files.keys()):
        recs = files[path]
        kept = []
        for m in recs:
            at = _parse_touch_at((m or {}).get("at", ""))
            if at is not None and at >= cutoff:
                kept.append(m)
        if len(kept) != len(recs):
            changed = True
        if kept:
            files[path] = kept
        else:
            del files[path]
    return changed


def _touch_age_label(at: str) -> str:
    """Human '12m ago' / '2h ago' for orient."""
    ts = _parse_touch_at(at)
    if ts is None:
        return "?"
    # now_iso() is naive local; touches use the same — compare apples to apples.
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    delta = _dt.datetime.now() - ts
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{delta.days}d ago"


def _resolve_repo_relpath(repo: Path, raw: str) -> str:
    """Normalize a user path to a repo-relative POSIX path."""
    target = Path(raw.strip())
    if target.is_absolute():
        try:
            return str(target.resolve().relative_to(repo.resolve())).replace("\\", "/")
        except ValueError:
            die(f"{raw} is outside the project ({repo})")
    return raw.strip().lstrip("./").replace("\\", "/")


def _active_touches(ctx: Path) -> list[dict]:
    """Non-stale touches, sorted by recency (newest first). Prunes stale entries on read."""
    data = _load_touches(ctx)
    if _prune_touches(data, _touch_ttl_hours(ctx)):
        _save_touches(ctx, data)
    rows = []
    for path, recs in (data.get("files") or {}).items():
        for meta in recs:
            meta = meta or {}
            rows.append({
                "path": path,
                "by": meta.get("by") or "unknown",
                "family": meta.get("family") or "",
                "model": meta.get("model") or "",
                "why": meta.get("why") or "",
                "at": meta.get("at") or "",
                "task": meta.get("task") or "",
                "age": _touch_age_label(meta.get("at", "")),
            })
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return rows


def _task_has_active_touches(ctx: Path, task_id: str) -> bool:
    if not task_id:
        return False
    files = (_load_touches(ctx).get("files") or {})
    return any((m or {}).get("task") == task_id for recs in files.values() for m in recs)


def _is_stale_doing(ctx: Path, t: dict) -> bool:
    """DOING card with no active file touches — work is finished or the claim was abandoned."""
    return t.get("state") == "doing" and not _task_has_active_touches(ctx, t["id"])


def _doing_for_owner(tasks: list[dict], owner: str, *, exclude: set[str] | None = None) -> list[dict]:
    who = (owner or "").strip()
    if not who:
        return []
    skip = exclude or set()
    return [t for t in tasks if t["state"] == "doing" and (t.get("owner") or "").strip() == who
            and t["id"] not in skip]


def _claim_preflight(ctx: Path, tasks: list[dict], by: str, claim_ids: list[str], *, force: bool) -> None:
    """One in-flight card per agent. Block new claims while other DOING cards are open unless --force."""
    who = (by or "").strip()
    if not who:
        return
    others = _doing_for_owner(tasks, who, exclude=set(claim_ids))
    if not others:
        return
    lines = []
    stale = []
    for t in others:
        tag = "stale" if _is_stale_doing(ctx, t) else "in flight"
        lines.append(f"  [{_display_tid(ctx, t['id'])}] {t['title']} ({tag})")
        if _is_stale_doing(ctx, t):
            stale.append(t["id"])
    msg = (f"@{who} already has {len(others)} DOING card(s) — ship or demote before claiming another:\n"
           + "\n".join(lines))
    if stale:
        msg += "\n  stale → `celeborn ship <id>` or `celeborn tasks move <id> todo`"
    if not force:
        die(msg + "\n  Pass --force to claim anyway (not recommended).")
    warn(msg + "\n  (--force — proceeding)")


def _touch_release_nudge(ctx: Path, task_id: str) -> str | None:
    """P1: task has no remaining touches but is still DOING — nudge the agent to ship the card."""
    if not task_id:
        return None
    if _task_has_active_touches(ctx, task_id):
        return None
    t = _find_task(_load_tasks(ctx), task_id)
    if not t or t["state"] != "doing":
        return None
    return (f"[{task_id}] has no active touches but is still DOING — "
            f"ship it: `celeborn ship {task_id}`")


def _release_touches_for_task(ctx: Path, task_id: str) -> list[str]:
    """Release every touch tagged with `task_id`. Used by `celeborn ship`."""
    if not task_id:
        return []
    data = _load_touches(ctx)
    files = data.get("files") or {}
    released = []
    for path in list(files.keys()):
        recs = files[path]
        keep = [m for m in recs if (m or {}).get("task") != task_id]
        if len(keep) != len(recs):
            released.append(path)          # this task had at least one toucher on the file
        if keep:
            files[path] = keep
        else:
            del files[path]
    if released:
        _save_touches(ctx, data)
    return released


def _touches_orient_summary(ctx: Path) -> str:
    """Compact active-touch view for Orient — file-level 'who is editing what'."""
    rows = _active_touches(ctx)
    if not rows:
        return ""
    n = len(rows)
    line = f"{n} active touch{'es' if n != 1 else ''}    (`celeborn touch list` · protocol: references/multi-agent-editing.md)"
    out = [line]
    for r in rows:
        label = _agent_label(r.get("family", ""), r.get("model", ""))
        who = f"@{r['by']}" + (f" ({label})" if label else "")
        task = f" [{r['task']}]" if r.get("task") else ""
        why = f" — {r['why']}" if r.get("why") else ""
        out.append(f"  {who} → {r['path']}{task}  ({r['age']}){why}")
    return "\n".join(out)


def cmd_touch(args):
    """`celeborn touch <file>` — register a file edit before other agents collide. `release` / `list` /
    `clear` manage the registry (design: references/multi-agent-editing.md)."""
    ctx = require_context(args)
    words = list(getattr(args, "words", None) or [])
    cmd = words[0] if words and words[0] in ("list", "clear", "release") else None
    path_arg = ""
    if cmd == "release":
        path_arg = words[1] if len(words) > 1 else ""
    elif not cmd and words:
        path_arg = words[0]

    if cmd == "list":
        rows = _active_touches(ctx)
        if getattr(args, "json", False):
            print(json.dumps({"touches": rows}, indent=2))
            return
        if not rows:
            print("(no active touches)")
            return
        for r in rows:
            task = f" [{r['task']}]" if r.get("task") else ""
            label = _agent_label(r.get("family", ""), r.get("model", ""))
            who = f"@{r['by']}" + (f" ({label})" if label else "")
            why = f" — {r['why']}" if r.get("why") else ""
            print(f"{who} → {r['path']}{task}  ({r['age']}){why}")
        return

    if cmd == "clear":
        data = _load_touches(ctx)
        by = (getattr(args, "by", None) or "").strip()
        files = data.get("files") or {}
        if by:
            dropped = 0
            for path in list(files.keys()):
                recs = files[path]
                keep = [m for m in recs if _touch_by(m) != by]
                dropped += len(recs) - len(keep)
                if keep:
                    files[path] = keep
                else:
                    del files[path]
            print(f"Cleared {dropped} touch(es) for @{by}")
        else:
            n = sum(len(recs) for recs in files.values())
            data["files"] = {}
            print(f"Cleared {n} touch(es)")
        _save_touches(ctx, data)
        return

    if not path_arg:
        die("usage: celeborn touch <file> [--by <agent>] [--task <id>] [--why <reason>]\n"
            "       celeborn touch release <file> [--by <agent>]\n"
            "       celeborn touch list | clear [--by <agent>]")

    relpath = _resolve_repo_relpath(ctx.parent, path_arg)
    data = _load_touches(ctx)
    files = data.setdefault("files", {})
    ident = _agent_identity(args, ctx)
    who = ident["handle"]

    if cmd == "release":
        recs = files.get(relpath) or []
        if not recs:
            warn(f"no touch on {relpath}")
            return
        mine = [m for m in recs if _touch_by(m) == who]
        if mine:
            # Release only my own touch — peers stay registered on a shared hotspot.
            keep = [m for m in recs if _touch_by(m) != who]
            released_task = (mine[0].get("task") or "").strip()
            owner = who
        else:
            # I have no touch here; only foreign ones. --force releases the whole file.
            owners = ", @".join(sorted({_touch_by(m) or "?" for m in recs}))
            if not getattr(args, "force", False):
                die(f"{relpath} is touched by @{owners} — pass --force to release anyway")
            keep = []
            released_task = (recs[0].get("task") or "").strip()
            owner = owners
        if keep:
            files[relpath] = keep
        else:
            del files[relpath]
        _save_touches(ctx, data)
        remaining = f" — {len(keep)} other toucher(s) still on it" if keep else ""
        print(f"Released {relpath} (@{owner}){remaining}")
        nudge = _touch_release_nudge(ctx, released_task)
        if nudge:
            warn(nudge)
        return

    # register (default) — add/refresh MY record; a peer on the same file is now kept alongside me
    # (schema/2) rather than overwritten, so a declared two-writer hotspot shows both (CELE-t309).
    recs = files.setdefault(relpath, [])
    others = sorted({o for o in (_touch_by(m) for m in recs) if o and o != who})
    if others:
        warn("@" + ", @".join(others) + f" also touching {relpath} — both registered (declared hotspot)")
    task = (getattr(args, "task", None) or "").strip()
    why = (getattr(args, "why", None) or "").strip()
    _upsert_toucher(recs, {
        "by": who,
        "family": ident["family"],
        "model": ident["model"],
        "at": now_iso(),
        "task": task,
        "why": why,
    })
    _save_touches(ctx, data)
    tag = f" [{task}]" if task else ""
    label = _agent_label(ident["family"], ident["model"])
    who_str = f"@{who}" + (f" ({label})" if label else "")
    why_str = f" — {why}" if why else ""
    print(f"Touch {who_str} → {relpath}{tag}{why_str}")
    if not label:
        warn("no model on record — run `celeborn identify --family <Claude|Grok|GPT…> "
             "--model \"<e.g. Opus 4.8>\"` once so touches show who you are.")
    if not why:
        warn("tip: add `--why \"<reason>\"` so other agents see why you're in this file.")


def cmd_board(args):
    """`celeborn board` — the board is Celeborn's UI, so this ensures the viewer is up and OPENS it in
    your browser (CELE-t228). Port resolution: explicit `board_port` in .celebornrc, else a stable hash
    of the project path. Script-friendly modes stay report-only and never launch/open: `--port`/`--url`
    print just that value, `--json` emits {port,url,live}. `--no-open` ensures the viewer but skips the
    browser tab; a non-interactive shell also never pops a tab."""
    if getattr(args, "supervise", False):
        # Detached restart-loop entrypoint (not a user-facing command) — resolves everything from
        # its own args, so no project context is required.
        _run_board_supervisor(args); return
    ctx = require_context(args)
    port = board_port(ctx)
    url = board_url(ctx)
    if getattr(args, "port_only", False):
        print(port); return
    if getattr(args, "url_only", False):
        print(url); return
    if getattr(args, "json", False):
        # Script/JSON mode: report-only unless `--start` explicitly asks to launch. Never opens a tab.
        st = ensure_board(ctx) if getattr(args, "start", False) else {
            "port": port, "url": url, "live": _board_live(port)}
        print(json.dumps(st)); return
    # Default `celeborn board` (and `--start`): bring the viewer up and open it — the board is the UI.
    st = ensure_board(ctx)
    # No npm/node/deps → the Next.js board can't run. Rather than a dead-end "can't start", serve a
    # zero-dependency onboarding page from the stdlib whose first step is REGISTER, always carrying a
    # live Support button (CELE-t229). Only in the interactive open path — scripts/hooks stay report-
    # only, and `--no-open` never blocks the terminal in a foreground serve loop.
    if (st.get("action") == "unavailable" and _init_is_interactive()
            and not getattr(args, "no_open", False)):
        print(f"🏹 {_project_name(ctx)} — full board unavailable ({st.get('reason')}); "
              f"serving onboarding at {url}  (Ctrl-C to stop)")
        _serve_onboarding(port, url, _onboarding_html(ctx, reason=st.get("reason", "")))
        return
    verb = {"live": "already live", "started": "started", "booting": "starting up",
            "off": "autostart off", "no-tasks": "no kanban here",
            "unavailable": "can't start"}.get(st["action"], st["action"])
    extra = f" — {st['reason']}" if st.get("reason") else (f" (pid {st['pid']})" if st.get("pid") else "")
    print(f"🏹 {_project_name(ctx)} kanban → {url}  ({verb}{extra})")
    # Pop a browser tab unless suppressed / non-interactive, and only if the viewer is actually up.
    if (not getattr(args, "no_open", False) and _init_is_interactive()
            and st.get("action") in ("live", "started", "booting")):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:                                   # noqa: BLE001 — opening a tab must never fail the command
            pass


# --------------------------------------------------------------------------- run (real-time swarm / Elves tracker)
#
# `fleet` watches multiple *projects* at human cadence (10/30-min staleness). A swarm is the
# opposite problem: ONE run, N short-lived workers (sub-agent "elves") each living seconds-to-
# minutes. `run` tracks that — per-worker heartbeat, current item, progress, yield, and a shared
# blackboard the elves learn from. Concurrency model mirrors the outbox: ONE FILE PER WORKER
# (`run/w-<id>.json`, single writer → no lock needed); the orchestrator writes `run/meta.json`;
# the blackboard is append-only. `run status`/`watch`/the board aggregate by globbing the dir.

RUN_DIRNAME = "run"
RUN_SCHEMA = "celeborn-run/1"
_RUN_WORKING_SECONDS = 45    # last beat newer than this → "working"
_RUN_STUCK_SECONDS = 150     # last beat older than this, not finished → "stuck"


def _safe_worker_slug(wid: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", (wid or "").strip()).strip("-")
    return s or "worker"


def _run_dir(ctx: Path) -> Path:
    return ctx / RUN_DIRNAME


def _run_meta_path(ctx: Path) -> Path:
    return _run_dir(ctx) / "meta.json"


def _worker_path(ctx: Path, wid: str) -> Path:
    return _run_dir(ctx) / f"w-{_safe_worker_slug(wid)}.json"


def _blackboard_path(ctx: Path) -> Path:
    return _run_dir(ctx) / "blackboard.md"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2) + "\n"
    json.loads(text)                          # re-parse: never write something we can't read back
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _load_run_meta(ctx: Path) -> dict:
    p = _run_meta_path(ctx)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_worker(ctx: Path, wid: str) -> dict:
    p = _worker_path(ctx, wid)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _seconds_since_iso(at: str) -> int | None:
    ts = _parse_touch_at(at)
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return max(0, int((_dt.datetime.now() - ts).total_seconds()))


def _fmt_secs(secs: int | None) -> str:
    if secs is None:
        return "?"
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _worker_live_status(w: dict) -> str:
    """done | failed | working | lagging | stuck — explicit terminal state, else heartbeat freshness."""
    st = (w.get("status") or "").strip()
    if st in ("done", "failed"):
        return st
    age = _seconds_since_iso(w.get("last_beat_at", ""))
    if age is None:
        return "stuck"
    if age <= _RUN_WORKING_SECONDS:
        return "working"
    if age > _RUN_STUCK_SECONDS:
        return "stuck"
    return "lagging"


_RUN_STATUS_GLYPH = {
    "working": "●", "lagging": "◐", "stuck": "✗", "done": "✓", "failed": "✗",
}


def _all_workers(ctx: Path) -> list[dict]:
    """Every worker row, status-derived, sorted by id. Mechanical — no model."""
    d = _run_dir(ctx)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("w-*.json")):
        try:
            w = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(w, dict):
            continue
        prog = w.get("progress") or {}
        done = int(prog.get("done") or 0)
        total = int(prog.get("total") or 0)
        elapsed = w.get("elapsed_s")
        if elapsed is None:
            start = w.get("started_at") or w.get("first_beat_at")
            end = w.get("finished_at") or w.get("last_beat_at")
            if start and end:
                a, b = _parse_touch_at(start), _parse_touch_at(end)
                if a and b:
                    elapsed = max(0, int((b - a).total_seconds()))
        rate = None
        if elapsed and done:
            rate = round(done / (elapsed / 60.0), 1)
        out.append({
            "id": w.get("id") or p.stem[2:],
            "shard": w.get("shard") or "",
            "phase": w.get("phase") or "",
            "status": _worker_live_status(w),
            "current_item": w.get("current_item") or "",
            "done": done, "total": total,
            "found": int(prog.get("found") or 0),
            "missed": int(prog.get("missed") or 0),
            "elapsed_s": elapsed,
            "rate_per_min": rate,
            "beat_age_s": _seconds_since_iso(w.get("last_beat_at", "")),
            "last_error": w.get("last_error") or "",
            "sources": w.get("sources") or {},
        })
    return out


def _run_snapshot(ctx: Path) -> dict:
    """Aggregate the whole run: meta + per-worker rows + run-level rollup. The board reads this."""
    meta = _load_run_meta(ctx)
    workers = _all_workers(ctx)
    by_status: dict[str, int] = {}
    sum_done = sum_found = sum_missed = sum_elapsed = 0
    for w in workers:
        by_status[w["status"]] = by_status.get(w["status"], 0) + 1
        sum_done += w["done"]; sum_found += w["found"]; sum_missed += w["missed"]
        sum_elapsed += int(w["elapsed_s"] or 0)
    started = meta.get("started_at") or ""
    wall = _seconds_since_iso(started) if started else None
    totals = meta.get("totals") or {}
    src_roll: dict[str, dict] = {}
    for w in workers:
        for src, v in (w.get("sources") or {}).items():
            agg = src_roll.setdefault(src, {"ok": 0, "fail": 0, "ratelimited": 0})
            for k in ("ok", "fail", "ratelimited"):
                agg[k] += int((v or {}).get(k) or 0)
    finished = by_status.get("done", 0) + by_status.get("failed", 0)
    return {
        "schema": RUN_SCHEMA,
        "run_id": meta.get("run_id") or "",
        "goal": meta.get("goal") or "",
        "started_at": started,
        "updated_at": now_iso(),
        "totals": totals,
        "wall_clock_s": wall,
        "sum_worker_s": sum_elapsed,
        "parallel_efficiency": (round(sum_elapsed / wall, 1) if wall else None),
        "workers_total": len(workers),
        "workers_finished": finished,
        "by_status": by_status,
        "resolved": {"done": sum_done, "found": sum_found, "missed": sum_missed},
        "sources": src_roll,
        "workers": workers,
        "blackboard": _read_blackboard(ctx, limit=200),
    }


def _read_blackboard(ctx: Path, limit: int = 50) -> list[dict]:
    p = _blackboard_path(ctx)
    if not p.is_file():
        return []
    out = []
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return []
    in_comment = False
    for ln in lines:
        ln = ln.strip()
        if in_comment:
            if "-->" in ln:
                in_comment = False
            continue
        if ln.startswith("<!--"):
            if "-->" not in ln:
                in_comment = True
            continue
        if not ln or ln.startswith("#"):
            continue
        # format: - [ts] @worker: lesson
        m = re.match(r"^-\s*\[([^\]]+)\]\s*@(\S+):\s*(.*)$", ln)
        if m:
            out.append({"at": m.group(1), "worker": m.group(2), "lesson": m.group(3)})
        else:
            out.append({"at": "", "worker": "", "lesson": ln.lstrip("- ")})
    return out[-limit:]


def _blackboard_has(ctx: Path, lesson: str) -> bool:
    norm = re.sub(r"\s+", " ", lesson.strip().lower())
    for row in _read_blackboard(ctx, limit=10_000):
        if re.sub(r"\s+", " ", (row.get("lesson") or "").strip().lower()) == norm:
            return True
    return False


def cmd_run(args):
    """`celeborn run` — real-time tracker for ONE multi-agent swarm (the Elves)."""
    ctx = require_context(args)
    action = getattr(args, "run_cmd", None) or "status"

    if action == "start":
        rid = (getattr(args, "run_id", None) or "").strip() or f"run-{now_iso()}"
        d = _run_dir(ctx)
        if d.is_dir() and not getattr(args, "keep", False):
            for p in d.glob("w-*.json"):
                p.unlink()
            bb = _blackboard_path(ctx)
            if bb.is_file():
                bb.unlink()
        meta = {
            "schema": RUN_SCHEMA, "run_id": rid,
            "goal": getattr(args, "goal", None) or "",
            "started_at": now_iso(),
            "totals": {"shards": int(getattr(args, "shards", 0) or 0),
                       "units": int(getattr(args, "units", 0) or 0)},
        }
        _atomic_write_json(_run_meta_path(ctx), meta)
        bb = _blackboard_path(ctx)
        if not bb.is_file():
            bb.write_text(
                f"# Run blackboard · {rid}\n\n"
                "<!-- Shared, append-only, broadcast knowledge channel for the swarm. Each elf reads\n"
                "     `celeborn run learnings` at shard-start and appends discoveries with\n"
                "     `celeborn run learn --worker <id> \"<lesson>\"`. Unlike the outbox, it is never\n"
                "     drained — lesson #1 helps worker #30. -->\n\n")
        ok(f"run started → {rid}  ({meta['totals']['shards']} shards / {meta['totals']['units']} units)")
        return

    if action == "beat":
        wid = (getattr(args, "worker", None) or "").strip()
        if not wid:
            die("run beat needs --worker <id>")
        w = _load_worker(ctx, wid)
        now = now_iso()
        w.setdefault("id", wid)
        w.setdefault("first_beat_at", now)
        w["last_beat_at"] = now
        if w.get("status") in (None, "", "working", "lagging", "stuck"):
            w["status"] = "working"
        for fld in ("shard", "phase"):
            v = getattr(args, fld, None)
            if v is not None:
                w[fld] = v
        if getattr(args, "item", None) is not None:
            w["current_item"] = args.item
        prog = w.setdefault("progress", {})
        for fld in ("done", "total", "found", "missed"):
            v = getattr(args, fld, None)
            if v is not None:
                prog[fld] = int(v)
        # source counters: --source-ok pubchem / --source-fail gsrs / --source-rl gsrs
        srcs = w.setdefault("sources", {})
        for arg_name, key in (("source_ok", "ok"), ("source_fail", "fail"), ("source_rl", "ratelimited")):
            name = getattr(args, arg_name, None)
            if name:
                srcs.setdefault(name, {"ok": 0, "fail": 0, "ratelimited": 0})[key] += 1
        a, b = _parse_touch_at(w["first_beat_at"]), _parse_touch_at(now)
        if a and b:
            w["elapsed_s"] = max(0, int((b - a).total_seconds()))
        _atomic_write_json(_worker_path(ctx, wid), w)
        if not getattr(args, "quiet", False):
            ok(f"beat @{wid} · {prog.get('done',0)}/{prog.get('total',0)} · {w.get('current_item','')}")
        return

    if action in ("done", "fail"):
        wid = (getattr(args, "worker", None) or "").strip()
        if not wid:
            die(f"run {action} needs --worker <id>")
        w = _load_worker(ctx, wid)
        w.setdefault("id", wid)
        now = now_iso()
        w["finished_at"] = now
        w["last_beat_at"] = now
        w["status"] = "done" if action == "done" else "failed"
        prog = w.setdefault("progress", {})
        for fld in ("done", "total", "found", "missed"):
            v = getattr(args, fld, None)
            if v is not None:
                prog[fld] = int(v)
        if action == "fail" and getattr(args, "error", None):
            w["last_error"] = args.error
        a, b = _parse_touch_at(w.get("first_beat_at") or now), _parse_touch_at(now)
        if a and b:
            w["elapsed_s"] = max(0, int((b - a).total_seconds()))
        _atomic_write_json(_worker_path(ctx, wid), w)
        ok(f"{action} @{wid} · found {prog.get('found',0)} / missed {prog.get('missed',0)}")
        return

    if action == "learn":
        wid = _safe_worker_slug(getattr(args, "worker", None) or "anon")
        lesson = (getattr(args, "lesson", None) or "").strip()
        if not lesson:
            die('run learn needs a lesson: celeborn run learn --worker w "..."')
        if _blackboard_has(ctx, lesson):
            if not getattr(args, "quiet", False):
                ok("(already on the blackboard — skipped)")
            return
        bb = _blackboard_path(ctx)
        bb.parent.mkdir(parents=True, exist_ok=True)
        with open(bb, "a") as fh:   # append is atomic for one short line on POSIX
            fh.write(f"- [{now_iso()}] @{wid}: {lesson}\n")
        if not getattr(args, "quiet", False):
            ok(f"📌 blackboard ← @{wid}: {lesson}")
        return

    if action == "learnings":
        rows = _read_blackboard(ctx, limit=int(getattr(args, "limit", None) or 30))
        if getattr(args, "json", False):
            print(json.dumps({"blackboard": rows}, indent=2)); return
        if not rows:
            print("(blackboard empty — no shared learnings yet)"); return
        print(f"🏹 Swarm blackboard — {len(rows)} lesson(s) the elves have shared:")
        for r in rows:
            who = f"@{r['worker']}" if r.get("worker") else ""
            print(f"  • {r['lesson']}  {who}")
        return

    if action in ("status", "watch"):
        if action == "watch":
            _run_watch(ctx, interval=float(getattr(args, "interval", None) or 2.0))
            return
        snap = _run_snapshot(ctx)
        if getattr(args, "json", False):
            print(json.dumps(snap, indent=2)); return
        print(_render_run(snap))
        return

    die(f"unknown run command: {action}")


def _render_run(snap: dict) -> str:
    lines = []
    rid = snap.get("run_id") or "(no run)"
    goal = snap.get("goal") or ""
    bs = snap.get("by_status") or {}
    tot = snap.get("totals") or {}
    res = snap.get("resolved") or {}
    head = (f"🏹 run {rid} — {bs.get('working',0)}● working  {bs.get('lagging',0)}◐ lagging  "
            f"{bs.get('stuck',0)}✗ stuck  {bs.get('done',0)}✓ done  {bs.get('failed',0)} failed")
    lines.append(head)
    if goal:
        lines.append(f"  goal: {goal}")
    wc = _fmt_secs(snap.get("wall_clock_s"))
    eff = snap.get("parallel_efficiency")
    lines.append(f"  {snap.get('workers_finished',0)}/{snap.get('workers_total',0)} workers finished · "
                 f"wall {wc} · sum-worker {_fmt_secs(snap.get('sum_worker_s'))}"
                 f"{f' · {eff}x parallel' if eff else ''}")
    units = tot.get("units")
    lines.append(f"  resolved: {res.get('done',0)} units processed · "
                 f"{res.get('found',0)} found / {res.get('missed',0)} missed"
                 f"{f' (of {units} total)' if units else ''}")
    srcs = snap.get("sources") or {}
    if srcs:
        parts = []
        for name, v in sorted(srcs.items()):
            rl = f" rl{v['ratelimited']}" if v.get("ratelimited") else ""
            parts.append(f"{name}:{v.get('ok',0)}✓/{v.get('fail',0)}✗{rl}")
        lines.append("  sources: " + "  ".join(parts))
    lines.append("")
    workers = snap.get("workers") or []
    # show stuck/working first, then lagging, then done
    order = {"stuck": 0, "failed": 1, "working": 2, "lagging": 3, "done": 4}
    for w in sorted(workers, key=lambda x: (order.get(x["status"], 9), x["id"])):
        g = _RUN_STATUS_GLYPH.get(w["status"], "·")
        prog = f"{w['done']}/{w['total']}" if w["total"] else f"{w['done']}"
        rate = f" {w['rate_per_min']}/min" if w.get("rate_per_min") else ""
        el = _fmt_secs(w.get("elapsed_s"))
        item = w.get("current_item") or ""
        if len(item) > 32:
            item = item[:29] + "…"
        age = w.get("beat_age_s")
        agelbl = ""
        if w["status"] in ("working", "lagging", "stuck") and age is not None:
            agelbl = f" (beat {age}s ago)"
        err = f"  ⚠ {w['last_error']}" if w.get("last_error") else ""
        lines.append(f"  {g} {w['id']:<12} {prog:>7} {el:>6}{rate:<9} {w['phase']:<10} {item}{agelbl}{err}")
    return "\n".join(lines)


def _run_watch(ctx: Path, interval: float = 2.0) -> None:
    import os
    import time
    try:
        while True:
            snap = _run_snapshot(ctx)
            os.system("clear" if os.name != "nt" else "cls")
            print(_render_run(snap))
            print(f"\n  (refresh {interval:g}s · Ctrl-C to stop)")
            bs = snap.get("by_status") or {}
            wt = snap.get("workers_total", 0)
            if wt and (bs.get("done", 0) + bs.get("failed", 0)) >= wt:
                print("\n  ✓ all workers finished.")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  (stopped watching)")


# --------------------------------------------------------------------------- fleet (live multi-project dashboard)

FLEET_REGISTRY = "fleet.json"
FLEET_REGISTRY_SCHEMA = "celeborn-fleet/1"
_ACTIVITY_CAPTURE_RE = re.compile(r"^Last capture:\s*(\S+)", re.M)
_ACTIVITY_CAPTURE_RE = re.compile(r"^Last capture:\s*(\S+)", re.M)
_ACTIVITY_PROMPT_RE = re.compile(r"^Last prompt:\s*(.+)$", re.M)
_FLEET_WORKING_MINUTES = 10   # touches newer than this → agent is "working"
_FLEET_STUCK_MINUTES = 30     # touches older than this with open DOING → "stuck"


def _config_dir() -> Path:
    import os
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "celeborn"


def _fleet_registry_path() -> Path:
    return _config_dir() / FLEET_REGISTRY


def _load_fleet_registry() -> dict:
    p = _fleet_registry_path()
    if not p.is_file():
        return {"schema": FLEET_REGISTRY_SCHEMA, "projects": []}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"schema": FLEET_REGISTRY_SCHEMA, "projects": []}
    data.setdefault("schema", FLEET_REGISTRY_SCHEMA)
    data.setdefault("projects", [])
    return data


def _save_fleet_registry(data: dict) -> None:
    import os
    d = _config_dir()
    d.mkdir(parents=True, exist_ok=True)
    data["schema"] = FLEET_REGISTRY_SCHEMA
    data["updated_at"] = now_iso()
    p = _fleet_registry_path()
    p.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _resolve_project_dir(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _fleet_register_path(project_dir: Path) -> dict:
    """Add a project to the fleet registry. Idempotent on path."""
    project_dir = _resolve_project_dir(str(project_dir))
    ctx = project_dir / CONTEXT_DIRNAME
    if not ctx.is_dir():
        die(f"No .context/ at {project_dir} — run `celeborn init` there first.")
    key = str(project_dir)
    data = _load_fleet_registry()
    projects = data.get("projects") or []
    for row in projects:
        if _resolve_project_dir(row.get("path", "")) == project_dir:
            return row
    # Cross-fleet dedup: qualified ids (SLUG-tN) must be unambiguous across the machine. Compare this
    # project's qualifier (case-insensitively) against the already-registered ones. An explicit
    # project_slug is the user's authority — keep it, only WARN on a clash; a derived (folder-name) slug
    # is auto-suffixed and the resolved value is persisted to .celebornrc so display + markers agree.
    explicit = bool((load_config(ctx).get("project_slug") or "").strip())
    base = project_slug(ctx)
    taken = [r.get("slug", "") for r in projects]
    if explicit:
        final = base
        if base.upper() in {str(t).upper() for t in taken}:
            warn(f"project_slug {base!r} is already used by another fleet project — qualified ids "
                 f"({base.upper()}-tN) will be ambiguous. Set a distinct project_slug in {RC_NAME}.")
    else:
        final = _dedupe_slug(base, taken)
        if final != base:
            _update_config(ctx, project_slug=final)
            info(f"Fleet dedup: slug '{base}' is taken — this project's qualifier is "
                 f"'{final.upper()}-tN' (saved to {RC_NAME}).")
    row = {"path": key, "slug": final, "name": _project_name(ctx), "added": now_iso()}
    projects.append(row)
    data["projects"] = projects
    _save_fleet_registry(data)
    return row


def _fleet_autoregister(ctxdir: Path) -> None:
    """Best-effort: ensure the orienting project is in the fleet registry (CELE-t124). The board's
    savings bar and `celeborn fleet` only count REGISTERED projects, so a project the user never ran
    `celeborn fleet register` on stayed invisible in the fleet economics even while recording locally — the
    'why are there only 3 projects?' bug. Self-registering on orient closes that gap: every project that
    actually runs Celeborn shows up. Quiet (stdout swallowed) and swallow-all — a registry hiccup must
    never break rehydration. Idempotent. The global ~/.context sink is NOT a project, so skip it."""
    try:
        if ctxdir.resolve() == _global_context().resolve():
            return
        proj = ctxdir.parent.resolve()
        for row in _load_fleet_registry().get("projects") or []:
            if _resolve_project_dir(row.get("path", "")) == proj:
                return                                  # already registered — nothing to do
        with contextlib.redirect_stdout(io.StringIO()):
            _fleet_register_path(proj)
    except Exception:
        pass


def _fleet_repair(apply: bool = True) -> list[dict]:
    """One-shot re-dedup of the whole fleet registry — the repair t85 deferred. t85 only deduped at
    REGISTER time, so projects registered before it (or before t84's short slugs) kept a stale slug
    that never went through dedup; a later project's *short* qualifier could then collide undetected.

    This recomputes each project's CURRENT effective qualifier (explicit `project_slug` is authority and
    kept verbatim — only a clash is flagged; a derived slug is the short folder form), walks them in
    registration order assigning a unique qualifier (case-insensitive, numeric-suffixed on collision),
    and reconciles BOTH the registry row AND the project's `.celebornrc` so display + markers + dedup all
    agree. Returns a list of change records; `apply=False` is a dry run (no writes). Idempotent."""
    data = _load_fleet_registry()
    projects = data.get("projects") or []
    taken: set[str] = set()          # qualifiers (upper-cased) already assigned this pass
    changes: list[dict] = []
    dirty = False
    for row in projects:
        pdir = _resolve_project_dir(row.get("path", ""))
        ctx = pdir / CONTEXT_DIRNAME
        if not ctx.is_dir():
            changes.append({"path": str(pdir), "name": row.get("name", ""), "action": "skip",
                            "reason": "no .context/ (unreachable project)"})
            continue
        explicit = bool((load_config(ctx).get("project_slug") or "").strip())
        base = project_slug(ctx)
        old_slug = row.get("slug", "")
        rc_written = False
        collision = False
        if explicit:
            final = base
            collision = base.upper() in taken     # authority kept; only flagged, never suffixed
        else:
            final = _dedupe_slug(base, taken)
            if final != base and apply:
                _update_config(ctx, project_slug=final)
                rc_written = True
        if row.get("slug") != final:
            if apply:
                row["slug"] = final
            dirty = True
        taken.add(final.upper())
        if old_slug != final or collision:
            changes.append({"path": str(pdir), "name": row.get("name", ""), "old": old_slug,
                            "new": final, "explicit": explicit, "collision": collision,
                            "rc_written": rc_written})
    if apply and dirty:
        data["projects"] = projects
        _save_fleet_registry(data)
    return changes


def _fleet_unregister_path(project_dir: Path) -> bool:
    project_dir = _resolve_project_dir(str(project_dir))
    key = str(project_dir)
    data = _load_fleet_registry()
    before = len(data.get("projects") or [])
    data["projects"] = [r for r in (data.get("projects") or [])
                        if _resolve_project_dir(r.get("path", "")) != project_dir and r.get("path") != key]
    if len(data["projects"]) == before:
        return False
    _save_fleet_registry(data)
    return True


def _fleet_project_paths(ctx: Path | None) -> list[Path]:
    """Registered fleet projects, plus the orienting project if it isn't registered yet."""
    seen: set[str] = set()
    out: list[Path] = []
    for row in _load_fleet_registry().get("projects") or []:
        p = _resolve_project_dir(row.get("path", ""))
        key = str(p)
        if key in seen or not (p / CONTEXT_DIRNAME).is_dir():
            continue
        seen.add(key)
        out.append(p)
    if ctx is not None:
        cur = ctx.parent.resolve()
        key = str(cur)
        if key not in seen and (cur / CONTEXT_DIRNAME).is_dir():
            out.insert(0, cur)
    return out


# --------------------------------------------------------------------------- commit intents (CELE-t303)
#
# The blackboard's THIRD coordination channel (design thread: plan/cele-fleet-blackboard.md; first
# consumer: the CSP parallel builds, CELE-t302). Touches say WHERE an agent is, the board says WHAT
# it owns — an intent says what it is ABOUT TO DO to the shared tree: "I plan to commit these files,
# under this card, roughly this soon." Peers editing the same files get the warning in their
# per-turn envelope BEFORE they commit, so concurrent agents negotiate commit order up front instead
# of discovering a same-file sweep after the fact (`git commit --only` is file-granular — see
# references/multi-agent-editing.md). Substrate: fleet.json — machine-global, so worktrees of one
# repo share the choreography (the t154 §5 substrate argument) — with the same concurrency rules as
# the roster design: field-scoped writes (upsert your own row, never rewrite a peer's), atomic
# replace, TTL-bounded. One intent per (agent, project): your NEXT commit, not a queue.
INTENT_TTL_HOURS_DEFAULT = 2.0   # matches the touch TTL — a plan older than this is stale noise


def _intent_ttl_hours(ctx: Path | None) -> float:
    if ctx is not None:
        try:
            return float(load_config(ctx).get("intent_ttl_hours", INTENT_TTL_HOURS_DEFAULT))
        except Exception:  # noqa: BLE001 — a broken rc must never break the blackboard
            pass
    return INTENT_TTL_HOURS_DEFAULT


def _prune_intents(rows: list, ttl_hours: float) -> list:
    """Intents past TTL are dropped on read — the planned commit either landed or went stale."""
    if ttl_hours <= 0:
        return rows
    cutoff = _dt.datetime.now() - _dt.timedelta(hours=ttl_hours)
    keep = []
    for r in rows:
        at = _parse_touch_at((r or {}).get("at", ""))
        if at is not None:
            if at.tzinfo is not None:
                at = at.replace(tzinfo=None)
            if at >= cutoff:
                keep.append(r)
    return keep


def _active_intents(project: Path | None, ctx: Path | None = None) -> list[dict]:
    """Live declared intents, newest first — one project's, or machine-wide (project=None).
    Prunes stale rows on read, like `_active_touches`."""
    data = _load_fleet_registry()
    rows = [r for r in (data.get("intents") or []) if isinstance(r, dict)]
    kept = _prune_intents(rows, _intent_ttl_hours(ctx))
    if len(kept) != len(rows):
        data["intents"] = kept
        _save_fleet_registry(data)
    if project is not None:
        key = str(_resolve_project_dir(str(project)))
        kept = [r for r in kept if r.get("project") == key]
    return sorted(kept, key=lambda r: r.get("at") or "", reverse=True)


def _upsert_intent(project: Path, row: dict) -> None:
    """One intent per (agent, project) — re-declaring replaces your previous plan. Field-scoped:
    a peer's row is never rewritten, only your own is dropped and re-appended."""
    data = _load_fleet_registry()
    rows = [r for r in (data.get("intents") or []) if isinstance(r, dict)]
    rows = _prune_intents(rows, _intent_ttl_hours(None))
    key = str(_resolve_project_dir(str(project)))
    rows = [r for r in rows if not (r.get("project") == key and r.get("by") == row.get("by"))]
    rows.append(row)
    data["intents"] = rows
    _save_fleet_registry(data)


def _drop_intents(project: Path, by: str = "", task: str = "") -> list[dict]:
    """Remove matching intents — an agent's `intent done`, or a shipping card withdrawing its plans.
    No filter drops all of the project's intents (`intent clear`). Returns what was dropped."""
    data = _load_fleet_registry()
    rows = [r for r in (data.get("intents") or []) if isinstance(r, dict)]
    key = str(_resolve_project_dir(str(project)))

    def _matches(r: dict) -> bool:
        return (r.get("project") == key
                and (not by or r.get("by") == by)
                and (not task or r.get("task") == task))

    dropped = [r for r in rows if _matches(r)]
    if dropped:
        data["intents"] = [r for r in rows if not _matches(r)]
        _save_fleet_registry(data)
    return dropped


def _parse_eta_minutes(raw: str):
    """'20' / '45m' / '1.5h' → minutes (int), or None when absent/unparseable."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minutes|h|hr|hrs|hours)?", s)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2) or "m"
    return int(val * 60) if unit.startswith("h") else int(val)


def _intent_eta_label(eta_iso: str) -> str:
    ts = _parse_touch_at(eta_iso)
    if ts is None:
        return ""
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    mins = int((ts - _dt.datetime.now()).total_seconds() // 60)
    return "due now" if mins <= 0 else f"~{mins}m out"


def _intent_line(r: dict) -> str:
    """One human-readable intent row — shared by `intent list`, orient, and the envelope warning."""
    label = _agent_label(r.get("family", ""), r.get("model", ""))
    who = f"@{r.get('by') or 'unknown'}" + (f" ({label})" if label else "")
    task = f" [{r['task']}]" if r.get("task") else ""
    files = ", ".join(r.get("files") or []) or "(files unspecified)"
    what = f" — \"{r['what']}\"" if r.get("what") else ""
    bits = [b for b in (_touch_age_label(r.get("at", "")), _intent_eta_label(r.get("eta", "")))
            if b and b != "?"]
    when = f"  ({' · '.join(bits)})" if bits else ""
    return f"{who}{task} plans to commit {files}{what}{when}"


def _intent_json_row(r: dict) -> dict:
    """One intent row for machine consumers (the board's `/api/intents`, CELE-t347), carrying the
    same derived display strings the CLI text uses — so the board's chip/warn wording is verbatim
    the t303 CLI (`_intent_line` · the envelope eta/age labels) rather than re-derived in TS."""
    return {
        **r,
        "line": _intent_line(r),
        "age_label": _touch_age_label(r.get("at", "")),
        "eta_label": _intent_eta_label(r.get("eta", "")),
        "qualified": _display_tid(None, r["task"], slug=r.get("slug") or "") if r.get("task") else "",
    }


def _intents_orient_summary(ctx: Path) -> str:
    """Compact declared-commit view for Orient — the whole project's choreography at a glance."""
    rows = _active_intents(ctx.parent, ctx)
    if not rows:
        return ""
    n = len(rows)
    out = [f"{n} declared commit intent{'s' if n != 1 else ''}    "
           f"(`celeborn intent list` — coordinate before committing these files)"]
    out += [f"  {_intent_line(r)}" for r in rows]
    return "\n".join(out)


def _intent_overlap_notice(ctx: Path, handle: str) -> str:
    """Peer intents whose files overlap MY active touches — the per-turn telepathy warning.
    Own intents never warn (you already know your plan); no touches → nothing to collide with."""
    if not handle:
        return ""
    mine = {r["path"] for r in _active_touches(ctx) if r.get("by") == handle}
    if not mine:
        return ""
    lines = []
    for r in _active_intents(ctx.parent, ctx):
        if (r.get("by") or "") == handle:
            continue
        overlap = sorted(mine & {str(f) for f in (r.get("files") or [])})
        if overlap:
            lines.append(f"🏹 Celeborn intent —> {_intent_line(r)} — you are touching: "
                         f"{', '.join(overlap)}")
    return "\n".join(lines)


def cmd_intent(args):
    """`celeborn intent "<what>"` — declare a planned commit on the fleet blackboard (the third
    channel: touches = where I am, the board = what I own, intent = what I'm ABOUT to do to the
    shared tree). Peers editing the same files are warned in their next turn. `list` / `done` /
    `clear` manage the register; files default to your active touches for --task."""
    ctx = require_context(args)
    words = list(getattr(args, "words", None) or [])
    cmd = words[0] if words and words[0] in ("list", "done", "clear") else None
    repo = ctx.parent.resolve()
    ident = _agent_identity(args, ctx)
    who = ident["handle"]

    if cmd == "list":
        rows = _active_intents(None if getattr(args, "all", False) else repo, ctx)
        if getattr(args, "json", False):
            # The board (CELE-t347) matches declared files vs live touches to place the chip on the
            # declaring card and the amber hold-warn on an overlapped peer, so ship both channels in
            # one call. Touches are this project's only (the fleet-wide `--all` view omits them —
            # touches are project-scoped and there is no single ctx to read them from).
            payload = {"intents": [_intent_json_row(r) for r in rows]}
            if not getattr(args, "all", False):
                payload["touches"] = _active_touches(ctx)
            print(json.dumps(payload, indent=2))
            return
        if not rows:
            print("(no declared intents)")
            return
        for r in rows:
            print(_intent_line(r))
        return

    if cmd == "done":
        # Release is session-scoped, always (CELE-t370): a release with no identity used to fall
        # through to `by=""`/`by="unknown"` and could clobber a peer's live plan on the same file.
        # Requiring a session means `done` can only ever withdraw YOUR own intent, never a peer's.
        if not _resolve_session(args):
            die("intent done requires a session id so it releases ONLY your own intent, never a "
                "peer's — run inside a Claude session or pass --session <id> (CELE-t370).")
        dropped = _drop_intents(repo, by=who)
        if dropped:
            print(f"Intent released for @{who} — {len(dropped)} plan(s) withdrawn (yours only)")
        else:
            print(f"(no intent on record for @{who})")
        return

    if cmd == "clear":
        # The old bare `clear` silently wiped every agent's intent — the exact footgun t370 closes.
        # Now bare `clear` is session-scoped (identical to `done`); the fleet-wide wipe is an
        # explicit, deliberate `--all-agents`, and both still require a session as identity.
        if not _resolve_session(args):
            die("intent clear requires a session id (CELE-t370). Bare `clear` releases only your "
                "own intent; add --all-agents to deliberately wipe every agent's intent.")
        if getattr(args, "all_agents", False):
            dropped = _drop_intents(repo)
            print(f"Cleared ALL {len(dropped)} intent(s) for this project (--all-agents) — "
                  "every agent's plan withdrawn.")
        else:
            dropped = _drop_intents(repo, by=who)
            print(f"Released {len(dropped)} intent(s) for @{who} (yours only; "
                  "use --all-agents to wipe the whole fleet's).")
        return

    what = " ".join(words).strip()
    task = (getattr(args, "task", None) or "").strip()
    # An intent with no identity or no purpose is noise to peers (CELE-t370). Require BOTH before
    # anything lands on the blackboard: a live session (WHO plans the commit — the `by` handle is
    # session-derived, same as a claim) and a real card (the purpose peers can look up). The
    # free-text "<what>" is the human line; the card is its context.
    if not _resolve_session(args):
        die("intent requires a session id — the intent is filed under your session so peers know "
            "WHO plans the commit. Run inside a Claude session or pass --session <id> (CELE-t370).")
    if not what:
        die('intent requires a description: celeborn intent "<what you will commit>" --task <id> '
            "[--files a.py,b.ts] [--eta 30m]  (CELE-t370)")
    if not task:
        die("intent requires --task <id> — an intent with no card has no purpose peers can read; "
            "name the card this commit is for (CELE-t370).")
    bare_task = _resolve_task_arg(ctx, task)
    if not _find_task(_load_tasks(ctx), bare_task):
        die(f"intent --task {task}: no such card on this board — file the card first "
            "(`celeborn tasks add …`) so the intent points at a real purpose (CELE-t370).")
    task = bare_task
    raw_files = (getattr(args, "files", None) or "").strip()
    files = ([_resolve_repo_relpath(repo, f) for f in raw_files.split(",") if f.strip()]
             if raw_files else [])
    if not files and task:
        files = sorted(p for p, recs in (_load_touches(ctx).get("files") or {}).items()
                       if any((m or {}).get("task") == task for m in recs))
    if not what and not files:
        die('usage: celeborn intent "<what you will commit>" [--files a.py,b.ts] [--task tN] [--eta 30m]\n'
            "       celeborn intent list [--all] [--json] | done | clear")
    if not files:
        warn("no --files and no touches for the task — name the files so peers know what to hold")
    eta_min = _parse_eta_minutes(getattr(args, "eta", None) or "")
    row = {
        "by": who,
        "family": ident["family"],
        "model": ident["model"],
        "project": str(repo),
        "slug": project_slug(ctx),
        "task": task,
        "files": files,
        "what": what,
        "eta": ((_dt.datetime.now() + _dt.timedelta(minutes=eta_min)).isoformat(timespec="seconds")
                if eta_min else ""),
        "at": now_iso(),
    }
    _upsert_intent(repo, row)
    print(f"Intent declared: {_intent_line(row)}")
    print("  peers touching these files are warned before they commit; "
          "run `celeborn intent done` once yours lands.")


def _load_session(ctx: Path) -> dict:
    p = ctx / "session.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _clip_store(text: str, limit: int) -> tuple[str, bool]:
    """Bound a stored Hot-tier string to `limit` chars. Unlike `_clip` (which points at a file for
    display), this is for what we persist: long-form detail belongs in state.md/notes.md, not a JSON
    scalar. Returns (text, was_clipped)."""
    if limit <= 0 or not isinstance(text, str) or len(text) <= limit:
        return text, False
    cut = text.rfind(" ", 0, limit)
    if cut < limit // 2:
        cut = limit
    return text[:cut].rstrip() + " …[clipped — keep long-form in state.md / notes.md]", True


def _write_session(ctx: Path, data: dict, cfg: dict | None = None) -> list[str]:
    """The single safe writer for session.json: guarantees a schema, clips the fragile free-text
    fields to `hot_focus_max_chars`, and always emits valid JSON. Returns the list of clipped field
    names (for caller messaging). This is the choke point that hand-editing kept getting wrong."""
    cfg = cfg or load_config(ctx)
    limit = int(cfg.get("hot_focus_max_chars", 1500))
    data.setdefault("schema", "celeborn/1")
    clipped = []
    for field in ("focus", "next_action"):
        if isinstance(data.get(field), str):
            new, hit = _clip_store(data[field], limit)
            data[field] = new
            if hit:
                clipped.append(field)
    (ctx / "session.json").write_text(json.dumps(data, indent=2) + "\n")
    return clipped


def _prep_stale_reasons(ctx: Path, cfg: dict, data: dict, owner: str) -> list[str]:
    """Freshness gate for `checkpoint --for-clear` (CELE-t208). Returns the human-readable reasons the
    card is NOT yet losslessly resumable after a /clear — an empty list means clean. This never touches
    files; it only decides the verdict + exit code so an auto-clear (t209) can trust a 0 exit before it
    pulls the trigger. The insight in state.md is model-authored prose a command can't invent — so the
    gate verifies the model *did* freshen it, rather than pretending to do it for them."""
    reasons: list[str] = []
    stale_min = int(cfg.get("prep_stale_minutes", 20) or 20)

    # 1. state.md — the Hot headline a resume reads first. It must exist, be authored (not the scaffold
    #    placeholders), and have been rewritten recently: the pre-clear ritual is rewrite-then-checkpoint,
    #    so a headline older than the staleness window means the model skipped the rewrite.
    sm = ctx / "state.md"
    if not sm.is_file():
        reasons.append("state.md is missing — the Hot headline a resume reads first.")
    else:
        text = ""
        try:
            text = sm.read_text()
        except OSError:
            pass
        # Both the raw template placeholders AND the scaffold's own "not authored yet" sentinel (the
        # `celeborn scaffold` starter headline) count as un-authored — a /clear here resumes into a stub.
        placeholders = ("<what we are working on", "<the single next concrete step>",
                        "not a work focus yet")
        if not text.strip():
            reasons.append("state.md is empty — rewrite the Focus / Next action headline.")
        elif any(p in text for p in placeholders):
            reasons.append("state.md still holds the scaffold placeholders — author the real headline.")
        else:
            try:
                age_min = (_dt.datetime.now()
                           - _dt.datetime.fromtimestamp(sm.stat().st_mtime)).total_seconds() / 60
                if age_min > stale_min:
                    reasons.append(f"state.md last rewritten {int(age_min)}m ago (> {stale_min}m) — "
                                   "freshen the headline so the resume isn't reading stale work.")
            except OSError:
                pass

    # 2. session.json — focus + next_action are exactly what a fresh thread continues from; empty OR
    #    still carrying the scaffold's starter text = the Hot tier was never authored this session.
    scaffold_sentinels = ("no work focus set yet", "then rewrite state.md")
    for field, flag in (("focus", "--focus"), ("next_action", "--next")):
        val = (data.get(field) or "").strip()
        label = "focus" if field == "focus" else "next action"
        if not val:
            reasons.append(f"session.json {label} is empty — set it ({flag}) so the resume knows where "
                           "to pick up.")
        elif any(s in val for s in scaffold_sentinels):
            reasons.append(f"session.json {label} still carries the scaffold starter text — author it "
                           f"({flag}) with the real state.")

    # 3. The DOING card(s) this session owns must carry a REAL Stop condition, not the generic default —
    #    the Stop point is the marker that says 'this is a clean place to clear'.
    if owner:
        for t in _doing_for_owner(_load_tasks(ctx), owner):
            if (t.get("stop") or "").strip() == DEFAULT_STOP:
                reasons.append(f"card {_display_tid(ctx, t['id'])} still carries the generic default Stop — "
                               f"set a real one: celeborn tasks edit {t['id']} --stop \"…\".")
    return reasons


def _prep_for_clear(ctx: Path, args, reasons: list[str]) -> None:
    """The mechanical half of `checkpoint --for-clear`: regenerate handoff.md and take a restorable
    panic-save snapshot — ALWAYS, even when the gate fails, because the safety net must exist regardless
    — then print the verdict. A clean gate exits 0 ('resumable'); a stale one exits 1 with the fix-list
    so an auto-clear holds off. The `reasons` already drove session.json's stop_allowed in the caller."""
    # handoff.md — the paste-into-a-fresh-thread resume prompt, regenerated from the just-written session.
    try:
        cmd_handoff(args)
    except SystemExit:
        raise
    except Exception as e:                          # noqa: BLE001 — snapshot is the real safety net
        warn(f"handoff regeneration skipped: {e}")
    # panic-save — the deterministic restore point (survives a /clear regardless of the gate outcome).
    snap = _do_panic_save(ctx, reason="prep-for-clear", session=_resolve_session(args))
    m = _load_metrics(ctx)
    m["panic_saves"] = int(m.get("panic_saves", 0) or 0) + 1
    _save_metrics(ctx, m)
    ok(f"pre-clear snapshot → .context/{PANIC_DIR}/{snap.get('stamp', '')}/ "
       f"({len(snap.get('files', []))} files · restore: celeborn restore)")

    if reasons:
        warn(f"NOT yet losslessly resumable — {len(reasons)} thing(s) to fix before you /clear "
             "(session marked stop_allowed=false):")
        for r in reasons:
            print(f"      • {r}")
        warn("fix the above, then re-run `celeborn checkpoint --for-clear`.")
        sys.exit(1)
    ok("resumable — Hot tier fresh, handoff + snapshot written, Stop point set. Safe to /clear.")


def cmd_checkpoint(args):
    """`celeborn checkpoint` — the safe way to update session.json. Loads the current file (repairing
    it from the template if it's missing or unparseable), applies only the flags you pass, stamps
    `updated_at`, clips over-long focus/next_action, and writes valid JSON. Run it with no flags to
    re-stamp and repair in place. This replaces hand-editing the raw JSON (the recurring corruption
    source). With `--for-clear` it becomes the pre-clear routine (CELE-t208): after writing session.json
    it regenerates handoff, takes a restorable snapshot, and verify-gates that a /clear would lose
    nothing — exiting nonzero with a fix-list when the Hot tier is stale."""
    ctx = require_context(args)
    cfg = load_config(ctx)
    sj = ctx / "session.json"

    repaired = False
    data: dict = {}
    if sj.is_file():
        try:
            loaded = json.loads(sj.read_text())
            data = loaded if isinstance(loaded, dict) else {}
            if not isinstance(loaded, dict):
                repaired = True
        except (json.JSONDecodeError, OSError):
            repaired = True
    if not data:
        try:
            data = json.loads((TEMPLATES_DIR / "session.json").read_text())
        except (json.JSONDecodeError, OSError):
            data = {"schema": "celeborn/1", "focus": "", "next_action": "",
                    "branch": "", "status": "in-progress", "stop_allowed": True, "open_threads": []}

    if getattr(args, "focus", None) is not None:
        data["focus"] = args.focus
    if getattr(args, "next", None) is not None:
        data["next_action"] = args.next
    if getattr(args, "branch", None) is not None:
        data["branch"] = args.branch
    if getattr(args, "status", None) is not None:
        data["status"] = args.status
    if getattr(args, "stop_allowed", False):
        data["stop_allowed"] = True
    if getattr(args, "no_stop_allowed", False):
        data["stop_allowed"] = False

    for_clear = getattr(args, "for_clear", False)
    stale_reasons: list[str] = []
    if for_clear:
        stale_reasons = _prep_stale_reasons(ctx, cfg, data, _claim_identity(args))
        # The gate owns stop_allowed for a pre-clear checkpoint unless the caller forced it by hand:
        # clean → safe to /clear, stale → not.
        if not getattr(args, "stop_allowed", False) and not getattr(args, "no_stop_allowed", False):
            data["stop_allowed"] = not stale_reasons

    data["updated_at"] = now_iso()

    clipped = _write_session(ctx, data, cfg)
    if repaired:
        warn("session.json was missing or invalid — rebuilt from a clean template.")
    if clipped:
        warn(f"clipped {', '.join(clipped)} to {cfg.get('hot_focus_max_chars', 1500)} chars — "
             "put the long-form detail in state.md / notes.md.")
    fields = [f for f in ("focus", "next_action", "branch", "status") if data.get(f)]
    ok(f"checkpoint written → .context/session.json (updated_at + {', '.join(fields) or 'no fields'})")

    if for_clear:
        _prep_for_clear(ctx, args, stale_reasons)


# ------------------------------------------------------------- auto-clear (CELE-t209, opt-in)

def _autoclear_due(cfg: dict, cur: dict) -> bool:
    """Whether a session's live window should trigger the opt-in OpenCode auto-clear: the feature is
    on, the just-graded pressure is hard, and the per-session cooldown has lapsed. Pure predicate —
    `record tokens` prints the due-marker off it, and `cmd_autoclear` re-checks it before acting."""
    if not cfg.get("opencode_autoclear"):
        return False
    if cur.get("pressure") != "hard":
        return False
    at = (cur.get("autoclear_at") or "").strip()
    if at:
        try:
            gap_min = (_dt.datetime.now() - _dt.datetime.fromisoformat(at)).total_seconds() / 60
            if gap_min < float(cfg.get("autoclear_cooldown_minutes", 10) or 10):
                return False
        except ValueError:
            pass
    return True


def _autoclear_brief(ctx: Path, data: dict, sid: str) -> str:
    """The resume brief queued to the session's outbox before an auto-clear — what the coder reads
    on its first post-compaction turn. Focus/next come from the just-checkpointed session.json; the
    card marker makes claim-on-receipt re-assert the session→card link even if the compaction
    summary mangled it."""
    lines = [
        "🏹 Celeborn auto-clear (CELE-t209): this session crossed the hard context threshold and "
        "was compacted losslessly — your durable memory is on disk and re-injected every turn. "
        "Resume the in-flight work now; no human step is coming.",
        f"- Focus: {(data.get('focus') or '').strip() or '(see .context/state.md)'}",
        f"- Next action: {(data.get('next_action') or '').strip() or '(see .context/state.md)'}",
        "- Full resume prompt: .context/handoff.md (regenerated moments ago).",
    ]
    tid = _session_task_id(ctx, sid)
    if tid:
        card = _find_task(_load_tasks(ctx), tid)
        if card is not None and card.get("state") == "doing":
            stop = (card.get("stop") or "").strip()
            lines.insert(1, f"- Your card: [{_display_tid(ctx, tid)}] {card['title']} — still yours "
                            f"(DOING).{' Stop: ' + stop if stop else ''}")
            lines.append("")
            lines.append(_card_marker(tid, project_slug(ctx)))
    return "\n".join(lines)


def cmd_autoclear(args):
    """`celeborn autoclear --session <sid>` — the decision step of the opt-in OpenCode seamless
    clear-and-continue (CELE-t209). Called by the plugin at a turn boundary after `record tokens`
    printed the due-marker; re-verifies everything (opt-in, still-hard pressure, cooldown), then
    runs the t208 freshness gate. Verdicts on stdout, one machine-readable first line:
      `autoclear: skip (…)`    — not due after all; the plugin does nothing.        exit 0
      `autoclear: blocked — …` — Hot tier stale; fix-list follows, the plugin       exit 1
                                 hands it to the coder so IT freshens and retries.
      `autoclear: ready — …`   — handoff + snapshot written, resume brief queued    exit 0
                                 to outbox/<sid6>.md, cooldown stamped; compact now."""
    ctx = require_context(args)
    cfg = load_config(ctx)
    sid = _resolve_session(args)
    if not sid:
        die("autoclear requires --session <id> (or an ambient session)")
    if not cfg.get("opencode_autoclear"):
        print("autoclear: skip (disabled — set \"opencode_autoclear\": true in .celebornrc to opt in)")
        return
    m = _load_metrics(ctx)
    caps = m.get("captures") if isinstance(m.get("captures"), dict) else {}
    cur = dict(caps.get(sid) or {})
    total = int(cur.get("tokens_session") or 0)
    soft, hard = _context_thresholds(cfg)
    cur["pressure"] = _pressure_level(total, soft, hard)
    if cur["pressure"] != "hard":
        print(f"autoclear: skip (pressure {cur['pressure']}, {total:,} tokens < hard {hard:,})")
        return
    if not _autoclear_due(cfg, cur):
        print(f"autoclear: skip (cooldown — last attempt {cur.get('autoclear_at')})")
        return

    # The t208 freshness gate — the whole point of the blocked verdict: an auto-clear must never
    # compact a session whose Hot tier would resume stale. The model fixes, then we retry.
    sj = ctx / "session.json"
    data: dict = {}
    if sj.is_file():
        try:
            loaded = json.loads(sj.read_text())
            data = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            pass
    reasons = _prep_stale_reasons(ctx, cfg, data, sid[:6])
    if reasons:
        print("autoclear: blocked — the Hot tier is stale; an auto-clear now would lose context. "
              "Fix these, then it proceeds on its own:")
        for r in reasons:
            print(f"  • {r}")
        print("  Then run: celeborn checkpoint --for-clear  (the gate re-checks at the next turn)")
        sys.exit(1)

    # Gate clean — the mechanical prep (mirrors _prep_for_clear, without its verdict wording).
    try:
        cmd_handoff(args)
    except SystemExit:
        raise
    except Exception as e:                          # noqa: BLE001 — snapshot is the real safety net
        warn(f"handoff regeneration skipped: {e}")
    snap = _do_panic_save(ctx, reason="autoclear", session=sid)
    m = _load_metrics(ctx)
    m["panic_saves"] = int(m.get("panic_saves", 0) or 0) + 1
    caps = m.get("captures") if isinstance(m.get("captures"), dict) else {}
    cur = dict(caps.get(sid) or {})
    cur["autoclear_at"] = now_iso()
    _write_capture(m, caps, sid, cur)
    _save_metrics(ctx, m)
    slug = _outbox_queue(ctx, _autoclear_brief(ctx, data, sid), sid[:6], tag=" [autoclear]")
    print(f"autoclear: ready — snapshot .context/{PANIC_DIR}/{snap.get('stamp', '')}/, resume brief "
          f"→ outbox/{slug}.md; compact the session now.")


def _parse_activity_meta(ctx: Path) -> dict:
    """Mechanical digest fields from activity.md — last capture time + last user prompt."""
    p = ctx / "activity.md"
    if not p.is_file():
        return {}
    try:
        text = p.read_text()
    except OSError:
        return {}
    out: dict = {}
    m = _ACTIVITY_CAPTURE_RE.search(text)
    if m:
        out["last_capture"] = m.group(1).strip()
    m = _ACTIVITY_PROMPT_RE.search(text)
    if m:
        out["last_prompt"] = m.group(1).strip()
    return out


def _minutes_since_iso(at: str) -> int | None:
    ts = _parse_touch_at(at)
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return max(0, int((_dt.datetime.now() - ts).total_seconds() // 60))


def _fleet_agent_status(ctx: Path, owner: str, doing: list[dict], touches: list[dict], *, live: bool = False) -> str:
    """working | stuck | idle — per-agent liveness from touches + DOING cards.

    `live` (CELE-t172): the owning project shows real recent activity — a live transcript window, a
    fresh mechanical capture, or a session heartbeat. File touches are an optional, releasable
    protocol, so a session that released its touches (or merely let them age) while still DOING must
    not read as "stuck" if it's demonstrably alive. When `live`, a touch-derived "stuck" is
    downgraded back to "working"."""
    who = (owner or "").strip() or "_unassigned"
    mine = [t for t in touches if (t.get("by") or "").strip() == who]
    cards = [t for t in doing if ((t.get("owner") or "").strip() or "_unassigned") == who]
    if mine:
        ages = [_minutes_since_iso(t.get("at", "")) for t in mine]
        ages = [a for a in ages if a is not None]
        if ages and min(ages) <= _FLEET_WORKING_MINUTES:
            status = "working"
        elif ages and min(ages) > _FLEET_STUCK_MINUTES and cards:
            status = "stuck"
        elif cards and not _task_has_active_touches(ctx, cards[0]["id"]):
            status = "stuck"
        else:
            status = "working" if ages and min(ages) <= _FLEET_STUCK_MINUTES else "idle"
    elif cards:
        status = "stuck" if any(_is_stale_doing(ctx, t) for t in cards) else "idle"
    else:
        status = "idle"
    if status == "stuck" and live and cards:
        return "working"
    return status


def _fleet_project_snapshot(project_dir: Path) -> dict | None:
    """One project's live fleet row — mechanical, no model."""
    project_dir = _resolve_project_dir(str(project_dir))
    ctx = project_dir / CONTEXT_DIRNAME
    if not ctx.is_dir():
        return None
    tasks = _load_tasks(ctx)
    touches = _active_touches(ctx)
    session = _load_session(ctx)
    activity = _parse_activity_meta(ctx)
    doing = [t for t in tasks if t["state"] == "doing"]

    # Real session liveness (CELE-t172), shared by the stale gate below and the context band. A live
    # transcript window, a fresh mechanical capture, or a recent session heartbeat all mean "someone
    # is actively here" — regardless of whether file touches are present.
    #
    # CELE-t178: context tracking follows the SESSION, not the card — a shipped card must not clear the
    # fleet widget's context band while the owning session is still alive. So we scan transcripts when
    # the project has DOING cards OR shows fresh mechanical activity (capture/session within the stale
    # window). The transcript scan (and its token estimate) stays skipped for genuinely idle projects,
    # preserving the CELE-t170 per-poll perf win — a completed-but-live session keeps a fresh
    # activity.md capture every turn, so it still qualifies.
    last_mins = _minutes_since_iso(activity.get("last_capture", ""))
    sess_mins = _minutes_since_iso(session.get("updated_at", ""))
    _cheap_live = (
        (last_mins is not None and last_mins <= _FLEET_STUCK_MINUTES)
        or (sess_mins is not None and sess_mins <= _FLEET_STUCK_MINUTES)
    )
    agent_rows = _active_agents(ctx, AGENT_ACTIVE_WINDOW_MIN, False) if (doing or _cheap_live) else []
    tokens_by_task: dict[str, int] = {}
    session_by_task: dict[str, str] = {}
    for r in agent_rows:  # sorted fullest-window first, so the loudest session wins per card
        tid = r.get("task_id")
        if tid:
            tokens_by_task[tid] = max(tokens_by_task.get(tid, 0), int(r.get("tokens") or 0))
            sid = (r.get("session") or "")[:6]
            if sid and tid not in session_by_task:
                session_by_task[tid] = sid
    project_live = bool(agent_rows) or _cheap_live

    owners: set[str] = set()
    for t in doing:
        owners.add((t.get("owner") or "").strip() or "_unassigned")
    for t in touches:
        owners.add((t.get("by") or "").strip() or "unknown")
    agents = []
    for who in sorted(owners):
        cards = [{"id": t["id"], "title": t["title"], "stale": _is_stale_doing(ctx, t) and not project_live}
                 for t in doing
                 if ((t.get("owner") or "").strip() or "_unassigned") == who]
        touch_rows = [{"path": t["path"], "task": t.get("task") or "", "age": t.get("age") or ""}
                      for t in touches if (t.get("by") or "").strip() == who]
        if not cards and not touch_rows:
            continue
        agents.append({
            "id": who,
            "status": _fleet_agent_status(ctx, who, doing, touches, live=project_live),
            "doing": cards,
            "touches": touch_rows,
        })
    port = board_port(ctx)
    live = _board_live(port)
    if any(a.get("status") == "working" for a in agents):
        proj_status = "working"
    elif any(a.get("status") == "stuck" for a in agents):
        proj_status = "stuck"
    else:
        proj_status = "idle"
    # Enrichment for the fleet home cards (CELE-t170): the board's owner→model join, a project-
    # qualified display id, and each DOING card's sand-fill progress; plus the top TODO the project
    # should pick up next. The per-card context-window band (`k`) rides `tokens_by_task` above
    # (CELE-t172) — the same live-window value `celeborn agents --json` returns — so the fleet cards
    # carry the /clear-nudge band, matching the tasks board.
    slug = project_slug(ctx)
    reg = _load_agents(ctx).get("agents") or {}
    alerts = _live_alerts(ctx)   # CELE-t195: drop alerts from ended sessions so a dead window can't blink

    def _doing_row(t: dict) -> dict:
        owner = (t.get("owner") or "").strip()
        model = (reg.get(owner) or {}).get("model") or ""
        tokens = tokens_by_task.get(t["id"])
        return {
            "id": t["id"],
            "display_id": _display_tid(ctx, t["id"], slug=slug),
            "title": t["title"],
            # Session / human handle only — a model-derived owner falls back to the live session id
            # (when known) or is suppressed, never rendered as the owner (CELE-t172).
            "owner": _display_owner(owner, model, session_by_task.get(t["id"], "")),
            "owner_model": model,
            # Live session short-id, the clean join key for the fleet living-card ticker (CELE-t349):
            # SSE events carry full session ids, cards are owned by short ids — matchSession() bridges
            # them. Owner alone is unreliable (a human handle isn't the session), so surface it directly.
            "session": session_by_task.get(t["id"], ""),
            # The card's Stop condition — the fleet living card renders it as the clean-/clear line,
            # full anatomy parity with the Stage card (CELE-t349).
            "stop": (t.get("stop") or "").strip(),
            "progress": t.get("progress") or 0,
            # Live session (recent transcript/capture/heartbeat) is never stale — touches are an
            # optional, releasable protocol, so a lapsed/released touch alone must not read stale.
            "stale": _is_stale_doing(ctx, t) and not project_live,
            # Live context window in k-tokens for the /clear-nudge band pill; None when no live
            # transcript attributes to this card (the pill degrades gracefully) (CELE-t172).
            "k": (tokens // 1000) if tokens else None,
            # Live blocked-alert (CELE-t169): permission / idle / stopped — None when unblocked.
            "alert": alerts.get(t["id"]),
        }

    # CELE-t178: when a live session has NO doing card (it just shipped one, or hasn't claimed yet)
    # the in-flight block above won't render — so surface the session's live context band separately,
    # keyed on the session id. "session id active = tracked context": a completed card never clears
    # this; the band shows until the session claims another card (then the in-flight block takes the
    # slot) or the session goes idle / clears (agent_rows drops it). agent_rows is fullest-window
    # first, so the loudest session wins the card's row.
    active_session = None
    if agent_rows and not doing:
        top = agent_rows[0]
        toks = int(top.get("tokens") or 0)
        sid = (top.get("session") or "")[:6]
        # _active_agents sets `agent` to a real handle (@claim / CELEBORN_AGENT) or, absent one, the
        # session short id itself — so a real owner is one that isn't just that session-id fallback.
        agent_h = (top.get("agent") or "").strip()
        active_session = {
            "session": sid,
            "owner": agent_h if (agent_h and agent_h != sid) else "",
            "k": (toks // 1000) if toks else None,
        }

    todo_top = next((t for t in tasks if t["state"] == "todo"), None)
    suggested_todo = (
        {"id": todo_top["id"], "display_id": _display_tid(ctx, todo_top["id"], slug=slug),
         "title": todo_top["title"]}
        if todo_top else None
    )
    return {
        "path": str(project_dir),
        "slug": slug,
        "name": _project_name(ctx),
        "status": proj_status,
        "session": {
            "focus": (session.get("focus") or "")[:280],
            "next_action": (session.get("next_action") or "")[:280],
            "branch": session.get("branch") or "",
            "status": session.get("status") or "",
            "updated_at": session.get("updated_at") or "",
        },
        "counts": {s: sum(1 for t in tasks if t["state"] == s) for s in TASK_STATES},
        "doing": [_doing_row(t) for t in doing],
        # Live context of the owning session when there is no in-flight card to carry the band (t178).
        "active_session": active_session,
        "suggested_todo": suggested_todo,
        "agents": agents,
        "activity": {**activity, "minutes_since_capture": last_mins},
        "board": {"port": port, "url": board_url(ctx), "live": live},
    }


def _fleet_snapshot(ctx: Path | None) -> dict:
    projects = []
    for pdir in _fleet_project_paths(ctx):
        row = _fleet_project_snapshot(pdir)
        if row:
            projects.append(row)
    working = sum(1 for p in projects if p["status"] == "working")
    stuck = sum(1 for p in projects if p["status"] == "stuck")
    return {
        "generated_at": now_iso(),
        "registry": str(_fleet_registry_path()),
        "summary": {
            "projects": len(projects),
            "working": working,
            "stuck": stuck,
            "idle": len(projects) - working - stuck,
        },
        "projects": projects,
    }


def _render_fleet(snapshot: dict) -> str:
    lines = ["🏹 Celeborn fleet — live agent dashboard", ""]
    s = snapshot.get("summary") or {}
    lines.append(f"  {s.get('projects', 0)} project(s) · "
                 f"{s.get('working', 0)} working · {s.get('stuck', 0)} stuck · "
                 f"{s.get('idle', 0)} idle")
    lines.append(f"  registry: {snapshot.get('registry', '')}")
    lines.append("")
    for p in snapshot.get("projects") or []:
        icon = {"working": "🟢", "stuck": "🟡", "idle": "⚪"}.get(p["status"], "⚪")
        board = p.get("board") or {}
        bl = "live" if board.get("live") else "down"
        lines.append(f"{icon} {p['name']} ({p['slug']}) — {p['status']} · board {bl}")
        if p.get("doing"):
            for t in p["doing"]:
                owner = f" @{t['owner']}" if t.get("owner") else ""
                stale = " ⚠ stale" if t.get("stale") else ""
                # Fleet is inherently cross-project — always qualify with the project's own slug so the
                # overseer can reference a card unambiguously (t79 driver).
                # No per-project ctx in the cross-project fleet view; the explicit slug is authority.
                disp = _display_tid(None, t["id"], slug=p.get("slug") or "")
                lines.append(f"    doing → [{disp}] {t['title']}{owner}{stale}")
        for a in p.get("agents") or []:
            if a.get("status") == "idle" and not a.get("touches"):
                continue
            touch = ""
            if a.get("touches"):
                t0 = a["touches"][0]
                touch = f" · {t0['path']}"
                if t0.get("task"):
                    touch += f" [{t0['task']}]"
            lines.append(f"    @{a['id']} — {a['status']}{touch}")
        act = p.get("activity") or {}
        if act.get("last_prompt"):
            prompt = act["last_prompt"]
            if len(prompt) > 72:
                prompt = prompt[:69] + "…"
            lines.append(f"    last prompt: {prompt}")
        if board.get("url"):
            lines.append(f"    → {board['url']}")
        lines.append("")
    if not snapshot.get("projects"):
        lines.append("  (no projects — run `celeborn fleet register` from each repo)")
    return "\n".join(lines).rstrip() + "\n"


def cmd_fleet(args):
    """`celeborn fleet` — live multi-project dashboard: who's working, stuck, or idle across every
    registered Celeborn project on this machine. `register` / `unregister` manage the fleet registry
    at ~/.config/celeborn/fleet.json. The orienting project is always included when run from a repo.
    `--json` feeds the board viewer's Fleet tab; hosted sync (Pro) extends this across devices."""
    ctx = find_context_root(Path(getattr(args, "path", ".") or "."))
    action = (getattr(args, "fleet_action", None) or "").strip().lower()
    if action == "register":
        raw = getattr(args, "fleet_path", None) or getattr(args, "fleet_target", None)
        pdir = _resolve_project_dir(raw) if raw else (ctx.parent if ctx else None)
        if pdir is None:
            die("usage: celeborn fleet register [--path <project-dir>]  (or run inside a Celeborn repo)")
        row = _fleet_register_path(pdir)
        ok(f"registered {row['name']} → {row['path']}")
        return
    if action == "unregister":
        raw = getattr(args, "fleet_target", None) or getattr(args, "fleet_path", None)
        if not raw:
            die("usage: celeborn fleet unregister <project-dir>")
        if _fleet_unregister_path(raw):
            ok(f"unregistered {raw}")
        else:
            warn(f"not in fleet registry: {raw}")
        return
    if action == "repair":
        dry = getattr(args, "dry_run", False)
        changes = _fleet_repair(apply=not dry)
        if not changes:
            ok("Fleet registry already consistent — every project's qualifier is unique. Nothing to repair.")
            return
        head = "Would repair" if dry else "Repaired"
        ok(f"{head} {len(changes)} fleet slug(s):")
        for c in changes:
            if c.get("action") == "skip":
                warn(f"  skip {c['name'] or c['path']} — {c['reason']}")
            elif c.get("collision"):
                warn(f"  ⚠ {c['name']}: explicit project_slug {c['new']!r} clashes with another project — "
                     f"qualified ids ({c['new'].upper()}-tN) stay ambiguous; set a distinct project_slug in {RC_NAME}")
            else:
                rc = " (+.celebornrc)" if c.get("rc_written") else ""
                print(f"    {c['old'] or '∅'} → {c['new']}{rc}  [{c['name'] or c['path']}]")
        if dry:
            info("Dry run — re-run `celeborn fleet repair` (without --dry-run) to apply.")
        return
    snap = _fleet_snapshot(ctx)
    if getattr(args, "json", False):
        print(json.dumps(snap, indent=2))
        return
    print(_render_fleet(snap))


def cmd_metrics(args):
    ctx = require_context(args)
    m = _load_metrics(ctx)
    if args.json:
        print(json.dumps(m, indent=2))
        return
    print("Celeborn memory economy (estimated)")
    for line in metrics_summary(ctx):
        print(f"  {line}")
    print(f"  handoffs written: {m['handoffs_written']} · orient events: {m['orient_events']}"
          f" · panic-saves: {m.get('panic_saves', 0)}")
    cpt = load_config(ctx)["chars_per_token"]
    print(f"\n  Estimate basis: ~{cpt} chars/token; 'saved' = tokens(all .context/) − tokens(Hot tier) per load event")


def _iter_transcript(path: Path, start_offset: int = 0):
    """Yield (byte_offset_after_line, parsed_obj) for each JSONL line from `start_offset`.

    Reads in binary and tracks `tell()` so the offset is an exact cursor for the next run.
    Skips blank lines and JSON-decode failures — the latter covers a truncated trailing line a
    Stop hook may catch mid-flush; the offset is only advanced past lines that fully parsed."""
    try:
        f = path.open("rb")
    except OSError:
        return
    with f:
        if start_offset:
            try:
                f.seek(start_offset)
            except OSError:
                f.seek(0)
        for raw in f:
            off = f.tell()
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8", "ignore"))
            except json.JSONDecodeError:
                continue
            yield off, obj


def _estimate_transcript_tokens(path: Path, cpt: int) -> int:
    """Best-effort *current context size* from a Claude Code transcript (JSONL).

    Claude Code records each assistant turn's `usage`; the most recent one's
    input + cache + output ≈ the live window the model just saw — the real number, not a proxy.
    Falls back to a char/token estimate over message text if no usage is present."""
    latest_usage = 0
    char_total = 0
    for _off, obj in _iter_transcript(path):
        msg = obj.get("message") or {}
        usage = msg.get("usage") or obj.get("usage") or {}
        if usage:
            total = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0)
                + (usage.get("output_tokens") or 0)
            )
            if total:
                latest_usage = total  # last wins ≈ most recent turn's window
        content = msg.get("content")
        if isinstance(content, str):
            char_total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    char_total += len(str(part.get("text", "")))
    return latest_usage or (char_total // max(1, cpt))


# --- active agents (live per-session context windows) --------------------------------------------
# `celeborn agents` answers "who is working right now, and how full is each one's context window?"
# The board renders it as the per-session /clear-nudge chips. It JOINS two real signals:
#   1. live transcripts  — every Claude session writes a JSONL transcript under ~/.claude/projects/<enc>.
#      Its mtime is the truth of "active recently"; `_estimate_transcript_tokens` reads the latest
#      `usage` for the live window — the real number, not the cumulative `tokens_session` proxy.
#   2. agent_sessions     — the session→{owner,task} link `cmd_claim` records (the claim hook passes the
#      session id). Lets us attribute each live window to a handle + the DOING card it owns.
# An active session with no link is shown by its short id (never a raw uuid) with no card.

AGENT_ACTIVE_WINDOW_MIN = 30   # a transcript touched within this many minutes = a live agent
ENDED_SESSIONS_KEEP = 50       # how many ended-session tombstones to retain (bounded FIFO)
_SESSION_ID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def _looks_like_session_id(s: str) -> bool:
    return bool(_SESSION_ID_RE.fullmatch((s or "").strip()))


# Tokens that occur only in model names — never in a hex session short-id (hex letters are a–f, so
# 'opus'/'sonnet'/'gemini'/… can't appear) nor in a bare family/human handle ('grok', 'scotch').
# Used to keep model strings out of the owner chip: a card is owned by its session, not its model
# (CELE-t131/t172).
_MODEL_TOKEN_RE = re.compile(r"opus|sonnet|haiku|fable|gpt[0-9]|gemini[0-9]|claude[0-9]")

# Bare model-family words that _MODEL_TOKEN_RE (which needs a digit for gpt/gemini/claude) misses —
# used only to sharpen the "record your model with `identify`" nudge when a superseded --by was a
# generic family name (e.g. `--by claude`). NOT used to change ownership: outside a Claude window a
# human may legitimately attribute `--by claude`, so this never rejects, it only advises.
_GENERIC_MODEL_FAMILIES = {"claude", "gpt", "chatgpt", "gemini", "opus", "sonnet", "haiku", "fable",
                           "llama", "mistral", "anthropic", "openai"}


def _looks_like_model_handle(owner: str, model: str = "") -> bool:
    """True when a claim handle embeds a model name rather than an identity — e.g. 'claude-opus48',
    'Claude/Opus 4.8', 'Opus 4.8'. A session short-id, a bare family, or a human handle is not.
    `model` (the handle's registered model, when known) catches custom names the token list misses."""
    norm = re.sub(r"[^a-z0-9]", "", (owner or "").lower())
    if not norm:
        return False
    m = re.sub(r"[^a-z0-9]", "", (model or "").lower())
    if len(m) >= 4 and m in norm:
        return True
    return bool(_MODEL_TOKEN_RE.search(norm))


def _display_owner(owner: str, model: str = "", session_id: str = "") -> str:
    """The owner chip shows a session / human handle, never a model (CELE-t172). When a card's
    recorded owner is model-derived, prefer the live session short-id if known, else show nothing
    rather than leak model text onto the board."""
    owner = (owner or "").strip()
    if owner and _looks_like_model_handle(owner, model):
        return (session_id or "").strip()
    return owner


# The /clear-nudge band for a live context window, keyed by size in k tokens. Python mirror of
# board/lib/band.ts — the single source both the hosted band pill and the active-agents chips use —
# so the terminal board's per-card band matches the viewer's exactly (CELE-t206). Returns a colored
# dot (the palette rendered as the nearest terminal emoji) plus the same one/two-word label.
_PRESSURE_RANK = {"none": 0, "soft": 1, "hard": 2}


def _pressure_level(tokens, soft: int, hard: int) -> str:
    """Classify a live context size against the configurable soft/hard warning thresholds
    (CELE-t207): "none" | "soft" | "hard". A threshold ≤ 0 is disabled. Pure — the single
    decision point the remind warnings, the capture cursor flag, and the board chips all share."""
    t = max(0, int(tokens or 0))
    if hard and hard > 0 and t >= hard:
        return "hard"
    if soft and soft > 0 and t >= soft:
        return "soft"
    return "none"


def _context_thresholds(cfg: dict, soft_override=None, hard_override=None) -> tuple[int, int]:
    """Resolve the (soft, hard) context-pressure thresholds: an explicit CLI/hook override wins,
    else the project's .celebornrc, else the built-in defaults (the band.ts clear bands)."""
    try:
        soft = int(soft_override if soft_override is not None else cfg.get("context_soft_tokens", 100_000) or 0)
    except (TypeError, ValueError):
        soft = 100_000
    try:
        hard = int(hard_override if hard_override is not None else cfg.get("context_hard_tokens", 125_000) or 0)
    except (TypeError, ValueError):
        hard = 125_000
    return soft, hard


def _pressure_line(tokens, level: str, soft: int, hard: int, clear_cmd: str) -> str:
    """The context-pressure warning (CELE-t207) — the urgent sibling of `_remind_line`, spoken only
    when a live window newly crosses a configured threshold. Hard names the stop-now stakes (it is
    the future auto-clear trigger); soft asks for an orderly wrap-up. Same 🏹 channel, every surface."""
    if level == "hard":
        return (f"🏹 Celeborn —> ⛔ HARD context limit crossed: ~{tokens:,} tokens ≥ {hard:,}. "
                f"Stop new work — checkpoint the Hot tier and {clear_cmd} NOW. "
                f"State is saved; nothing will be lost.")
    return (f"🏹 Celeborn —> ⚠ Context pressure: ~{tokens:,} tokens ≥ the soft limit ({soft:,}). "
            f"Wrap the current step, checkpoint, then {clear_cmd} — sooner is cheaper.")


def _context_band(k: int) -> tuple[str, str]:
    if k < 50:
        return ("🟢", "fresh")
    if k < 75:
        return ("🔵", "mid")
    if k < 100:
        return ("🟠", "clear soon")
    if k < 125:
        return ("🟡", "clear now")
    return ("🔴", "clear urgent")


def _doing_card_annotation(tokens: int | None, session: str, model: str, owner: str,
                           pressure: str = "none") -> str:
    """The live triple the text board writes onto a DOING card (CELE-t206, closes CELE-t163): the
    context-window size + /clear-nudge band, the working session's short id, and the coder model.
    Plus, when the window has crossed a configured context-pressure threshold (CELE-t207), an
    explicit ⚠/⛔ limit chip — the band words track fixed bands, the chip tracks the project's
    configurable soft/hard limits, so they stay honest independently.

    Automatic for EVERY doing card — the data rides the live agent join (`_active_agents`), never the
    explicit `celeborn claim` alone, so a prompt-autogenerated card shows it too (the t163 bug). Each
    part renders only when known, so a card with no live window degrades to '' — no phantom band, and
    the session id never shoves out to the right where tokens used to sit (the other half of t163).
    The session chip is suppressed when the owner handle already IS that short id (no `@d4ea23 · d4ea23`)."""
    parts: list[str] = []
    if tokens:
        k = tokens // 1000
        emoji, word = _context_band(k)
        parts.append(f"~{k}k ctx {emoji} {word}")
        if pressure == "hard":
            parts.append("⛔ hard limit")
        elif pressure == "soft":
            parts.append("⚠ soft limit")
    sid = (session or "").strip()
    if sid and sid != (owner or "").strip():
        parts.append(sid)
    model = (model or "").strip()
    if model:
        parts.append(model)
    return ("  · " + " · ".join(parts)) if parts else ""


def _doing_context_join(ctx: Path) -> tuple[dict[str, int], dict[str, str], dict[str, str]]:
    """Per-DOING-card live context, joined off `_active_agents` exactly like the fleet snapshot and
    the hosted band pill (CELE-t206): {task_id: tokens} (fullest window wins), {task_id: session
    short-id}, and {task_id: pressure level} graded against the configured soft/hard thresholds
    (CELE-t207). Shared so the terminal board and the JSON projection can't drift apart."""
    tokens_by_task: dict[str, int] = {}
    session_by_task: dict[str, str] = {}
    for r in _active_agents(ctx, AGENT_ACTIVE_WINDOW_MIN, False):
        tid = r.get("task_id")
        if not tid:
            continue
        tokens_by_task[tid] = max(tokens_by_task.get(tid, 0), int(r.get("tokens") or 0))
        sid = (r.get("session") or "")[:6]
        if sid and tid not in session_by_task:
            session_by_task[tid] = sid
    soft, hard = _context_thresholds(load_config(ctx))
    pressure_by_task = {tid: _pressure_level(tok, soft, hard) for tid, tok in tokens_by_task.items()}
    return tokens_by_task, session_by_task, pressure_by_task


def _cc_project_dir(repo: Path) -> Path:
    """Where Claude Code stores `<session>.jsonl` transcripts for `repo`: ~/.claude/projects/<enc>,
    with <enc> the repo's absolute path and every non-alphanumeric char replaced by '-' (CC's rule)."""
    enc = re.sub(r"[^A-Za-z0-9]", "-", str(repo))
    return Path.home() / ".claude" / "projects" / enc


def _record_agent_session(ctx: Path, session: str | None, owner: str, task_ids: list[str]) -> None:
    """Remember that `session` (a Claude session id) is owned by `owner` and now holds `task_ids` — the
    bridge `celeborn agents` joins against the live transcripts. No-op without a real session id or a
    handle that's just the session-id fallback. Pruned like `captures` so the map stays bounded."""
    sid = (session or "").strip()
    owner = (owner or "").strip()
    if not sid or not task_ids or not owner or _looks_like_session_id(owner):
        return
    m = _load_metrics(ctx)
    sess = m.get("agent_sessions")
    if not isinstance(sess, dict):
        sess = {}
    sess.pop(sid, None)                       # reinsert at the end so order tracks recency
    sess[sid] = {"owner": owner, "task": task_ids[-1], "at": now_iso()}
    while len(sess) > CAPTURE_KEEP_SESSIONS:
        sess.pop(next(iter(sess)))
    m["agent_sessions"] = sess
    _save_metrics(ctx, m)


def _mark_session_ended(ctx: Path, session: str | None) -> bool:
    """Tombstone a Claude session as ENDED (`/clear`, logout, exit) so it drops off the active-agents
    board immediately instead of lingering for the 30-min mtime window (CELE-t131). A `/clear` starts a
    fresh session id, so the old window's transcript keeps a recent mtime and would otherwise show as a
    ghost chip. Keyed by full session id; bounded FIFO. Returns True if a new tombstone was recorded."""
    sid = (session or "").strip()
    if not sid:
        return False
    m = _load_metrics(ctx)
    ended = m.get("ended_sessions")
    if not isinstance(ended, dict):
        ended = {}
    ended.pop(sid, None)                      # reinsert at the end so FIFO eviction tracks recency
    ended[sid] = now_iso()
    while len(ended) > ENDED_SESSIONS_KEEP:
        ended.pop(next(iter(ended)))
    m["ended_sessions"] = ended
    # A cleared session no longer owns its card on the board — drop its session→card link too.
    sess = m.get("agent_sessions")
    if isinstance(sess, dict):
        sess.pop(sid, None)
    _save_metrics(ctx, m)
    return True


def _stash_clear_carryover(ctx: Path, session: str | None) -> None:
    """A `/clear` mints a NEW session id, orphaning the `agent_sessions` link (owner+card) the cleared
    session held — so the continuation shows as an unowned chip even though it's the same agent on the
    same DOING card (CELE-t131). Called from SessionEnd(reason="clear"): stash the ending session's
    attribution so SessionStart(source="clear") can hand it to the new session. Precise to THIS cleared
    session (multi-agent-safe), one-shot, and time-boxed on consume. No-op for an unattributed session."""
    sid = (session or "").strip()
    if not sid:
        return
    m = _load_metrics(ctx)
    link = (m.get("agent_sessions") or {}).get(sid) or {}
    owner = (link.get("owner") or "").strip()
    task = (link.get("task") or "").strip()
    if not owner or not task or _looks_like_session_id(owner):
        return
    m["clear_carryover"] = {"owner": owner, "task": task, "from": sid, "at": now_iso()}
    _save_metrics(ctx, m)


def _consume_clear_carryover(ctx: Path, new_session: str | None) -> bool:
    """SessionStart(source="clear"): inherit the cleared session's owner+card (stashed by
    `_stash_clear_carryover`) so the continuation keeps showing the same agent on the same card instead
    of a fresh unowned chip (CELE-t131). One-shot (always cleared), fail-safe: ignored if stale (>15m,
    e.g. hook ordering left it unconsumed) or if the carried card is no longer in flight. Returns True
    if an attribution was inherited."""
    sid = (new_session or "").strip()
    m = _load_metrics(ctx)
    co = m.get("clear_carryover") or {}
    if "clear_carryover" in m:
        m.pop("clear_carryover", None)            # one-shot regardless of outcome
        _save_metrics(ctx, m)
    owner = (co.get("owner") or "").strip()
    task = (co.get("task") or "").strip()
    if not sid or not owner or not task or sid == co.get("from"):
        return False
    try:                                          # only inherit a RECENT clear — the continuation is seconds later
        if (_dt.datetime.now() - _dt.datetime.fromisoformat(co["at"])).total_seconds() > 900:
            return False
    except Exception:
        pass
    card = next((t for t in _load_tasks(ctx) if t["id"] == task), None)
    if card is None or card.get("state") not in ("doing",):
        return False                              # card shipped/abandoned since the clear — don't inherit
    _record_agent_session(ctx, sid, owner, [task])
    return True


def _active_agents(ctx: Path, window_min: float, show_all: bool) -> list[dict]:
    """One row per live context window for this repo (see the section header). Sorted fullest-first."""
    cfg = load_config(ctx)
    cpt = int(cfg.get("chars_per_token", 4)) or 4
    soft, hard = _context_thresholds(cfg)   # context-pressure grading for every row (CELE-t207)
    repo = ctx.parent
    slug = project_slug(ctx)
    _m = _load_metrics(ctx)
    sessions_map = _m.get("agent_sessions") or {}
    ended = set(_m.get("ended_sessions") or {})   # sessions /cleared or ended — never show as live (t131)
    by_id = {t["id"]: t for t in _load_tasks(ctx)}
    now = _dt.datetime.now().timestamp()

    rows: list[dict] = []
    proj_dir = _cc_project_dir(repo)
    if proj_dir.is_dir():
        for tp in proj_dir.glob("*.jsonl"):
            try:
                mtime = tp.stat().st_mtime
            except OSError:
                continue
            age_min = (now - mtime) / 60.0
            if not show_all and age_min > window_min:
                continue
            sid = tp.stem
            if sid in ended:
                continue                       # session was /cleared or ended — not a live window (t131)
            link = sessions_map.get(sid) or {}
            owner = (link.get("owner") or "").strip()
            if _looks_like_session_id(owner):
                owner = ""
            card = by_id.get((link.get("task") or "").strip())
            if card is not None and card.get("state") not in ("doing",):
                card = None                   # the card was shipped/moved — don't keep claiming it
            est = _estimate_transcript_tokens(tp, cpt)
            rows.append({
                # The session id IS the agent's name (CELE-t131): show its short head ("d0c13a"), not
                # "session d0c13a". A real handle (CELEBORN_AGENT / claim) still wins and renders "@handle".
                "agent": owner or sid[:6],
                "task": _display_tid(ctx, card["id"], cfg=cfg, slug=slug) if card else None,
                "task_id": card["id"] if card else None,
                "tokens": est,
                "session": sid[:8],
                "last_active": _dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S"),
                "age_min": round(age_min, 1),
                "owned": bool(owner),
                "project": slug,
                "pressure": _pressure_level(est, soft, hard),
            })
    # Transcript-less live windows (P4, CELE-t141): a harness that reports REAL token usage
    # (`celeborn record tokens` — the OpenCode plugin, per assistant message) stamps captures[sid]
    # with live=True + updated_at. Those sessions are context windows every bit as real as a Claude
    # transcript, so emit them as rows too — same shape, same bands. A Claude session's cursor never
    # carries `live`, so no window is ever double-counted.
    caps = _m.get("captures") if isinstance(_m.get("captures"), dict) else {}
    for sid, cur in caps.items():
        if not isinstance(cur, dict) or not cur.get("live") or sid in ended:
            continue
        ts = _parse_dt(str(cur.get("updated_at") or ""))
        if ts is None:
            continue
        age_min = (now - ts.timestamp()) / 60.0
        if not show_all and age_min > window_min:
            continue
        link = sessions_map.get(sid) or {}
        owner = (link.get("owner") or "").strip()
        if _looks_like_session_id(owner):
            owner = ""
        card = by_id.get((link.get("task") or "").strip())
        if card is not None and card.get("state") not in ("doing",):
            card = None
        rows.append({
            "agent": owner or sid[:6],
            "task": _display_tid(ctx, card["id"], cfg=cfg, slug=slug) if card else None,
            "task_id": card["id"] if card else None,
            "tokens": int(cur.get("tokens_session") or 0),
            "session": sid[:8],
            "last_active": str(cur.get("updated_at") or ""),
            "age_min": round(age_min, 1),
            "owned": bool(owner),
            "project": slug,
            "pressure": _pressure_level(int(cur.get("tokens_session") or 0), soft, hard),
        })
    rows.sort(key=lambda r: r["tokens"], reverse=True)
    return rows


def cmd_agents(args):
    """`celeborn agents [--json] [--window-min N] [--all]` — the live per-session context windows the
    board renders as /clear-nudge chips. Active = a Claude transcript touched within the window."""
    ctx = require_context(args)
    # `celeborn agents forget <session>` — manually wipe a ghost chip (a session that ended without a
    # clean SessionEnd hook). Accepts a full session id or the 8-char short id the board shows. Matches
    # against live transcripts so the short id resolves to the real session id (CELE-t131).
    forget = (getattr(args, "session", None) or "").strip() if getattr(args, "action", None) == "forget" else ""
    if getattr(args, "action", None) == "forget" and not forget:
        die("usage: celeborn agents forget <session-id>")
    if forget:
        proj_dir = _cc_project_dir(ctx.parent)
        full = forget
        if proj_dir.is_dir():
            hit = next((tp.stem for tp in proj_dir.glob("*.jsonl") if tp.stem == forget or tp.stem.startswith(forget)), None)
            full = hit or forget
        _mark_session_ended(ctx, full)
        try:
            __import__("celeborn_sync").schedule_agents_push(ctx, min_interval_s=0)
        except Exception:
            pass
        ok(f"Forgot session {full[:8]} — wiped from the active-agents board (local + hosted).")
        return
    window_min = float(getattr(args, "window_min", None) or AGENT_ACTIVE_WINDOW_MIN)
    show_all = bool(getattr(args, "all", False))
    rows = _active_agents(ctx, window_min, show_all)
    out = {
        "generated_at": now_iso(),
        "project": project_slug(ctx),
        "window_min": window_min,
        "count": len(rows),
        "agents": rows,
    }
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
        return
    if not rows:
        print(f"No active agents — no transcript touched in the last {int(window_min)}m "
              f"(`celeborn agents --all` to include idle sessions).")
        return
    print(f"🏹 Active agents — {out['project']} ({len(rows)} live in the last {int(window_min)}m)")
    for r in rows:
        k = r["tokens"] // 1000
        task = f" · {r['task']}" if r["task"] else ""
        print(f"  @{r['agent']}{task} · ~{k}k ctx · {r['age_min']}m ago · {r['session']}")


# --- automatic capture (deterministic; no model) -------------------------------------------------
# Mechanically ingests the Claude Code transcript into a local-only, searchable auto tier + an
# always-fresh activity digest. Writes ONLY .context/auto/* and .context/activity.md — never the
# judgment-authored tiers (state.md/journal.md/session.json).

_FILE_TOOLS = {"Edit", "Write", "NotebookEdit"}
_SKIP_TYPES = {"file-history-snapshot", "last-prompt", "system", "ai-title", "queue-operation", "attachment"}
_GIT_COMMIT_CMD_RE = re.compile(r"\bgit\s+commit\b")
_COMMIT_OUT_RE = re.compile(r"\[[\w./-]+\s+([0-9a-f]{7,40})\]\s*(.*)")
_TEST_RE = re.compile(r"(\d+ passed|\d+ failed|\bFAILED\b|Ran \d+ tests?|failures=\d+|\d+ error)", re.I)


def _user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
    return ""


def _is_tool_result_carrier(entry: dict, msg: dict) -> bool:
    if entry.get("toolUseResult") is not None:
        return True
    c = msg.get("content")
    return isinstance(c, list) and bool(c) and isinstance(c[0], dict) and c[0].get("type") == "tool_result"


def _result_text(entry: dict, msg: dict) -> str:
    tur = entry.get("toolUseResult")
    if isinstance(tur, dict):
        return (str(tur.get("stdout") or "") + "\n" + str(tur.get("stderr") or "")).strip()
    c = msg.get("content")
    if isinstance(c, list) and c and isinstance(c[0], dict):
        rc = c[0].get("content")
        if isinstance(rc, str):
            return rc
        if isinstance(rc, list):
            return "\n".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in rc)
    return ""


def _tool_summary(name: str, inp: dict) -> str:
    """A faithful summary of a tool call's meaningful input — best-effort per known tool, compact
    JSON fallback for the rest (incl. mcp__*). Bash keeps its FULL command body (the cold record is
    faithful); the bounded digest separately keeps just the first line."""
    if not isinstance(inp, dict):
        return ""
    if name in _FILE_TOOLS:
        return str(inp.get("file_path") or inp.get("notebook_path") or "")
    if name == "Bash":
        return str(inp.get("command") or "").strip()
    if name in ("Read", "Glob"):
        return str(inp.get("file_path") or inp.get("path") or inp.get("pattern") or "")
    if name == "Grep":
        pat, path = str(inp.get("pattern") or ""), inp.get("path")
        return f"{pat} in {path}" if path else pat
    if name in ("Task", "Agent"):
        return str(inp.get("description") or inp.get("subagent_type") or "")
    if name == "WebFetch":
        return str(inp.get("url") or "")
    if name == "WebSearch":
        return str(inp.get("query") or "")
    try:
        return json.dumps(inp, sort_keys=True)[:300]
    except (TypeError, ValueError):
        return str(inp)[:300]


def _extract_turns(path: Path, start_offset: int):
    """Walk new transcript entries; return (turns, last_offset, last_uuid, first_sid).
    A turn = {ts, prompt, events[], files[], commands[], commits[], tests[]}. `events` is the
    FAITHFUL, ordered render stream (assistant text, every tool call, every tool result) that goes to
    the cold auto file; files/commands/commits/tests are the derived, bounded digest facts that feed
    activity.md. One pass yields both; pure structural extraction, no model."""
    turns, cur = [], None
    last_offset, last_uuid, first_sid = start_offset, None, None
    for off, obj in _iter_transcript(path, start_offset):
        last_offset = off
        if obj.get("uuid"):
            last_uuid = obj["uuid"]
        if first_sid is None:
            first_sid = obj.get("sessionId")
        t = obj.get("type")
        if t in _SKIP_TYPES or obj.get("isMeta") or obj.get("isSidechain"):
            continue
        msg = obj.get("message") or {}
        if t == "user":
            if _is_tool_result_carrier(obj, msg):
                if cur is None:
                    continue
                c = msg.get("content")
                head = c[0] if isinstance(c, list) and c and isinstance(c[0], dict) else {}
                out = _result_text(obj, msg)
                cur["events"].append({"kind": "tool_result", "tool_use_id": head.get("tool_use_id"),
                                      "text": out, "is_error": bool(head.get("is_error"))})
                cm = _COMMIT_OUT_RE.search(out)
                if cm:
                    cur["commits"].append(f"{cm.group(1)[:7]} {cm.group(2).strip()}".strip())
                tm = _TEST_RE.search(out)
                if tm:
                    verdict = "fail" if re.search(r"fail|error", out, re.I) else "pass"
                    cur["tests"].append(f"{tm.group(0)} ({verdict})")
                continue
            text = _user_text(msg.get("content")).strip()
            if not text:
                continue
            cur = {"ts": obj.get("timestamp", ""), "prompt": text, "events": [],
                   "files": [], "commands": [], "commits": [], "tests": []}
            turns.append(cur)
        elif t == "assistant" and cur is not None:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    txt = (b.get("text") or "").strip()
                    if txt:
                        cur["events"].append({"kind": "assistant_text", "text": txt})
                elif btype == "tool_use":
                    name, inp = b.get("name"), (b.get("input") or {})
                    cur["events"].append({"kind": "tool_use", "name": name or "",
                                          "summary": _tool_summary(name, inp),
                                          "tool_use_id": b.get("id")})
                    if name in _FILE_TOOLS:
                        fp = inp.get("file_path")
                        if fp and fp not in cur["files"] and len(cur["files"]) < 50:
                            cur["files"].append(fp)
                    elif name == "Bash":
                        cmd = (inp.get("command") or "").strip()
                        if cmd and len(cur["commands"]) < 50:
                            cur["commands"].append(cmd.splitlines()[0][:200])
    return turns, last_offset, last_uuid, first_sid


def _redact_turn(turn: dict, patterns: list, output_max: int = 8000) -> dict:
    """Return a redacted copy of a turn — EVERY persisted field scrubbed of secrets (defense in
    depth; the auto tier is local-only but rides `celeborn sync`). Tool-result bodies are redacted
    FIRST, then size-capped, so a secret straddling the cap boundary can never survive."""
    from celeborn_sync import redact

    def red(s):
        return redact(s or "", patterns)[0]

    events = []
    for e in turn.get("events", []):
        k = e.get("kind")
        if k == "assistant_text":
            events.append({"kind": k, "text": red(e.get("text", ""))})
        elif k == "tool_use":
            events.append({"kind": k, "name": e.get("name", ""), "summary": red(e.get("summary", ""))})
        elif k == "tool_result":
            body = red(e.get("text", ""))
            if len(body) > output_max:
                body = body[:output_max] + f"\n…[truncated {len(body) - output_max} chars]"
            events.append({"kind": k, "text": body, "is_error": bool(e.get("is_error"))})
    return {
        "ts": turn["ts"],
        "prompt": red(turn["prompt"]),
        "events": events,
        "files": list(turn["files"]),
        "commands": [red(c) for c in turn["commands"]],
        "commits": list(turn["commits"]),
        "tests": list(turn["tests"]),
    }


def _digest_facts(rt: dict) -> dict:
    """The bounded, facts-only projection of a (redacted) turn for window.json + activity.md.
    Excludes the faithful `events` stream, which is large and lives ONLY in the cold auto file —
    this is what keeps the Hot tier (activity.md, loaded on Orient) small."""
    return {
        "ts": rt.get("ts", ""),
        "prompt": (rt.get("prompt") or "").replace("\n", " ").strip()[:200],
        "files": list(rt.get("files", [])),
        "commands": list(rt.get("commands", [])),
        "commits": list(rt.get("commits", [])),
        "tests": list(rt.get("tests", [])),
    }


def _format_turn_block(rt: dict) -> str:
    """Render one redacted turn as the faithful cold-tier block: the prompt, then assistant text and
    tool calls/results interleaved in transcript order. Keeps the `## turn <ts>` heading so each turn
    stays one indexed section."""
    lines = [f"## turn {rt['ts']}", "", f"**prompt:** {rt['prompt']}"]
    for e in rt.get("events", []):
        k = e.get("kind")
        if k == "assistant_text":
            lines += ["", f"**assistant:** {e['text']}"]
        elif k == "tool_use":
            lines += ["", f"- tool `{e['name']}`: {e['summary']}"]
        elif k == "tool_result" and e.get("text"):
            lines += [f"  result{' (error)' if e.get('is_error') else ''}:", "~~~", e["text"], "~~~"]
    sig = []
    if rt["commits"]:
        sig.append("commits: " + "; ".join(rt["commits"]))
    if rt["tests"]:
        sig.append("tests: " + ", ".join(rt["tests"]))
    if sig:
        lines += ["", "**signals:** " + " · ".join(sig)]
    return "\n".join(lines) + "\n"


def _write_activity_digest(ctx: Path, window: list, sid8: str, max_lines: int = 40) -> None:
    """Overwrite .context/activity.md from the rolling window of (already-redacted) turn facts.
    Bounded by construction so it can load on Orient without bloating it."""
    files: dict = {}
    commands, commits, last_prompt = [], [], ""
    for rt in window:
        last_prompt = rt.get("prompt") or last_prompt
        for f in rt.get("files", []):
            files[f] = files.get(f, 0) + 1
        commands += rt.get("commands", [])
        commits += rt.get("commits", [])
    out = ["# Automatic Context Record — current activity (mechanical)", "",
           "<!-- Regenerated by `celeborn capture` every turn. Local-only, gitignored.",
           "     Always-current 'what actually happened' — backstops a stale state.md. -->", "",
           f"Last capture: {now_iso()}  ·  session {sid8}"]
    if last_prompt:
        out.append(f"Last prompt: {last_prompt[:200]}")
    if files:
        out += ["", "## Recently touched files"]
        out += [f"- {f}" + (f" (×{n})" if n > 1 else "")
                for f, n in sorted(files.items(), key=lambda kv: -kv[1])[:12]]
    if commands:
        out += ["", "## Recent commands"] + [f"- `{c}`" for c in commands[-10:]]
    if commits:
        out += ["", "## Recent commits"] + [f"- {c}" for c in commits[-8:]]
    (ctx / "activity.md").write_text("\n".join(out[:max_lines]) + "\n")


def _prune_auto(ctx: Path, keep: int) -> None:
    autod = ctx / "auto"
    files = sorted(autod.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


CAPTURE_KEEP_SESSIONS = 200   # bound the per-session cursor map; the oldest sessions age out


def _write_capture(m: dict, caps: dict, sid: str, entry: dict,
                   keep: int = CAPTURE_KEEP_SESSIONS) -> None:
    """Persist `entry` as session `sid`'s cursor: store it under `captures[sid]` (reinserted at the
    end so insertion order tracks recency), prune the oldest sessions past `keep`, and mirror it to
    the flat `capture` slot (back-compat + the no-session-id heartbeat/statusline fallback)."""
    caps.pop(sid, None)
    caps[sid] = entry
    while len(caps) > keep:
        caps.pop(next(iter(caps)))
    m["captures"] = caps
    m["capture"] = entry


def _capture_cursor(m: dict, sid: str | None) -> dict:
    """Read a capture cursor for display (heartbeat/statusline): the entry for `sid` from the
    per-session map, else the most-recently-active session (the flat `capture` mirror)."""
    caps = m.get("captures")
    if isinstance(caps, dict) and sid and sid in caps:
        return caps[sid]
    return m.get("capture") or {}


def _capture_note(delta: int, session_total: int, idle_streak: int) -> str:
    """The Stop hook's per-turn line, as a Claude Code `systemMessage` JSON object.

    Every note is deliberately UNIQUE turn-to-turn. Claude Code suppresses a Stop-hook
    `systemMessage` that is identical to the one it just showed, so a constant string (the old
    "Nothing material happened this turn.") rendered once and then silently vanished — which is
    exactly why the per-turn note seemed never to fire. An active turn varies by the running
    session total (which only grows); an idle turn varies by a consecutive-idle counter. So the
    heartbeat stays visible on every single turn."""
    if delta > 0:
        msg = f"🏹 Celeborn —> +{delta:,} tokens this turn · {session_total:,} this session"
    else:
        msg = f"🏹 Celeborn —> idle ×{idle_streak} · {session_total:,} this session"
    return json.dumps({"systemMessage": msg})


def _count_auto_allowed(turns: list, allow_names: set) -> int:
    """Estimate the permission prompts a CMM pre-clear avoided in these turns: one per agent call to
    a pre-cleared tool (`allow_names`). Each such `tool_use` is a structural query that ran without an
    Allow/Always-allow click — what it replaced (a Bash `grep`/`rg`/`find` shell-out) would have
    prompted. Zero when nothing's pre-cleared, so non-engaged projects never accrue."""
    if not allow_names:
        return 0
    n = 0
    for t in turns:
        for e in t.get("events", []):
            if e.get("kind") == "tool_use" and e.get("name") in allow_names:
                n += 1
    return n


def _bash_allow_matches(command: str, inner: str) -> bool:
    """Does Bash `command` fall under the allow-rule body `inner` (the text inside `Bash(...)`)?
    Mirrors Claude Code's prefix semantics: `grep:*` (or the advisor's `grep *`) auto-allows any
    command starting with `grep`; a bare `ls` auto-allows only the exact command `ls`."""
    inner = inner.strip()
    if inner.endswith(":*"):
        prefix = inner[:-2]
    elif inner.endswith("*"):
        prefix = inner[:-1].rstrip()
    else:
        return command == inner
    return bool(prefix) and (command == prefix or command.startswith(prefix))


def _effective_allow_rules(ctx: Path) -> list:
    """The permission allow-rules in force for this project: the global baseline
    (`~/.claude/settings.json`, where `wire --global` writes the t100 baseline) plus the project's own
    shared + local settings. Deduped, order-stable; best-effort — a missing or malformed file just
    contributes nothing."""
    out: list = []
    seen: set = set()
    for p in (Path.home() / ".claude" / "settings.json",
              ctx.parent / ".claude" / "settings.json",
              ctx.parent / ".claude" / "settings.local.json"):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for r in (data.get("permissions") or {}).get("allow") or []:
            if isinstance(r, str) and r not in seen:
                seen.add(r)
                out.append(r)
    return out


def _count_allowlist_auto_allowed(turns: list, allow: list, exclude_names: set) -> int:
    """Estimate the permission prompts the settings.json allow-LIST avoided in these turns — the t100
    safe baseline plus any rule the user added. Each matching `tool_use` ran without an Allow click: a
    built-in present verbatim in `allow` (Read/Glob/Grep/…), or a Bash call whose command matches a
    `Bash(<prefix>:*)` rule. `exclude_names` (CMM's provenance-credited tools) are skipped so the
    figure never double-counts what the CMM bucket already claims."""
    named = {r for r in allow if isinstance(r, str) and "(" not in r}
    bash_rules = [inner for r in allow if isinstance(r, str)
                  for inner in (_parse_bash_rule(r),) if inner is not None]
    if not named and not bash_rules:
        return 0
    n = 0
    for t in turns:
        for e in t.get("events", []):
            if e.get("kind") != "tool_use":
                continue
            name = e.get("name") or ""
            if name in exclude_names:
                continue                                 # CMM already credited this call
            if name == "Bash":
                cmd = (e.get("summary") or "").strip()   # _tool_summary keeps Bash's full command
                if cmd and any(_bash_allow_matches(cmd, inner) for inner in bash_rules):
                    n += 1
            elif name in named:
                n += 1
    return n


# --- transcript-less activity (P4, CELE-t141) ---------------------------------------------------
# OpenCode has no Claude-style transcript for `celeborn capture` to ingest, but its plugin DOES
# report every completed tool call (tool.execute.after / file.edited → `hook post-tool-use`). These
# helpers keep the transcript capture's two Hot surfaces — the rolling window (auto/window.json) and
# activity.md — current for transcript-less sessions too, so orient's "what actually happened"
# backstop works there. Only the digest FACTS are recorded (prompt heads, files, command heads);
# there is no faithful event stream to archive without a transcript.

def _oc_load_window(ctx: Path) -> list:
    win_path = ctx / "auto" / "window.json"
    if win_path.is_file():
        try:
            return json.loads(win_path.read_text())
        except (OSError, json.JSONDecodeError):
            return []
    return []


def _oc_save_window(ctx: Path, window: list, sid8: str) -> None:
    cfg = load_config(ctx)
    window = window[-int(cfg.get("activity_window_turns", 15)):]
    autod = ctx / "auto"
    autod.mkdir(parents=True, exist_ok=True)
    (autod / "window.json").write_text(json.dumps(window, indent=2) + "\n")
    _write_activity_digest(ctx, window, sid8, int(cfg.get("activity_max_lines", 40)))


def _oc_redact(ctx: Path, s: str) -> str:
    try:
        from celeborn_sync import redact
        return redact(s or "", load_config(ctx).get("secret_patterns", []))[0]
    except Exception:
        return s or ""


def _record_turn_prompt(ctx: Path, sid: str, prompt: str) -> None:
    """Open a fresh window entry at the user-turn boundary (the transcript capture's analog of a
    new `turn`), so this turn's reported tool calls fold into one bounded fact row."""
    sid8 = (sid or "session")[:8]
    window = _oc_load_window(ctx)
    window.append({"ts": now_iso(), "prompt": _oc_redact(ctx, prompt).replace("\n", " ").strip()[:200],
                   "files": [], "commands": [], "commits": [], "tests": [], "sid": sid8})
    _oc_save_window(ctx, window, sid8)


def _record_tool_activity(ctx: Path, sid: str, tool: str, inp: dict) -> None:
    """Fold one reported tool call into the current window entry (same-session tail entry, else a
    new one — a tool call can arrive before any prompt was seen, e.g. right after plugin install).
    Only the shapes the digest records: file paths from file tools, command heads from Bash — a
    read's filePath must never masquerade as an edit."""
    fp = (str(inp.get("file_path") or inp.get("filePath") or inp.get("notebook_path") or "").strip()
          if tool in _FILE_TOOLS else "")
    cmd = str(inp.get("command") or "").strip() if tool == "Bash" else ""
    if not fp and not cmd:
        return
    sid8 = (sid or "session")[:8]
    window = _oc_load_window(ctx)
    cur = window[-1] if window and isinstance(window[-1], dict) and window[-1].get("sid") == sid8 else None
    if cur is None:
        cur = {"ts": now_iso(), "prompt": "", "files": [], "commands": [], "commits": [],
               "tests": [], "sid": sid8}
        window.append(cur)
    if fp and fp not in cur["files"] and len(cur["files"]) < 50:
        cur["files"].append(fp)
    if cmd and len(cur["commands"]) < 50:
        cur["commands"].append(_oc_redact(ctx, cmd).splitlines()[0][:200])
    _oc_save_window(ctx, window, sid8)


def _auto_touch_for_session(ctx: Path, sid: str, path_str: str) -> None:
    """Auto-register a touch for a transcript-less harness edit (P4): the board's active-file chips
    must not depend on a fast non-thinking model remembering the touch protocol. Attribution mirrors
    the claim path — the agent_sessions link's owner when the session is signed in, else the session
    short id (the session IS the agent's name, CELE-t131-B). Registers MY record alongside any peer
    already on the file (schema/2, CELE-t309) — a same-owner re-edit just freshens my timestamp, and
    a peer's touch stays visible instead of being clobbered. Best-effort: any failure degrades to no
    touch, never a broken hook."""
    try:
        relpath = _resolve_repo_relpath(ctx.parent, path_str)
        link = (_load_metrics(ctx).get("agent_sessions") or {}).get(sid) or {}
        owner = (link.get("owner") or "").strip() or (sid or "")[:6]
        if not owner:
            return
        data = _load_touches(ctx)
        files = data.setdefault("files", {})
        recs = files.setdefault(relpath, [])
        prev = next((m for m in recs if _touch_by(m) == owner), {}) or {}
        _upsert_toucher(recs, {
            "by": owner, "family": prev.get("family", ""), "model": prev.get("model", ""),
            "at": now_iso(), "task": _session_task_id(ctx, sid),
            "why": prev.get("why") or "auto: opencode edit"})
        _save_touches(ctx, data)
    except Exception:
        pass


def cmd_capture(args):
    ctx = find_or_create_context(args)
    cfg = load_config(ctx)
    patterns = cfg.get("secret_patterns", [])
    path = Path(args.transcript)
    if not path.is_file():
        die(f"transcript not found: {path}")
    m = _load_metrics(ctx)
    caps = m.get("captures")
    if not isinstance(caps, dict):
        caps = {}
    legacy = m.get("capture") or {}
    if not caps and legacy.get("session_id"):       # migrate the old single-slot layout
        caps[legacy["session_id"]] = dict(legacy)
    run_sid = args.session or None

    # Per-session cursor: each Claude session advances its OWN offset/total, so alternating sessions
    # sharing this metrics.json (esp. the global ~/.context sink) can't invalidate each other and
    # force a full re-read every turn. With no session id, fall back to the most-recent session.
    sid_key = run_sid or legacy.get("session_id")
    cur = dict(caps.get(sid_key) or {})

    # Decide where to start reading + which session file to write.
    new_session = bool(run_sid) and run_sid not in caps
    start_offset = 0 if new_session else int(cur.get("offset") or 0)
    if start_offset > path.stat().st_size:   # file shrank (compaction rewrote it) → reset
        start_offset, new_session = 0, True

    turns, last_offset, last_uuid, first_sid = _extract_turns(path, start_offset)
    sid = run_sid or first_sid or cur.get("session_id") or "session"
    sid8 = sid[:8]

    # Per-turn heartbeat state — reset when a new session begins.
    sess_tokens = 0 if new_session else int(cur.get("tokens_session") or 0)
    idle_streak = 0 if new_session else int(cur.get("idle_streak") or 0)

    if not turns:
        # Nothing to record. Advance the cursor past any consumed (meta/snapshot) lines so we don't
        # re-scan them, and bump the idle counter so the heartbeat note stays unique; never create
        # files. On a new session, drop the stale file pointer.
        idle_streak += 1
        _write_capture(m, caps, sid, {"session_id": sid, "offset": last_offset,
                       "last_uuid": last_uuid or cur.get("last_uuid"),
                       "file": (None if new_session else cur.get("file")),
                       "tokens_session": sess_tokens, "idle_streak": idle_streak,
                       "last_delta": 0})
        _save_metrics(ctx, m)
        if not getattr(args, "quiet", False):
            print("capture: no new entries")
        if getattr(args, "note", False):
            print(_capture_note(0, sess_tokens, idle_streak))
        return

    autod = ctx / "auto"
    autod.mkdir(parents=True, exist_ok=True)
    sess_file = cur.get("file") if not new_session and cur.get("file") else f"auto/{now_iso()[:10]}-{sid8}.md"
    sp = ctx / sess_file
    if not sp.is_file():
        sp.write_text(f"# Automatic Context Record — session {sid8}\n\n"
                      "<!-- Mechanical capture by `celeborn capture`. Local-only, gitignored. "
                      "Do not edit by hand. -->\n")

    omax = int(cfg.get("capture_output_max_chars", 8000))
    redacted = [_redact_turn(t, patterns, omax) for t in turns]
    recorded_chars = 0
    for rt in redacted:
        block = _format_turn_block(rt)
        _append(sp, "\n" + block)
        recorded_chars += len(block)

    # rolling window holds only the bounded digest FACTS (not the faithful `events`), so activity.md
    # stays small; the complete record lives in the cold auto file above.
    win_path = autod / "window.json"
    window = []
    if win_path.is_file():
        try:
            window = json.loads(win_path.read_text())
        except (OSError, json.JSONDecodeError):
            window = []
    if new_session:
        window = []
    window = (window + [_digest_facts(rt) for rt in redacted])[-int(cfg.get("activity_window_turns", 15)):]
    win_path.write_text(json.dumps(window, indent=2) + "\n")
    _write_activity_digest(ctx, window, sid8, int(cfg.get("activity_max_lines", 40)))

    _prune_auto(ctx, int(cfg.get("auto_keep_files", 30)))

    cpt = int(cfg.get("chars_per_token", 4)) or 4
    delta = (recorded_chars + cpt - 1) // cpt   # mirrors _est_tokens
    sess_tokens += delta

    # CMM economics (CELE-t92): credit the permission prompts the pre-clear avoided this capture —
    # the agent's calls to CMM-pre-cleared tools that ran without an Allow click. Provenance-gated in
    # celeborn_cmm so only engaged projects accrue; best-effort (never block capture on it).
    try:
        allow_names = __import__("celeborn_cmm").credited_tool_names(ctx)
    except Exception:
        allow_names = set()
    auto_allowed = _count_auto_allowed(turns, allow_names)
    if auto_allowed:
        cmm_m = dict(m.get("cmm") or {})
        cmm_m["prompts_auto_allowed"] = int(cmm_m.get("prompts_auto_allowed", 0) or 0) + auto_allowed
        m["cmm"] = cmm_m

    # Permission allow-list economics (t100): also credit the prompts the settings.json allow-list
    # avoided this capture — the safe baseline `wire --global` ships plus the user's own rules. Counts
    # Bash commands under a `Bash(<prefix>:*)` rule and built-ins (Read/Glob/Grep/…) that ran without
    # an Allow click; excludes CMM's credited tools so the buckets stay disjoint. Best-effort.
    try:
        perm_allowed = _count_allowlist_auto_allowed(turns, _effective_allow_rules(ctx), allow_names)
    except Exception:
        perm_allowed = 0
    if perm_allowed:
        perm_m = dict(m.get("permissions") or {})
        perm_m["prompts_auto_allowed"] = int(perm_m.get("prompts_auto_allowed", 0) or 0) + perm_allowed
        m["permissions"] = perm_m

    _write_capture(m, caps, sid, {"session_id": sid, "offset": last_offset,
                   "last_uuid": last_uuid or cur.get("last_uuid"), "file": sess_file,
                   "tokens_session": sess_tokens, "idle_streak": 0, "last_delta": delta})
    _save_metrics(ctx, m)

    if not getattr(args, "quiet", False):
        nf = sum(len(t["files"]) for t in turns)
        nc = sum(len(t["commands"]) for t in turns)
        nk = sum(len(t["commits"]) for t in turns)
        print(f"captured: {len(turns)} turn(s), {nf} file(s), {nc} command(s), {nk} commit(s) -> {sess_file}")
    if getattr(args, "note", False):
        print(_capture_note(delta, sess_tokens, 0))
    try:
        __import__("celeborn_jira").flush_auto_push(ctx, quiet=True)
    except Exception:
        pass
    try:
        __import__("celeborn_github").flush_auto_push(ctx, quiet=True)  # CELE-t214
    except Exception:
        pass
    # Keep the hosted active-agents token chips tracking this live window between card mutations
    # (CELE-t131) — throttled + detached, a no-op when hosted sync isn't configured / signed in.
    try:
        __import__("celeborn_sync").schedule_agents_push(ctx)
    except Exception:
        pass  # hosted liveness is best-effort — never break capture
    # Self-heal the Hot/Warm tiers: FIFO over-budget journal + state.md history into cold archives so
    # they don't balloon the Orient load. No-op once under budget; never blocks capture.
    try:
        _auto_archive(ctx, cfg)
    except Exception:
        pass


def cmd_heartbeat(args):
    """Print the per-turn capture heartbeat to PLAIN stdout — for the UserPromptSubmit hook.

    Why a second channel: the Stop hook's `systemMessage` is shown inline in a terminal but is NOT
    surfaced by the Claude desktop/web app (it lands there as a hidden `hook_system_message`
    transcript attachment). UserPromptSubmit-hook stdout, by contrast, is reliably user-visible on
    BOTH surfaces — it's the same channel the context reminder rides. So this is how app users
    actually see the heartbeat. UserPromptSubmit fires at the START of a turn, so it reports what was
    banked as of the PREVIOUS turn's capture (read from the metrics cursor; no transcript needed)."""
    ctx = find_context_root(Path(getattr(args, "path", ".") or "."))
    if ctx is None:
        return                                   # outside a .context/ repo — stay silent
    cap = _capture_cursor(_load_metrics(ctx), getattr(args, "session", None))
    if not cap.get("session_id"):
        return                                   # nothing captured yet this machine — stay silent
    total = int(cap.get("tokens_session") or 0)
    delta = int(cap.get("last_delta") or 0)
    if cap.get("live"):
        # A live-reported window (P4, CELE-t141: `celeborn record tokens`, OpenCode) — the number IS
        # the context size the model just saw, not "content banked so far"; word it that way.
        line = f"🏹 Celeborn —> ~{total:,} tokens in the live context window"
    else:
        line = f"🏹 Celeborn —> {total:,} tokens recorded this session"
    if delta > 0:
        line += f" · +{delta:,} last turn"
    print(line)


def cmd_statusline(args):
    """Render Celeborn's status line (the Claude Code `statusLine` command).

    A statusLine is painted persistently in the host's UI chrome and — unlike a hook `systemMessage`,
    which some surfaces (the Claude app) deliver to the model but never show the user — it can't be
    suppressed. So it's the deterministic way to keep the per-turn capture visible. Compact:
    banked-this-session from the capture cursor, plus the live context size when a transcript is
    passed. Always prints one line (statusLine output replaces the default status line)."""
    ctx = find_context_root(Path(getattr(args, "path", ".") or "."))
    if ctx is None and _global_context().exists():
        ctx = _global_context()
    parts = []
    if ctx is not None:
        cap = _capture_cursor(_load_metrics(ctx), getattr(args, "session", None))
        recorded = int(cap.get("tokens_session") or 0)
        if recorded:
            parts.append(f"{recorded:,} tokens recorded")
    tp = getattr(args, "transcript", None)
    if tp and Path(tp).is_file():
        cpt = int((load_config(ctx) if ctx else {}).get("chars_per_token", 4)) or 4
        live = _estimate_transcript_tokens(Path(tp), cpt)
        if live:
            parts.append(f"ctx ~{live:,}")
    print(f"🏹 Celeborn —> {' · '.join(parts)}".rstrip())


# --------------------------------------------------------------------------- hook dispatch (collapse)
#
# Phase 1 of the executable-app plan (references/executable-app.md §3, §9.1): one in-process entry
# point for every Claude Code hook event. `celeborn hook <event>` reads the host's JSON payload from
# stdin and runs the per-turn work HERE — no bash control flow, no inline `python3 -c` JSON parsing,
# no `$CELEBORN_HOME` resolver. dispatch_hook() is the importable "one logic" module §3 calls for:
# today the thin client runs it cold against disk; the daemon (phase 2) will run the same dispatch
# against warm state. It never raises — a hook must never break the user's turn.

# event token (CLI arg) -> the Claude Code hook event name it serves.
HOOK_EVENTS = {
    "session-start": "SessionStart",
    "user-prompt-submit": "UserPromptSubmit",
    "stop": "Stop",
    "pre-compact": "PreCompact",
    "session-end": "SessionEnd",
    "statusline": "statusLine",
    # Quality gates (t70 Phase 2) — installed only by `celeborn wire-quality`, never by `wire`.
    "post-edit": "PostToolUse",      # cheap per-edit check (py_compile / `tsc --noEmit`)
    "quality-stop": "Stop",          # full suite once per turn when test-relevant files changed
    # Safety guard (t101) — installed by `wire`. Blocks an un-approvable `cd … > rel/file` compound.
    "pre-tool-use": "PreToolUse",    # steer shell redirection → the Write/Edit tool
    # Transcript-less touch + activity (P4, CELE-t141) — never wired for Claude Code (its transcript
    # capture already records every tool call); the OpenCode plugin shells it on tool.execute.after.
    "post-tool-use": "PostToolUse",
}

# The checkpoint reminder the PreCompact hook prints (was hooks/pre-compact.sh's heredoc).
PRECOMPACT_MSG = (
    "[celeborn] Compaction imminent. CHECKPOINT now before context is summarized:\n"
    "  1. Rewrite .context/state.md in place (Now / Next action / Open threads).\n"
    "  2. Append one entry to the bottom of .context/journal.md (what + evidence + next).\n"
    "  3. Run: celeborn checkpoint --for-clear --focus \"...\" --next \"...\" --status \"...\"  "
    "(the one-command pre-clear routine: writes session.json, regenerates handoff, takes a restore "
    "snapshot, and verify-gates the Hot tier — it exits nonzero with a fix-list if a /clear would "
    "still lose work).\n"
    "Anything not written to .context/ will be lost on compaction."
)


def _read_hook_payload(raw: str | None = None) -> dict:
    """Parse the Claude Code hook JSON from stdin into a dict. Returns {} on anything unexpected
    (no stdin, empty, non-JSON, non-object) — the hooks must degrade to a no-op, never crash."""
    if raw is None:
        try:
            raw = sys.stdin.read()
        except Exception:
            return {}
    if not raw or not raw.strip():
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _hook_run(fn, **ns) -> str:
    """Run an existing cmd_* function with a synthesized Namespace and return its stdout as a string.

    This is what makes the collapse a *reuse*, not a rewrite: every hook drives the same command
    implementations the CLI exposes. die()/SystemExit and any unexpected error are swallowed (the bash
    hooks `|| true`'d everything) so one bad turn degrades to silence, never a broken turn."""
    buf = io.StringIO()
    args = argparse.Namespace(**ns)
    with contextlib.redirect_stdout(buf):
        try:
            fn(args)
        except (Exception, SystemExit):
            pass
    return buf.getvalue()


def _compose_user_prompt_envelope(heartbeat: str, nudge: str, handoff: str = "", claim: str = "",
                                  directive: str = "", progress_nudge: str = "", arch_notice: str = "",
                                  intents: str = "") -> str:
    """Build the UserPromptSubmit JSON envelope (was the python3 tail of hooks/context-watch.sh).

    `additionalContext` is delivered to the MODEL only — never painted for the user on any surface
    (cc#50542). The heartbeat rides as quiet do-not-surface context; the nudge is phrased as an
    imperative so the model relays it into its reply, where every surface renders it. A handoff is a
    card the user sent from the board to be worked on NOW — injected as an actual work instruction. A
    directive is the card-less-work gate (t131) — a top-priority instruction, so it leads the envelope.
    Returns "" when there is nothing to inject (so the hook emits no output at all)."""
    parts = []
    if directive:
        parts.append(
            "[Celeborn card gate — TOP PRIORITY, resolve this before doing the work this turn:]\n"
            + directive)
    if handoff:
        parts.append(
            "[Celeborn task hand-off — the user sent the following card from the board to be worked on "
            "NOW. Treat it as the user's prompt for this turn and act on it (alongside anything they "
            "also typed):]\n" + handoff)
    if claim:
        parts.append(
            "[Celeborn card claim — you now own the following board card(s); they have been moved to "
            "DOING under your name. Every other agent sharing this .context/ will see this on their next "
            "orient. Identify yourself in your reply (who claimed this). Before future autonomous "
            "claims, read the board and avoid cards that would interrupt in-flight work. Work these "
            "cards as the user's request for this turn:]\n" + claim)
    if intents:
        parts.append(
            "[Celeborn blackboard intents — inter-agent commit choreography (CELE-t303). Context for "
            "your commit planning, do NOT surface verbatim — ACT on it: a peer agent has declared a "
            "planned commit overlapping files YOU are touching. Do not commit the overlapping files "
            "this turn without coordinating — wait for the peer's intent to clear (`celeborn intent "
            "list`), or agree an order. Declare your own planned commit with `celeborn intent "
            "\"<what>\" --task <id> --eta <mins>` so peers hold for you too:]\n" + intents)
    if heartbeat:
        parts.append("[Celeborn heartbeat — context only, do NOT surface this to the user]\n" + heartbeat)
    if progress_nudge:
        parts.append(
            "[Celeborn progress nudge — SURFACE THIS TO THE USER. Your doing card's bar hasn't moved; "
            "relay the line below verbatim (on its own line) and run the copy-pasteable command it "
            "names if the milestone is genuinely done:]\n" + progress_nudge)
    if arch_notice:
        parts.append(
            "[Celeborn architecture trace — SURFACE THIS TO THE USER. The auto-trace (CELE-t201) found a "
            "new piece in the stack and remapped the hosted Stack; relay the line(s) below verbatim, on "
            "their own line(s):]\n" + arch_notice)
    if nudge:
        parts.append(
            "[Celeborn context-health notice — FRESHEN THE HOT TIER FIRST, THEN SURFACE THIS TO THE USER] "
            "A checkpoint is due: Celeborn is about to tell the user it's safe to /clear \"without need to "
            "rehydrate\". That promise only holds if the Hot tier is fresh RIGHT NOW — so before you relay "
            "the line below, checkpoint the authored Hot tier so a /clear or compaction loses nothing:\n"
            "  1. Rewrite .context/state.md in place (Now / Next action / Open threads) to reflect this turn.\n"
            "  2. Run: celeborn checkpoint --for-clear --focus \"...\" --next \"...\" --status \"...\"  "
            "(the one-command pre-clear routine: updates session.json, regenerates handoff, takes a "
            "restore snapshot, and verify-gates that a /clear loses nothing — it exits nonzero with a "
            "fix-list if the Hot tier is still stale).\n"
            "  3. If meaningful work landed since the last entry, append one line to .context/journal.md.\n"
            "Skip a step only if it is already current. THEN, because the user is likely on a surface (e.g. "
            "the Claude desktop app) where hook output is invisible to them and only your reply is shown, "
            "begin your reply with the following line(s) verbatim, on their own lines, before anything else, "
            "so the notice reaches them — then answer normally:\n"
            + nudge)
    if not parts:
        return ""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\n\n".join(parts),
    }})


# --------------------------------------------------------------------------- PreToolUse safety guard
#
# (t101) Two levers steer the model away from a `cd … && … > relative/file` compound. Such a command
# can't be statically approved: once the cwd changes, a relative redirect target resolves against the
# *new* dir, so the permission matcher can't bound where the write lands (the path-resolution bypass
# the gate exists to stop) — Claude Code falls back to a manual prompt on every run.
#   • Lever 1 (soft, cheap): SHELL_HYGIENE_RULE rides the SessionStart orient channel — a standing
#     directive to prefer the Write/Edit tool and tool-native dir flags, so the pattern rarely forms.
#   • Lever 2 (hard, deterministic): the `pre-tool-use` hook turns the recurring prompt into a *deny*
#     with a corrective message. The model keeps an explicit escape hatch — a trailing
#     `# celeborn:allow-redirect[: why]` comment makes the guard AUTO-ALLOW the command with no prompt
#     (the operator accepted the path-resolution risk for marked writes), for the rare case a shell
#     redirect is genuinely the only way.
SHELL_HYGIENE_RULE = (
    "🏹 Celeborn shell rule —> Prefer the Write/Edit tool over shell output redirection (`>`/`>>`), and "
    "reach a directory with a tool's own flag (`git -C`, `npm --prefix`, `make -C`) or an absolute path "
    "rather than `cd … && …`. A `cd` plus a relative-path redirect can't be auto-approved — Celeborn's "
    "PreToolUse guard will block it (override with a trailing `# celeborn:allow-redirect: <why>`)."
)
REDIRECT_BYPASS_MARKER = "celeborn:allow-redirect"
# `cd <arg>` beginning a command segment (start of string, or after a newline / ; / & / | separator —
# `&&` and `||` end in the single-char class, so they match too). Requires an argument, so a bare
# `cd` (go-home) doesn't trip it.
_CD_SEGMENT_RE = re.compile(r"(?:^|[\n;&|])\s*cd\s+\S")
# A `>`/`>>` redirect to a FILE. Negative lookbehind drops fd-numbered/chained forms (`2>`, `>>`'s
# inner `>`); negative lookahead drops fd duplication (`>&2`). The target is captured for the
# absolute-vs-relative test below.
_REDIRECT_TARGET_RE = re.compile(r"(?<![\d&>])>>?(?!&)\s*(\"[^\"]+\"|'[^']+'|[^\s|;&<>]+)")


def _has_relative_write_redirect(cmd: str) -> bool:
    """True if `cmd` writes to a RELATIVE-path file via `>`/`>>` — the only redirect a `cd` can move.
    Absolute targets and `/dev/null` resolve the same regardless of cwd, and fd dups (`2>&1`, `>&2`)
    aren't file writes — none are the bypass risk, so none count."""
    for m in _REDIRECT_TARGET_RE.finditer(cmd):
        tgt = m.group(1).strip("\"'")
        if not tgt or tgt.startswith("&"):
            continue                                   # fd duplication, not a file write
        if tgt == "/dev/null" or tgt.startswith("/"):
            continue                                   # absolute / null target — cwd-independent
        return True
    return False


def _is_cd_redirect_pattern(cmd: str) -> bool:
    """The gated shape: a `cd` into a new dir AND a relative-path write redirect in the same command —
    exactly what forces a manual approval on every run. Independent of the bypass marker (the decision
    function inspects that separately to choose deny vs auto-allow)."""
    return bool(cmd) and bool(_CD_SEGMENT_RE.search(cmd)) and _has_relative_write_redirect(cmd)


_CD_REDIRECT_DENY = (
    "🏹 Celeborn blocked `cd … > file`: a directory change plus a relative-path redirect can't be "
    "statically approved — the write target resolves against the cd'd dir (the path-resolution bypass the "
    "permission gate guards), so it would prompt on every run. Use one of, in order:\n"
    "  • the Write/Edit tool to create or modify the file (no shell redirect at all — preferred)\n"
    "  • a tool's own directory flag instead of `cd`: `git -C <abs>`, `npm --prefix <abs>`, `make -C <abs>`\n"
    "  • an ABSOLUTE redirect target (`> /abs/path/out`) so the destination is unambiguous\n"
    "If a shell redirect is genuinely the only option, re-run with a trailing "
    "`# celeborn:allow-redirect: <why>` comment — Celeborn will auto-allow it with no prompt (the operator "
    "opted into the path-resolution risk for marked writes)."
)
_CD_REDIRECT_ALLOW = (
    "🏹 Celeborn auto-allowed `cd … > file` on an explicit `# celeborn:allow-redirect` marker — the "
    "operator accepted the path-resolution risk for this write. The default posture denies the pattern "
    "and steers to the Write/Edit tool; this command opted out by name."
)


def _pre_tool_use_decision(payload: dict) -> str:
    """PreToolUse guard (lever 2). For the gated `cd … > relative/file` Bash compound: DENY with a
    corrective message by default, or AUTO-ALLOW (no prompt) when the command carries an explicit
    `# celeborn:allow-redirect` marker — the operator's accepted-risk escape hatch. Emits nothing for
    anything else, so every other tool call flows through untouched. Harness-independent on purpose —
    a universal shell-hygiene rule, not a `.context/` concern."""
    if (payload.get("tool_name") or "") != "Bash":
        return ""
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not _is_cd_redirect_pattern(cmd):
        return ""                                      # not the gated shape — flow through untouched
    if REDIRECT_BYPASS_MARKER in cmd:
        decision, reason = "allow", _CD_REDIRECT_ALLOW
    else:
        decision, reason = "deny", _CD_REDIRECT_DENY
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }})


# --------------------------------------------------------------------------- PreToolUse publish guard (t191)
#
# Layer B of the product federation (CELE-t188 §6). A publish/release action — twine/flit/poetry/hatch/
# maturin publish, npm/pnpm/yarn/bun publish, cargo publish, `gh release create`, or a tag push — that
# targets a facet whose role is `server:private` or any `oss:*` is a policy violation: server:private
# never publishes (full rights reserved), and oss:* is stewarded code we contribute back via fork→PR, never
# publish as ours. Only `client:public` publishes (still honoring the CELE-t168 BUSL gate elsewhere).
# Same rail + hard-DENY vocabulary as the redirect guard above, and the same accepted-risk escape hatch:
# a trailing `# celeborn:allow-publish: <why>` marker auto-ALLOWS the command (mirrors allow-redirect).
# Silent for single-repo projects (no product.md) and for any command that isn't a publish action — the
# cheap regex runs first, so only publish-shaped Bash commands ever pay the registry lookup.
PUBLISH_BYPASS_MARKER = "celeborn:allow-publish"
_PUBLISH_ACTION_RE = re.compile(
    r"\btwine\s+upload\b"
    r"|\bpython\d?\s+-m\s+twine\b"
    r"|\b(?:flit|poetry|hatch|maturin)\s+publish\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+publish\b"
    r"|\bcargo\s+publish\b"
    r"|\bgh\s+release\s+create\b"
    r"|\bgit\s+push\b[^\n|;&]*--(?:tags|follow-tags)\b",
    re.I,
)


def _is_publish_action(cmd: str) -> bool:
    """True if `cmd` is a package-registry publish or a release/tag push — the actions the publish guard
    (and cmd_push's in-command check) enforce role policy on. A plain branch `git push` is NOT one."""
    return bool(cmd) and bool(_PUBLISH_ACTION_RE.search(cmd))


def _role_forbids_publish(role: str) -> bool:
    """Publish policy from the role vocabulary (t188 §3): server:private never publishes; every oss:*
    contributes via fork→PR, never publish-as-ours. Only client:public may publish."""
    role = (role or "").strip()
    return role == "server:private" or role.startswith("oss:")


def _publish_policy_reason(key: str, role: str, action: str = "this publish/release action") -> str:
    """The hard-DENY / refusal message for a publish targeting a forbidden facet — shared by the
    PreToolUse guard and cmd_push's in-command check so the wording is identical wherever it fires."""
    if (role or "").startswith("oss:"):
        why = f"role {role} — stewarded OSS; contribute via fork → PR, never publish as ours"
    else:
        why = f"role {role} — private, full rights reserved; it never publishes"
    return (f"🏹 Celeborn publish guard: {action} targeting facet '{key}' is refused ({why}). If this is "
            f"genuinely intended, the operator can override the raw command with a trailing "
            f"`# {PUBLISH_BYPASS_MARKER}: <why>` comment (accepted-risk, exactly like `# celeborn:allow-redirect`).")


def _publish_guard_decision(payload: dict, project_dir: str) -> str:
    """PreToolUse publish guard (t191). Hard-DENY a Bash publish/release action that targets a
    server:private/oss:* facet — resolved from the product registry either by a bound checkout path
    appearing in the command or by the project the command runs in. AUTO-ALLOW on an explicit
    `# celeborn:allow-publish` marker. Emits nothing for anything else (no product.md, not a publish
    action, or targeting a client:public facet), so every other Bash call flows through untouched."""
    if (payload.get("tool_name") or "") != "Bash":
        return ""
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not _is_publish_action(cmd):
        return ""                                      # not a publish action — flow through (cheap fast path)
    ctxdir = find_context_root(Path(project_dir))
    if ctxdir is None:
        return ""                                      # not a Celeborn project — never guard
    targets = _publish_guard_targets(ctxdir, cmd, project_dir)
    if not targets:
        return ""                                      # no forbidden facet in scope (e.g. client:public) — allow
    key, role = targets[0]
    if PUBLISH_BYPASS_MARKER in cmd:
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": (
                f"🏹 Celeborn auto-allowed a publish action on facet '{key}' ({role}) via an explicit "
                f"`# {PUBLISH_BYPASS_MARKER}` marker — the operator accepted the policy risk. The default "
                f"posture hard-DENYs publishing a server:private/oss:* facet."),
        }})
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _publish_policy_reason(key, role),
    }})


# --------------------------------------------------------------------------- card-less-work gate (t131)
#
# Celeborn is the live source of truth for who's doing what — work done off-board is invisible to the
# other agents sharing the repo. So a session that owns no board card (and was handed none) gets
# steered onto one before it acts, with the same two-lever shape as the redirect guard above:
#   • Lever 1 (soft): a top-priority UserPromptSubmit directive — "no task: ask the user or claim the
#     obvious open card" — injected each turn the session is card-less.
#   • Lever 2 (hard, deterministic): the PreToolUse hook soft-DENIES Edit/Write/NotebookEdit until a
#     card is linked, with a corrective message. The `celeborn` CLI (over Bash) and read-only tools are
#     never gated. Two exemptions keep it from ever being a dead-end: the board being EMPTY (no open
#     card to claim — nothing to gate on, "add one") and the operator's accepted-risk escape hatch
#     CELEBORN_ALLOW_NO_CARD=1 (analogous to the `# celeborn:allow-redirect` marker).
CARDLESS_BYPASS_ENV = "CELEBORN_ALLOW_NO_CARD"
# Tools the PreToolUse gate hard-denies for a card-less session (CELE-t134 hardens CELE-t131): not just
# file edits but the tools substantive *research* and delegation run through — web access and subagent
# spawn. Deliberately EXCLUDES Bash and Read so a gated session can always orient and claim (the
# `celeborn` CLI is Bash; reading the board is Read). `Task`/`Agent` cover the subagent tool across
# harnesses (Claude Code names it `Task`; this harness names it `Agent`).
_CARD_GATED_TOOLS = ("Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent")


def _cardless_bypass() -> bool:
    """The operator's accepted-risk escape hatch for the card-less-work gate (t131) — set
    CELEBORN_ALLOW_NO_CARD=1 in the launch env to let a session edit files without owning a board card
    (both the directive and the Edit/Write deny fall silent). Mirrors the `# celeborn:allow-redirect`
    marker: a deliberate, named opt-out, not a default."""
    import os
    return (os.environ.get(CARDLESS_BYPASS_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _session_owns_live_card(ctx: Path, sid: str) -> bool:
    """True if this session is attributable to a live (doing) card — by the recorded
    session→card link (`_session_has_task`) or, as a fallback, because a live card's owner is this
    session's short id (the t131 part-B identity). The fallback means a `claim --by <sid[:6]>` that
    omitted `--session` still clears the gate."""
    if _session_has_task(ctx, sid):
        return True
    short = (sid or "").strip()[:6]
    if not short:
        return False
    return any((t.get("owner") or "").strip() == short
               for t in _load_tasks(ctx)
               if (t.get("state") or "") in ("doing",))


def _card_gate_enabled(ctx: Path) -> bool:
    """The `card_gate` config switch (CELE-t352, default True): when False the whole card-less-work
    gate (t131/t140) stands down — no PreToolUse deny, no directive, no opencode auto-provision. The
    operator sets it from the board's OpenCode Settings section; a fresh project keeps today's
    always-on enforcement."""
    return bool(load_config(ctx).get("card_gate", True))


def _card_gate_status(ctx: Path, sid: str) -> str:
    """Classify a session for the card-less-work gate (t131):
      'ok'    — owns a live card, or the bypass env is armed → never gate.
      'empty' — the board has no open card to claim → exempt ('add one'); never hard-block.
      'gated' — open cards exist but this session owns none → nudge + soft-deny edits.
    """
    if _cardless_bypass() or _session_owns_live_card(ctx, sid):
        return "ok"
    has_open = any((t.get("state") or "") in ("todo", "doing") for t in _load_tasks(ctx))
    return "gated" if has_open else "empty"


def _cardless_claim_hint(sid: str) -> str:
    """`celeborn claim` invocation that records THIS session as the card's owner — so claiming it
    clears the gate in the same turn (`--session` writes the session→card link `_session_has_task`
    reads). The full session id is embedded because the agent has no other way to know it."""
    return "celeborn claim <id>" + (f" --session {sid}" if sid else "")


def _cardless_directive(sid: str) -> str:
    """Top-priority UserPromptSubmit directive (t131 lever 1): injected when a session owns no card and
    named none but the board has open cards. Steers the session onto a card before it acts."""
    return (
        "⛔ NO TASK CLAIMED — this session owns no Celeborn board card and you (the user) named none. "
        "Celeborn is the live source of truth for who's doing what; work done off-board is invisible to "
        "the other agents sharing this repo. A card is MANDATORY before ANY work this turn — answering a "
        "question, research, design, or edits all count equally. Do NOT rationalize that you are 'just "
        "answering' or that the work 'touches no files': if the turn produces work, it needs a card "
        "first. Web access (WebFetch/WebSearch), subagents (Task/Agent), and file edits "
        "(Edit/Write/NotebookEdit) are hard-blocked until a card is linked. Before doing the work, do "
        "ONE of:\n"
        "  • Ask the user which card this belongs to (best when it's ambiguous), or\n"
        f"  • Claim the obvious open card yourself: `{_cardless_claim_hint(sid)}`, or\n"
        "  • If no open card fits, add one and claim it: `celeborn tasks add \"<title>\"`.\n"
        "Read the board first (`celeborn tasks`) and don't grab a card another agent is mid-flight on. "
        "The `celeborn` CLI and board-reading (Bash/Read) are never blocked, so you can always orient "
        "and claim. Only a launch with CELEBORN_ALLOW_NO_CARD=1, or an empty board (nothing to claim), "
        "lifts this gate."
    )


def _cardless_deny(sid: str) -> str:
    """Corrective message for the PreToolUse hard-deny (CELE-t131 lever 2, widened in CELE-t134). Fires
    on edits AND research/subagent tools, so the wording is tool-agnostic."""
    return (
        "🏹 Celeborn blocked this action: a card is MANDATORY and this session owns none. Celeborn is the "
        "source of truth for who's doing what, so untracked work — edits, research, or subagents — is "
        "invisible to the other agents sharing this repo. Link a card first:\n"
        f"  • Claim the card you're working, then re-try: `{_cardless_claim_hint(sid)}`.\n"
        "  • Unsure which? Read the board (`celeborn tasks`) and ask the user, or "
        "`celeborn tasks add \"<title>\"` then claim it.\n"
        "If you must work without a card, the operator can launch with CELEBORN_ALLOW_NO_CARD=1 to lift "
        "this gate (accepted-risk, like `# celeborn:allow-redirect`). The `celeborn` CLI and board-reading "
        "(Bash/Read) are never blocked, so you can always orient and claim."
    )


# --------------------------------------------------------------------------- PM auto-provision (t211)
#
# Under the OpenCode harness there is no human at a permission prompt — the deny/steer loop above
# costs a non-thinking coder whole turns. So there the PM (this deterministic plumbing, acting for
# the operator who installed the platform) resolves a card-less coder itself, at the moment it
# starts substantive work: claim the card already assigned to the session (owner == sid6, staged
# ahead of a t213 dispatch), else create a fresh `auto` card titled from the session's last
# recorded prompt — and let the tool call through. The board stays truthful without human clicks,
# and the coder NEVER hits the gate (contract t203 §1.3: the agent_sessions link is written at bind
# time, which is also what lifts the gate for the rest of the session). Claude Code (and every
# prompting harness) keeps the deny/steer behavior above unchanged — there the human IS present.
#
# Autonomy of an auto-provisioned card: `research,edits,tests` — everything a working coder needs
# to keep working, but NEVER `commit` (t203 §3.4: git-write is opt-in, never implied). Without a
# grant set, an ungroomed card under opencode denies everything (t212), which would just move the
# coder's dead-end from the card gate to the autonomy gate. Evaluated lazily — AUTONOMY_GRANTS is
# defined in the autonomy-gate section below this one.
def _autoprovision_grants() -> list[str]:
    return [g for g in AUTONOMY_GRANTS if g != "commit"]


def _oc_last_prompt(ctx: Path, sid: str) -> str:
    """The most recent user prompt this session recorded in the transcript-less activity window
    (`_record_turn_prompt`, P4) — the PM's only deterministic signal of what the coder was asked to
    do; used to title an auto-provisioned card. '' when the session never recorded a prompt."""
    sid8 = (sid or "session")[:8]
    for entry in reversed(_oc_load_window(ctx)):
        if isinstance(entry, dict) and entry.get("sid") == sid8 and (entry.get("prompt") or "").strip():
            return str(entry["prompt"]).strip()
    return ""


def _autoprovision_title(ctx: Path, sid: str, payload: dict) -> str:
    """Title for an auto-provisioned card: the session's last recorded prompt (first line, bounded),
    else a description of the triggering tool call — never empty, never multi-line."""
    prompt = _oc_last_prompt(ctx, sid)
    if prompt:
        line = prompt.splitlines()[0].strip()
        return (line[:95] + "…") if len(line) > 96 else line
    ti = payload.get("tool_input") or {}
    fp = str(ti.get("file_path") or ti.get("filePath") or ti.get("notebook_path") or "").strip()
    tool = (payload.get("tool_name") or "work").strip().lower()
    return f"Auto: untitled coder-session {tool}" + (f" on {Path(fp).name}" if fp else "")


def _pm_autoprovision(ctx: Path, sid: str, payload: dict) -> str:
    """PM auto-provision (CELE-t211, contract t203 §1.3/§5): put a card-less working coder on the
    board without blocking it. Claim path first — a todo card already ASSIGNED to this session
    (owner == sid6: staged for it, but the coder started working instead of pasting the marker);
    else CREATE a fresh `auto`-tagged card titled from the session's last recorded prompt. Either
    way the card goes doing, the §1.3 agent_sessions link is written at bind time, and the returned
    model-facing provenance line becomes the allow reason ('' = could not provision — the caller
    falls back to the deny). An assigned card keeps its groomed autonomy; only an ungroomed one
    (which under opencode would deny everything, t212) gets the default grants."""
    sid = (sid or "").strip()
    if not sid:
        return ""
    owner = sid[:6]
    tasks = _load_tasks(ctx)
    t = next((x for x in tasks if (x.get("owner") or "").strip() == owner
              and (x.get("state") or "") == "todo"), None)
    created = t is None
    if created:
        stamp = now_iso()
        t = {"id": _next_task_id(tasks), "title": _autoprovision_title(ctx, sid, payload),
             "state": "todo", "owner": owner, "tags": ["auto"], "blocked_by": [], "phase": "",
             "stop": DEFAULT_STOP, "autonomy": _autoprovision_grants(), "progress": 0,
             "jira": "", "github": "", "created": stamp, "updated": stamp, "subtasks": [],
             "notes": ("Auto-provisioned by the Celeborn PM (CELE-t211): this session began "
                       "substantive work with no board card. Groom me — sharpen the title, set a "
                       "real Stop condition, add blocked_by edges if this work depends on other "
                       "cards. git-write is NOT granted (t203 §3.4): the operator opts in with "
                       "--autonomy research,edits,tests,commit.")}
        tasks.append(t)
    if not t.get("autonomy"):
        t["autonomy"] = _autoprovision_grants()
    t["state"] = "doing"
    t["updated"] = now_iso()
    _progress_stamp_claim(ctx, t)                      # CELE-t161: engine floor 5 on going doing
    tasks = _bring_to_state_front(tasks, t["id"])
    _save_tasks(ctx, tasks, autopush_ids=[t["id"]])
    _record_agent_session(ctx, sid, owner, [t["id"]])  # the §1.3 binding rule — written at bind time
    try:
        __import__("celeborn_sync").schedule_agents_push(ctx, min_interval_s=0)
    except Exception:  # noqa: BLE001
        pass
    disp = _display_tid(ctx, t["id"])
    verb = (f"auto-provisioned [{disp}] \"{t['title']}\" and claimed it" if created
            else f"auto-claimed [{disp}] \"{t['title']}\" — the card already assigned to this session")
    return (
        f"🏹 Celeborn PM {verb} for you (owner @{owner}, CELE-t211): you began substantive work "
        f"with no board card, so the PM put the board right instead of blocking you. Groom the card "
        f"as you work: `celeborn tasks edit {t['id']} --title \"…\" --stop \"…\"` (plus --blocked-by "
        f"edges if this depends on other cards), and `celeborn ship {t['id']}` when done. Autonomy: "
        f"{','.join(t['autonomy'])} — git-write (commit) stays OFF until the operator grants it."
    )


def _card_gate_pre_tool_use(payload: dict, project_dir: str, harness: str = "") -> str:
    """PreToolUse card-less-work gate (CELE-t131 lever 2, widened in CELE-t134). Hard-DENY any tool in
    `_CARD_GATED_TOOLS` (edits + web research + subagent spawn) when this session owns no live board card
    and the board has an open one to claim. No-op outside a Celeborn project, when the bypass env is
    armed, or when the board is empty (nothing to claim). Unlike the redirect guard this needs the
    `.context/` — but only after the cheap tool-name filter, so ungated calls still return in
    microseconds.

    Under the OpenCode harness (CELE-t211) a card-less coder is AUTO-PROVISIONED instead of denied —
    including on an empty board, where other harnesses stay exempt ('add one'): with no human in the
    loop, silence would just mean off-board work. Subagent sessions are excluded (contract t203 §1.2:
    child sessions never register identity or claim cards — the plugin marks them `child`); they keep
    the deny."""
    if (payload.get("tool_name") or "") not in _CARD_GATED_TOOLS:
        return ""
    ctxdir = find_context_root(Path(project_dir))
    if ctxdir is None:
        return ""                                      # not a Celeborn project — never gate
    if not _card_gate_enabled(ctxdir):
        return ""                                      # card_gate turned off in Settings (CELE-t352)
    sid = payload.get("session_id") or ""
    status = _card_gate_status(ctxdir, sid)
    if status == "ok":
        return ""
    if harness == "opencode" and not payload.get("child_session"):
        try:
            notice = _pm_autoprovision(ctxdir, sid, payload)
        except Exception:  # noqa: BLE001
            notice = ""                                # provisioning must never crash the gate — deny instead
        if notice:
            # The provisioned card's own autonomy grants still bound THIS call (a pre-assigned
            # narrowly-groomed card must not widen just because the gate claimed it) — deny wins,
            # and it already names the card and the grooming command.
            autonomy = _autonomy_gate_pre_tool_use(payload, project_dir, harness)
            if autonomy:
                return autonomy
            return json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": notice,
            }})
    if status != "gated":
        return ""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _cardless_deny(sid),
    }})


# --------------------------------------------------------------------------- per-card autonomy gate (t212)
#
# WS-B of the autonomy program (contract t203 §3.4): each card may carry an `autonomy` grant list —
# a subset of AUTONOMY_GRANTS, set on the kanban at GROOMING time — that bounds what its owning
# session may do without a human. The gate is DENY-ONLY, deliberately: it never emits "allow", so a
# plain-text board field can bound work below the harness's own permission layer but can never
# bypass it (an agent editing its own card's grants gains nothing a harness prompt wouldn't ask).
# Silence = granted: in Claude Code the normal permission prompts still apply; in OpenCode — where
# the plugin's tool.execute.before throw IS the whole permission system (no OS prompts) — silence
# is what makes a groomed card runnable overnight.
#
# Absent field (per §3.4, "most restrictive interpretation"): under the opencode harness an
# ungroomed card grants NOTHING — every gated class denies until grooming grants it. Under Claude
# (and every prompting harness) an absent field keeps the gate silent so the harness's own
# permission system governs, exactly as before this gate existed — "risky ops confirmed" is the
# operator answering the prompt. Unknown tokens in a hand-edited field grant nothing (same clause);
# `commit` (= git-write, the headline toggle) is never implied by any other grant.
AUTONOMY_GRANTS = ("research", "edits", "tests", "commit")
# `commit` is the one git-write grant — deliberately amber (`risk`) and never on by default (t203 §3.4).
_AUTONOMY_GRANT_RISK = {"commit"}
# Night-question behaviour: what a raised hand does while the operator sleeps (t144 mockup :559-568).
AUTONOMY_NIGHT_QUESTIONS = (
    ("queue", "Queue for the morning report"),
    ("notify", "Push notification, wait 10 min, then queue"),
    ("block", "Block the card until answered"),
)
# The head-elf model that stamps READY / dispatches / raises hands (t144 mockup :577-586).
AUTONOMY_PM_MODELS = (
    # Key is a stored settings enum (stable across configs); the label tracks the real weave tag —
    # Pippin · PM = qwen3:4b-instruct (CELE-t373; the old `qwen-4b` alias is retired, CELE-t374).
    ("qwen-4b-local", "Pippin · qwen3:4b-instruct · local"),
    ("haiku-hosted", "Haiku 4.5 · hosted"),
    ("off", "Off — I dispatch manually"),
)
AUTONOMY_ELVES_MIN, AUTONOMY_ELVES_MAX = 1, 12
# Ship pre-flight is the spine's load-bearing discipline: a card can't ship until the next spine head is
# startable verbatim. Deliberately LOCKED on — surfaced read-only, never a board-toggleable field (t144).
AUTONOMY_SHIP_PREFLIGHT_LOCKED = True
AUTONOMY_SHIP_PREFLIGHT_WHY = "Always on — the spine depends on it"
# Tool-name → grant class, lowercase so one table covers Claude tool names (Edit/WebFetch/Task…)
# and OpenCode's built-ins (edit/write/patch/webfetch/task — plan §2.1 normalization table).
_AUTONOMY_TOOL_CLASS = {
    "edit": "edits", "write": "edits", "notebookedit": "edits", "patch": "edits",
    "webfetch": "research", "websearch": "research", "task": "research", "agent": "research",
}
# Bash test-runner shapes → the `tests` grant. Non-exhaustive by design: a miss just means the
# command falls through to the harness's own permission layer, never a silent over-grant.
_TEST_RUN_RE = re.compile(
    r"(?:^|[;&|(\s])"
    r"(?:pytest\b|py\.test\b"
    r"|python\d?(?:\.\d+)?\s+-m\s+(?:pytest|unittest|tox|nose2?)\b"
    r"|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?tests?\b\S*"
    r"|(?:deno|cargo|go|dotnet|swift)\s+test\b"
    r"|jest\b|vitest\b|mocha\b|ava\b|tox\b|nox\b|rspec\b|phpunit\b|ctest\b"
    r"|playwright\s+test\b|cypress\s+run\b"
    r"|mvn\s+(?:\S+\s+)*test\b|gradlew?\s+(?:\S+\s+)*test\b"
    r"|make\s+(?:\S+\s+)*(?:test|check)\b"
    r")", re.I)
# One git invocation: optional pre-subcommand global options (`-C <dir>`, `-c k=v`, `--git-dir=…`,
# bare `--flags`), then the subcommand, then the rest of that command segment.
_GIT_INVOCATION_RE = re.compile(
    r"\bgit"
    r"((?:\s+(?:-[Cc]\s+\S+|--(?:git-dir|work-tree|namespace|exec-path)(?:=\S+|\s+\S+)?|--?[A-Za-z-]+))*)"
    r"\s+([a-z][a-z-]*)"
    r"([^;&|\n]*)")
# Subcommands that mutate history / the remote / the working tree whatever their arguments. `add`
# rides along: staging exists only to feed a commit. fetch/status/diff/log/show/… are absent — reads
# never gate, so a no-`commit` card can always orient.
_GIT_WRITE_UNCONDITIONAL = frozenset((
    "commit", "push", "pull", "merge", "rebase", "cherry-pick", "revert", "reset", "restore",
    "switch", "checkout", "clean", "am", "apply", "add", "rm", "mv",
    "filter-branch", "update-ref", "replace"))
# Dual-mode subcommands: WRITE unless the rest of the segment starts with a read form. First-token
# heuristic on purpose — ambiguity resolves toward write (§3.4 most-restrictive), never toward a
# silent pass. notes/worktree/submodule are deliberately unlisted (rare, low-harm); the harness
# permission layer still sees them.
_GIT_READ_REST = {
    "stash":  re.compile(r"^\s+(?:list|show)\b"),
    "tag":    re.compile(r"^\s*$|^\s+(?:-l\b|-n\d*\b|--list\b|--contains\b|--points-at\b|--merged\b|--no-merged\b|--sort|--format)"),
    "branch": re.compile(r"^\s*$|^\s+(?:-[arv]+\b|-l\b|--list\b|--show-current\b|--contains\b|--merged\b|--no-merged\b|--points-at\b|--sort|--format|--column|--color)"),
    "remote": re.compile(r"^\s*$|^\s+(?:-v\b|show\b|get-url\b)"),
    "config": re.compile(r"^\s+(?:--get\b|--get-all\b|--get-regexp\b|--list\b|-l\b|--show-origin\b)"),
}


def _is_git_write(cmd: str) -> bool:
    """True when any git invocation in `cmd` mutates history, the remote, config, or the working
    tree — the `commit` grant's scope (the git-write toggle, off by default). Quoted-string false
    positives are accepted: the gate errs restrictive, and the deny message names the exact grant."""
    for m in _GIT_INVOCATION_RE.finditer(cmd or ""):
        verb, rest = m.group(2), m.group(3)
        if verb in _GIT_WRITE_UNCONDITIONAL:
            return True
        read_re = _GIT_READ_REST.get(verb)
        if read_re is not None and not read_re.match(rest):
            return True
    return False


def _bash_autonomy_class(cmd: str) -> str:
    """Grant class of one Bash command: git-write → 'commit' (checked first — the more restrictive
    class wins a compound like `git commit && npm test`), test-runner → 'tests', anything else → ''
    (never gated, so `celeborn`/orient/read commands always flow)."""
    if not cmd:
        return ""
    if _is_git_write(cmd):
        return "commit"
    if _TEST_RUN_RE.search(cmd):
        return "tests"
    return ""


def _autonomy_class(payload: dict) -> str:
    """The AUTONOMY_GRANTS class this tool call needs, or '' when it is never autonomy-gated."""
    tool = (payload.get("tool_name") or "").strip().lower()
    cls = _AUTONOMY_TOOL_CLASS.get(tool)
    if cls:
        return cls
    if tool == "bash":
        return _bash_autonomy_class((payload.get("tool_input") or {}).get("command") or "")
    return ""


def _session_live_cards(ctx: Path, sid: str) -> list[dict]:
    """The live (doing) cards this session is attributable to — the recorded session→card link
    first (authoritative, single card), else every doing card owned by the session's short id.
    Same resolution as `_session_owns_live_card`, but returning the cards themselves."""
    tasks = _load_tasks(ctx)
    tid = ((_load_metrics(ctx).get("agent_sessions") or {}).get((sid or "").strip()) or {}).get("task") or ""
    if tid.strip():
        bare = _split_qualified_tid(tid.strip())[1]
        card = next((t for t in tasks if t["id"] == bare and t.get("state") == "doing"), None)
        if card is not None:
            return [card]
    short = (sid or "").strip()[:6]
    if not short:
        return []
    return [t for t in tasks if (t.get("owner") or "").strip() == short and t.get("state") == "doing"]


def _autonomy_deny(ctx: Path, card: dict, cls: str, grants: frozenset, groomed: bool) -> str:
    """Corrective deny for an out-of-grant op: name the card, what it does grant, and the exact
    grooming command that widens it — an operator decision by design, so the message says so."""
    disp = _display_tid(ctx, card["id"])
    have = ", ".join(g for g in AUTONOMY_GRANTS if g in grants) or "none"
    want = ",".join(g for g in AUTONOMY_GRANTS if g in grants or g == cls)
    head = (f"🏹 Celeborn autonomy gate: [{disp}] does not grant `{cls}` — this card pre-authorizes: {have}."
            if groomed else
            f"🏹 Celeborn autonomy gate: [{disp}] carries no autonomy grants, and under this harness the "
            f"gate is the whole permission system — an ungroomed card runs most-restrictive (t203 §3.4).")
    commit_note = (" git-write stays OFF by default: `commit` is never implied by any other grant."
                   if cls == "commit" else "")
    return (f"{head}{commit_note} The autonomy bound is set at grooming time on the kanban — the OPERATOR "
            f"widens it with `celeborn tasks edit {card['id']} --autonomy {want}`; do not self-grant to "
            f"get past this gate. Read/orient and the `celeborn` CLI are never gated.")


def _autonomy_gate_pre_tool_use(payload: dict, project_dir: str, harness: str = "") -> str:
    """PreToolUse per-card autonomy gate (CELE-t212, contract t203 §3.4). DENY a tool call whose
    grant class falls outside the owning card's `autonomy` grants; silent otherwise (silence defers
    to the harness's own permission layer — this gate never auto-allows). Card-less sessions are the
    t131 gate's concern; no card resolved here means nothing to bound. Cheap classification runs
    first, so never-gated calls skip the `.context/` lookup entirely."""
    cls = _autonomy_class(payload)
    if not cls:
        return ""
    ctxdir = find_context_root(Path(project_dir))
    if ctxdir is None:
        return ""                                      # not a Celeborn project — never gate
    cards = _session_live_cards(ctxdir, payload.get("session_id") or "")
    if not cards:
        return ""
    fielded = [t for t in cards if t.get("autonomy")]
    if not fielded:
        if harness != "opencode":
            return ""          # no bound set → the harness's own permission prompts govern (pre-t212 behavior)
        grants, card, groomed = frozenset(), cards[0], False   # OpenCode has no prompts: ungroomed ⇒ nothing granted
    else:
        # Multiple attributable groomed cards (owner-match fallback only): most-restrictive ⇒ intersection.
        grants = frozenset.intersection(
            *(frozenset(g for g in t["autonomy"] if g in AUTONOMY_GRANTS) for t in fielded))
        card, groomed = fielded[0], True
    if cls in grants:
        return ""                                      # granted — fall through, never auto-allow
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": _autonomy_deny(ctxdir, card, cls, grants, groomed),
    }})


def dispatch_hook(event: str, payload: dict, project_dir: str, harness: str = "") -> str:
    """Run one hook event in-process and return the text to write to stdout ("" = emit nothing).

    The single per-turn logic module. `project_dir` is where to start the `.context/` search (the
    host's CLAUDE_PROJECT_DIR / cwd). Resolution mirrors the old bash hooks exactly, including the
    hybrid sink (capture/statusline fall through to the global ~/.context when outside a repo) and the
    no-op-outside-.context safety property that makes the hooks safe to enable globally.

    `harness` is cmd_hook's resolved harness name ("" = Claude default) — payload shape stays frozen
    (t203 §7); the rare event whose SEMANTICS differ per harness (pre-compact's metric) branches on
    this instead of sniffing the payload."""
    payload = payload or {}
    # PreToolUse fires on EVERY tool call. The Bash redirect guard (t101) runs first — cheap and
    # `.context/`-free, so the overwhelmingly common case returns in microseconds. If it has no opinion,
    # fall through to the card-less-work gate (CELE-t131, widened in CELE-t134) on the gated tool set
    # (edits + web research + subagents), which does need the `.context/` (gated behind its own tool-name
    # filter so ungated calls — Bash/Read included — never pay for the lookup).
    if event == "pre-tool-use":
        redirect = _pre_tool_use_decision(payload)
        if redirect:
            return redirect
        # Publish guard (CELE-t191): after the redirect guard has no opinion, refuse a publish/release
        # action targeting a server:private/oss:* facet (§6). Its own cheap publish-action regex gates the
        # registry lookup, so a non-publish Bash call returns in microseconds, same as the redirect guard.
        publish = _publish_guard_decision(payload, project_dir)
        if publish:
            return publish
        # Work resumed (CELE-t195): a tool call means this session is actively working again, so any
        # "awaiting you" alert on its card (permission / idle / stopped) is stale. Clear it here — the
        # earliest resume signal — so the badge drops within seconds even when the user unblocks
        # WITHOUT a new prompt (permission grant, AskUserQuestion answer). Fast-guarded; best-effort.
        _clear_alert_on_activity(project_dir, payload.get("session_id") or "")
        cardless = _card_gate_pre_tool_use(payload, project_dir, harness)
        if cardless:
            return cardless
        # Last in the chain (CELE-t212): a session that DOES own a card is bounded by that card's
        # `autonomy` grants. Deny-only — the shell-hygiene and publish guards above stay senior, and
        # a granted class falls through to the harness's own permission layer, never an auto-allow.
        return _autonomy_gate_pre_tool_use(payload, project_dir, harness)
    sid = payload.get("session_id") or ""
    tp = payload.get("transcript_path") or ""
    ctxdir = find_context_root(Path(project_dir))      # the .context/ dir, or None
    proj = str(ctxdir.parent) if ctxdir is not None else str(Path(project_dir))

    if event == "session-start":
        if ctxdir is None:
            return ""                                  # not a Celeborn project — do nothing
        # A `/clear` mints a new session id; inherit the cleared session's card attribution so the
        # active-agents chip keeps showing the same agent on the same DOING card (CELE-t131) instead of
        # a fresh unowned chip. Gated to source="clear"; best-effort + fail-safe.
        if (payload.get("source") or "") == "clear":
            try:
                if _consume_clear_carryover(ctxdir, sid):
                    __import__("celeborn_sync").schedule_agents_push(ctxdir, min_interval_s=0)
            except Exception:
                pass
        _hook_run(cmd_record, path=proj, event="orient", session=sid, tokens=None)
        # Self-register into the fleet (CELE-t124): an active project must show up in the fleet
        # economics even if it was never `fleet add`ed by hand. Best-effort, quiet, idempotent.
        _fleet_autoregister(ctxdir)
        # Ensure-on-orient: bring the kanban viewer up on its resolved port if it's down. Detached and
        # best-effort — swallow anything so a launch hiccup never breaks rehydration.
        try:
            ensure_board(ctxdir)
        except Exception:
            pass
        # Keep the Matt Pocock skills current: a detached, throttled (weekly) background refresh when due.
        # Claude-only + best-effort — never blocks or breaks orient (t116).
        try:
            _ensure_skills_fresh(ctxdir)
        except Exception:
            pass
        # Cheap install-integrity check: if the installed core modules were edited in place, lead the
        # Orient load with a one-line self-diagnosing notice. Best-effort (never raises); silent on a
        # clean or source/dev install.
        # Lead the Orient load with at most two one-line self-diagnosing notices: the install-integrity
        # check, then the skill advisor (t70) — friction/quality recommendations the agent can act on,
        # throttled to one per session. Both are best-effort and silent when there's nothing to say.
        notices = [_integrity_notice(), _advisor_notice(ctxdir, sid), _product_banner(ctxdir), SHELL_HYGIENE_RULE]
        head = "\n\n".join(n for n in notices if n)
        head = (head + "\n\n") if head else ""
        return head + "## Celeborn memory (Orient load)\n\n" + _hook_run(cmd_status, path=proj, full=False)

    if event == "pre-compact":
        if ctxdir is None:
            return ""
        # Claude Code has no post-compaction hook, so the metric is recorded here, at "imminent".
        # OpenCode DOES tell us when a compaction actually lands (`session.compacted` → the plugin
        # runs `celeborn record compaction`, CELE-t142), so there we defer to that: recording at
        # compacting time too would double-count, and would count compactions that abort.
        if harness != "opencode":
            _hook_run(cmd_record, path=proj, event="compaction", session=None, tokens=None)
        # Pre-compaction panic-save (t36): snapshot the authored tiers to a restore point NOW —
        # deterministically, before the window is summarized — so survival is a felt, recoverable
        # artifact and not just a nag. Best-effort: a hiccup here must never break compaction.
        saved_line = ""
        try:
            info = _do_panic_save(ctxdir, reason="compaction", session=sid)
            m = _load_metrics(ctxdir)
            m["panic_saves"] = int(m.get("panic_saves", 0) or 0) + 1
            saved_line = _panic_save_line(info)
            # The snapshot + stdout line below happen every time. We do NOT raise a native OS dialog
            # here: focus-stealing modal alert windows were repeatedly flagged as annoying (t47/t50/t62)
            # and have been removed — the reassurance rides the returned stdout line instead.
            _save_metrics(ctxdir, m)
        except Exception:
            pass
        return (saved_line + "\n\n" + PRECOMPACT_MSG) if saved_line else PRECOMPACT_MSG

    if event == "session-end":
        if ctxdir is None:
            return ""
        # A session ending — `/clear` (reason="clear"), logout, or exit — should drop its active-agents
        # chip from the board NOW, not linger for the 30-min mtime window (CELE-t131). `/clear` opens a
        # fresh session id, so the old transcript keeps a recent mtime and would otherwise ghost. We
        # tombstone the ending session and force a hosted refresh so celeborncode.ai prunes it too.
        try:
            # On a `/clear`, stash this session's card attribution FIRST (before the tombstone drops the
            # link) so its continuation can inherit it (CELE-t131). Gate to "clear" or a missing reason
            # (robust if the host omits the field) — but NOT an explicit non-clear end (logout/exit), so
            # a closed terminal can't bleed its card onto someone else's later /clear.
            if (payload.get("reason") or "") in ("", "clear"):
                _stash_clear_carryover(ctxdir, sid)
            # A stopped/idle/permission alert dies with its session (CELE-t195): the window is gone,
            # so it awaits nothing. Clear the record now so .alerts.json doesn't accumulate the stale
            # badges that _live_alerts otherwise has to filter (belt-and-suspenders with that guard).
            _tid = _session_task_id(ctxdir, sid)
            if _tid:
                _clear_alert(ctxdir, _tid)
            if _mark_session_ended(ctxdir, sid):
                __import__("celeborn_sync").schedule_agents_push(ctxdir, min_interval_s=0)
        except Exception:
            pass
        return _hook_run(cmd_handoff, path=proj)

    if event == "stop":
        # Idle-Stop alert (CELE-t169): the turn ended and the session's DOING card is unfinished, so
        # coding progress has paused awaiting the user's next direction. Raise a low-severity "stopped"
        # alert on the card — it clears the instant the user replies (user-prompt-submit below). A card
        # that was just shipped is no longer doing, so `_session_task_id` returns "" and no alert fires.
        # Runs BEFORE the transcript gate: a transcript-less harness (OpenCode's session.idle,
        # CELE-t139) still means "turn ended, card unfinished" and the board should show it.
        if ctxdir is not None:
            try:
                tid = _session_task_id(ctxdir, sid)
                if tid:
                    _set_alert(ctxdir, tid, "stopped", "Turn ended — awaiting your direction.", sid)
                    _refresh_alerted_card(ctxdir, tid)
                    __import__("celeborn_sync").schedule_agents_push(ctxdir, min_interval_s=0)
            except Exception:  # noqa: BLE001
                pass
        # Hybrid sink: a repo's own .context/ when inside one, else the global ~/.context — so no
        # session goes unrecorded. Needs a transcript to read; without one there's nothing to capture
        # (OpenCode's own transcript ingestion is P4 — CELE-t141), so the alert above is the whole effect.
        if not tp:
            return ""
        return _hook_run(cmd_capture, path=proj, transcript=tp, session=sid,
                         quiet=True, note=True, global_=(ctxdir is None))

    if event == "post-tool-use":
        # Transcript-less touch + activity (P4, CELE-t141). Claude Code never wires this — its
        # transcript capture already records every tool call, and touches are the agent's own
        # protocol move. A transcript-less harness (OpenCode's tool.execute.after / file.edited)
        # reports each completed call here instead: a file mutation auto-registers the touch (board
        # active-file chips), and the call folds into the rolling activity window. No model-facing
        # output (PostToolUse stdout reaches the model only, and there is nothing to say).
        if ctxdir is None:
            return ""
        tool = (payload.get("tool_name") or "").strip()
        inp = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        fp = str(inp.get("file_path") or inp.get("filePath") or inp.get("notebook_path") or "").strip()
        if tool in _FILE_TOOLS and fp:
            _auto_touch_for_session(ctxdir, sid, fp)
        try:
            _record_tool_activity(ctxdir, sid, tool, inp)
        except Exception:  # noqa: BLE001
            pass
        return ""

    if event == "notification":
        # Claude Code fires Notification when it needs tool-use permission or the prompt has been idle
        # ~60s — exactly "agentic progress is blocked, the user's input is needed" (CELE-t169). Raise the
        # matching alert on the session's DOING card so it surfaces on the board (locally + hosted). The
        # message classifies the kind; it clears when the user next replies. Best-effort — never break.
        if ctxdir is None:
            return ""
        try:
            tid = _session_task_id(ctxdir, sid)
            if tid:
                msg = (payload.get("message") or "").strip()
                low = msg.lower()
                kind = "permission" if ("permission" in low or "approve" in low or "waiting for your" in low) else "idle"
                _set_alert(ctxdir, tid, kind, msg or ("Needs permission to proceed." if kind == "permission"
                                                      else "Waiting for your input."), sid)
                _refresh_alerted_card(ctxdir, tid)
                __import__("celeborn_sync").schedule_agents_push(ctxdir, min_interval_s=0)
        except Exception:  # noqa: BLE001
            pass
        return ""

    if event == "statusline":
        # cmd_statusline handles its own global fallback; pass the start dir and a transcript if any.
        return _hook_run(cmd_statusline, path=proj, transcript=(tp or None), session=sid)

    if event == "user-prompt-submit":
        if ctxdir is None:
            return ""
        # Only the context-size nudge needs a transcript; heartbeat, claim-on-receipt/mention, the
        # card-gate directive, and the progress engine all run without one — so a transcript-less
        # harness (OpenCode's chat.message, CELE-t139) still gets the full per-turn envelope.
        # Turn boundary for the transcript-less activity record (P4, CELE-t141): open a fresh
        # window entry now so this turn's post-tool-use reports fold into one bounded fact row.
        if harness == "opencode":
            try:
                _record_turn_prompt(ctxdir, sid, payload.get("prompt") or "")
            except Exception:  # noqa: BLE001
                pass
            # A human turn in OpenCode IS a human↔OpenCode interaction (CELE-t216): enqueue a PM wake so
            # the next march re-reads a board the human may have just steered. Best-effort.
            try:
                _pm_wake_enqueue(ctxdir, "opencode", "user turn")
            except Exception:  # noqa: BLE001
                pass
        # Resume clears the block (CELE-t169): the user has replied, so any permission/idle/stopped
        # alert on this session's DOING card is stale — drop it and refresh the card. Best-effort.
        try:
            _tid = _session_task_id(ctxdir, sid)
            if _tid and _clear_alert(ctxdir, _tid):
                _refresh_alerted_card(ctxdir, _tid)
                __import__("celeborn_sync").schedule_agents_push(ctxdir, min_interval_s=0)
        except Exception:  # noqa: BLE001
            pass
        # Per-turn board re-ensure (CELE-t99 safety net): the user taking a turn is a good proxy for
        # "the board tab is open", so revive a downed board here too — covers the gap if even the
        # supervisor was killed. Cheap (~150ms probe; relaunch is detached) and strictly best-effort:
        # never delay or break a turn.
        try:
            ensure_board(ctxdir)
        except Exception:                              # noqa: BLE001
            pass
        # /clear nudge + context-pressure warnings (CELE-t207). Thresholds resolve inside
        # cmd_remind (.celebornrc context_soft_tokens/context_hard_tokens, else band.ts defaults).
        # With a transcript (Claude Code) the live size is estimated from it; without one
        # (OpenCode), the session's capture cursor — fed by `record tokens` (t205) — supplies the
        # real window, so the warning rides the same per-turn envelope into the TUI.
        if tp:
            nudge = _hook_run(cmd_remind, path=proj, transcript=tp, tokens=None, every=50_000,
                              last=None, auto=False, force=False, soft_limit=None, hard_limit=None,
                              session=None, clear_cmd="/clear").strip()
        elif sid:
            nudge = _hook_run(cmd_remind, path=proj, transcript=None, tokens=None, every=50_000,
                              last=None, auto=False, force=False, soft_limit=None, hard_limit=None,
                              session=sid, clear_cmd="/clear").strip()
        else:
            nudge = ""
        heartbeat = _hook_run(cmd_heartbeat, path=proj, session=sid).strip()
        # Session-aware drain (CELE-t213): the session id rides along so this coder also drains the
        # queue a PM `dispatch` staged to its 6-char handle — without it, a session-addressed
        # hand-off would sit undelivered forever ($CELEBORN_AGENT is a name, not a session).
        handoff = _hook_run(cmd_outbox, path=proj, outbox_cmd="drain", session=sid).strip()
        # Claim-on-receipt: if a card marker rides in the prompt text OR the drained hand-off, this
        # session claims it — owner ← me, TODO → DOING. The act of receiving the card is the
        # assignment; the human chose *which* model by choosing *which* window to paste into, and a
        # dispatched brief arriving via the outbox is the same receipt (CELE-t213) — so drain →
        # claim → §1.3 agent_sessions link all land in the coder's first turn after a dispatch.
        slug = project_slug(ctxdir) if ctxdir is not None else ""
        prompt_text = payload.get("prompt") or ""
        refs, rejects = _find_card_refs(prompt_text + ("\n" + handoff if handoff else ""),
                                        expected_slug=slug or None)
        claim = _hook_run(cmd_claim, path=proj, ids=refs, by=None, session=sid).strip() if refs else ""
        # Prose claim-on-mention (CELE-t131): no pasted marker, but the human named a project-qualified
        # card in prose ("work on CELE-t131"). An explicit opening mention is a strong, intentional
        # signal — treat it like a paste and CLAIM the card: owner ← this session's short id (the
        # session IS the agent's name), TODO → DOING. The session-id owner holds no other cards, so the
        # one-in-flight preflight never blocks it. Vacuum-fill — only when this session has no live task
        # yet — so a later casual mention of another card can't thrash the board off your current work.
        if not refs and not rejects and not _session_has_task(ctxdir, sid):
            open_ids = {t["id"] for t in _load_tasks(ctxdir)
                        if (t.get("state") or "") in ("todo", "doing")}
            prose = _find_prose_card_refs(prompt_text, expected_slug=slug or None, claimable_ids=open_ids)
            if prose:
                prose_claim = _hook_run(cmd_claim, path=proj, ids=prose[:1], by=None, session=sid).strip()
                if prose_claim:
                    claim = f"{claim}\n\n{prose_claim}".strip() if claim else prose_claim
                try:
                    __import__("celeborn_sync").schedule_agents_push(ctxdir, min_interval_s=0)
                except Exception:
                    pass
        if rejects:
            reject_blk = "[Celeborn card markers — project mismatch, not claimed:]\n" + "\n".join(rejects)
            claim = f"{claim}\n\n{reject_blk}".strip() if claim else reject_blk
        # Card-less-work gate (t131 lever 1): with no card claimed this turn and none already owned, but
        # open cards on the board, lead the envelope with a top-priority directive to claim one. Computed
        # AFTER the claim block so a paste/prose claim this turn (which now owns a card) suppresses it; a
        # hand-off is the user explicitly sending work, so it's exempt too.
        directive = (_cardless_directive(sid)
                     if not handoff and _card_gate_enabled(ctxdir)
                     and _card_gate_status(ctxdir, sid) == "gated" else "")
        # Progress engine (CELE-t161): tick THIS session's doing card off observable signals and, if the
        # bar is lagging, return a copy-pasteable nudge. Best-effort — never delay or break a turn.
        try:
            progress_nudge = _progress_hook(ctxdir, sid)
        except Exception:  # noqa: BLE001
            progress_nudge = ""
        # Auto-architecture-trace (CELE-t201): every few turns (and draining any manifest-edit trace note),
        # re-detect the stack and remap the hosted Stack when a new piece appears. Best-effort — never breaks.
        try:
            arch_notice = _maybe_arch_trace_on_turn(ctxdir)
        except Exception:  # noqa: BLE001
            arch_notice = ""
        # Blackboard intents (CELE-t303): warn THIS session before it commits a file a peer has
        # declared a planned commit on. The handle is the session short-id — the same key touches
        # and claims use. Best-effort — the blackboard must never break a turn.
        try:
            intents_note = _intent_overlap_notice(ctxdir, (sid or "")[:6])
        except Exception:  # noqa: BLE001
            intents_note = ""
        return _compose_user_prompt_envelope(heartbeat, nudge, handoff, claim, directive, progress_nudge, arch_notice,
                                             intents_note)

    if event == "post-edit":
        # Quality gate (t70 Phase 2), PostToolUse after Edit/Write. CHEAP check only: byte-compile an
        # edited .py for a syntax error / type-check the board — and, for a test-relevant edit, mark the
        # turn dirty so the full suite runs once on quality-stop. Surfaces failures, never blocks.
        if ctxdir is None:
            return ""
        ti = payload.get("tool_input") or {}
        fp = ti.get("file_path") or ti.get("path") or ""
        if not fp:
            return ""
        proj_root = Path(proj)
        try:
            rel = str(Path(fp).resolve().relative_to(proj_root.resolve()))
        except (ValueError, OSError):
            rel = fp
        # Auto-architecture-trace (CELE-t201): a dependency-manifest edit means a piece may have entered
        # the stack — trace NOW (bypassing the cadence) and remap. The note is stashed for the next
        # user-prompt-submit to surface (PostToolUse additionalContext reaches the model only). Best-effort.
        try:
            _maybe_arch_trace_on_edit(ctxdir, rel)
        except Exception:  # noqa: BLE001
            pass
        qcfg = _quality_config(ctxdir)
        gate = _quality_gate_for(rel, qcfg)
        if gate is None:
            return ""
        notice = None
        if gate == "test":
            m = _load_metrics(ctxdir)
            m["quality"] = dict(m.get("quality") or {}, dirty_session=(sid or "session"))
            _save_metrics(ctxdir, m)
            notice = _run_quality_cmd([sys.executable, "-m", "py_compile", str(proj_root / rel)], cwd=proj)
        elif gate == "type":
            notice = _run_quality_cmd(qcfg["type_cmd"], cwd=str(proj_root / qcfg["type_dir"]))
        if not notice:
            return ""
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": notice}})

    if event == "quality-stop":
        # The heavy half of the gate: run the full suite ONCE per turn, but only when a scripts/** or
        # tests/** edit marked the session dirty. A separate Stop group from `capture` so it never
        # collides with capture's systemMessage envelope. Surfaces failures, never blocks.
        if ctxdir is None:
            return ""
        suite = _maybe_run_suite_on_stop(ctxdir, sid)
        nudge = _review_nudge_on_stop(ctxdir, sid)   # Phase 3: review/security nudge when tree is dirty
        return "\n\n".join(p for p in (suite, nudge) if p)

    return ""


def _quality_config(ctx: Path) -> dict:
    """Quality-gate settings (t70 Phase 2). Defaults match this project (a unittest suite under tests/
    + a Next.js board type-checked with `tsc --noEmit`); override via a `quality: {...}` block in
    .celebornrc for a different layout. NEVER `next build` — it clobbers the live dev `.next`."""
    out = {
        "test_cmd": ["python3", "-m", "unittest", "discover", "-s", "tests"],
        "test_globs": ["scripts/", "tests/"],
        "test_suffixes": [".py"],
        "type_cmd": ["npx", "tsc", "--noEmit"],
        "type_dir": "board",
        "type_globs": ["board/"],
        "type_suffixes": [".tsx", ".ts"],
    }
    try:
        block = load_config(ctx).get("quality")
    except Exception:
        block = None
    if isinstance(block, dict):
        for k, v in block.items():
            if v is not None and k in out:
                out[k] = v
    return out


def _quality_gate_for(rel: str, qcfg: dict) -> str | None:
    """Which gate applies to an edited repo-relative path: 'test', 'type', or None."""
    rel = rel.replace("\\", "/").lstrip("./")
    if rel.endswith(tuple(qcfg["test_suffixes"])) and any(rel.startswith(g) for g in qcfg["test_globs"]):
        return "test"
    if rel.endswith(tuple(qcfg["type_suffixes"])) and any(rel.startswith(g) for g in qcfg["type_globs"]):
        return "type"
    return None


def _run_quality_cmd(cmd: list, cwd: str | None = None, timeout: int = 180) -> str | None:
    """Run a quality-gate command. Returns None when it PASSES (exit 0) — or when the tool is simply
    absent (e.g. no `npx`/`tsc`), so an unconfigured machine stays silent rather than nagging. Returns
    a one-block failure notice (the tail of its output) when it actually fails. Never raises."""
    import subprocess
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None                                  # tool not installed — skip quietly
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        return None
    out = ((r.stdout or "") + ("\n" if r.stdout and r.stderr else "") + (r.stderr or "")).strip()
    tail = "\n".join(out.splitlines()[-20:]) if out else "(no output)"
    return (f"🏹 Celeborn quality gate FAILED — `{' '.join(cmd)}`:\n{tail}\n"
            f"(surfaced, not blocking — fix before you call this change done.)")


def _maybe_run_suite_on_stop(ctx: Path, session: str | None) -> str:
    """If a test-relevant file was edited this turn (post-edit marked the session dirty), run the full
    suite ONCE and clear the marker. Returns a surfaced notice on failure, else "". Best-effort: clears
    the marker BEFORE running so a crash can't loop, and never raises into the hook."""
    try:
        m = _load_metrics(ctx)
        q = m.get("quality") or {}
        sess = session or "session"
        if (q.get("dirty_session") or None) != sess:
            return ""
        m["quality"] = dict(q, dirty_session=None)   # clear first — a mid-run crash must not re-trigger
        _save_metrics(ctx, m)
        return _run_quality_cmd(_quality_config(ctx)["test_cmd"], cwd=str(ctx.parent), timeout=600) or ""
    except Exception:
        return ""


# Stop-time quality recommendation (t70 Phase 3) — surfaced through the quality Stop group, so it
# reaches the model exactly when a turn of edits has finished. Only the change-derived quality signals
# fire here (permission friction is an orient concern); security review outranks code review.
_QUALITY_TRIGGERS = ("sensitive-changes", "uncommitted-changes")


def _review_nudge_on_stop(ctx: Path, session: str | None) -> str:
    """When the working tree has review-worthy changes at Stop, surface the top quality recommendation
    (security > code review) ONCE per session. Honors the advisor enable flag + dismissed intents.
    Best-effort and read-only (the dedupe marker aside); never raises into the hook."""
    try:
        if not _advisor_config(ctx)["enabled"]:
            return ""
        m = _load_metrics(ctx)
        q = m.get("quality") or {}
        sess = session or "session"
        if (q.get("review_nudged_session") or None) == sess:
            return ""                                  # already nudged this session — don't nag
        dismissed = set((m.get("advisor") or {}).get("dismissed") or [])
        adapter = active_adapter(ctx)
        sigs = [s for s in adapter.friction_signals(ctx, session)
                if s.get("signal") in _QUALITY_TRIGGERS]
        if not sigs:
            return ""
        intent = _signal_to_intent(sigs[0])
        if not intent or intent in dismissed:
            return ""
        text, _ch = adapter.render(intent, sigs[0])
        if not text:
            return ""
        m["quality"] = dict(q, review_nudged_session=sess)
        _save_metrics(ctx, m)
        return text
    except Exception:
        return ""


def cmd_hook(args):
    """`celeborn hook <event>` — the collapsed, in-process hook entry point (executable-app §3).

    Resolves the project dir (explicit --path wins, else $CLAUDE_PROJECT_DIR, else cwd), reads the
    host's JSON payload from stdin, and relays dispatch_hook()'s output to stdout. Never raises."""
    import os
    explicit = getattr(args, "path", None)
    if explicit and explicit != ".":
        project_dir = explicit
    else:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    payload = _read_hook_payload(getattr(args, "_stdin", None))
    event = args.event
    # OpenCode's plugin shells `celeborn hook <event> --harness opencode` with an OpenCode-shaped
    # payload; translate it into the Claude shape dispatch_hook already understands (CELE-t139).
    # Only the explicit flag or $CELEBORN_HARNESS (exported by the OpenCode plugin's shell-outs)
    # triggers translation — the rc `harness` pin deliberately does NOT, because running a
    # Claude-shaped payload through the translator would drop transcript_path/tool_name/prompt.
    harness = (getattr(args, "harness", None) or os.environ.get("CELEBORN_HARNESS") or "").strip().lower()
    if harness == "opencode":
        try:
            event, payload = _opencode_to_claude_shape(event, payload)
        except Exception:
            pass               # fail open — an untranslated payload degrades inside dispatch_hook
    out = dispatch_hook(event, payload, project_dir, harness=harness)
    if out:
        sys.stdout.write(out if out.endswith("\n") else out + "\n")


# The hook wiring Celeborn installs, in event order. Post-collapse (executable-app §3) every command
# is a single in-process `celeborn hook <event>` — no bash wrapper, no inline python3, no
# $CELEBORN_HOME. `wire` injects these (plus the statusLine) into settings.json. The legacy bash
# script names are kept only so re-running `wire` can detect and MIGRATE an old install in place.
WIRE_HOOKS = [
    ("SessionStart", "session-start", "session-start.sh"),
    ("UserPromptSubmit", "user-prompt-submit", "context-watch.sh"),
    ("PreCompact", "pre-compact", "pre-compact.sh"),
    ("SessionEnd", "session-end", "session-end.sh"),
    ("Stop", "stop", "capture.sh"),
    # Notification fires on a permission prompt / ~60s idle input — the blocked-progress alert
    # (CELE-t169). No legacy bash form ever existed; the third field is an inert migration label.
    ("Notification", "notification", "notification.sh"),
]
WIRE_STATUSLINE = {"type": "command", "command": "celeborn hook statusline"}
# Safety hooks `wire` installs alongside the five above (t101). Unlike WIRE_HOOKS these carry a
# Claude `matcher` (only run on the named tools) and have no legacy bash form. The single PreToolUse
# group fans out inside dispatch_hook: Bash → the cd+redirect guard (t101); Edit/Write/NotebookEdit →
# the card-less-work gate (t131). The matcher lists exactly those four so the hook never fires on
# Read/Grep/etc. (An older install wired with the Bash-only matcher is migrated up in `cmd_wire`.)
SAFETY_HOOKS = [
    ("PreToolUse", "pre-tool-use", "Bash|Edit|Write|NotebookEdit"),
]
# A legacy hooks/*.sh path in a wired command — what marks a group as ours but in the OLD bash form.
_LEGACY_HOOK_NAMES = ("statusline.sh", *(s for _, _, s in WIRE_HOOKS))


def _is_celeborn_command(text: str, event_token: str, legacy_script: str) -> bool:
    """True if `text` (a command string / serialized group) is a Celeborn hook for this event —
    either the new `celeborn hook <event>` form or the legacy `…/hooks/<script>.sh` form."""
    return f"hook {event_token}" in text or legacy_script in text


# t100 — the SAFE "big three" permission baseline merged into the GLOBAL Claude settings.json on
# `wire --global`. PROACTIVE + universal: it sets a safe floor *before* any approval history exists,
# so every user stops re-approving the same read-only commands out of the box. Complementary to
# `celeborn permissions --suggest|--apply` (which REACTIVELY learns wildcards from a user's own
# history). Read-only / trivially-reversible commands ONLY — NEVER sed/awk/redirection/rm or any
# non-localhost network reach. Emitted as Claude Code prefix wildcards: `Bash(<prefix>:*)`.
BASELINE_ALLOW_TOOLS = ["Read", "Glob", "Grep"]
BASELINE_BASH_PREFIXES = [
    # file / search (read-only)
    "grep", "rg", "find", "cat", "ls", "head", "tail", "wc", "tree", "file", "stat",
    "which", "pwd", "realpath", "dirname", "basename", "diff", "sort", "uniq",
    # git reads
    "git log", "git diff", "git show", "git status", "git branch", "git remote -v",
    # github reads
    "gh pr view", "gh pr list", "gh issue list", "gh repo view", "gh run list",
    # tool info
    "npm ls", "node --version", "python3 --version", "pip show", "jq", "env",
    # process / port (localhost only)
    "lsof", "ps", "curl -sS http://localhost",
]
BASELINE_DEFAULT_MODE = "acceptEdits"


def _baseline_allow_rules() -> list:
    """The full SAFE allow-list t100 ships: the read-only built-in tools, then each safe Bash prefix
    as a `Bash(<prefix>:*)` wildcard. One flat, order-stable list — the merge dedupes against
    whatever the user already has."""
    return list(BASELINE_ALLOW_TOOLS) + [f"Bash({pre}:*)" for pre in BASELINE_BASH_PREFIXES]


def _merge_permission_baseline(data: dict) -> dict:
    """Merge the t100 baseline into an already-loaded settings dict, IN PLACE and ask-wins. Returns a
    report {'added': [...], 'default_mode_set': bool}.

    Iron rules: never replace or reorder an existing entry; only APPEND allow-rules absent from BOTH
    `permissions.allow` and `permissions.deny` (deny wins); only set `defaultMode` when the user has
    not set it to anything. Re-running is a no-op (dedupe by exact string)."""
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    existing = set(allow)
    denied = set(perms.get("deny") or [])
    added = []
    for rule in _baseline_allow_rules():
        if rule in existing or rule in denied:
            continue                          # already allowed, or the user denied it → leave it
        allow.append(rule)
        existing.add(rule)
        added.append(rule)
    default_mode_set = False
    if "defaultMode" not in perms:            # ask-wins: never override a user's chosen mode
        perms["defaultMode"] = BASELINE_DEFAULT_MODE
        default_mode_set = True
    return {"added": added, "default_mode_set": default_mode_set}


# --------------------------------------------------------------------------- Danger Zone (t115)
# The FULL, intentionally-unsafe auto-allow spectrum, surfaced (and gated behind a typed confirmation)
# on the board Settings page. Arming this lets the agent run ANY command, read/write ANY file, reach
# ANY network host, and use every MCP tool — and `bypassPermissions` stops Claude asking about anything.
# Listed explicitly so the UI can enumerate exactly what gets turned on. NEVER applied without --yes.
DANGER_SPECTRUM = [
    "Bash(*)",                 # any shell command — incl. rm, git push, curl to any host
    "Read", "Edit", "Write",   # unrestricted file read + write
    "WebFetch", "WebSearch",   # arbitrary network access
    "mcp__*",                  # every MCP tool, including mutating ones
]
DANGER_DEFAULT_MODE = "bypassPermissions"   # Claude stops asking permission for ANYTHING
DANGER_CONFIRM_PHRASE = "DISABLE ALL SAFETY"


# --------------------------------------------------------------------------- per-rule permissions (t351)
# The board Settings "Permissions" panel (t144 shape) renders the live allow-list as grouped,
# individually-removable prefix-rule chips, plus a set of *Dangerous allows* (real settings.json allow
# entries that let an agent act OUTSIDE this machine) and one *locked* display-only exfiltration DENY.
# The new per-rule verbs below (`permissions --add/--rm/--set-mode`) back those chips.

# The `defaultMode` stances we expose in the mode selector, mapped to real Claude Code modes and
# honestly labelled (the panel writes exactly what it names — no aspirational relabelling).
PERMISSION_MODES = [
    {"value": "default", "label": "Ask when no rule matches",
     "hint": "The standard stance — allow/deny rules apply, otherwise Claude asks first."},
    {"value": "acceptEdits", "label": "Auto-allow file edits",
     "hint": "File edits auto-approve; Bash and anything outward-facing still prompts. Celeborn's safe baseline."},
    {"value": "plan", "label": "Plan mode (read-only)",
     "hint": "Claude proposes a plan and changes nothing until you approve it."},
    {"value": "bypassPermissions", "label": "Night-run autonomy — never prompt",
     "hint": "Full bypass: Claude asks about nothing. The same stance the Danger Zone arms."},
]
VALID_PERMISSION_MODES = {m["value"] for m in PERMISSION_MODES}
PERMISSION_RULE_KINDS = ("allow", "ask", "deny")

# Dangerous allows — real allow entries revocable in one click, but re-allowable only behind a typed
# phrase (the board enforces the phrase; the CLI just writes the rule once armed, mirroring danger-arm).
DANGEROUS_ALLOWS = [
    {"phrase": "ALLOW GIT PUSH",
     "rules": ["Bash(git push:*)"],
     "why": "Standalone pushes to any remote this repo knows. Compound commands (cmd && git push) never "
            "match — pushes run alone, visibly."},
    {"phrase": "ALLOW PUBLIC RELEASE",
     "rules": ["Bash(gh release:*)", "Bash(gh api:*)"],
     "why": "Cuts public releases and writes to GitHub via API (brew tap, scoop bucket). This is the public "
            "ship pipeline."},
]

# The exfiltration guard — display-only. Pushing this private repo's history to the public remote is
# hard-blocked by the auto-mode classifier at the hook layer regardless of allow rules, so it is NOT a
# togglable settings.json deny entry; the panel shows it locked to explain the guard.
EXFIL_DENY_LABEL = "DENY: private → public git push"
EXFIL_DENY_WHY = ("The exfiltration guard. Pushing this private repo's history to the public remote is "
                  "hard-blocked by the classifier regardless of allow rules — the public client ships by "
                  "wholesale copy + leak scan, never by push.")

# Prefixes that mean "runs code, but only to judge it" — their own chip group so a reader can see the
# test/typecheck surface distinctly from the read-only tools.
TEST_TYPECHECK_PREFIXES = [
    "python3 -m unittest", "python3 -m pytest", "python3 -m py_compile", "pytest",
    "npx tsc", "tsc", "npm test", "npm run test", "jest", "vitest", "deno test",
]

# Ordered chip groups for non-dangerous allow rules. `dangerous` rules are surfaced separately as
# risky-rule cards; anything unmatched falls to `other`.
PERMISSION_GROUPS = [
    {"key": "cli_readonly", "label": "Celeborn CLI & read-only",
     "hint": "The board's own tools and anything that can't mutate. Safe to auto-allow everywhere."},
    {"key": "tests", "label": "Tests & typecheck",
     "hint": "Runs code but only to judge it. Note: this suite has live side-effects — scope with care."},
    {"key": "file_scopes", "label": "File scopes",
     "hint": "Where Read / Edit / Write run without a prompt. Paths are prefix-matched, most specific wins."},
    {"key": "other", "label": "Other allows",
     "hint": "Everything else you've allowed. Prefix-matched, most specific wins."},
]

_BASH_RULE_RE = re.compile(r"^Bash\((.*)\)$")
_FILE_SCOPE_RE = re.compile(r"^(?:Read|Edit|Write|MultiEdit|NotebookEdit|Edit\+Write)\(.+\)$")


def _dangerous_allow_rule_set() -> set:
    return {r for grp in DANGEROUS_ALLOWS for r in grp["rules"]}


def _classify_allow_rule(rule: str) -> str:
    """Bucket a single allow rule into one of PERMISSION_GROUPS' keys, or 'dangerous'."""
    if rule in _dangerous_allow_rule_set():
        return "dangerous"
    m = _BASH_RULE_RE.match(rule)
    if m:
        body = m.group(1)
        body = body[:-2] if body.endswith(":*") else body
        for pre in TEST_TYPECHECK_PREFIXES:
            if body == pre or body.startswith(pre + " ") or body.startswith(pre):
                return "tests"
        for pre in ["celeborn", "scripts/celeborn.py", *BASELINE_BASH_PREFIXES]:
            if body == pre or body.startswith(pre + " ") or body.startswith(pre):
                return "cli_readonly"
        return "other"
    if _FILE_SCOPE_RE.match(rule):          # a Read/Edit/Write with a path argument → a file scope
        return "file_scopes"
    if rule in BASELINE_ALLOW_TOOLS:        # bare Read / Glob / Grep
        return "cli_readonly"
    if rule.startswith("mcp__"):
        return "cli_readonly"
    return "other"


# --------------------------------------------------------------------------- skill catalog (t115)
# The three groups the board Settings page renders: Celeborn's own bundled verbs (SKILL.md), the Claude
# slash-commands the t70 advisor points at, and the Matt Pocock skill suite that Celeborn installs
# default-on (https://github.com/mattpocock/skills).
CELEBORN_CORE_SKILLS = [
    {"name": "Claim", "command": "celeborn claim <id> --by <name>", "featured": True,
     "description": "The most-used verb: takes a TODO card, marks it DOING, and stamps you as owner so every "
                    "other agent sharing the board sees it on their next orient. Do this before any work."},
    {"name": "Orient", "command": "celeborn status",
     "description": "Cheap rehydration — prints the Hot tier (state headline, session focus, board, "
                    "recent activity) so a fresh thread knows where things stand without re-reading everything."},
    {"name": "Checkpoint", "command": "celeborn checkpoint",
     "description": "Safe writer for session.json — records focus/next-action/branch/status, stamps "
                    "updated_at, clips over-long fields, repairs a corrupt file. Use instead of hand-editing the JSON."},
    {"name": "Forget", "command": "celeborn archive",
     "description": "Moves old journal entries to cold storage so the Hot tier stays small and cheap to load every turn."},
    {"name": "Promote", "command": "celeborn promote --to learnings|durable",
     "description": "Distills knowledge up a tier (journal -> learnings -> durable docs) so hard-won facts survive and stop being rediscovered."},
    {"name": "Handoff", "command": "celeborn handoff",
     "description": "Writes a tiny resume prompt so a brand-new thread can pick up exactly where the last one died."},
]
RECOMMENDED_SKILLS = [
    {"name": "/code-review", "description": "Reviews the working diff for correctness bugs + cleanups; "
        "the advisor surfaces it when there are substantial uncommitted code changes."},
    {"name": "/verify", "description": "Runs the app and confirms a change actually behaves as intended — paired with /code-review before calling work done."},
    {"name": "/security-review", "description": "Security pass over pending changes (authn/authz, secret handling, input validation, injection/SSRF); advised when changes touch sensitive paths."},
    {"name": "/fewer-permission-prompts", "description": "Learns wildcard allow-rules from your own approval history so you stop re-approving the same commands. Pairs with `celeborn permissions`."},
    {"name": "/loop", "description": "Repeats a prompt/step on an interval for polling or recurring tasks; checkpoints via Celeborn so a restart resumes."},
    {"name": "/elves", "description": "Multi-batch autonomous development for long unattended runs — implements a plan in sprint-sized batches with PR-based review."},
]
# Matt Pocock's suite. Names match the skill directories the `skills` CLI installs under .claude/skills/.
MATTPOCOCK_SKILLS = [
    {"name": "ask-matt", "description": "Routes you to the right skill for your current situation."},
    {"name": "grill-with-docs", "description": "Detailed discovery interview that builds a project domain model and updates CONTEXT.md."},
    {"name": "triage", "description": "Moves issues through a state-machine triage workflow."},
    {"name": "improve-codebase-architecture", "description": "Identifies architectural improvements and presents a visual HTML report."},
    {"name": "to-issues", "description": "Breaks a plan into independently-grabbable issues using vertical slices."},
    {"name": "to-prd", "description": "Synthesizes a conversation into a publishable PRD."},
    {"name": "prototype", "description": "Builds throwaway prototypes to explore designs."},
    {"name": "diagnosing-bugs", "description": "Systematic debugging loop: reproduce, minimize, hypothesize, instrument, fix, test."},
    {"name": "tdd", "description": "Red-green-refactor loop for features and bug fixes."},
    {"name": "domain-modeling", "description": "Actively builds and sharpens domain models with terminology validation."},
    {"name": "codebase-design", "description": "Establishes shared vocabulary for designing deep, maintainable modules."},
    {"name": "grill-me", "description": "Comprehensive interview that resolves all decision branches before building."},
    {"name": "handoff", "description": "Creates compact handoff documents for agent-to-agent continuation."},
    {"name": "teach", "description": "Multi-session skill instruction using a directory as a stateful workspace."},
    {"name": "writing-great-skills", "description": "Reference guide for skill vocabulary and authoring principles."},
    {"name": "grilling", "description": "The reusable interview loop underlying grill-me and grill-with-docs."},
    {"name": "git-guardrails-claude-code", "description": "Blocks dangerous git operations via pre-execution hooks."},
    {"name": "migrate-to-shoehorn", "description": "Converts type assertions to @total-typescript/shoehorn."},
    {"name": "scaffold-exercises", "description": "Creates structured exercise directories."},
    {"name": "setup-pre-commit", "description": "Configures Husky hooks with linting and testing."},
    {"name": "setup-matt-pocock-skills", "description": "Configures the suite for your repo — run once after install."},
]
MATTPOCOCK_INSTALL_CMD = ["npx", "--yes", "skills@latest", "add", "mattpocock/skills"]
MATTPOCOCK_SOURCE = "https://github.com/mattpocock/skills"

# t116 — "stay updated": the suite is refreshed (re-pull @latest) on a weekly cadence. State lives GLOBAL
# (the skills install to ~/.claude/skills, so the throttle must be fleet-wide, not per-project) in
# ~/.config/celeborn/skills.json. The SessionStart hook fires a DETACHED, non-blocking refresh when due
# — never delaying orient. Opt out with autoupdate:false there, the CELEBORN_NO_SKILLS env, or
# `wire --no-skills`. Claude-only (the skills only exist under .claude/skills).
SKILLS_STATE_FILE = "skills.json"
SKILLS_REFRESH_DAYS = 7


def _skills_state_path() -> Path:
    return _config_dir() / SKILLS_STATE_FILE


def _load_skills_state() -> dict:
    p = _skills_state_path()
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_skills_state(data: dict) -> None:
    p = _skills_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")


def _skills_autoupdate_due(state: dict | None = None) -> bool:
    """True when a weekly Matt Pocock refresh is due. False when opted out, or refreshed within the
    window. A missing/garbled timestamp counts as due (first run)."""
    import os
    if os.environ.get("CELEBORN_NO_SKILLS"):
        return False
    state = _load_skills_state() if state is None else state
    if state.get("autoupdate") is False:
        return False
    last = state.get("last_refresh")
    if not last:
        return True
    try:
        then = _dt.datetime.fromisoformat(str(last))
    except ValueError:
        return True
    return (_dt.datetime.now() - then).days >= SKILLS_REFRESH_DAYS


def _spawn_skills_refresh() -> bool:
    """Fire a DETACHED `celeborn skills update --global` (own session, stdio discarded) so the weekly
    refresh never blocks orient. Stamps `last_refresh` optimistically BEFORE spawning so a slow/failed
    background run doesn't re-trigger every session — the next window retries. Returns False (no-op) when
    npx is absent. Best-effort: never raises."""
    import os
    import shutil
    import subprocess
    import sys
    if shutil.which("npx") is None:
        return False
    state = _load_skills_state()
    state["last_refresh"] = now_iso()
    try:
        _save_skills_state(state)
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "skills", "update", "--global"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:                                  # noqa: BLE001 — a refresh hiccup never breaks orient
        return False


def _ensure_skills_fresh(ctx: Path) -> None:
    """SessionStart seam: when due (weekly) and the active harness is Claude, kick off a detached
    background refresh of the Matt Pocock suite. Claude-only — the skills only exist under .claude/skills;
    Grok/Codex read .grok//.codex/ and get the advisor's guidance as prose instead. Never raises."""
    try:
        if active_adapter(ctx).name != "claude":
            return
        if _skills_autoupdate_due():
            _spawn_skills_refresh()
    except Exception:                                  # noqa: BLE001
        pass


def _settings_path_for_scope(ctx: Path, scope: str) -> Path:
    """Resolve the Claude settings file for a permission scope. 'global' -> ~/.claude/settings.json;
    'shared' -> project .claude/settings.json; 'local' (default) -> project .claude/settings.local.json."""
    if scope == "global":
        return Path.home() / ".claude" / "settings.json"
    base = ctx.parent / ".claude"
    return base / ("settings.json" if scope == "shared" else "settings.local.json")


def _read_settings(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _resolved_permissions(ctx: Path) -> dict:
    """Effective allow set + defaultMode across all Claude settings scopes. `allow` is the union;
    `effective_mode` follows local > project(shared) > global precedence."""
    scopes = [("global", _settings_path_for_scope(ctx, "global")),
              ("shared", _settings_path_for_scope(ctx, "shared")),
              ("local", _settings_path_for_scope(ctx, "local"))]
    union, seen, per_file, mode = [], set(), {}, {}
    for name, p in scopes:
        data = _read_settings(p)
        perms = data.get("permissions") or {}
        allow = list(perms.get("allow") or [])
        per_file[name] = {"path": str(p), "exists": p.is_file(), "allow": allow,
                          "ask": list(perms.get("ask") or []), "deny": list(perms.get("deny") or []),
                          "defaultMode": perms.get("defaultMode")}
        mode[name] = perms.get("defaultMode")
        for r in allow:
            if r not in seen:
                seen.add(r); union.append(r)
    effective_mode = mode["local"] or mode["shared"] or mode["global"]
    return {"allow": union, "allow_set": seen, "effective_mode": effective_mode, "per_file": per_file}


def _permissions_state_json(ctx: Path) -> dict:
    """The full read-only state the board Settings page renders: which baseline rules are active, the
    Danger Zone spectrum + whether armed, the resolved allow-list, and per-scope file breakdown."""
    res = _resolved_permissions(ctx)
    allow_set, eff_mode = res["allow_set"], res["effective_mode"]
    tools = [{"rule": t, "active": t in allow_set} for t in BASELINE_ALLOW_TOOLS]
    bash = [{"rule": f"Bash({pre}:*)", "prefix": pre, "active": f"Bash({pre}:*)" in allow_set}
            for pre in BASELINE_BASH_PREFIXES]
    danger = [{"rule": r, "active": r in allow_set} for r in DANGER_SPECTRUM]

    # t351 — which scope(s) each resolved allow rule actually lives in (so a chip's ✕ can target the
    # right file; a rule can be present in more than one scope).
    rule_scopes: dict = {}
    for name in ("local", "shared", "global"):
        for r in res["per_file"][name]["allow"]:
            rule_scopes.setdefault(r, [])
            if name not in rule_scopes[r]:
                rule_scopes[r].append(name)

    # t351 — grouped, individually-removable chips for the non-dangerous allow rules.
    grouped: dict = {g["key"]: [] for g in PERMISSION_GROUPS}
    for r in res["allow"]:
        key = _classify_allow_rule(r)
        if key == "dangerous":
            continue
        grouped.setdefault(key, []).append({"rule": r, "scopes": rule_scopes.get(r, [])})
    groups_out = [{**g, "rules": grouped[g["key"]]} for g in PERMISSION_GROUPS if grouped[g["key"]]]

    # t351 — dangerous allows: each group is "active" when ALL its rules are present.
    dangerous_out = []
    for grp in DANGEROUS_ALLOWS:
        scopes = sorted({s for rr in grp["rules"] for s in rule_scopes.get(rr, [])})
        dangerous_out.append({"rules": grp["rules"], "phrase": grp["phrase"], "why": grp["why"],
                              "active": all(rr in allow_set for rr in grp["rules"]), "scopes": scopes})

    return {
        "effective_default_mode": eff_mode,
        "mode": {"value": eff_mode, "options": PERMISSION_MODES},
        "groups": groups_out,
        "dangerous": dangerous_out,
        "locked_deny": {"label": EXFIL_DENY_LABEL, "why": EXFIL_DENY_WHY},
        "baseline": {
            "tools": tools,
            "bash_prefixes": bash,
            "default_mode": {"value": BASELINE_DEFAULT_MODE, "active": eff_mode == BASELINE_DEFAULT_MODE},
            "all_active": all(x["active"] for x in tools + bash) and eff_mode == BASELINE_DEFAULT_MODE,
        },
        "danger": {
            "spectrum": danger,
            "default_mode": {"value": DANGER_DEFAULT_MODE, "active": eff_mode == DANGER_DEFAULT_MODE},
            # "Armed" keys ONLY on the unambiguous danger signals — blanket shell (`Bash(*)`) or
            # bypassPermissions. Benign spectrum members (Read/Edit/Write) being individually allowed is
            # NOT the Danger Zone; per-rule `active` flags above still show exactly what is present.
            "armed": eff_mode == DANGER_DEFAULT_MODE or ("Bash(*)" in allow_set),
            "confirm_phrase": DANGER_CONFIRM_PHRASE,
        },
        "current_allow": res["allow"],
        "scopes": res["per_file"],
    }


def _backup_and_load_settings(path: Path) -> dict:
    """Load a settings file for writing, keeping a .celeborn-bak backup (mirrors cmd_wire). Refuses to
    proceed on invalid JSON so a bad write can never clobber a good file."""
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            die(f"{path} is not valid JSON — refusing to rewrite it. Fix the file by hand first.")
        (path.parent / (path.name + ".celeborn-bak")).write_text(json.dumps(data, indent=2) + "\n")
    return data


def _remove_permission_baseline(data: dict) -> dict:
    """Strip exactly the t100 baseline rules; revert defaultMode only if it is still the baseline value."""
    perms = data.setdefault("permissions", {})
    allow = perms.get("allow") or []
    baseline = set(_baseline_allow_rules())
    removed = [r for r in allow if r in baseline]
    perms["allow"] = [r for r in allow if r not in baseline]
    reverted = False
    if perms.get("defaultMode") == BASELINE_DEFAULT_MODE:
        perms.pop("defaultMode", None)
        reverted = True
    return {"removed": removed, "default_mode_reverted": reverted}


def _arm_danger_zone(data: dict) -> dict:
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    existing, added = set(allow), []
    for r in DANGER_SPECTRUM:
        if r not in existing:
            allow.append(r); existing.add(r); added.append(r)
    prev = perms.get("defaultMode")
    perms["defaultMode"] = DANGER_DEFAULT_MODE
    return {"added": added, "prev_mode": prev}


def _disarm_danger_zone(data: dict) -> dict:
    perms = data.setdefault("permissions", {})
    allow = perms.get("allow") or []
    danger = set(DANGER_SPECTRUM)
    removed = [r for r in allow if r in danger]
    perms["allow"] = [r for r in allow if r not in danger]
    perms["defaultMode"] = BASELINE_DEFAULT_MODE
    return {"removed": removed}


def _add_permission_rule(data: dict, rule: str, kind: str) -> dict:
    """Append a single rule to permissions.<kind> (allow|ask|deny), IN PLACE and dedup-safe. Never
    reorders existing entries; re-adding is a no-op."""
    perms = data.setdefault("permissions", {})
    lst = perms.setdefault(kind, [])
    if rule in lst:
        return {"added": False}
    lst.append(rule)
    return {"added": True}


def _remove_permission_rule(data: dict, rule: str) -> dict:
    """Remove an exact rule from every permissions list it appears in (allow/ask/deny). Returns the
    list names it was pulled from."""
    perms = data.setdefault("permissions", {})
    removed_from = []
    for kind in PERMISSION_RULE_KINDS:
        lst = perms.get(kind)
        if lst and rule in lst:
            perms[kind] = [r for r in lst if r != rule]
            removed_from.append(kind)
    return {"removed_from": removed_from}


def _set_permission_mode(data: dict, mode: str) -> dict:
    """Set (or, when mode is empty, unset) permissions.defaultMode IN PLACE."""
    perms = data.setdefault("permissions", {})
    prev = perms.get("defaultMode")
    if mode:
        perms["defaultMode"] = mode
    else:
        perms.pop("defaultMode", None)
    return {"prev": prev, "mode": mode or None}


def _skills_dirs(ctx: Path) -> list:
    return [ctx.parent / ".claude" / "skills", Path.home() / ".claude" / "skills"]


def _mattpocock_installed_names(ctx: Path) -> set:
    known = {s["name"] for s in MATTPOCOCK_SKILLS}
    found = set()
    for d in _skills_dirs(ctx):
        if d.is_dir():
            for child in d.iterdir():
                if child.is_dir() and child.name in known:
                    found.add(child.name)
    return found


def _skills_state_json(ctx: Path) -> dict:
    installed = _mattpocock_installed_names(ctx)
    mp = [{**s, "installed": s["name"] in installed} for s in MATTPOCOCK_SKILLS]
    sstate = _load_skills_state()
    return {
        # Harness scope (t116): the recommended slash-commands AND the Matt Pocock suite are Claude-only.
        # Grok/Codex receive the SAME advisor recommendations as branded prose (no slash commands), and
        # don't read .claude/skills. The board surfaces this so "Claude skills" isn't misread.
        "harness": "claude",
        "recommended_note": "Claude Code slash-commands. On Grok/Codex the advisor surfaces the same "
                            "recommendations as prose, not installable skills.",
        "core": CELEBORN_CORE_SKILLS,
        "recommended": RECOMMENDED_SKILLS,
        "mattpocock": {
            "source": MATTPOCOCK_SOURCE,
            "install_cmd": " ".join(MATTPOCOCK_INSTALL_CMD),
            "setup_hint": "/setup-matt-pocock-skills",
            "installed_count": len(installed),
            "total": len(MATTPOCOCK_SKILLS),
            "claude_only": True,
            "last_refresh": sstate.get("last_refresh"),
            "autoupdate": sstate.get("autoupdate", True),
            "refresh_days": SKILLS_REFRESH_DAYS,
            "skills": mp,
        },
    }


def _install_mattpocock(ctx: Path, scope: str = "local") -> dict:
    """Run the community `skills` CLI to add the Matt Pocock suite. Network + Node required — dies with a
    clear message if npx is missing. Idempotent (re-detects what is now installed)."""
    import shutil
    import subprocess
    if shutil.which("npx") is None:
        die("npx not found — install Node.js to add the Matt Pocock skills "
            f"(`{' '.join(MATTPOCOCK_INSTALL_CMD)}`).")
    cwd = Path.home() if scope == "global" else ctx.parent
    try:
        # stdin closed so any installer prompt fails fast (EOF) instead of hanging a non-interactive run.
        proc = subprocess.run(MATTPOCOCK_INSTALL_CMD, cwd=str(cwd),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        die(f"failed to run the skills installer: {e}")
    if proc.returncode != 0:
        die(f"skills installer failed (rc={proc.returncode}): {(proc.stderr or proc.stdout or '').strip()[:500]}")
    installed = sorted(_mattpocock_installed_names(ctx))
    return {"ok": True, "cwd": str(cwd), "installed": installed, "count": len(installed)}


def cmd_skills(args):
    """`celeborn skills [list|install-mattpocock]` — list Celeborn/recommended/Matt-Pocock skills (the
    board Settings page consumes `--json`), or install the Matt Pocock suite into .claude/skills/."""
    ctx = require_context(args)
    action = getattr(args, "skills_cmd", None) or "list"
    if action in ("install-mattpocock", "update"):
        scope = "global" if getattr(args, "global_", False) else "local"
        rep = _install_mattpocock(ctx, scope)
        # Record the refresh so the weekly auto-update throttle (t116) counts a manual run too.
        st = _load_skills_state()
        st["last_refresh"] = now_iso()
        _save_skills_state(st)
        if getattr(args, "json", False):
            print(json.dumps({**rep, "last_refresh": st["last_refresh"]}, indent=2))
            return
        verb = "updated" if action == "update" else "installed"
        ok(f"{verb} Matt Pocock skills (latest) — {rep['count']} present under .claude/skills/.")
        if action != "update":
            info("finish setup: run `/setup-matt-pocock-skills` in a Claude Code session.")
        return
    state = _skills_state_json(ctx)
    if getattr(args, "json", False):
        print(json.dumps(state, indent=2))
        return
    print("Celeborn skills")
    print("  Core (bundled — the five verbs):")
    for s in state["core"]:
        print(f"    {s['name']:<11} {s['command']}")
    print("  Recommended (the advisor points at these Claude skills):")
    for s in state["recommended"]:
        print(f"    {s['name']}")
    mp = state["mattpocock"]
    print(f"  Matt Pocock ({mp['installed_count']}/{mp['total']} installed) — {mp['source']}:")
    for s in mp["skills"]:
        print(f"    [{'x' if s['installed'] else ' '}] {s['name']}")


# ------------------------------------------------------------------------------- git post-commit (t216)
#
# The Claude/OpenCode hooks wake the PM on session events; the git side is the missing producer. A
# marker-fenced post-commit hook wakes the PM on EVERY commit — a human's plain `git commit` included,
# not just `celeborn commit`. Fencing preserves any pre-existing hook (our block is appended) and lets
# a re-install update in place. The wake line is `|| true`-guarded and silenced, so a commit never
# fails — and outside a Celeborn project `celeborn pm wake` exits non-zero and is swallowed (no-op).

GIT_HOOK_START = "# >>> celeborn post-commit (managed by `celeborn wire`) >>>"
GIT_HOOK_END = "# <<< celeborn post-commit (managed by `celeborn wire`) <<<"
_GIT_POST_COMMIT_BODY = (
    'sha="$(git rev-parse --short HEAD 2>/dev/null)"\n'
    'celeborn pm wake --source git-commit --detail "$sha" >/dev/null 2>&1 || true'
)


def _git_post_commit_block() -> str:
    return f"{GIT_HOOK_START}\n{_GIT_POST_COMMIT_BODY}\n{GIT_HOOK_END}\n"


def _git_hooks_dir(work_dir: Path) -> Path | None:
    """The repo's hooks directory (honors core.hooksPath and worktrees), or None outside a work tree."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", str(work_dir), "rev-parse", "--git-path", "hooks"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return None
        hp = Path(r.stdout.strip())
        return hp if hp.is_absolute() else (work_dir / hp)
    except Exception:  # noqa: BLE001
        return None


def _install_git_hooks(work_dir: Path) -> str | None:
    """Install/refresh the post-commit PM-wake hook idempotently (CELE-t216). Returns 'installed',
    'updated', 'present', or None outside a git work tree. Preserves a non-Celeborn hook by appending
    our fenced block; updates our block in place on re-run."""
    import os
    import stat as _stat
    hooks = _git_hooks_dir(work_dir)
    if hooks is None:
        return None
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "post-commit"
    block = _git_post_commit_block()

    def _chmod_x(p: Path) -> None:
        st = os.stat(p)
        os.chmod(p, st.st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)

    if not hook.is_file():
        hook.write_text("#!/bin/sh\n" + block)
        _chmod_x(hook)
        return "installed"
    text = hook.read_text()
    if GIT_HOOK_START in text and GIT_HOOK_END in text:
        new = re.sub(re.escape(GIT_HOOK_START) + r".*?" + re.escape(GIT_HOOK_END) + r"\n?",
                     block, text, flags=re.S)
        _chmod_x(hook)
        if new != text:
            hook.write_text(new)
            return "updated"
        return "present"
    sep = "" if text.endswith("\n") else "\n"
    hook.write_text(text + sep + "\n" + block)
    _chmod_x(hook)
    return "installed"


def cmd_wire(args):
    """Merge Celeborn's `statusLine` and five hook groups into a Claude Code settings.json —
    idempotently. The programmatic alternative to hand-merging hooks/settings.snippet.json. Preserves
    everything already in the file: existing keys, unrelated hooks, and (unless `--force`) a
    non-Celeborn statusLine. Re-running never duplicates a hook group, and MIGRATES a legacy
    bash-based install to the collapsed `celeborn hook <event>` form. Backs the file up before writing."""
    if getattr(args, "global_", False):
        settings = Path.home() / ".claude" / "settings.json"
        scope = "global"
    else:
        settings = Path(getattr(args, "path", ".") or ".").resolve() / ".claude" / "settings.json"
        scope = "project"
    settings.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    if settings.is_file():
        try:
            data = json.loads(settings.read_text())
        except json.JSONDecodeError:
            die(f"{settings} is not valid JSON; refusing to overwrite. Fix or remove it first.")
        (settings.parent / (settings.name + ".celeborn-bak")).write_text(json.dumps(data, indent=2) + "\n")

    added, migrated, skipped = [], [], []

    sl = data.get("statusLine")
    if sl == WIRE_STATUSLINE:
        pass
    elif sl and "statusline.sh" in json.dumps(sl):
        data["statusLine"] = dict(WIRE_STATUSLINE)        # migrate a legacy Celeborn statusLine
        migrated.append("statusLine")
    elif sl and not getattr(args, "force", False):
        skipped.append("statusLine — a non-Celeborn statusLine is already set; rerun with --force to replace it")
    else:
        data["statusLine"] = dict(WIRE_STATUSLINE)
        added.append("statusLine")

    hooks = data.setdefault("hooks", {})
    for event, token, legacy in WIRE_HOOKS:
        groups = hooks.setdefault(event, [])
        new_cmd = f"celeborn hook {token}"
        mine = [g for g in groups if _is_celeborn_command(json.dumps(g), token, legacy)]
        if not mine:
            groups.append({"hooks": [{"type": "command", "command": new_cmd}]})
            added.append(f"hooks.{event}")
            continue
        # Already wired — migrate any legacy bash command in our group(s) to the collapsed form.
        for g in mine:
            for h in g.get("hooks", []):
                if h.get("command") != new_cmd:
                    h["command"] = new_cmd
                    migrated.append(f"hooks.{event}")

    # t101 — safety hooks carry a tool matcher and have no legacy bash form, so they're wired in a
    # separate matcher-aware pass (idempotent: detect our group by the `hook <token>` command string).
    for event, token, matcher in SAFETY_HOOKS:
        groups = hooks.setdefault(event, [])
        existing = next((g for g in groups if f"hook {token}" in json.dumps(g)), None)
        if existing is not None:
            # Already wired — but migrate an outdated matcher in place (t131 widened PreToolUse from
            # "Bash" to also cover Edit/Write/NotebookEdit for the card-less-work gate).
            if matcher and existing.get("matcher") != matcher:
                existing["matcher"] = matcher
                migrated.append(f"hooks.{event} matcher")
            continue
        group = {"hooks": [{"type": "command", "command": f"celeborn hook {token}"}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        added.append(f"hooks.{event}")

    # t100 — merge the SAFE permission baseline, but ONLY on a global Claude wire (the "big three" are
    # Claude Code concepts; project settings + other harnesses are out of scope). Opt-out via
    # --no-permission-baseline. Ask-wins + idempotent + scoped, all inside _merge_permission_baseline.
    baseline = None
    if (scope == "global" and active_adapter(None).name == "claude"
            and not getattr(args, "no_permission_baseline", False)):
        baseline = _merge_permission_baseline(data)

    _atomic_write_json(settings, data)

    if added or migrated:
        bits = []
        if added:
            bits.append("added " + ", ".join(added))
        if migrated:
            bits.append("migrated " + ", ".join(sorted(set(migrated))))
        ok(f"wired Celeborn into {settings} ({scope}): {'; '.join(bits)}")
    else:
        info(f"{settings} already wired — nothing to add")
    for s in skipped:
        warn(s)
    # t216 — a git post-commit hook so every commit (a human's plain `git commit` included) wakes the
    # PM. Best-effort and idempotent; silent when this isn't a git work tree.
    try:
        gstatus = _install_git_hooks(Path(getattr(args, "path", ".") or ".").resolve())
    except Exception:  # noqa: BLE001
        gstatus = None
    if gstatus in ("installed", "updated"):
        ok(f"git post-commit hook {gstatus} — commits now wake the PM (CELE-t216)")
    if baseline is not None:
        n, dm = len(baseline["added"]), baseline["default_mode_set"]
        if n or dm:
            bits = []
            if n:
                bits.append(f"+{n} allow rule(s)")
            if dm:
                bits.append(f"defaultMode={BASELINE_DEFAULT_MODE}")
            ok(f"safe permission baseline: {', '.join(bits)} → {settings}")
            info("revert any time: run `/permissions`, or edit ~/.claude/settings.json "
                 "(a settings.json.celeborn-bak backup was kept if the file already existed).")
            info("⚠ applies to NEW sessions — `Shift+Tab` toggles acceptEdits in the current one; "
                 "`/permissions` reloads config.")
        else:
            info("safe permission baseline already present — nothing added.")
    # t115 — install the Matt Pocock skill suite default-on on a GLOBAL Claude wire (opt out via
    # --no-skills). Best-effort: a missing npx / failed install must NEVER fail the wire.
    if (scope == "global" and active_adapter(None).name == "claude"
            and not getattr(args, "no_skills", False)):
        import shutil
        if shutil.which("npx") is None:
            warn("skipped Matt Pocock skills: npx not found (install Node) — add later with "
                 "`celeborn skills install-mattpocock`.")
        else:
            ctx0 = find_context_root(Path(getattr(args, "path", ".") or "."))
            try:
                rep = _install_mattpocock(ctx0 or Path(getattr(args, "path", ".") or "."), "global")
                ok(f"Matt Pocock skills installed default-on — {rep['count']} present. "
                   f"Run `/setup-matt-pocock-skills` to finish.")
            except SystemExit:
                warn("skipped Matt Pocock skills (installer failed) — add later with "
                     "`celeborn skills install-mattpocock`. Opt out permanently with `wire --no-skills`.")
    info("commands are in-process `celeborn hook <event>` — `celeborn` must be on PATH "
         "(pip/uv install). No $CELEBORN_HOME or hooks/ dir needed.")
    if scope == "project":
        info("project-scoped — pass --global to wire ~/.claude/settings.json for every session.")
    consent = _load_consent()
    if consent.get("agreed"):
        n = len(consent.get("opted_out") or [])
        info(f"consent on record for {consent.get('name')}"
             + (f" ({n} opt-out{'s' if n != 1 else ''})." if n else " — all click-reducers enabled."))
    else:
        info("review what Celeborn automates for you (all opt-out) + accept the User Agreement: "
             "run `celeborn consent`")
        info(f"User Agreement: {AGREEMENT_URL}")
    info(_legal_docs_line())
    if getattr(args, "grok", False):
        ctx = find_context_root(Path(getattr(args, "path", ".") or "."))
        if ctx is None:
            warn("--grok: no .context/ here — run `celeborn init` first")
        elif _wire_grok(ctx.parent):
            ok("also wired Grok Build for this project")
    if getattr(args, "opencode", False):
        ctx = find_context_root(Path(getattr(args, "path", ".") or "."))
        if ctx is None:
            warn("--opencode: no .context/ here — run `celeborn init` first")
        elif _wire_opencode(ctx.parent):
            ok("also wired OpenCode (plugin + PM agent + provider) for this project")


# ------------------------------------------------------------------------------- init / first-run (t120, t228)
#
# "Install like Modal" (CELE-t120). Modal's whole onboarding is `pip install modal` + `python3 -m
# modal setup` (a browser auth) — two commands and you're running code. `celeborn init` is the
# post-package-install half of that: ONE guided command that wires Claude Code, scaffolds the current
# project, signs you in (browser PKCE — required, Modal parity), and opens the board, then prints a
# "you're ready" next-step. It is a THIN, idempotent, resumable orchestrator over the existing
# first-class verbs (`wire`/`scaffold`/`login`) — never a reimplementation, so the manual path stays
# intact. Re-running it resumes: `wire` is idempotent, `scaffold` is skipped when `.context/` already
# exists, and `login` is skipped when a session is already on record — so a first-run interrupted at any
# step finishes on a re-run. CELE-t228 renamed this everything-command from `setup` → `init` (the old
# scaffold-only `init` became `scaffold`; `setup` stays a hidden back-compat alias). Design + rationale:
# references/setup-onboarding-plan.md.


def _setup_step_init(args, path: str) -> None:
    """Scaffold the current project unless it's already a Celeborn project (idempotent/resumable)."""
    if getattr(args, "no_init", False):
        info("scaffold: skipped (--no-init).")
        return
    root = Path(path or ".").resolve()
    if (root / CONTEXT_DIRNAME).is_dir():
        info(f"scaffold: already a Celeborn project (.context/ present at {root.name}/) — skipped.")
        return
    init_args = argparse.Namespace(
        path=path, private=False, public=False, claude_md=True, agents_md=True, scan=True,
        no_cmm=getattr(args, "no_cmm", False), name=getattr(args, "name", None),
        open_board=not getattr(args, "no_open", False),
        open_browser=not getattr(args, "no_browser", False))
    cmd_scaffold(init_args)


def _setup_step_orientation(args) -> Path | None:
    """First-run bootstrap (CELE-t387): ensure the dedicated Orientation (ORIE) tutorial project exists
    so a brand-new user lands on a populated board. Idempotent and never blocking — skippable with
    --no-orientation. On first creation (and on an interactive install) it opens /board/ORIE so the
    user sees their onboarding board. Returns the Orientation ctx when it was newly created (the signal
    that this is a first-run landing), else None."""
    if getattr(args, "no_orientation", False):
        info("orientation: skipped (--no-orientation).")
        return None
    ctx, created = _ensure_orientation_project()
    if ctx is None:
        info("orientation: could not scaffold the Orientation project (templates unavailable) — skipped.")
        return None
    # Additive curriculum pass on EVERY init (CELE-t388): first creation seeds the full starter
    # deck; a later release that ships a new ORIENTATION_CURRICULUM entry tops up exactly that
    # card; otherwise it's a no-op.
    signals = {"low_disk"} if _low_disk_for_pippin(ctx) else set()
    seeded = _seed_orientation_cards(ctx, signals=signals)
    if created:
        ok(f"Orientation project ready — {len(seeded)} starter cards on your onboarding board "
           f"(ORIE) at {board_url(ctx)}")
        if not getattr(args, "no_open", False) and _init_is_interactive():
            _open_board_on_init(ctx, open_browser=not getattr(args, "no_browser", False))
    elif seeded:
        # Top-up stays quiet: one info line, never an announce + board pop (Amendment I — flow).
        info(f"orientation: {len(seeded)} new tutorial card(s) added to your ORIE board.")
    else:
        info(f"orientation: Orientation project already present ({_orientation_dir()}) — kept.")
    return ctx if created else None


def _setup_step_weave(args, path: str) -> None:
    """Offer the sovereign weave during init (CELE-t374) — the complete free local engine (OpenCode +
    Ollama + Pippin) for a machine with no AI coding assistant. Consent-gated per component and never
    blocking: a headless install prints the `celeborn weave` pointer instead of running installers,
    and any failure degrades to a warning — init always finishes."""
    if getattr(args, "no_weave", False):
        info("weave: skipped (--no-weave).")
        return
    ctx = find_context_root(Path(path or "."))
    if ctx is None:
        info("weave: no .context/ here — run `celeborn weave` after scaffolding.")
        return
    try:
        st = _weave_status(ctx)
        complete = (st["opencode"]["installed"] and st["ollama"]["installed"]
                    and all(st["models"].values()))
        if not complete and not _init_is_interactive():
            info("weave: local engine incomplete — non-interactive, so no installers ran. "
                 "Set it up any time: `celeborn weave` (attributed, consent-gated upstream installs).")
            return
        # Complete = a quick aligned read + wiring/config refresh (idempotent, no prompts hit);
        # incomplete + interactive = the consented install flow.
        _weave(ctx)
    except Exception as e:  # noqa: BLE001 — init must never die on the weave
        warn(f"weave step failed ({e}) — finish later with `celeborn weave`")


def _setup_step_login(args) -> bool:
    """Sign in (browser PKCE by default; email+password when --email is given). Required by default —
    Modal parity — but skippable with --no-login, and impossible to force on a non-TTY shell (CI/headless
    can't open a browser). Returns True if a session is on record afterward. A failed interactive sign-in
    WARNS and lets setup finish (wire+init already succeeded and are usable locally) rather than aborting."""
    sync = __import__("celeborn_sync")
    creds = sync.load_creds()
    if creds.get("access_token") or creds.get("refresh_token"):
        who = creds.get("email") or creds.get("username") or "your account"
        info(f"sign-in: already signed in as {who} — skipped (run `celeborn logout` to switch).")
        return True
    if getattr(args, "no_login", False):
        info("sign-in: skipped (--no-login). Run `celeborn login --github` later to enable the hosted board.")
        return False
    if not _init_is_interactive():
        warn("sign-in: non-interactive shell — can't open a browser. Run `celeborn login --github` from a "
             "terminal to enable the hosted board. (Local Celeborn works fully without an account.)")
        return False
    use_email = bool(getattr(args, "email", None))
    info("opening your browser to sign in with GitHub…" if not use_email
         else "signing in with email + password…")
    login_args = argparse.Namespace(github=not use_email, email=getattr(args, "email", None), password=None)
    try:
        sync.cmd_login(login_args)
        return True
    except SystemExit:
        warn("sign-in didn't complete — finish it later with `celeborn login --github` (or re-run "
             "`celeborn init`). Continuing — local Celeborn works without an account.")
        return False


def _setup_ready(path: str, signed_in: bool, orie_ctx: Path | None = None) -> None:
    """Modal-style closing: where the board is + the single next thing to do. The board is Celeborn's
    UI and the instruction surface for a first-time user, so point at it prominently — the scaffold
    step already opened it in the browser on an interactive install. On a first-run install (a freshly
    created Orientation project, CELE-t387) point at the ORIE onboarding board — that's where a
    brand-new user should start, not an empty project board."""
    print("\n✅ Celeborn is ready.\n")
    if orie_ctx is not None:
        print(f"  👉 Start here — your Orientation board: {board_url(orie_ctx)}"
              + ("   (just opened in your browser)" if _init_is_interactive() else "")
              + "\n     A guided tutorial that walks your coding assistant through setting you up.")
    ctx = find_context_root(Path(path or "."))
    if ctx is not None:
        url = board_url(ctx)
        opened = _init_is_interactive()
        label = "  This project's board" if orie_ctx is not None else "  👉 Your board"
        print(f"{label}: {url}   (Celeborn's UI"
              + (" — just opened in your browser)" if opened else ")"))
    print("\n  Next:")
    print("    • Open Claude Code in this project — Celeborn orients automatically every session.")
    print("    • The board is where your work lives: it opens on its own each session.")
    print("    • Inspect what an agent loads on orient:  celeborn status")
    if not signed_in:
        print("    • Sync your private memory across devices (optional):  celeborn login --github")
    print()


def cmd_init(args):
    """`celeborn init` — the ONE first-run command (CELE-t228). A guided everything-command that wires
    Claude Code + scaffolds this project + signs you in, then opens your kanban board. A thin
    orchestrator over `wire`/`scaffold`/`weave`/`login`, each idempotent so re-running resumes
    (CELE-t120). Order is wire → scaffold → weave → login so the local-first project is fully set up
    even if browser auth is the one step that doesn't complete; login is the final, gated step. The
    weave step (CELE-t374) offers the free local engine — OpenCode + Ollama + Pippin — consent-gated
    and never blocking. (`celeborn setup` is a hidden
    back-compat alias; `celeborn scaffold` is the secondary scaffold-only command.)"""
    path = getattr(args, "path", ".") or "."
    print("\n🏹 Celeborn init — wiring Claude Code, scaffolding this project, and signing you in.\n")

    print("[1/5] Wiring Claude Code (hooks + statusLine + safe baseline + skills)…")
    wire_args = argparse.Namespace(
        global_=not getattr(args, "project", False), force=getattr(args, "force", False),
        no_permission_baseline=getattr(args, "no_permission_baseline", False),
        no_skills=getattr(args, "no_skills", False), grok=False, path=path)
    cmd_wire(wire_args)

    print("\n[2/5] Scaffolding this project…")
    _setup_step_init(args, path)

    print("\n[3/5] First-run: your Orientation board (ORIE) — a guided tutorial project (CELE-t387)…")
    orie_ctx = _setup_step_orientation(args)

    print("\n[4/5] Weaving the local engine — OpenCode + Ollama + Pippin (free, local; CELE-t374)…")
    _setup_step_weave(args, path)

    print("\n[5/5] Signing you in (browser)…")
    signed_in = _setup_step_login(args)

    _setup_ready(path, signed_in, orie_ctx=orie_ctx)


# --------------------------------------------------------------------------- consent / opt-out (t102)
#
# Celeborn is a TOOL: it has no will of its own — it performs exactly the click-reducing automations the
# operator turns on by installing and wiring it. `celeborn consent` makes that explicit: it shows every
# behavior that removes an approval click (all ON by default — opt-out, not opt-in), links the User
# Agreement, and records the operator's name + timestamp + any opt-outs to ~/.context/consent.json.
# `wire` prints a one-line pointer to it but NEVER blocks — install must stay non-interactive for CI/hooks.
AGREEMENT_URL = "https://celeborncode.ai/agreement"
AGREEMENT_VERSION = "2026-06-18"

# Standard published legal documents (CELE-t158), hosted on the thot.ai apex site. These are the
# best-practices Privacy / Cookie / User-Agreement instruments (US + EU/GDPR), distinct from the
# automation-consent disclosure at AGREEMENT_URL above. Surfaced on the consent screen and linked from
# both the local and hosted board footers, the way a standard web app footers its required agreements.
PRIVACY_URL = "https://thot.ai/privacy"
COOKIE_URL = "https://thot.ai/cookies"
USER_AGREEMENT_URL = "https://thot.ai/user-agreement"
LEGAL_DOCS = (
    ("Privacy Policy", PRIVACY_URL),
    ("Cookie Policy", COOKIE_URL),
    ("User Agreement", USER_AGREEMENT_URL),
)


def _legal_docs_line() -> str:
    """One-line pointer to the published legal documents, for CLI surfaces (consent screen, etc.)."""
    return "Legal & policies: " + "  ·  ".join(f"{name} {url}" for name, url in LEGAL_DOCS)

# (key, what it does, why it saves clicks, safety note or "") — the single source the CLI checklist
# renders from; the web User Agreement mirrors it in prose. Keep the two in sync when this changes.
CONSENT_ITEMS = [
    ("permission-baseline",
     "Pre-approve safe read-only commands and enable acceptEdits mode",
     "so you stop clicking “Allow” for ls / grep / git-status and routine file edits",
     "file edits and the safe-listed commands run without a per-action prompt"),
    ("permission-learn",
     "Generalize your repeated approvals into reusable allow-rules (`celeborn permissions`)",
     "a command you approve more than once becomes a wildcard rule you never re-approve again",
     ""),
    ("session-hooks",
     "Capture and restore project context automatically across sessions",
     "orient load, per-turn capture and checkpoint reminders — so a /clear never makes you re-explain",
     ""),
    ("cd-redirect-guard",
     "Steer an un-approvable `cd … > file` write to the Write tool (PreToolUse guard)",
     "turns a recurring manual approval into an invisible, statically-safe file write",
     ""),
    ("cd-redirect-autoallow",
     "Auto-allow a marked `cd … > file` write with no prompt",
     "a command you tag `# celeborn:allow-redirect` runs without asking",
     "a write whose target the permission system cannot statically verify then runs with no human check"),
    ("board-autostart",
     "Auto-launch your local kanban board on orient",
     "the board comes up without a manual command",
     ""),
    ("claim-on-paste",
     "Auto-claim a task card when you paste its marker",
     "pasting a card assigns it to you without a separate claim step",
     ""),
    ("quality-gates",
     "Run tests / typecheck automatically after edits (opt-in via `wire-quality`)",
     "surfaces failures without you remembering to run the suite",
     ""),
]
CONSENT_KEYS = [k for k, *_ in CONSENT_ITEMS]


def _consent_path() -> Path:
    return _global_context() / "consent.json"


def _load_consent() -> dict:
    """The recorded agreement (name, timestamp, opt-outs), or {} if none / unreadable. Best-effort."""
    p = _consent_path()
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _render_consent_checklist(opted_out: set) -> str:
    """The opt-out list: every click-reducer with a checkbox — [x] enabled (default), [ ] opted out."""
    lines = []
    for i, (key, what, why, risk) in enumerate(CONSENT_ITEMS, 1):
        box = "[ ]" if key in opted_out else "[x]"
        lines.append(f"  {box} {i}. {what}{'   ⚠' if risk else ''}")
        lines.append(f"        ↳ {why}")
        if risk:
            lines.append(f"        ⚠ safety: {risk}")
    return "\n".join(lines)


def _parse_optouts(tokens, warn_unknown: bool = True) -> set:
    """Map a comma-list of item numbers or keys to the canonical opt-out keys."""
    out: set = set()
    for tok in str(tokens or "").replace(" ", "").split(","):
        if not tok:
            continue
        if tok in CONSENT_KEYS:
            out.add(tok)
        elif tok.isdigit() and 1 <= int(tok) <= len(CONSENT_ITEMS):
            out.add(CONSENT_KEYS[int(tok) - 1])
        elif warn_unknown:
            warn(f"unknown item '{tok}' — ignored (valid: 1–{len(CONSENT_ITEMS)} or a key)")
    return out


def cmd_consent(args):
    """`celeborn consent` — the install-time opt-out screen. Shows every behavior that removes an
    approval click (all ON by default), links the User Agreement, and records the operator's name +
    any opt-outs to ~/.context/consent.json. Non-interactive via --name / --opt-out / --yes (CI +
    tests); --show prints the recorded consent and exits."""
    existing = _load_consent()
    if getattr(args, "show", False):
        if not existing.get("agreed"):
            info("No consent on record. Run `celeborn consent` to review the automations and agree.")
            return
        print(json.dumps(existing, indent=2))
        return

    flag = getattr(args, "opt_out", None)
    name = getattr(args, "name", None)
    interactive = sys.stdin.isatty() and not getattr(args, "yes", False) and flag is None and not name
    opted_out = _parse_optouts(flag) if flag is not None else set()

    print("🏹  Celeborn — what it automates for you (opt-out)\n")
    print("Celeborn is a tool. It has no mind of its own; it performs only the actions you turn on by")
    print("using it. Every item below removes an approval click and is ENABLED by default — uncheck any")
    print(f"you don't want. Full detail + safety notes: {AGREEMENT_URL}")
    print(_legal_docs_line() + "\n")
    print(_render_consent_checklist(opted_out) + "\n")

    if interactive:
        try:
            raw = input("Numbers to OPT OUT of (comma-separated), or Enter to keep all enabled: ").strip()
        except EOFError:
            raw = ""
        opted_out |= _parse_optouts(raw)
        if opted_out:
            print("\nUpdated:\n" + _render_consent_checklist(opted_out))
        print(f"\nI agree to the Celeborn User Agreement ({AGREEMENT_URL}).")
        try:
            name = input("Type your full name to agree (blank to cancel): ").strip()
        except EOFError:
            name = ""

    if not name:
        die("Agreement not recorded — no name provided. Re-run `celeborn consent` to agree.")

    record = {
        "agreed": True,
        "name": name,
        "agreed_at": now_iso(),
        "agreement_url": AGREEMENT_URL,
        "agreement_version": AGREEMENT_VERSION,
        "enabled": [k for k in CONSENT_KEYS if k not in opted_out],
        "opted_out": sorted(opted_out),
    }
    _scaffold_global(_global_context())
    _consent_path().write_text(json.dumps(record, indent=2) + "\n")
    ok(f"Agreement recorded for {name} ({record['agreed_at']}).")
    if opted_out:
        info("Opted out of: " + ", ".join(sorted(opted_out)) +
             " — recorded to consent.json (behaviors that honor these flags read it on run).")
    else:
        info("All click-reducers enabled. Change any time with `celeborn consent` (or --opt-out).")
    info(f"User Agreement: {AGREEMENT_URL}")
    info(_legal_docs_line())


# Quality-gate hook groups (t70 Phase 2), installed ONLY by `celeborn wire-quality` (opt-in) — never by
# `wire`. PostToolUse runs the cheap per-edit check; Stop runs the deferred full suite once per turn.
QUALITY_HOOKS = [
    ("PostToolUse", "post-edit", "Edit|Write|MultiEdit"),
    ("Stop", "quality-stop", None),
]
QUALITY_MD_BEGIN = "<!-- BEGIN CELEBORN QUALITY (managed by `celeborn wire-quality`) -->"
QUALITY_MD_END = "<!-- END CELEBORN QUALITY -->"


def _quality_instruction_text() -> str:
    """The harness-neutral quality rule — used as the AGENTS.md fallback where a host has no hooks."""
    return ("After editing files under `scripts/**` or `tests/**`, run the test suite "
            "(`python3 -m unittest discover -s tests`). After editing `board/**/*.tsx`, run "
            "`npx tsc --noEmit` in `board/` — NEVER `next build` (it clobbers the live dev server). "
            "Surface any failure and fix it before calling the change done.")


def _wire_quality_hooks_json(settings: Path, scope: str):
    """Merge the PostToolUse + Stop quality groups into a Claude settings.json — idempotently, mirroring
    cmd_wire. Preserves everything already there; re-running never duplicates a group; backs up first."""
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if settings.is_file():
        try:
            data = json.loads(settings.read_text())
        except json.JSONDecodeError:
            die(f"{settings} is not valid JSON; refusing to overwrite. Fix or remove it first.")
        (settings.parent / (settings.name + ".celeborn-bak")).write_text(json.dumps(data, indent=2) + "\n")
    hooks = data.setdefault("hooks", {})
    added = []
    for event, token, matcher in QUALITY_HOOKS:
        groups = hooks.setdefault(event, [])
        if any(f"hook {token}" in json.dumps(g) for g in groups):
            continue                                 # already wired — leave it
        group = {"hooks": [{"type": "command", "command": f"celeborn hook {token}"}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        added.append(f"hooks.{event}")
    settings.write_text(json.dumps(data, indent=2) + "\n")
    if added:
        ok(f"wired quality gates into {settings} ({scope}): added {', '.join(added)}")
    else:
        info(f"{settings} already has the quality gates — nothing to add")
    info("on edit: py_compile (scripts/tests) · `tsc --noEmit` (board, never `next build`); "
         "on stop: full suite once per turn when scripts/** or tests/** changed. Failures surface, never block.")


def _wire_quality_agents_md(path: Path):
    """AGENTS.md fallback for a harness with no structured hooks: write/refresh a managed instruction
    block so a Codex/Grok-style host that auto-loads AGENTS.md still runs the gates by hand."""
    block = (f"{QUALITY_MD_BEGIN}\n## Quality gates (Celeborn)\n\n"
             f"{_quality_instruction_text()}\n{QUALITY_MD_END}\n")
    existing = path.read_text() if path.is_file() else ""
    if QUALITY_MD_BEGIN in existing and QUALITY_MD_END in existing:
        start = existing.index(QUALITY_MD_BEGIN)
        stop = existing.index(QUALITY_MD_END) + len(QUALITY_MD_END)
        new = existing[:start] + block.rstrip("\n") + existing[stop:]
        if new == existing:
            info(f"{path} quality block already current")
            return
        path.write_text(new)
        ok(f"refreshed the quality block in {path}")
        return
    sep = "\n" if (existing and not existing.endswith("\n")) else ""
    path.write_text(existing + sep + ("\n" if existing else "") + block)
    ok(f"appended quality gates to {path} (no structured hooks on this harness)")


def cmd_wire_quality(args):
    """`celeborn wire-quality` — opt-in deterministic quality gates (t70 Phase 2), routed through the
    active adapter. Claude: merge a PostToolUse + a Stop hook group into settings.json (SHARED by
    default — they help every contributor; `--local` → personal settings.local.json). A harness without
    structured hooks: append an AGENTS.md instruction instead. Surfaces failures, never blocks "done"."""
    ctx = require_context(args)
    shared = not getattr(args, "local", False)
    adapter = active_adapter(ctx)
    kind, target = adapter.quality_hook_target(ctx, shared=shared)
    if kind == "hooks-json":
        _wire_quality_hooks_json(target, scope=("shared" if shared else "personal"))
    elif kind == "agents-md":
        _wire_quality_agents_md(target)
    else:
        info("This harness exposes no quality-hook target. Add this to your agent instructions:")
        print("  " + _quality_instruction_text())


# The /clear nudge ends with a one-word (or one-line) sign-off that ALTERNATES every time the nudge
# fires, so a line a user sees often never reads stale. The workhorse pool is "flow" synonyms; roughly
# one firing in REMIND_WELLNESS_EVERY swaps in a wellness tidbit instead — a small wink that looks
# after the human in the chair, not just the context window. Rotation is deterministic (keyed off a
# persisted fire counter), never a clock or RNG — the codebase forbids both (they break resume/replay).
REMIND_FLOW_CLOSERS = (
    "Flow.", "Momentum.", "Cadence.", "Cruise.", "Glide.", "Coast.", "Roll.", "Stride.",
    "Rhythm.", "Tempo.", "Sail.", "Onward.", "Tessellate.", "Groove.", "Smooth.", "Continue.",
    "Next.", "Zone.", "Velocity.", "Resume.",
)
REMIND_WELLNESS_CLOSERS = (
    "Remember to hydrate your body.", "Unclench your jaw.", "Drop your shoulders.",
    "Blink — look 20 feet away.", "Stretch your wrists.", "Stand up, quick stretch.",
    "Take one slow breath.", "Rest your eyes a moment.", "Refill your water.",
    "Be kind to future-you — leave a note.",
)
REMIND_WELLNESS_EVERY = 10  # one wellness tidbit per this many firings; all the rest are flow words


def _remind_closer(n: int) -> str:
    """The rotating sign-off for the n-th (0-based) /clear nudge firing. Every REMIND_WELLNESS_EVERY-th
    firing is a wellness tidbit; all others cycle the flow pool. A pure function of n — deterministic,
    no clock/RNG — so it's reproducible and unit-testable. flow_idx = n minus the wellness firings
    already consumed, which keeps the flow cycle gap-free and even."""
    if n % REMIND_WELLNESS_EVERY == REMIND_WELLNESS_EVERY - 1:
        return REMIND_WELLNESS_CLOSERS[(n // REMIND_WELLNESS_EVERY) % len(REMIND_WELLNESS_CLOSERS)]
    flow_idx = n - (n // REMIND_WELLNESS_EVERY)
    return REMIND_FLOW_CLOSERS[flow_idx % len(REMIND_FLOW_CLOSERS)]


def _next_remind_closer(ctx) -> str:
    """Read-increment-persist the per-project nudge fire counter and return the closer for it, so the
    sign-off advances once per firing across sessions. Degrades to the first flow word if metrics
    can't be read/written (a nudge must never crash on a bad metrics file)."""
    try:
        m = _load_metrics(ctx)
        n = int(m.get("remind_fire_count", 0) or 0)
        closer = _remind_closer(n)
        m["remind_fire_count"] = n + 1
        _save_metrics(ctx, m)
        return closer
    except Exception:
        return REMIND_FLOW_CLOSERS[0]


def _remind_line(tokens, clear_cmd: str, closer: str) -> str:
    """The single, uniform /clear nudge line — used on every channel (stdout + the GUI modal). Names
    the stale-token weight (the precise live count; omitted when no count is known), the safe action,
    the no-rehydrate guarantee, and the rotating sign-off. The ~ marks it as an estimate, but the
    full digits are shown — never rounded."""
    weight = f"Carrying ~{tokens:,} stale tokens. " if tokens else ""
    return (f"🏹 Celeborn —> {weight}Safe to {clear_cmd} — state is saved, "
            f"nothing to re-explain. {closer}")


def cmd_remind(args):
    """Print a reassuring, Tolkien-voiced checkpoint-and-renew reminder.

    Portable across coding systems: the host supplies the live context size via `--tokens` (it is
    the only part the CLI cannot observe itself). With `--last`, the reminder stays silent unless a
    new `--every`-sized milestone has been crossed, so a host can call it on every render/turn and
    only surface it once per increment. The "button" is whatever clear action the host shows
    alongside it; `--clear-cmd` sets the wording.
    """
    ctx = require_context(args)
    cfg = load_config(ctx)
    every = args.every if args.every and args.every > 0 else 100_000
    tokens = args.tokens
    last = args.last
    # Context-pressure thresholds (CELE-t207): explicit --soft-limit/--hard-limit win, else the
    # project's .celebornrc (context_soft_tokens / context_hard_tokens), else the band.ts defaults.
    soft, hard = _context_thresholds(cfg, getattr(args, "soft_limit", None),
                                     getattr(args, "hard_limit", None))
    sid = (getattr(args, "session", None) or "").strip()

    # --transcript: read the live context size straight from the Claude Code transcript (the real
    # number). --auto: use Celeborn's own rolling estimate. Both persist to metrics so the
    # host hook can stay stateless and `status`/`metrics` reflect the latest reading.
    # --session: read the live window a transcript-less harness (OpenCode) reported onto this
    # session's capture cursor via `record tokens` (t205) — the cursor tracks its own mark.
    metrics = None
    cursor = None
    track = args.auto or bool(getattr(args, "transcript", None))
    if getattr(args, "transcript", None):
        metrics = _load_metrics(ctx)
        tokens = _estimate_transcript_tokens(Path(args.transcript), cfg["chars_per_token"])
        metrics["context_estimate"] = tokens
        # Keep the project-level pressure flag current on every reading (machine-readable, t207).
        metrics["context_pressure"] = {"level": _pressure_level(tokens, soft, hard),
                                       "tokens": tokens, "at": now_iso()}
        last = metrics.get("last_remind_estimate", 0)
        _save_metrics(ctx, metrics)  # record the reading even if we stay silent
    elif args.auto:
        metrics = _load_metrics(ctx)
        tokens = metrics.get("context_estimate", 0)
        last = metrics.get("last_remind_estimate", 0)
    elif sid:
        metrics = _load_metrics(ctx)
        caps = metrics.get("captures") if isinstance(metrics.get("captures"), dict) else {}
        cursor = dict(caps.get(sid) or {})
        if not cursor.get("live"):
            return                      # no reported live window for this session — nothing to say
        tokens = int(cursor.get("tokens_session") or 0)
        last = int(cursor.get("last_remind_tokens") or 0)

    def _remember(mark: int, level: str) -> None:
        # Persist the last-reminded mark (and the pressure flag) wherever this mode keeps state,
        # so the next call stays silent until something new happens.
        if metrics is None:
            return
        if cursor is not None:
            cursor["last_remind_tokens"] = mark
            cursor["pressure"] = level
            caps = metrics.get("captures") if isinstance(metrics.get("captures"), dict) else {}
            _write_capture(metrics, caps, sid, cursor)
        elif track:
            metrics["last_remind_estimate"] = mark
            metrics["context_pressure"] = {"level": level, "tokens": mark, "at": now_iso()}
        else:
            return
        _save_metrics(ctx, metrics)

    clear_cmd = args.clear_cmd or "/clear"
    level = _pressure_level(tokens or 0, soft, hard)

    # Session-mode window shrink (post-compaction/clear report): re-arm quietly at the new size so
    # both the milestone nudge and the pressure warnings can fire again as the window regrows.
    if cursor is not None and tokens < max(0, last or 0):
        _remember(tokens, level)
        return

    # Threshold crossing (CELE-t207): the live window newly climbed past a configured soft/hard
    # limit since the last-reminded mark — speak the urgent warning instead of the calm milestone
    # nudge. Needs a tracked mark (`last`); a bare one-shot `--tokens` keeps the legacy wording.
    if tokens and last is not None and _PRESSURE_RANK[level] > _PRESSURE_RANK[_pressure_level(max(0, last), soft, hard)]:
        _remember(tokens, level)
        print(_pressure_line(tokens, level, soft, hard, clear_cmd))
        return

    # Silence unless a fresh milestone was crossed (vs. the last-reminded token count).
    if tokens is not None and last is not None and not args.force:
        if tokens // every == max(0, last) // every:
            return

    # We're going to speak — remember where, so we stay silent until the next band.
    _remember(tokens or 0, level)

    # One rotating sign-off per firing. Advancing the counter here means it ticks once per nudge.
    line = _remind_line(tokens, clear_cmd, _next_remind_closer(ctx))

    print(line)


# --------------------------------------------------------------------------- pre-compaction panic-save


def _panic_stamp() -> str:
    """Filesystem-safe local timestamp for a panic-save dir name. Lexical order == chronological."""
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _panic_snapshots(ctx: Path) -> list:
    """Existing panic-save dirs under .context/.panic/, oldest first (names sort chronologically)."""
    base = ctx / PANIC_DIR
    if not base.is_dir():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)


def _do_panic_save(ctx: Path, reason: str = "manual", session=None, keep: int = PANIC_KEEP) -> dict:
    """Copy whichever PANIC_SAVE_FILES exist into .context/.panic/<stamp>/ (subpaths mirrored), write a
    meta.json, FIFO-prune to `keep`, and return {stamp, dir, files, reason}. This is the deterministic
    safety net behind the "🏹 Celeborn saved your session" moment: a restore point that survives a
    compaction regardless of whether the model freshened the Hot tier. Best-effort per file — an
    unreadable file is skipped, never fatal."""
    import shutil
    stamp = _panic_stamp()
    dest = ctx / PANIC_DIR / stamp
    # De-collide: two panic-saves in the same second (e.g. a burst of compaction events) would
    # otherwise share a stamp dir and silently clobber each other's restore point. Suffix -2, -3, …
    # which still sorts chronologically (the bare stamp is a prefix, so it sorts first).
    if dest.exists():
        n = 2
        while (ctx / PANIC_DIR / f"{stamp}-{n}").exists():
            n += 1
        stamp = f"{stamp}-{n}"
        dest = ctx / PANIC_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for rel in PANIC_SAVE_FILES:
        src = ctx / rel
        if not src.is_file():
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, out)
            saved.append(rel)
        except OSError:
            pass
    meta = {"schema": "celeborn-panic/1", "stamp": stamp, "reason": reason,
            "session": session, "at": now_iso(), "files": saved}
    try:
        (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    except OSError:
        pass
    if keep and keep > 0:                       # FIFO: keep the most recent `keep` (this one included)
        for old in _panic_snapshots(ctx)[:-keep]:
            shutil.rmtree(old, ignore_errors=True)
    return {"stamp": stamp, "dir": str(dest), "files": saved, "reason": reason}


def _panic_save_line(info: dict) -> str:
    """User/agent-visible panic-save confirmation (t36 felt moment, t43 copy). All counts/paths from `info`."""
    files = info.get("files") or []
    n = len(files)
    stamp = info.get("stamp") or ""
    snap_path = f".context/{PANIC_DIR}/{stamp}/" if stamp else f".context/{PANIC_DIR}/"
    file_word = "file" if n == 1 else "files"
    return (
        f"Model context window overflow. Celeborn saved you — {n} {file_word} snapshotted to "
        f"{snap_path} (restore: `celeborn restore`). Nothing lost to context compaction. "
        f"To avoid last-minute saves, `/clear` before the context window limit. "
        f"[read more: {PANIC_READ_MORE}]"
    )


def cmd_panic_save(args):
    """`celeborn panic-save` — snapshot the authored tiers to a restore point and print a visible
    "🏹 Celeborn saved your session" line. Runs automatically from the PreCompact hook (compaction
    imminent) and is callable by hand. The continuous Stop-hook `capture` already salvages live
    transcript work every turn; this adds the deterministic, restorable snapshot + the felt moment."""
    ctx = require_context(args)
    info = _do_panic_save(ctx, reason=getattr(args, "reason", None) or "manual",
                          session=getattr(args, "session", None),
                          keep=getattr(args, "keep", None) or PANIC_KEEP)
    m = _load_metrics(ctx)
    m["panic_saves"] = int(m.get("panic_saves", 0) or 0) + 1
    _save_metrics(ctx, m)
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
        return
    if not getattr(args, "quiet", False):
        print(_panic_save_line(info))


def cmd_restore(args):
    """`celeborn restore` — bring back a panic-save snapshot. Default restores the most recent; --from
    <stamp> picks one; --list shows what's available. The current files are themselves panic-saved
    first (reason "pre-restore"), so a restore is always reversible."""
    ctx = require_context(args)
    snaps = _panic_snapshots(ctx)
    if getattr(args, "list", False):
        if not snaps:
            print("No panic-saves yet.")
            return
        for p in reversed(snaps):               # newest first
            meta = {}
            try:
                meta = json.loads((p / "meta.json").read_text())
            except (OSError, ValueError):
                pass
            print(f"  {p.name}  ({meta.get('reason', '?')}, {len(meta.get('files', []))} files, "
                  f"{meta.get('at', '?')})")
        return
    if not snaps:
        die("no panic-saves to restore from.")
    want = getattr(args, "from_", None)
    if want:
        chosen = next((p for p in snaps if p.name == want), None)
        if chosen is None:
            die(f"no panic-save named {want!r}. Try `celeborn restore --list`.")
    else:
        chosen = snaps[-1]                       # most recent
    # Read the chosen snapshot into memory BEFORE backing up current state — the pre-restore save
    # FIFO-prunes, and could otherwise delete `chosen` out from under us.
    payload = {}
    for rel in PANIC_SAVE_FILES:
        src = chosen / rel
        if src.is_file():
            try:
                payload[rel] = src.read_bytes()
            except OSError:
                pass
    _do_panic_save(ctx, reason="pre-restore", keep=getattr(args, "keep", None) or PANIC_KEEP)
    restored = []
    for rel, data in payload.items():
        dst = ctx / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.write_bytes(data)
            restored.append(rel)
        except OSError:
            pass
    if getattr(args, "json", False):
        print(json.dumps({"restored_from": chosen.name, "files": restored}, indent=2))
        return
    print(f"🏹 Restored {len(restored)} file(s) from .context/{PANIC_DIR}/{chosen.name}/ "
          f"(current state backed up first — `celeborn restore --list`).")


# --------------------------------------------------------------------------- version / update check

GITHUB_REPO = "cloud-dancer-labs/celeborn"  # where Celeborn looks back to for updates


def _local_version() -> str:
    """Celeborn's version. Prefers installed package metadata (the only source that exists for a
    pip/uv install); falls back to the repo's pyproject.toml in a source checkout (regex — no toml dep)."""
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("celeborn")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    # Frozen binaries have no package metadata or source tree; the build bakes a VERSION file
    # alongside the bundled data (celeborn_refs/VERSION).
    try:
        baked = (DATA_DIR / "VERSION").read_text().strip()
        if baked:
            return baked
    except OSError:
        pass
    try:
        m = re.search(r'^version\s*=\s*"([^"]+)"', (REPO_ROOT / "pyproject.toml").read_text(), re.M)
        return m.group(1) if m else "unknown"
    except OSError:
        return "unknown"


def _git_head(root: Path):
    """Short+full HEAD sha if `root` is a git checkout, else None. Never raises."""
    if not (root / ".git").exists():
        return None
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


def _fetch_url(url: str, accept: str = "application/vnd.github+json") -> str:
    """GET a URL and return the body text. Lazy-imports urllib so the core stays import-light.
    Tests monkeypatch this. Raises urllib/OS errors on failure (caller handles offline)."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "celeborn-update-check", "Accept": accept})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode()


def cmd_version(args):
    """Print Celeborn's version (and git HEAD). With --check, look back at GitHub to see whether a
    newer Celeborn is available. The plain form is offline; only --check touches the network."""
    local_v = _local_version()
    head = _git_head(REPO_ROOT)
    line = f"Celeborn {local_v}"
    if head:
        # Source checkout: surface the repo path (used by the `git pull` update hint below).
        line += f" (git {head[:8]})  ·  {REPO_ROOT}"
    print(line)
    if not getattr(args, "check", False):
        return

    import json as _json
    import urllib.error
    try:
        if head:
            # git checkout: compare local HEAD against origin/main via the GitHub API.
            latest = _json.loads(_fetch_url(f"https://api.github.com/repos/{GITHUB_REPO}/commits/main"))
            remote = latest.get("sha", "")
            if not remote:
                warn("update check: couldn't read latest commit from GitHub."); return
            if remote == head:
                ok("up to date with origin/main."); return
            behind = None
            try:
                cmp = _json.loads(_fetch_url(
                    f"https://api.github.com/repos/{GITHUB_REPO}/compare/{head}...main"))
                if cmp.get("status") == "identical":
                    ok("up to date with origin/main."); return
                behind = cmp.get("ahead_by")  # commits on main not in local HEAD
            except (urllib.error.URLError, OSError, ValueError):
                pass
            n = f"{behind} commit(s) behind" if isinstance(behind, int) else "behind"
            warn(f"a newer Celeborn is available — {n} origin/main (latest {remote[:8]}).")
            print(f"  Update:  git -C \"{REPO_ROOT}\" pull")
        else:
            # non-git install (e.g. pip): compare the version in main's pyproject.toml.
            pp = _fetch_url(f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/pyproject.toml",
                            accept="text/plain")
            m = re.search(r'^version\s*=\s*"([^"]+)"', pp, re.M)
            remote_v = m.group(1) if m else None
            if not remote_v:
                warn("update check: couldn't read latest version from GitHub."); return
            if remote_v == local_v:
                ok(f"up to date (latest {remote_v})."); return
            warn(f"a newer Celeborn may be available: {remote_v} (you have {local_v}).")
            print(f"  Update:  reinstall from https://github.com/{GITHUB_REPO}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        warn(f"update check skipped — couldn't reach GitHub ({e}).")


def cmd_about(args):
    """Print Celeborn Code's identity + canonical links, and disambiguate from the same-named
    projects (Apache Celeborn; the frkngksl/Celeborn Windows tool). This exists so an agent that
    installed 'celeborn' mid-conversation can run `celeborn about` and confirm it grabbed the
    coding-agent context substrate — not one of the namesakes. Offline; no network."""
    local_v = _local_version()
    print(f"Celeborn Code {local_v} — a long-term context substrate for coding agents.")
    print("  Memory on disk for AI coding agents (Claude Code, Codex, Grok): survives compaction,")
    print("  keeps its place across sessions, saves tokens.")
    print()
    print(f"  Install:  uv tool install celeborn   (or: pip install celeborn)")
    print(f"  PyPI:     https://pypi.org/project/celeborn/")
    print(f"  Source:   https://github.com/{GITHUB_REPO}")
    print(f"  Home:     {CELEBORN_REGISTER_URL}")
    print()
    print("  This is NOT the other projects named 'Celeborn':")
    print("    · Apache Celeborn  — a Spark/Flink remote shuffle service (big-data infra). Unrelated.")
    print("    · frkngksl/Celeborn — a Windows API-unhooking research tool. Unrelated.")
    print("  If you were told to install 'Celeborn' for coding-agent memory, this is the right one.")


# --------------------------------------------------------------------------- install integrity (detection)

INTEGRITY_MANIFEST = "integrity.json"     # shipped inside DATA_DIR (celeborn_refs) by the release build
INTEGRITY_SCHEMA = "celeborn-integrity/1"
# The core modules whose bytes define behavior. If a user edits these in place, the install no longer
# matches the published release — that is what we DETECT (we cannot, and do not try to, prevent it), so
# a "I edited celeborn.py and it broke" situation self-reports instead of becoming a confused bug report.
INTEGRITY_MODULES = ("celeborn.py", "celeborn_sync.py", "celeborn_jira.py")


def _integrity_manifest_path() -> Path:
    """Where the per-version checksum manifest lives. The default ships inside the data package so
    pip/uv/wheel installs carry it; CELEBORN_INTEGRITY_MANIFEST overrides it (used by the release build
    that generates it, and by tests)."""
    import os
    override = os.environ.get("CELEBORN_INTEGRITY_MANIFEST")
    return Path(override) if override else (DATA_DIR / INTEGRITY_MANIFEST)


def _sha256_file(p: Path) -> str | None:
    import hashlib
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _compute_integrity(modules: tuple = INTEGRITY_MODULES) -> dict:
    """sha256 of each shipped core module that exists beside this file, keyed by filename."""
    out = {}
    for name in modules:
        digest = _sha256_file(SCRIPT_DIR / name)
        if digest:
            out[name] = digest
    return out


def _load_integrity_manifest() -> dict | None:
    p = _integrity_manifest_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def integrity_status() -> dict:
    """Detection, not prevention. Returns
        {'state': 'ok'|'modified'|'unverified', 'modified': [filenames], 'reason': str}.
    'unverified' = no manifest shipped (source/dev/editable checkout) or a version mismatch — we stay
    SILENT in that case so a contributor editing the tree is never nagged. Only a released install
    (manifest present AND version match) can ever report 'modified'."""
    man = _load_integrity_manifest()
    files = man.get("files") if isinstance(man, dict) else None
    if not isinstance(files, dict) or not files:
        return {"state": "unverified", "modified": [], "reason": "no integrity manifest (source/dev install)"}
    if man.get("version") and man["version"] != _local_version():
        return {"state": "unverified", "modified": [], "reason": "manifest is for a different version"}
    current = _compute_integrity(tuple(files.keys()))
    modified = sorted(name for name, digest in files.items() if current.get(name) != digest)
    if modified:
        return {"state": "modified", "modified": modified, "reason": ""}
    return {"state": "ok", "modified": [], "reason": ""}


def _integrity_notice() -> str:
    """One-line notice for the SessionStart Orient load when the install has been modified. Empty
    string when ok/unverified. Best-effort + never raises (the hook must degrade to silence)."""
    try:
        st = integrity_status()
    except Exception:
        return ""
    if st["state"] != "modified":
        return ""
    return ("⚠ Celeborn integrity: modified install detected (" + ", ".join(st["modified"]) + ") — run "
            "`celeborn doctor` for details. Reinstall to reset; local edits are unsupported (submit a PR).")


def cmd_integrity(args):
    """`celeborn integrity` — verify the installed core modules match the published per-version
    checksum manifest. `--write` (re)generates the manifest from the current files — a release/build
    step, not for end users. Detection only: a mismatch means the install was edited in place."""
    if getattr(args, "write", False):
        man = {
            "schema": INTEGRITY_SCHEMA,
            "version": _local_version(),
            "generated_at": now_iso(),
            "files": _compute_integrity(),
        }
        p = _integrity_manifest_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(man, indent=2) + "\n")
        ok(f"wrote integrity manifest ({len(man['files'])} file(s)) → {p}")
        return
    st = integrity_status()
    if st["state"] == "ok":
        ok("install integrity verified — core modules match the published release.")
    elif st["state"] == "unverified":
        info(f"integrity check skipped — {st['reason']} (nothing to verify).")
    else:
        warn("modified install detected — these core module(s) differ from the published release:")
        for name in st["modified"]:
            print(f"      {name}")
        print("  Fix:  reinstall to reset (`uv tool install --force celeborn` / `pipx reinstall celeborn`).")
        print("        Local edits to the installed CLI are unsupported — submit a PR instead.")
        sys.exit(1)


# --------------------------------------------------------------------------- skill advisor (t70)
#
# A throughput + quality layer (sibling of `_integrity_notice`). It detects FRICTION (the human is a
# bottleneck) and recommends the harness's fix. The engine is HARNESS-NEUTRAL: it speaks in canonical
# signals + neutral "intents" and never names a slash command or `.claude/` path itself. Each harness
# is a thin HarnessAdapter that (a) normalizes its raw friction into canonical signals — the same way
# `_TOOL_MAP` normalizes tool names — and (b) renders an intent into that harness's idiom + channel.
# Claude is the implicit default adapter that used to be hard-coded; an unknown harness degrades to the
# NeutralAdapter (plain instruction + the literal `celeborn` command), never an error. Grok/Codex
# adapters are future subclasses living in their own bridges (grok/, codex/) — core stays untouched.

# A neutral recommendation: a canonical `trigger` signal → an intent, with a harness-agnostic fallback
# string so an unknown harness still gets the advice. Phase 1 ships exactly one.
ADVISOR_INTENTS = {
    "reduce-permission-friction": {
        "trigger": "permission-friction",
        "summary": "Repeated permission approvals are interrupting the loop.",
        "auto_actionable": True,
        "neutral": ("Permission friction: {count} over-specific allow-rules that never re-match. "
                    "Run `celeborn permissions --suggest` to collapse them into reusable wildcard rules."),
    },
    # Phase 3 — portable quality recommendations. Celeborn can't RUN a review skill, so these
    # *recommend* one: the `neutral` text is a self-contained checklist (the portable "prompt pack")
    # for harnesses without the skill; the ClaudeAdapter render points at the matching slash command.
    "security-review-changes": {
        "trigger": "sensitive-changes",
        "summary": "Uncommitted changes touch security-sensitive paths — do a security pass.",
        "auto_actionable": False,
        "neutral": ("Security review: uncommitted changes touch sensitive paths ({files}). Before "
                    "finishing, review them for authn/authz gaps, secret handling, input validation, and "
                    "injection/SSRF/path-traversal."),
    },
    "review-changes": {
        "trigger": "uncommitted-changes",
        "summary": "Substantial uncommitted changes — review before calling it done.",
        "auto_actionable": False,
        "neutral": ("Code review: {count} changed code file(s) uncommitted. Before finishing, read each "
                    "hunk for correctness, edge cases, error paths, and leftover debug/TODOs — then verify "
                    "the behavior end to end, don't just trust that it compiles."),
    },
    # Phase 4 — throughput / autonomy. #1 auto-fires on a large changeset; #2/#3 are on-demand
    # (they need conversation judgment the CLI can't see) — surfaced only via `celeborn advise
    # --throughput`, never auto-nagged. Renders map to each harness's equivalent, else stay generic.
    "parallelize-large-changeset": {
        "trigger": "large-changeset",
        "summary": "Large changeset — parallelize the review instead of one linear pass.",
        "auto_actionable": False,
        "neutral": ("Large changeset ({count} code files): split the review across independent chunks / "
                    "parallel workers rather than one linear pass — it's faster and catches more."),
    },
    # CELE-t224 — secrets discipline. Fires when a repo-root .env* file holds a live-looking secret
    # VALUE (not just a declared name): the safe path is the Pro vault, and the nudge is how a vibe
    # coder learns it exists before the key lands in a commit or a prompt.
    "vault-disk-secrets": {
        "trigger": "secrets-on-disk",
        "summary": "Live secret values sitting in .env files — move them into the vault.",
        "auto_actionable": False,
        "neutral": ("Secrets discipline: {count} live-looking secret value(s) in {files}. Move each into "
                    "the encrypted vault — `celeborn secrets set <NAME>` (Pro) — then delete the line; "
                    "commands read them back at run time via `celeborn secrets run -- <cmd>`."),
    },
    "spawn-tangent": {
        "trigger": None,
        "on_demand": True,
        "summary": "Drifted onto unrelated work? Peel it into its own task.",
        "auto_actionable": False,
        "neutral": ("Working on something unrelated to the current task? Split it into its own "
                    "task/session so this thread stays focused and reviewable."),
    },
    "unattended-run": {
        "trigger": None,
        "on_demand": True,
        "summary": "Long unattended run? Drive it in checkpointed batches.",
        "auto_actionable": False,
        "neutral": ("For a long unattended run, drive it in batches and checkpoint state between them "
                    "so a restart resumes instead of redoing work."),
    },
}


def _signal_to_intent(sig: dict) -> str | None:
    """Map a canonical friction signal to the highest-value intent it triggers."""
    trig = sig.get("signal")
    for iid, spec in ADVISOR_INTENTS.items():
        if spec["trigger"] == trig:
            return iid
    return None


# The ONLY families a permission rule is auto-widened into: read-only inspection commands + the
# project's own trusted Celeborn CLI + the test runners. Each entry is a prefix to PRESERVE; the
# generalized rule is `Bash(<prefix>*)`. A literal whose command doesn't start with one of these is
# NEVER widened (it stays verbatim and is tallied as a "skipped bottleneck"). This deliberately
# mirrors the hand-written .claude/settings.json the user produced via `/fewer-permission-prompts`.
_SAFE_BASH_PREFIXES = (
    "python3 scripts/celeborn.py ",
    "python scripts/celeborn.py ",
    "scripts/celeborn.py ",
    "celeborn ",
    "sed -n ",
    "grep ",
    "git --no-pager diff ",
    "git --no-pager log ",
    "git --no-pager show ",
    "git diff ",
    "git log ",
    "git show ",
    "PYTHONPATH=scripts python3 -m unittest ",
    "python3 -m unittest ",
    "python -m unittest ",
    "python3 -m pytest ",
    "python -m pytest ",
    "python3 -m py_compile ",
)


def _parse_bash_rule(rule: str) -> str | None:
    """`'Bash(grep -n foo)'` → `'grep -n foo'`; a non-Bash permission (MCP/tool name) → None."""
    if rule.startswith("Bash(") and rule.endswith(")"):
        return rule[5:-1]
    return None


def _match_safe_family(inner: str) -> str | None:
    """The generalized wildcard rule that SAFELY subsumes this literal command, or None when the
    command falls outside the read-only/trusted allow-set. Longest prefix wins so a specific runner
    (`python3 -m unittest `) beats a shorter accidental prefix."""
    for pre in sorted(_SAFE_BASH_PREFIXES, key=len, reverse=True):
        if inner.startswith(pre):
            return f"Bash({pre}*)"
    return None


def _bottleneck_key(inner: str) -> str:
    """A short family label for a skipped (un-widenable) literal — the leading command, plus the
    subcommand when the head is a multiplexer (git/python/npm/…). Used to tally remaining friction."""
    toks = inner.split()
    if not toks:
        return "?"
    head = toks[0]
    if head in ("git", "python3", "python", "npm", "npx", "uv", "pip", "pip3",
                "cargo", "go", "docker", "make") and len(toks) > 1:
        return f"{head} {toks[1]}"
    return head


def _count_literal_bash_rules(allow: list) -> int:
    """How many allow-rules are over-specific Bash literals (a `Bash(cmd args)` that does NOT end in
    `*`). Wildcard rules and non-Bash permissions don't count."""
    n = 0
    for rule in allow or []:
        inner = _parse_bash_rule(rule) if isinstance(rule, str) else None
        if inner is not None and not inner.rstrip().endswith("*"):
            n += 1
    return n


def _count_generalizable_bash_rules(allow: list) -> int:
    """The ACTIONABLE friction signal: literals this tool can SAFELY collapse into a wildcard. The
    un-widenable bottlenecks (curl/rm/git-commit/…) are deliberately excluded — so once the safe rules
    are applied this drops to 0 and the advisor goes quiet, even though raw literals still remain."""
    n = 0
    for rule in allow or []:
        inner = _parse_bash_rule(rule) if isinstance(rule, str) else None
        if inner is not None and not inner.rstrip().endswith("*") and _match_safe_family(inner):
            n += 1
    return n


def _generalize_allow(rules: list) -> tuple[list, int, dict]:
    """Collapse an allow-list. Returns (new_rules, generalized_count, skipped_ledger).

    Safe-family literals are replaced by one shared wildcard each; already-general rules and non-Bash
    permissions pass through untouched; literals outside the safe set are KEPT VERBATIM and tallied by
    family in `skipped` (the bottlenecks Celeborn can't safely remove). Order: the new generalizations
    first, then everything preserved — deduped, original order otherwise kept."""
    family_rules: list = []      # the wildcards we synthesize, in first-seen order
    preserved: list = []         # general rules + non-Bash perms + skipped literals
    seen_family: set = set()
    generalized = 0
    skipped: dict = {}
    for rule in rules or []:
        inner = _parse_bash_rule(rule) if isinstance(rule, str) else None
        if inner is None or inner.rstrip().endswith("*"):
            preserved.append(rule)               # non-Bash perm or already a wildcard → keep
            continue
        fam = _match_safe_family(inner)
        if fam:
            generalized += 1
            if fam not in seen_family:
                seen_family.add(fam)
                family_rules.append(fam)
        else:
            skipped[_bottleneck_key(inner)] = skipped.get(_bottleneck_key(inner), 0) + 1
            preserved.append(rule)               # un-widenable literal → keep verbatim
    new_rules: list = []
    for r in family_rules + preserved:
        if r not in new_rules:
            new_rules.append(r)
    return new_rules, generalized, skipped


# Phase 3 — harness-agnostic quality signals derived from the working-tree diff.
_CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".sql", ".go", ".rs", ".rb", ".java",
                  ".kt", ".c", ".cc", ".cpp", ".h", ".hpp", ".php", ".swift", ".scala", ".cs")


def _changed_files(root: Path) -> list:
    """Repo-relative paths with uncommitted changes (staged + unstaged + untracked) via
    `git status --porcelain`. Returns [] outside a git checkout or on any error — a non-git project
    is never nagged. Rename records resolve to the new path; git-quoted paths are de-quoted best-effort."""
    if not (root / ".git").exists():
        return []
    import subprocess
    try:
        # --untracked-files=all expands wholly-new directories to individual files (git otherwise
        # collapses them to `dir/`, under-counting the review heuristic); .gitignore is still honored.
        r = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    if r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:                  # rename/copy record: keep the destination path
            path = path.split(" -> ", 1)[1]
        out.append(path.strip().strip('"'))
    return out


def _is_code_file(rel: str) -> bool:
    return rel.lower().endswith(_CODE_SUFFIXES)


def _is_sensitive(rel: str, globs: list) -> bool:
    """True if a repo-relative path matches any sensitive glob — tested against both the full path and
    the bare filename so `*auth*` catches `lib/auth.ts` and `supabase/**` catches a nested file."""
    import fnmatch
    rl = (rel or "").replace("\\", "/")
    base = rl.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rl, g) or fnmatch.fnmatch(base, g) for g in (globs or []))


def _change_review_signals(ctx: Path) -> list:
    """Harness-agnostic Phase-3 signals from the working tree: changes touching sensitive paths
    (→ security review) and a substantial count of changed code files (→ code review + verify).
    Best-effort and read-only; [] outside a git repo. Sensitive is listed first so it wins the single
    orient-nudge slot when both fire."""
    try:
        acfg = _advisor_config(ctx)
    except Exception:
        return []
    files = _changed_files(ctx.parent)
    if not files:
        return []
    sensitive = sorted({f for f in files if _is_sensitive(f, acfg.get("sensitive_globs") or [])})
    code = sorted({f for f in files if _is_code_file(f)})
    sigs = []
    if sensitive:
        shown = ", ".join(sensitive[:4]) + ("…" if len(sensitive) > 4 else "")
        sigs.append({"signal": "sensitive-changes", "count": len(sensitive), "files": shown})
    if len(code) >= int(acfg.get("review_min_files", 3)):
        sigs.append({"signal": "uncommitted-changes", "count": len(code)})
    if len(code) >= int(acfg.get("parallelize_min_files", 12)):
        sigs.append({"signal": "large-changeset", "count": len(code)})  # Phase 4: fan out the review
    return sigs


def _secrets_on_disk_signal(ctx: Path) -> list:
    """CELE-t224 discipline signal: live secret VALUES in repo-root .env* files. Independent of git
    state (a committed-then-ignored .env is just as leaky), so it is produced alongside — not inside —
    the change-derived signals. Best-effort; [] on any error."""
    try:
        hits = _env_file_secret_hits(ctx.parent, load_config(ctx).get("secret_patterns", []))
    except Exception:
        return []
    if not hits:
        return []
    files = ", ".join(sorted({f for f, _ in hits}))
    return [{"signal": "secrets-on-disk", "count": len(hits), "files": files}]


class HarnessAdapter:
    """The harness seam. The base class is also the NEUTRAL fallback: it produces the harness-agnostic
    quality signals (Phase 3), exposes no permission target, and renders an intent as the neutral
    instruction. Concrete adapters override for their host. Core calls only these — never a
    harness-specific path/name."""

    name = "neutral"

    def friction_signals(self, ctx: Path, session: str | None = None) -> list:
        # Quality signals are host-independent (a git diff is a git diff), so every harness — including
        # the neutral fallback Grok/Codex ride — gets them. Permission friction is added per-adapter.
        return _change_review_signals(ctx) + _secrets_on_disk_signal(ctx)

    def permission_target(self, ctx: Path, shared: bool = False) -> tuple:
        return (None, None)

    def render(self, intent: str, signal: dict | None = None) -> tuple:
        spec = ADVISOR_INTENTS.get(intent, {})
        sig = signal or {}
        text = (spec.get("neutral") or spec.get("summary") or "").format(
            count=sig.get("count", "several"), files=sig.get("files", "several files"))
        return (text, "instruction")

    def inject(self, text: str, channel: str) -> str:
        # Neutral hosts have no structured channel — the text is returned for the caller to place.
        return text

    def quality_hook_target(self, ctx: Path, shared: bool = True) -> tuple:
        # No structured PostToolUse/Stop hooks on a neutral host — fall back to an AGENTS.md instruction
        # so a Codex/Grok-style harness that auto-loads it still gets the "run tests after editing" rule.
        return ("agents-md", ctx.parent / "AGENTS.md")


class ClaudeAdapter(HarnessAdapter):
    """Claude Code: per-command `permissions.allow` in `.claude/settings*.json`, slash-command skills,
    and the SessionStart `hookSpecificOutput` orient channel. This is the path that was implicit in
    core before t70 — now an explicit adapter so the engine no longer hard-codes Claude."""

    name = "claude"

    def permission_target(self, ctx: Path, shared: bool = False) -> tuple:
        base = ctx.parent / ".claude"
        fname = "settings.json" if shared else "settings.local.json"
        return (base / fname, "per-command-allow")

    def quality_hook_target(self, ctx: Path, shared: bool = True) -> tuple:
        # Quality gates ride PostToolUse + Stop in settings.json — shared by default (they help every
        # contributor on the checkout); `--local` writes the personal settings.local.json instead.
        base = ctx.parent / ".claude"
        fname = "settings.json" if shared else "settings.local.json"
        return ("hooks-json", base / fname)

    def _allow(self, ctx: Path) -> list:
        out: list = []
        for shared in (False, True):
            target, _how = self.permission_target(ctx, shared=shared)
            if target and target.is_file():
                try:
                    data = json.loads(target.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                out.extend((data.get("permissions") or {}).get("allow") or [])
        return out

    def friction_signals(self, ctx: Path, session: str | None = None) -> list:
        # Host-independent quality signals (Phase 3) first, then Claude's own permission signal. Count
        # only the literals this tool can collapse — the moment the safe rules apply the signal clears,
        # even though un-widenable bottlenecks (curl/rm/…) still remain.
        sigs = list(super().friction_signals(ctx, session))
        n = _count_generalizable_bash_rules(self._allow(ctx))
        thresh = _advisor_config(ctx)["permission_bloat_min"]
        if n >= thresh:
            target, _how = self.permission_target(ctx)
            sigs.append({"signal": "permission-friction", "count": n, "file": str(target)})
        return sigs

    def render(self, intent: str, signal: dict | None = None) -> tuple:
        sig = signal or {}
        if intent == "reduce-permission-friction":
            n = sig.get("count")
            cnt = f"{n} " if n else ""
            text = (f"🏹 Celeborn advisor —> {cnt}repeated approvals can be auto-generalized into reusable "
                    f"wildcard rules. Fix: run `celeborn permissions --suggest` then `--apply` (or the "
                    f"`/fewer-permission-prompts` skill).")
            return (text, "orient")
        if intent == "security-review-changes":
            files = sig.get("files") or "sensitive paths"
            text = (f"🏹 Celeborn advisor —> uncommitted changes touch sensitive paths ({files}). Run the "
                    f"`/security-review` skill before finishing — check authn/authz, secret handling, "
                    f"input validation, and injection/SSRF.")
            return (text, "orient")
        if intent == "review-changes":
            n = sig.get("count")
            cnt = f"{n} " if n else ""
            text = (f"🏹 Celeborn advisor —> {cnt}uncommitted code file(s) — run `/code-review` then "
                    f"`/verify` before calling it done (correctness + edge cases, then confirm behavior).")
            return (text, "orient")
        if intent == "parallelize-large-changeset":
            n = sig.get("count")
            cnt = f"{n} " if n else ""
            text = (f"🏹 Celeborn advisor —> large changeset ({cnt}files) — fan the review out: spawn "
                    f"subagents (the Task tool) per area, or a Workflow for staged review, instead of one "
                    f"linear pass.")
            return (text, "orient")
        if intent == "spawn-tangent":
            text = ("🏹 Celeborn advisor —> off on a tangent? Use spawn_task (or a fresh session) to peel "
                    "the unrelated fix into its own thread — keeps this change focused and reviewable.")
            return (text, "instruction")
        if intent == "unattended-run":
            text = ("🏹 Celeborn advisor —> long unattended run ahead? `/loop` repeats a step on an "
                    "interval; `/elves` runs multi-batch autonomous development — both checkpoint via "
                    "Celeborn so a restart resumes.")
            return (text, "instruction")
        return super().render(intent, signal)


# --------------------------------------------------------------------------- Grok / Codex adapters
#
# Grok Build and the OpenAI Codex CLI both ride the NEUTRAL surface for quality (a git diff is a git
# diff) and orient via a pending file rather than a structured SessionStart channel — so neither has
# Claude's slash commands. `_branded_quality_render` is the shared advisor voice for both: the same
# branded `🏹 Celeborn advisor —>` text Claude emits, minus the `/code-review`-style command pointers.

def _branded_quality_render(intent: str, signal: dict | None) -> tuple | None:
    """The advisor render shared by harnesses with no slash-command surface (Grok, Codex). Returns
    (text, channel) for the quality + throughput intents, else None so the caller can fall through to
    a harness-specific intent (e.g. Codex's permission hint) or the neutral base render."""
    sig = signal or {}
    if intent == "security-review-changes":
        files = sig.get("files") or "sensitive paths"
        return ((f"🏹 Celeborn advisor —> uncommitted changes touch sensitive paths ({files}). Before "
                 f"finishing, do a security pass — authn/authz gaps, secret handling, input validation, "
                 f"and injection/SSRF/path-traversal."), "orient")
    if intent == "review-changes":
        n = sig.get("count")
        cnt = f"{n} " if n else ""
        return ((f"🏹 Celeborn advisor —> {cnt}uncommitted code file(s) — review each hunk for correctness, "
                 f"edge cases, and error paths, then verify the behavior end to end before calling it done."),
                "orient")
    if intent == "parallelize-large-changeset":
        n = sig.get("count")
        cnt = f"{n} " if n else ""
        return ((f"🏹 Celeborn advisor —> large changeset ({cnt}files) — split the review across independent "
                 f"chunks (a separate session/agent per area) instead of one linear pass."), "orient")
    if intent == "spawn-tangent":
        return (("🏹 Celeborn advisor —> off on a tangent? Peel the unrelated fix into its own Celeborn "
                 "task/session so this change stays focused and reviewable."), "instruction")
    if intent == "unattended-run":
        return (("🏹 Celeborn advisor —> long unattended run ahead? Drive it in checkpointed batches — "
                 "update `.context` between them so a restart resumes instead of redoing work."), "instruction")
    return None


def _codex_home() -> Path:
    import os
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _codex_permission_status(root: Path) -> dict:
    """Read Codex's coarse permission lever from ~/.codex/config.toml WITHOUT a TOML parser (stdlib
    regex, tolerant of any layout): global `approval_policy`/`sandbox_mode`, plus whether THIS project
    is trusted via a `[projects."<root>"]` table with `trust_level = "trusted"`. Mirrors the codex/
    bridge so core's advisor sees the same friction the bridge does."""
    cfg = _codex_home() / "config.toml"
    status = {"config": str(cfg), "exists": cfg.is_file(),
              "approval_policy": None, "sandbox_mode": None, "project_trusted": False}
    if not cfg.is_file():
        return status
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return status
    m = re.search(r'(?m)^\s*approval_policy\s*=\s*"([^"]+)"', text)
    if m:
        status["approval_policy"] = m.group(1)
    m = re.search(r'(?m)^\s*sandbox_mode\s*=\s*"([^"]+)"', text)
    if m:
        status["sandbox_mode"] = m.group(1)
    rp = str(root.resolve())
    hdr = re.search(r'(?m)^\s*\[projects\.\"' + re.escape(rp) + r'\"\]\s*$', text)
    if hdr:
        rest = text[hdr.end():]
        nxt = re.search(r'(?m)^\s*\[', rest)
        block = rest[: nxt.start()] if nxt else rest
        if re.search(r'(?m)^\s*trust_level\s*=\s*"trusted"', block):
            status["project_trusted"] = True
    return status


def _codex_interactive(status: dict) -> bool:
    # Default (no config / unset) is interactive `on-request`; `never` is the only non-prompting mode.
    pol = status.get("approval_policy")
    if pol is None:
        return True
    return pol in ("untrusted", "on-request", "on-failure")


class GrokAdapter(HarnessAdapter):
    """Grok Build: project rules auto-load from `.grok/rules/celeborn.md` and orient rides a pending
    file (no structured SessionStart channel). Grok has NO per-command permission allow-list — its
    rules file isn't a lever — so permission_target/friction_signals stay neutral (quality only). The
    advisor renders Grok-flavored guidance (no Claude slash commands)."""

    name = "grok"

    def render(self, intent: str, signal: dict | None = None) -> tuple:
        r = _branded_quality_render(intent, signal)
        return r if r is not None else super().render(intent, signal)


class CodexAdapter(HarnessAdapter):
    """OpenAI Codex CLI: orient rides AGENTS.md + a pending file. The permission lever is COARSE —
    ~/.codex/config.toml (`approval_policy`/`sandbox_mode` + a per-project `[projects."<root>"]`
    trust_level), NOT a per-command allow-list, so `celeborn permissions` declines to generalize it.
    friction_signals flags an interactive, untrusted workspace; render emits the config.toml trust
    hint. Mirrors the codex/ bridge so core's `celeborn advise` is Codex-aware under CELEBORN_HARNESS=codex."""

    name = "codex"

    def permission_target(self, ctx: Path, shared: bool = False) -> tuple:
        # Coarse workspace-trust lever — deliberately NOT "per-command-allow", so cmd_permissions
        # reports it as a coarse lever rather than trying to widen it like Claude's allow-list.
        return (_codex_home() / "config.toml", "workspace-trust")

    def friction_signals(self, ctx: Path, session: str | None = None) -> list:
        sigs = list(super().friction_signals(ctx, session))
        status = _codex_permission_status(ctx.parent)
        if not status["project_trusted"] and _codex_interactive(status):
            sigs.append({"signal": "permission-friction",
                         "approval_policy": status["approval_policy"] or "on-request",
                         "config": status["config"]})
        return sigs

    def render(self, intent: str, signal: dict | None = None) -> tuple:
        sig = signal or {}
        if intent == "reduce-permission-friction":
            cfg = sig.get("config") or str(_codex_home() / "config.toml")
            pol = sig.get("approval_policy") or "on-request"
            text = (f"🏹 Celeborn advisor —> Codex still pauses for approval in this workspace "
                    f"(approval_policy={pol}). Trust it once so Codex stops re-prompting — add a "
                    f'`[projects."<root>"]` table with `trust_level = "trusted"` to {cfg} (or set '
                    f'`approval_policy = "never"` + `sandbox_mode = "workspace-write"` globally). The '
                    f"Codex lever is coarse — a workspace trust flag, not per-command rules like Claude.")
            return (text, "orient")
        r = _branded_quality_render(intent, sig)
        return r if r is not None else super().render(intent, sig)


# --------------------------------------------------------------------------- OpenCode (CELE-t139)
#
# OpenCode plugin hook name -> the `celeborn hook <event>` event it drives. The plugin normally
# shells the Celeborn event vocabulary directly (HOOK_EVENTS needs no new entries), so for those
# names this map is a passthrough; the dotted OpenCode names are kept so a raw SDK event name
# arriving via the translation path still routes instead of silently mis-dispatching.
# Sourced from plan/opencode-integration.md §3; reference stub: opencode/scripts/opencode_celeborn.py.
OPENCODE_HOOK_TO_CELEBORN_EVENT = {
    "session.created": "session-start",
    "message.updated": "user-prompt-submit",
    "tool.execute.before": "pre-tool-use",
    "tool.execute.after": "post-tool-use",   # touch + activity record, P4 (CELE-t141)
    "file.edited": "post-tool-use",          # the edit signal that doesn't ride a tool call (P4)
    "session.idle": "stop",
    "session.error": "session-end",
    # session.compacted is deliberately ABSENT (CELE-t142): the plugin maps it straight to
    # `celeborn record compaction` — the compaction-succeeded metric — with no hook call at all.
    # Routing it here into pre-compact would fire a second panic-save per compaction; unmapped, a
    # raw event name arriving via the translation path passes through and dispatch_hook no-ops.
    "experimental.session.compacting": "pre-compact",
}

# OpenCode built-in tool name -> gate treatment (plan §2.1 normalization table). Reference data for
# P3's card gate (CELE-t140) — NOT consulted by _card_gate_pre_tool_use yet.
OPENCODE_TOOL_GATE_CLASS = {
    "edit": "gated-edit", "write": "gated-edit", "patch": "gated-edit",
    "webfetch": "gated-research", "websearch": "gated-research",
    "task": "gated-delegation",
    "bash": "never-gated", "read": "never-gated", "glob": "never-gated",
    "grep": "never-gated", "list": "never-gated",
}

# OpenCode built-in tool name -> the Claude-cased name the pre-tool-use deny chain matches on
# (CELE-t140). The redirect/publish guards match tool_name == "Bash" and the card-less gate matches
# _CARD_GATED_TOOLS exactly, so a lowercase OpenCode name would silently skip all three; normalizing
# here — in the one translation seam — keeps dispatch_hook single-shape (INTEGRATION.md §3). `patch`
# has no Claude twin; it maps to Edit (same gated-edit class). Unknown names (MCP/custom tools) pass
# through unchanged: the deny chain has no opinion on them and the autonomy gate lowercases anyway.
OPENCODE_TOOL_TO_CLAUDE = {
    "edit": "Edit", "write": "Write", "patch": "Edit",
    "webfetch": "WebFetch", "websearch": "WebSearch",
    "task": "Task",
    "bash": "Bash", "read": "Read", "glob": "Glob", "grep": "Grep", "list": "List",
}


def _opencode_to_claude_shape(opencode_event: str, opencode_payload: dict) -> tuple:
    """Translate one OpenCode plugin hook call into the (event, payload) shape dispatch_hook()
    already understands (`session_id`, `tool_name`, `tool_input`, `prompt`, `reason`, `cwd`).

    Pure, no-I/O, fail-open: only fields present in the payload are lifted, unknown/missing fields
    degrade to omitted keys, and an unmapped event name passes through unchanged (fails loud in a
    test rather than silently mis-routing). Same contract as the Grok bridge's payload helpers —
    a translation bug must never crash the OpenCode plugin process (INTEGRATION.md §3)."""
    event = OPENCODE_HOOK_TO_CELEBORN_EVENT.get(opencode_event, opencode_event)
    p = opencode_payload if isinstance(opencode_payload, dict) else {}
    translated = {}
    session_id = p.get("sessionID") or p.get("session_id")
    if session_id:
        translated["session_id"] = session_id
    directory = p.get("directory") or p.get("worktree") or p.get("cwd")
    if directory:
        translated["cwd"] = directory
    if p.get("tool"):
        tool = str(p["tool"])
        translated["tool_name"] = OPENCODE_TOOL_TO_CLAUDE.get(tool.strip().lower(), tool)
    if isinstance(p.get("args"), dict):
        translated["tool_input"] = p["args"]
    if isinstance(p.get("file"), str) and p["file"] and "tool_input" not in translated:
        # OpenCode's `file.edited` event carries only the path (P4, CELE-t141) — surface it as an
        # Edit so the post-tool-use touch path treats it like any other file mutation.
        translated.setdefault("tool_name", "Edit")
        translated["tool_input"] = {"file_path": p["file"]}
    if isinstance(p.get("text"), str):
        translated["prompt"] = p["text"]
    if p.get("reason"):
        translated["reason"] = p["reason"]
    if p.get("child"):
        # Subagent marker (CELE-t211, additive per t203 §7.1): the plugin flags sessions it saw
        # born with a parentID so the card gate's PM auto-provision never claims a card for a
        # child session (contract §1.2) — they keep the plain deny.
        translated["child_session"] = True
    return event, translated


class OpenCodeAdapter(HarnessAdapter):
    """OpenCode: orient injection + capture ride the celeborn.js plugin (opencode/ package), which
    shells `celeborn hook <event> --harness opencode`. OpenCode has NO structured permission lever
    today — the P3 card gate is a behavioral throw in tool.execute.before, not a config-file lever —
    so friction_signals/permission_target stay neutral (quality only), like Grok. The advisor
    renders the shared slash-command-free voice."""

    name = "opencode"

    def render(self, intent: str, signal: dict | None = None) -> tuple:
        r = _branded_quality_render(intent, signal)
        return r if r is not None else super().render(intent, signal)


def active_adapter(ctx: Path | None = None, name: str | None = None) -> HarnessAdapter:
    """Resolve the active harness adapter: explicit `name` > $CELEBORN_HARNESS > rc `harness` >
    'claude'. An unknown name degrades to the neutral base adapter (never raises)."""
    import os
    chosen = (name or os.environ.get("CELEBORN_HARNESS") or "").strip().lower()
    if not chosen and ctx is not None:
        try:
            chosen = str(load_config(ctx).get("harness") or "").strip().lower()
        except Exception:
            chosen = ""
    if not chosen:
        chosen = "claude"
    return {"claude": ClaudeAdapter, "grok": GrokAdapter, "codex": CodexAdapter,
            "opencode": OpenCodeAdapter, "neutral": HarnessAdapter}.get(chosen, HarnessAdapter)()


def _advisor_notice(ctx: Path, session: str | None = None) -> str:
    """One-line SessionStart recommendation (sibling of `_integrity_notice`). Emits at most
    `advisor.max_per_session` nudges per session (default 1) — throttled via the advisor metrics block
    so the same nudge never repeats turn after turn — and skips any intent the user has `--dismiss`ed.
    Best-effort + never raises (the hook must degrade to silence)."""
    try:
        acfg = _advisor_config(ctx)
        if not acfg["enabled"]:
            return ""
        m = _load_metrics(ctx)
        adv = m.get("advisor") or {}
        sess = session or ""
        count = int(adv.get("notices_this_session", 0) or 0) if sess == (adv.get("last_notice_session") or "") else 0
        if count >= acfg["max_per_session"]:
            return ""                                # session nudge budget already spent
        dismissed = set(adv.get("dismissed") or [])
        adapter = active_adapter(ctx)
        sigs = adapter.friction_signals(ctx, session)
        if not sigs:
            return ""
        intent = _signal_to_intent(sigs[0])
        if not intent or intent in dismissed:
            return ""
        text, _channel = adapter.render(intent, sigs[0])
        if not text:
            return ""
        new_adv = dict(adv)                          # reassign (don't mutate the shared template dict)
        new_adv["last_notice_session"] = sess
        new_adv["notices_this_session"] = count + 1
        m["advisor"] = new_adv
        _save_metrics(ctx, m)
        return text
    except Exception:
        return ""


def cmd_advise(args):
    """`celeborn advise` — print the throughput/quality recommendations that apply RIGHT NOW given the
    detected friction signals. Read-only; also the engine `_advisor_notice` calls on every orient.
    `--dismiss <id>` permanently silences one intent (and `--restore <id>` un-silences it)."""
    ctx = require_context(args)

    dismiss = getattr(args, "dismiss", None)
    restore = getattr(args, "restore", None)
    if dismiss or restore:
        iid = (dismiss or restore)
        if iid not in ADVISOR_INTENTS:
            die(f"unknown recommendation id: {iid}\n  known ids: {', '.join(sorted(ADVISOR_INTENTS))}")
        m = _load_metrics(ctx)
        adv = dict(m.get("advisor") or {})
        dismissed = [d for d in (adv.get("dismissed") or []) if d in ADVISOR_INTENTS]
        if dismiss:
            if iid not in dismissed:
                dismissed.append(iid)
            ok(f"Dismissed '{iid}' — the advisor will no longer recommend it. Restore: "
               f"`celeborn advise --restore {iid}`")
        else:
            dismissed = [d for d in dismissed if d != iid]
            ok(f"Restored '{iid}' — the advisor may recommend it again.")
        adv["dismissed"] = dismissed
        m["advisor"] = adv
        _save_metrics(ctx, m)
        return

    adapter = active_adapter(ctx, getattr(args, "harness", None))
    dismissed = set((_load_metrics(ctx).get("advisor") or {}).get("dismissed") or [])
    sigs = adapter.friction_signals(ctx, None)
    recs, suppressed = [], 0
    for sig in sigs:
        intent = _signal_to_intent(sig)
        if not intent:
            continue
        if intent in dismissed:
            suppressed += 1
            continue
        text, channel = adapter.render(intent, sig)
        recs.append({"intent": intent, "signal": sig, "text": text, "channel": channel})
    if getattr(args, "throughput", False):
        # On-demand throughput recommendations (Phase 4): not signal-triggered (they need judgment the
        # CLI can't make), so they surface only when explicitly asked. Skip dismissed + unrenderable.
        for iid, spec in ADVISOR_INTENTS.items():
            if not spec.get("on_demand") or iid in dismissed:
                continue
            text, channel = adapter.render(iid, None)
            if text:
                recs.append({"intent": iid, "signal": None, "text": text, "channel": channel})
    if getattr(args, "json", False):
        print(json.dumps({"harness": adapter.name, "recommendations": recs,
                          "dismissed": sorted(dismissed)}, indent=2))
        return
    if not recs:
        msg = "No friction detected — nothing to recommend right now."
        if suppressed:
            msg += f" ({suppressed} dismissed)"
        ok(msg)
        return
    print(f"🏹 Celeborn advisor ({adapter.name}) — {len(recs)} recommendation(s):")
    for r in recs:
        print(f"  • [{r['intent']}] {r['text']}")
    print(f"\n  Silence one: celeborn advise --dismiss <id>")


_KNOWN_HARNESSES = ("claude", "grok", "codex", "opencode", "neutral")


def cmd_harness(args):
    """`celeborn harness [<name>]` — read or pin the active harness in `.celebornrc`. With no name it
    prints the resolved adapter (env $CELEBORN_HARNESS > rc `harness` > default 'claude'). With a name
    it persists `harness: <name>` to the project rc — the durable half of harness selection the Grok/
    Codex bridges call at bootstrap (the env var covers per-call resolution; the rc covers any direct
    `celeborn` invocation in the repo)."""
    ctx = require_context(args)
    name = (getattr(args, "name", None) or "").strip().lower()
    if not name:
        adapter = active_adapter(ctx)
        rc = (load_config(ctx).get("harness") or "").strip().lower() or "(unset)"
        ok(f"Active harness: {adapter.name}  (rc harness: {rc})")
        return
    if name not in _KNOWN_HARNESSES:
        die(f"unknown harness: {name}\n  known: {', '.join(_KNOWN_HARNESSES)}")
    _update_config(ctx, harness=name)
    ok(f"Pinned harness '{name}' in {ctx / RC_NAME} — `active_adapter` now resolves '{name}' for this repo.")


# --------------------------------------------------------------------------- agents & autonomy (t353)
# The board Settings "Agents & autonomy" section (t144 mockup :543-596). These are the DEFAULTS stamped
# onto freshly-groomed cards — the per-card autonomy grant still wins at gate time (t212). Persisted in
# the project's `.celebornrc` under an `autonomy` block; read via `--json`, written via the set-flags.
# Ship pre-flight is surfaced but LOCKED on: it is the spine discipline, never a board-toggleable field.

def _autonomy_config(ctx: Path) -> dict:
    """Normalized autonomy defaults from `.celebornrc` (`autonomy` block), overlaid on the built-in
    defaults so an absent/partial block still yields a complete, valid config. Grants default to
    `_autoprovision_grants()` (research/edits/tests — never commit); unknown grant tokens are dropped."""
    block = load_config(ctx).get("autonomy")
    block = block if isinstance(block, dict) else {}

    raw_grants = block.get("default_grants")
    if isinstance(raw_grants, list):
        grants = [g for g in AUTONOMY_GRANTS if g in raw_grants]      # canonical order, unknowns dropped
    else:
        grants = _autoprovision_grants()

    nq_keys = [k for k, _ in AUTONOMY_NIGHT_QUESTIONS]
    night = block.get("night_questions")
    night = night if night in nq_keys else nq_keys[0]

    try:
        elves = int(block.get("elves_per_night", 4))
    except (TypeError, ValueError):
        elves = 4
    elves = max(AUTONOMY_ELVES_MIN, min(AUTONOMY_ELVES_MAX, elves))

    pm_keys = [k for k, _ in AUTONOMY_PM_MODELS]
    pm = block.get("pm_model")
    pm = pm if pm in pm_keys else pm_keys[0]

    return {"default_grants": grants, "night_questions": night, "elves_per_night": elves, "pm_model": pm}


def _autonomy_state_json(ctx: Path) -> dict:
    """The read-only state the board Settings "Agents & autonomy" section renders: each control's
    current value plus the option/label vocabulary and bounds, and the LOCKED ship-preflight marker."""
    cfg = _autonomy_config(ctx)
    return {
        "grants": {
            "value": cfg["default_grants"],
            "options": [{"value": g, "label": g, "risk": g in _AUTONOMY_GRANT_RISK,
                         "active": g in cfg["default_grants"]} for g in AUTONOMY_GRANTS],
        },
        "night_questions": {
            "value": cfg["night_questions"],
            "options": [{"value": k, "label": v} for k, v in AUTONOMY_NIGHT_QUESTIONS],
        },
        "elves_per_night": {"value": cfg["elves_per_night"],
                            "min": AUTONOMY_ELVES_MIN, "max": AUTONOMY_ELVES_MAX},
        "pm_model": {
            "value": cfg["pm_model"],
            "options": [{"value": k, "label": v} for k, v in AUTONOMY_PM_MODELS],
        },
        "ship_preflight": {"value": True, "locked": AUTONOMY_SHIP_PREFLIGHT_LOCKED,
                           "why": AUTONOMY_SHIP_PREFLIGHT_WHY},
    }


def _write_autonomy(ctx: Path, **changes) -> dict:
    """Merge validated changes into the `.celebornrc` `autonomy` block (preserving the rest of the rc),
    then return the freshly-normalized config. Values of None are ignored."""
    cfg = _autonomy_config(ctx)
    cfg.update({k: v for k, v in changes.items() if v is not None})
    _update_config(ctx, autonomy=cfg)
    return _autonomy_config(ctx)


def cmd_autonomy(args):
    """`celeborn autonomy` — read or set the fleet's default autonomy grants and night-run knobs (the
    defaults a freshly-groomed card inherits; per-card grants still win at gate time, t212). No args
    prints the current config; `--json` emits it for the board; the set-flags persist to `.celebornrc`.
    Ship pre-flight is intentionally not settable here — it is locked on as the spine's ship discipline."""
    ctx = require_context(args)

    if getattr(args, "json", False):
        print(json.dumps(_autonomy_state_json(ctx), indent=2))
        return

    changes = {}
    if getattr(args, "set_grants", None) is not None:
        changes["default_grants"] = _validate_autonomy(args.set_grants)
    if getattr(args, "night_questions", None) is not None:
        keys = [k for k, _ in AUTONOMY_NIGHT_QUESTIONS]
        if args.night_questions not in keys:
            die(f"unknown --night-questions '{args.night_questions}'\n  known: {', '.join(keys)}")
        changes["night_questions"] = args.night_questions
    if getattr(args, "elves", None) is not None:
        if not (AUTONOMY_ELVES_MIN <= args.elves <= AUTONOMY_ELVES_MAX):
            die(f"--elves must be {AUTONOMY_ELVES_MIN}..{AUTONOMY_ELVES_MAX} (got {args.elves})")
        changes["elves_per_night"] = args.elves
    if getattr(args, "pm_model", None) is not None:
        keys = [k for k, _ in AUTONOMY_PM_MODELS]
        if args.pm_model not in keys:
            die(f"unknown --pm-model '{args.pm_model}'\n  known: {', '.join(keys)}")
        changes["pm_model"] = args.pm_model

    if changes:
        cfg = _write_autonomy(ctx, **changes)
        ok(f"autonomy defaults updated in {ctx / RC_NAME}.")
    else:
        cfg = _autonomy_config(ctx)

    grants = ", ".join(cfg["default_grants"]) or "none"
    nq_label = dict(AUTONOMY_NIGHT_QUESTIONS)[cfg["night_questions"]]
    pm_label = dict(AUTONOMY_PM_MODELS)[cfg["pm_model"]]
    info(f"Default autonomy grants: {grants}  (commit is never implied — grant it eyes-open)")
    info(f"Night questions:         {nq_label}")
    info(f"Elves per night run:     {cfg['elves_per_night']}")
    info(f"Project manager:         {pm_label}")
    info(f"Ship pre-flight:         ON — locked ({AUTONOMY_SHIP_PREFLIGHT_WHY})")


def cmd_permissions(args):
    """`celeborn permissions --suggest|--apply` — productize the manual `/fewer-permission-prompts`
    fix. Scans the harness's allow-list, proposes generalized wildcard rules for the safe families
    (read-only inspection + the trusted Celeborn CLI + test runners), and leaves every un-widenable
    literal verbatim while tallying it as a remaining bottleneck. `--apply` writes; default target is
    the personal `settings.local.json` (`--shared` → the committed `settings.json`)."""
    ctx = require_context(args)

    # t115 — read-only JSON state for the board Settings page.
    if getattr(args, "json", False):
        print(json.dumps(_permissions_state_json(ctx), indent=2))
        return

    scope = ("global" if getattr(args, "global_", False)
             else "shared" if getattr(args, "shared", False) else "local")

    # t115 — apply / remove the SAFE t100 baseline. Default target is GLOBAL (where `wire --global` puts
    # it), unless the caller explicitly chose --shared/local.
    if getattr(args, "baseline", False):
        if not getattr(args, "global_", False) and not getattr(args, "shared", False):
            scope = "global"
        target = _settings_path_for_scope(ctx, scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _backup_and_load_settings(target)
        if getattr(args, "remove", False):
            rep = _remove_permission_baseline(data)
            _atomic_write_json(target, data)
            ok(f"removed {len(rep['removed'])} safe-baseline rule(s) from {target}"
               + (" (defaultMode reverted)" if rep["default_mode_reverted"] else "") + ".")
        else:
            rep = _merge_permission_baseline(data)
            _atomic_write_json(target, data)
            ok(f"safe baseline applied -> {target}: +{len(rep['added'])} rule(s)"
               + (f", defaultMode={BASELINE_DEFAULT_MODE}" if rep["default_mode_set"] else "")
               + ". Applies to NEW sessions.")
        return

    # t115 — Danger Zone arm/disarm (the FULL unsafe spectrum). Default target is LOCAL (least blast
    # radius). Arming requires --yes; the board passes it only after a typed confirmation.
    if getattr(args, "danger_zone", False):
        target = _settings_path_for_scope(ctx, scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        if getattr(args, "disarm", False):
            data = _backup_and_load_settings(target)
            rep = _disarm_danger_zone(data)
            _atomic_write_json(target, data)
            ok(f"Danger Zone DISARMED -> {target}: removed {len(rep['removed'])} rule(s), "
               f"defaultMode restored to {BASELINE_DEFAULT_MODE}.")
            return
        if not getattr(args, "yes", False):
            die("refusing to ARM the Danger Zone without --yes — this enables the FULL unsafe "
                "auto-allow spectrum + bypassPermissions.")
        data = _backup_and_load_settings(target)
        rep = _arm_danger_zone(data)
        _atomic_write_json(target, data)
        warn("DANGER ZONE ARMED — the agent may now run ANY command, read/write ANY file, reach ANY "
             "network host, and use every MCP tool; Claude will NOT ask permission (bypassPermissions).")
        ok(f"wrote {target}: +{len(rep['added'])} rule(s), defaultMode={DANGER_DEFAULT_MODE}. Disarm: "
           f"`celeborn permissions --danger-zone --disarm{' --global' if scope == 'global' else ''}`.")
        return

    # t351 — per-rule verbs backing the board's grouped-chip Permissions panel. Default scope is the
    # committed project settings.json (--shared), since that panel describes ".claude/settings.json";
    # --global / (bare) local still override.
    per_rule_scope = ("global" if getattr(args, "global_", False)
                      else "local" if getattr(args, "local", False) else "shared")

    if getattr(args, "add", None) is not None:
        kind = getattr(args, "kind", None) or "allow"
        if kind not in PERMISSION_RULE_KINDS:
            die(f"--kind must be one of {', '.join(PERMISSION_RULE_KINDS)}.")
        rule = args.add.strip()
        if not rule:
            die("--add requires a non-empty rule pattern, e.g. --add 'Bash(npm run build:*)'.")
        target = _settings_path_for_scope(ctx, per_rule_scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _backup_and_load_settings(target)
        rep = _add_permission_rule(data, rule, kind)
        _atomic_write_json(target, data)
        ok(f"{'added' if rep['added'] else 'already present:'} {kind} rule {rule!r} -> {target}"
           + ("." if rep["added"] else " — no change."))
        return

    if getattr(args, "rm", None) is not None:
        rule = args.rm.strip()
        if not rule:
            die("--rm requires the exact rule string to remove.")
        target = _settings_path_for_scope(ctx, per_rule_scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _backup_and_load_settings(target)
        rep = _remove_permission_rule(data, rule)
        _atomic_write_json(target, data)
        if rep["removed_from"]:
            ok(f"removed rule {rule!r} from {', '.join(rep['removed_from'])} in {target}.")
        else:
            ok(f"rule {rule!r} not found in {target} — no change.")
        return

    if getattr(args, "set_mode", None) is not None:
        mode = args.set_mode.strip()
        if mode and mode not in VALID_PERMISSION_MODES:
            die(f"--set-mode must be one of {', '.join(sorted(VALID_PERMISSION_MODES))} (or '' to unset).")
        target = _settings_path_for_scope(ctx, per_rule_scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _backup_and_load_settings(target)
        _set_permission_mode(data, mode)
        _atomic_write_json(target, data)
        ok(f"defaultMode {('set to ' + mode) if mode else 'unset'} -> {target}.")
        return

    adapter = active_adapter(ctx, getattr(args, "harness", None))
    target, how = adapter.permission_target(ctx, shared=getattr(args, "shared", False))
    if target is None or how != "per-command-allow":
        die(f"permissions: the '{adapter.name}' harness has no per-command allow-list to generalize "
            f"(its lever is coarse approval/sandbox config, not yet productized).")

    data = {}
    if target.is_file():
        try:
            data = json.loads(target.read_text())
        except json.JSONDecodeError:
            die(f"{target} is not valid JSON — refusing to rewrite it. Fix the file by hand first.")
    allow = (data.get("permissions") or {}).get("allow") or []
    new_rules, generalized, skipped = _generalize_allow(allow)
    skipped_total = sum(skipped.values())
    removed = len(allow) - len(new_rules)

    info(f"Permission target: {target}")
    print(f"  current rules:     {len(allow)}")
    print(f"  after generalize:  {len(new_rules)}  ({generalized} literal(s) collapsed, {removed} fewer)")
    if generalized == 0 and not skipped:
        ok("Nothing to generalize — the allow-list is already lean.")
        return
    if skipped:
        print(f"  ⚠ skipped bottlenecks (kept verbatim — can't be widened safely): {skipped_total}")
        for key, cnt in sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"      {cnt:>3}×  {key}")

    if not getattr(args, "apply", False):
        print("\n  Proposed allow-list (run again with --apply to write it):")
        for r in new_rules:
            print(f"      {r}")
        print(f"\n  Apply:  celeborn permissions --apply"
              f"{' --shared' if getattr(args, 'shared', False) else ''}")
        return

    if not getattr(args, "yes", False):
        try:
            resp = input(f"\nRewrite {target.name} with {len(new_rules)} rule(s)? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            warn("Aborted — nothing written.")
            return

    data.setdefault("permissions", {})["allow"] = new_rules
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")

    m = _load_metrics(ctx)
    adv = dict(m.get("advisor") or {})
    adv["permission_rules_generalized"] = int(adv.get("permission_rules_generalized", 0) or 0) + generalized
    adv["skipped_bottlenecks"] = skipped
    adv["skipped_bottlenecks_total"] = skipped_total
    adv["last_applied_at"] = now_iso()
    m["advisor"] = adv
    _save_metrics(ctx, m)
    ok(f"Wrote {target} — {generalized} literal rule(s) generalized, {skipped_total} bottleneck(s) remain.")


# A path-shaped token: optional ./ or ../ lead, then a name and at least one slash. The trailing
# `.ext` is enforced by the caller (so we only flag things that look like real files, not URLs/dirs).
_DRIFT_PATH_RE = re.compile(r"(?:\.{0,2}/)?[\w.\-]+(?:/[\w.\-]+)+")


def _extract_memory_paths(text: str) -> list[str]:
    """Repo-relative file paths referenced inside inline-code spans of authored memory.

    Only backtick-wrapped tokens that look like a path (a slash plus a real file extension) are
    returned — prose words, command examples, and bare identifiers are ignored. This is a trust
    feature: a wrong drift flag costs more than a missed one, so precision beats recall here."""
    out: list[str] = []
    for span in re.findall(r"`([^`]+)`", text):
        if "://" in span:  # a URL span, not a file reference
            continue
        for tok in _DRIFT_PATH_RE.findall(span):
            tok = tok.strip().rstrip(".,;:)")
            if tok.startswith("/") or "<" in tok or ">" in tok:  # absolute/host frag, <placeholder>
                continue
            base = tok.rsplit("/", 1)[-1]
            if "." not in base or base.startswith("."):  # need name.ext, not a dotfile/dir
                continue
            ext = base.rsplit(".", 1)[-1]
            if not (1 <= len(ext) <= 5 and ext.isalnum()):  # plausible extension
                continue
            out.append(tok)
    return out


def _repo_tracked_files(repo: Path) -> set[str]:
    """Git-tracked files (repo-relative POSIX) — the authoritative 'what exists' set. Empty on any
    git failure, which makes the caller fall back to plain filesystem existence."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(repo), "ls-files"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return {ln for ln in r.stdout.split("\n") if ln}
    except (OSError, subprocess.SubprocessError):
        pass
    return set()


def _memory_drift(ctx: Path) -> list[tuple[str, str]]:
    """File paths referenced in the LIVE memory tiers (state.md, notes.md) that the repo no longer
    has — a deleted or renamed file/module the authored memory still points at.

    journal/decisions/learnings are append-only HISTORY, so a since-deleted file named there is
    correct, not drift; they're exempt. Returns (tier, path) pairs, deduped per tier."""
    repo = ctx.parent
    tracked = _repo_tracked_files(repo)

    def present(p: str) -> bool:
        if (repo / p).exists() or (ctx / p).exists():  # filesystem (handles ../ and untracked)
            return True
        if p in tracked:
            return True
        suffix = "/" + p
        return any(f.endswith(suffix) for f in tracked)  # path given relative to a subdir

    drift: list[tuple[str, str]] = []
    for tier in ("state.md", "notes.md"):
        fp = ctx / tier
        if not fp.is_file():
            continue
        seen: set[str] = set()
        for p in _extract_memory_paths(fp.read_text()):
            if p in seen:
                continue
            seen.add(p)
            if not present(p):
                drift.append((tier, p))
    return drift


def cmd_doctor(args):
    ctx = require_context(args)
    cfg = load_config(ctx)
    problems = 0
    warnings = 0
    print("celeborn doctor")

    # required files
    for rel in REQUIRED_FILES:
        if (ctx / rel).is_file():
            ok(rel)
        else:
            warn(f"MISSING required file: {rel}")
            problems += 1

    # state.md budget
    sp = ctx / "state.md"
    if sp.is_file():
        text = sp.read_text()
        n = len(text.splitlines())
        _, state_hist = plan_state_archive(text, cfg.get("state_keep_sessions", 6))
        if state_hist:
            warn(f"state.md carries {len(state_hist)} old ## Now history bullet(s) past the "
                 f"keep-{cfg.get('state_keep_sessions', 6)} cap — run `celeborn archive` "
                 f"(auto-trims on capture when auto_archive is on)")
            warnings += 1
        elif n > cfg["state_max_lines"]:
            warn(f"state.md is {n} lines (budget {cfg['state_max_lines']}) — condense it")
            warnings += 1
        else:
            ok(f"state.md within budget ({n}/{cfg['state_max_lines']} lines)")

    # Hot-tier char budget — the Orient load is injected as SessionStart additionalContext; if a
    # piece outgrows its char budget, `status` clips it (with a pointer), so authored detail stops
    # reaching the model on rehydration. Flag the clip so it's visible, not silent.
    state_max = int(cfg.get("hot_state_max_chars", 4000))
    act_max = int(cfg.get("hot_activity_max_chars", 2000))
    hot_over = []
    if sp.is_file() and len(sp.read_text()) > state_max:
        hot_over.append(f"state.md {len(sp.read_text())}/{state_max} chars")
    ap = ctx / "activity.md"
    if ap.is_file() and len(ap.read_text()) > act_max:
        hot_over.append(f"activity.md {len(ap.read_text())}/{act_max} chars")
    if hot_over:
        warn("Hot tier over char budget — clipped on Orient load, so detail won't fully rehydrate: "
             + "; ".join(hot_over))
        print("  Fix:  condense state.md (history → journal.md), or raise hot_*_max_chars in .celebornrc")
        warnings += 1
    else:
        ok("Hot tier within char budget (full Orient load reaches the model)")

    # journal budget
    jp = ctx / "journal.md"
    if jp.is_file():
        _, entries = split_journal(jp.read_text())
        if len(entries) > cfg["journal_keep_entries"]:
            warn(f"journal.md has {len(entries)} entries (keep {cfg['journal_keep_entries']}) — run `celeborn archive`")
            warnings += 1
        else:
            ok(f"journal.md within budget ({len(entries)}/{cfg['journal_keep_entries']} entries)")

    # done-column budget (auto-archives on the next `celeborn tasks` save)
    if (ctx / TASKS_FILE).is_file():
        all_tasks = _load_tasks(ctx)
        done_n = len(_done_tasks_ordered(all_tasks))
        keep_done = cfg["done_keep_cards"]
        if done_n > keep_done:
            warn(f"tasks.md has {done_n} done card(s) (keep {keep_done}) — run `celeborn tasks archive`")
            warnings += 1
        else:
            ok(f"done column within budget ({done_n}/{keep_done} cards)")

        # Stop-condition contract (CELE-t81): every open card should carry a logical Stop condition,
        # and ideally a real one rather than the generic auto-filled default. Advisory only — flag
        # open cards (not done) that are missing or still carry the default so an owner can sharpen it.
        open_tasks = [t for t in all_tasks if t["state"] != "done"]
        missing_stop = [t for t in open_tasks if not (t.get("stop") or "").strip()]
        default_stop = [t for t in open_tasks if (t.get("stop") or "").strip() == DEFAULT_STOP]
        if missing_stop:
            warn(f"{len(missing_stop)} open card(s) have no Stop condition: "
                 + ", ".join(_display_tid(ctx, t["id"]) for t in missing_stop))
            print("  Fix:  set one — `celeborn tasks edit <id> --stop \"<clean /clear point>\"`")
            warnings += len(missing_stop)
        elif default_stop:
            info(f"{len(default_stop)} open card(s) still carry the generic default Stop condition: "
                 + ", ".join(_display_tid(ctx, t["id"]) for t in default_stop)
                 + " — replace with a card-specific one when you pick them up.")
        else:
            ok("every open card carries a Stop condition")
        # Spine discipline (CELE-t282, design §4): the spine head — the first READY todo card in
        # board order, exactly what `celeborn next` would dispatch — must be startable verbatim by
        # a fresh agent: blockers done, real Stop condition, a brief in the note, no open question.
        # Head-only by design: the ship ritual repairs the spine one card at a time; flagging the
        # whole column would be noise nobody actions.
        if any(t["state"] == "todo" for t in all_tasks):
            sp_ready, _ = _ready_set(all_tasks, _archived_done_ids(ctx))
            if not sp_ready:
                warn("spine has no READY head — every todo card is blocked; a fresh agent has nothing startable")
                print("  Fix:  unblock or reorder the spine — `celeborn next --all` shows the ready set")
                warnings += 1
            else:
                head = sp_ready[0]
                head_why = _spine_audit(head, alerts=_live_alerts(ctx))
                if head_why:
                    warn(f"spine head [{_display_tid(ctx, head['id'])}] is not startable verbatim: "
                         + "; ".join(head_why))
                    print(f"  Fix:  celeborn tasks edit {head['id']} --stop \"<clean /clear point>\" "
                          f"--note \"<3-8 line brief>\"   (design: docs/plans/cele-t144-spine-and-stage.md §4)")
                    warnings += 1
                else:
                    ok(f"spine head [{_display_tid(ctx, head['id'])}] is READY — startable verbatim")
        # Progress-engine drift (CELE-t161): a doing card stuck at 0% that already has commits carrying
        # its trailer means the engine never moved it — flag so it's visible (complements the Stop check).
        drifted = [t for t in all_tasks if t.get("state") == "doing"
                   and int(t.get("progress", 0) or 0) == 0 and _commits_for_task(ctx, t["id"], limit=50)]
        if drifted:
            warn(f"{len(drifted)} doing card(s) at 0% despite commits with their trailer: "
                 + ", ".join(_display_tid(ctx, t["id"]) for t in drifted))
            print("  Fix:  `celeborn progress <id> --explain`  (runs the engine + shows the derivation)")
            warnings += len(drifted)
        else:
            ok("no progress-engine drift on doing cards")
        # Owner-attribution contract (CELE-t194): a DOING card is owned by its SESSION short-id (the
        # code grabs it), never by a model name and never left "unknown". A card in either bad state
        # was claimed by an unfixed binary or a hand-run `--by claude` — the fleet then shows @claude /
        # @unknown and can't attach a context-token chip. Re-claiming from the owning window now grabs
        # CLAUDE_CODE_SESSION_ID automatically and repairs both. Backstop for the in-path guard.
        doing = [t for t in all_tasks if t.get("state") == "doing"]
        mis_owned = [t for t in doing
                     if (o := (t.get("owner") or "").strip()).lower() in ("", "unknown")
                     or _looks_like_model_handle(o)]
        if mis_owned:
            warn(f"{len(mis_owned)} doing card(s) not owned by a session short-id (shows @unknown/"
                 f"model, no context chip): " + ", ".join(_display_tid(ctx, t["id"]) for t in mis_owned))
            print("  Fix:  re-claim from the owning window — `celeborn claim <id>` "
                  "(auto-grabs CLAUDE_CODE_SESSION_ID; no --by/--session needed)")
            warnings += len(mis_owned)
        else:
            ok("every doing card is owned by a session short-id")
        arch_path = ctx / DONE_ARCHIVE_FILE
        if arch_path.is_file():
            arch_n = len(_parse_tasks(arch_path.read_text()))
            arch_cap = cfg["done_archive_keep_cards"]
            ok(f"done-archive.md: {arch_n}/{arch_cap} card(s)")

    # session.json valid
    sj = ctx / "session.json"
    if sj.is_file():
        try:
            json.loads(sj.read_text())
            ok("session.json is valid JSON")
        except json.JSONDecodeError as e:
            warn(f"session.json INVALID JSON: {e}")
            info("    repair it with: celeborn checkpoint  (rebuilds valid JSON from the template)")
            problems += 1

    # index freshness
    if (ctx / INDEX_NAME).is_file():
        if _index_is_stale(ctx):
            warn("index.db is stale — run `celeborn index`")
            warnings += 1
        else:
            ok("index.db is fresh")
    else:
        warn("index.db absent — run `celeborn index` to enable search")
        warnings += 1

    # memory drift — live tiers (state.md/notes.md) pointing at files the repo no longer has.
    # This is the honesty check: if memory references a deleted or renamed file, the next session
    # rehydrates a lie. History tiers (journal/decisions/learnings) are exempt — see _memory_drift.
    drift = _memory_drift(ctx)
    if drift:
        warn(f"memory drift — {len(drift)} stale file reference(s) in live memory (deleted/renamed):")
        for tier, p in drift:
            print(f"      {tier} → {p}")
        print("  Fix:  correct the path in state.md/notes.md (or restore the file) so memory matches the repo")
        warnings += len(drift)
    else:
        ok("memory matches repo (no stale file references in state.md/notes.md)")

    # secret scan
    hits = _secret_scan(ctx, cfg["secret_patterns"])
    if hits:
        for h in hits:
            warn(f"POSSIBLE SECRET in {h}")
        problems += len(hits)
    else:
        ok("no obvious secrets in committed memory")

    # secrets discipline (CELE-t224) — live secret VALUES in repo .env* files belong in the vault,
    # not on disk where they get committed or pasted into prompts. The scan is free for everyone;
    # the vault it points at (`celeborn secrets`) is the Pro Infisical integration.
    env_hits = _env_file_secret_hits(ctx.parent, cfg["secret_patterns"])
    if env_hits:
        for fname, key in env_hits:
            warn(f"LIVE SECRET VALUE in {fname} — `{key}` looks like a real credential")
        if (ctx.parent / ".infisical.json").is_file() or load_config(ctx).get("secrets"):
            print("  Fix:  move it to the vault — `celeborn secrets set <NAME>` — then delete the line "
                  "(consume via `celeborn secrets run -- <cmd>`)")
        else:
            print("  Fix:  vault it — `celeborn secrets setup` (Pro), then `celeborn secrets set <NAME>` "
                  "and delete the line")
        warnings += len(env_hits)
    else:
        ok("no live secret values in repo .env files")

    # install integrity — shipped core modules vs the published per-version checksum manifest.
    # Detection, not prevention: an in-place edit means the install no longer matches the release, so a
    # "works after I hacked celeborn.py" break self-reports here instead of becoming a confused bug
    # report. Source/editable checkouts ship no manifest → 'unverified' → we stay silent (no dev nag).
    ist = integrity_status()
    if ist["state"] == "modified":
        warn("modified install detected — core module(s) differ from the published release: "
             + ", ".join(ist["modified"]))
        print("  Fix:  reinstall to reset (uv tool install --force / pipx reinstall); local edits are "
              "unsupported — submit a PR.")
        warnings += 1
    elif ist["state"] == "ok":
        ok("install integrity verified (core modules match the published release)")
    # 'unverified' (source/dev install): say nothing — never nag a contributor editing the tree.

    # Fleet economics integrity (CELE-t124): the board's savings bar and `celeborn fleet` only count
    # REGISTERED projects, and savings from any session that runs outside a project .context/ (e.g. from
    # your home dir) land in the global ~/.context sink — attributed to no project. Surface a large sink
    # so a project's economics never silently goes missing. Orient now self-registers (so the sink stops
    # growing for projects you actually open), but historical sink savings stay until redistributed.
    gctx = _global_context()
    if gctx.is_dir() and (gctx / METRICS_NAME).is_file():
        sink_tokens = _load_metrics(gctx).get("tokens_saved_estimate", 0)
        proj_tokens = _load_metrics(ctx).get("tokens_saved_estimate", 0)
        if sink_tokens > max(proj_tokens, 5_000_000):
            warn(f"global ~/.context sink holds ~{sink_tokens:,} tokens of savings not attributed to any "
                 f"project — work that ran outside a project .context/.")
            print("  Fix:  open each project from inside its own dir so orient records (and self-registers) "
                  "there; `celeborn fleet register --path <dir>` counts a project in the board economics.")
            warnings += 1

    # Informational heads-up (NOT a Celeborn problem): Claude Code's own PR-status panel shells out
    # to `gh`. If gh is installed but not logged in, Claude Code shows a "GitHub CLI authentication
    # expired" banner. Most people run Celeborn inside Claude Code, so surface a friendly, actionable
    # note rather than leaving them to wonder whether Celeborn caused it. Doesn't touch the counts.
    if _gh_unauthenticated():
        info("Claude Code's PR-status panel may show a `gh` auth banner — that's Claude Code, not "
             "Celeborn. Clear it with `gh auth login` (git keeps working via your keychain regardless).")

    # Grok Build — hooks + per-project rules (orient survives /clear when cwd is this repo).
    import shutil
    root = ctx.parent
    grok_rules = root / ".grok" / "rules" / "celeborn.md"
    if shutil.which("grok") and (Path.home() / ".grok").is_dir():
        hooks = Path.home() / ".grok" / "hooks" / "celeborn.json"
        if hooks.is_file():
            ok("Grok hooks installed (~/.grok/hooks/celeborn.json)")
        else:
            warn("Grok detected but Celeborn hooks missing — run `celeborn grok wire`")
            warnings += 1
        if grok_rules.is_file():
            ok(".grok/rules/celeborn.md present (Grok auto-loads orient + kanban binding)")
        else:
            warn("missing .grok/rules/celeborn.md — run `celeborn grok sync-rules`")
            warnings += 1

    # Sovereign weave — Engine Room health + pin drift (CELE-t375; wording from
    # references/weave-contract.md §4). ADVISORY ONLY (rule 4: doctor explains drift, never blocks) —
    # a drift ⚠ counts as a warning (exit 0), an optional • is pure info; nothing here is a `problem`.
    wst = _weave_status(ctx)
    pins = wst["pins"]
    room = _engine_room_status(ctx)
    print(f"  Engine Room: {room['headline']}")
    for e in _ENGINES:
        r = room["engines"][e]
        prov = f" ({r['provenance']})" if r.get("provenance") else ""
        info(f"{r['label']}: {r['state']}{prov} · {r['base']}")
    oc_inst = wst["opencode"]["version"]
    if oc_inst and oc_inst != pins["opencode_version"]:
        warn(f"OpenCode {oc_inst} is installed; Celeborn's plugin is tested against {pins['opencode_version']}.")
        print("      This is fine — nothing is blocked. If the board Stage or PM misbehaves, run")
        print("      `celeborn opencode wire` to re-pin the plugin, or install the tested version from")
        print("      https://opencode.ai. Celeborn never changes your OpenCode for you.")
        warnings += 1
    ol_inst = wst["ollama"]["version"]
    if ol_inst and _version_lt(ol_inst, pins["ollama_floor"]):
        warn(f"Ollama {ol_inst} is below Celeborn's tested floor {pins['ollama_floor']}. The engine may still work;")
        print("      if `ollama pull` or serve behaves oddly, update Ollama from https://ollama.com. Not blocked.")
        warnings += 1
    for tag, present in wst["models"].items():
        if not present:
            info(f"Pippin's model {tag} isn't pulled yet (~2.5 GB). Run `celeborn ollama pull {tag}` to")
            print("      enable the local PM/assistant. Optional — Celeborn runs fine on Claude Code or Grok.")
    if any(str(m.get("name") or "").removesuffix(":latest") == "qwen-4b"
           for m in _ollama_status(ctx).get("models") or []):
        info("A local `qwen-4b` alias is present — that name is retired. Celeborn now uses the upstream")
        print("      tags qwen3:4b-instruct (Pippin·PM) and qwen3:4b (Pippin·ghost). The alias is harmless;")
        print("      you may `ollama rm qwen-4b` once nothing references it.")
    if (oc_inst and oc_inst == pins["opencode_version"] and ol_inst
            and not _version_lt(ol_inst, pins["ollama_floor"]) and all(wst["models"].values())):
        ok(f"Sovereign weave aligned: OpenCode {oc_inst}, Ollama {ol_inst}, "
           f"Pippin {pins['pippin_pm']} + {pins['pippin_ghost']}.")

    print(f"\n{warnings} warning(s), {problems} problem(s).")
    if problems:
        print("Doctor found problems that should be fixed.")
        sys.exit(1)


def _gh_unauthenticated() -> bool:
    """True iff the GitHub CLI is installed but not logged in — the exact state that makes Claude
    Code's PR-status panel show its `gh` auth banner. Returns False if gh is absent or the check
    can't run, so this never invents a problem. Used only for an informational note in `doctor`."""
    import shutil
    if not shutil.which("gh"):
        return False
    import subprocess
    try:
        # `gh auth status` exits non-zero when no host is logged in; it's a local, network-free read.
        r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=5)
        return r.returncode != 0
    except Exception:
        return False


def _secret_scan(ctx: Path, patterns: list[str]) -> list[str]:
    regexes = [re.compile(p) for p in patterns]
    hits: list[str] = []
    for path in ctx.rglob("*"):
        if not path.is_file() or path.name == INDEX_NAME or path.name == RC_NAME:
            continue
        if path.suffix not in (".md", ".json", ".txt", ""):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for rx in regexes:
            if rx.search(text):
                hits.append(str(path.relative_to(ctx)))
                break
    return hits


def _env_file_secret_hits(root: Path, patterns: list[str]) -> list[tuple[str, str]]:
    """(filename, KEY) pairs for repo-root `.env*` entries whose VALUE matches a secret pattern —
    i.e. a live credential sitting on disk, not just a declared name. Example/template files are
    skipped (their whole point is placeholder values). Used by doctor, the secrets-on-disk advise
    signal, and `celeborn secrets doctor` (CELE-t224)."""
    regexes = [re.compile(p) for p in patterns]
    hits: list[tuple[str, str]] = []
    for path in sorted(root.glob(".env*")):
        if not path.is_file() or path.name.endswith((".example", ".sample", ".template")):
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip().strip("\"'")
            if val and any(rx.search(val) for rx in regexes):
                hits.append((path.name, key.strip()))
    return hits


def _index_is_stale(ctx: Path) -> bool:
    """Stale if any indexed source file is newer than the index file. Uses filesystem mtime
    only — no DB open — so status/doctor stay sqlite-free. (`built_at` still lives in the DB's
    meta table for informational inspection; it just isn't needed here.)

    MECHANICAL_GLOBS are skipped: `celeborn capture` rewrites them every turn, so counting them
    would report the index stale within one turn of any live session even when nothing a user
    would re-index for has changed. They remain indexed/searchable — only this heuristic ignores
    them."""
    db = ctx / INDEX_NAME
    if not db.is_file():
        return True
    built = db.stat().st_mtime
    for tier, glob in TIER_GLOBS:
        if glob in MECHANICAL_GLOBS:
            continue
        for path in ctx.glob(glob):
            if path.is_file() and path.stat().st_mtime > built + 1:
                return True
    return False


# --------------------------------------------------------------------------- tasks (Phase 11)
#
# A lightweight, agent-native task/kanban board. `tasks.md` is the markdown source of truth
# (`celeborn tasks` edits it); `tasks.json` is a derived projection the board viewer reads,
# regenerated on every command and gitignored — the same markdown-truth / disposable-derived
# split the SQLite index follows. Offline, stdlib-only, no board UI in the core.

TASKS_FILE = "tasks.md"
TASKS_JSON = "tasks.json"
# The "blocked" state was retired (CELE-t135): kanban discipline discourages a Blocked column, so
# DOING reclaims its space. Cards still record dependencies in their `blocked_by` list — that lives on
# independently of any column. Legacy cards stored as `blocked` load as `todo` (see _load_tasks).
TASK_STATES = ["todo", "doing", "done"]
TASK_STATE_LABELS = {"todo": "TODO", "doing": "DOING", "done": "DONE"}

# Every task carries a logical Stop condition (CELE-t81): the "this is a clean place to stop" marker
# that tells the model when the card is at a defensible `/clear` point. `tasks add` auto-fills this
# generic default when no `--stop` is supplied so no card is ever stop-less — the agent protocol then
# nudges the owner to replace it with a real, card-specific condition. Deterministic, stdlib-only (the
# core makes no LLM calls); the "intelligent query" lives in the agent loop + CLAUDE.md contract, not here.
DEFAULT_STOP = "Acceptance criteria met, tests green, change committed"

TASK_HEADING_RE = re.compile(r"^\[(?P<id>[A-Za-z0-9_-]+)\]\s+(?P<title>.*)$")
# Only `## [id] title` lines open a new card — `##` headings inside a card's notes must stay in notes.
TASK_BLOCK_SPLIT_RE = re.compile(r"(?m)^##[ \t]+(?=\[[A-Za-z0-9_-]+\])")
TASK_META_RE = re.compile(r"^-\s+(?P<key>[a-z-]+):\s*(?P<val>.*)$")
# Subtask checklist (CELE-t106): markdown checkbox lines under a `### Subtasks` heading in a card body.
# Optional trailing `×N` weight (default 1). Checking items auto-derives the card's `progress`.
SUBTASKS_HEADING = "### Subtasks"
SUBTASK_RE = re.compile(r"^-\s+\[(?P<done>[ xX])\]\s+(?P<text>.*?)(?:\s+×(?P<weight>\d+))?\s*$")

TASKS_HEADER = (
    "# Tasks\n\n"
    "<!-- Celeborn task board (Phase 11). Markdown is the source of truth; `celeborn tasks`\n"
    "     edits it. `.context/tasks.json` is the derived projection the board viewer reads —\n"
    "     regenerated on every `celeborn tasks` command, gitignored, disposable.\n"
    "     One task per `## [id]` block; states: todo | doing | done. -->\n"
)

# Machine-readable block injected into every card's agent view (tasks.json, copy prompt, board DOM).
# Omitted from tasks.md and from the board's visible UI — agents must read it to work the card.
AGENT_PROTOCOL_MARKER = "⟨celeborn:protocol⟩"


def _agent_card_protocol(task_id: str) -> str:
    """Required agent instructions bundled with every kanban card. Not shown to humans on the board."""
    return (
        f"{AGENT_PROTOCOL_MARKER}\n"
        f"AGENT PROTOCOL for [{task_id}] — required before any file edit:\n"
        f"ALIGNMENT GATE — before doing the work: this card was likely typed quickly, so its title "
        f"and description may be rough or under-specified. Do NOT start editing on assumptions. First "
        f"confirm you understand it: ask the user 1–3 short clarifying questions about intent, scope, "
        f"and the right Stop condition, and wait for their answers. Use the project's permanent context "
        f"(you already have it on orient) to ask sharp questions. Once aligned, sharpen the card itself "
        f"(`celeborn tasks edit {task_id} --title \"…\" --note \"…\" --stop \"…\"`) so the next reader "
        f"inherits the clarity — then proceed. Skip the questions only if the card is already unambiguous.\n"
        f"0. Identify once per session: `celeborn identify --family <Claude|Grok|GPT…> "
        f"--model \"<e.g. Opus 4.8>\"` so your touches show who you are.\n"
        f"1. Move this card to DOING FIRST: `celeborn claim {task_id} --by <you>` "
        f"(or `celeborn tasks move {task_id} doing --owner <you>`). The board must show DOING "
        f"before you touch files — session focus alone is not enough. If Celeborn is blocking your "
        f"edits with a NO-TASK gate, add `--session <your-session-id>` to the claim (it hands you the "
        f"exact command) so the claim records the session→card link that lifts the gate this turn.\n"
        f"2. Then register each shared file: `celeborn touch <file> --by <you> --task {task_id} "
        f"--why \"<reason>\"`.\n"
        f"3. CLOSE OUT: as you START writing your ready-to-ship message, crest the sand-fill bar with "
        f"`celeborn tasks edit {task_id} --progress 99` (an unshipped card tops out at 99 — 100% is "
        f"reserved for Done); THEN, at the end, `celeborn ship {task_id}` (releases touches, moves to "
        f"Done, and fills the bar to 100%). 100% means shipped — never set it by hand on a DOING card. "
        f"DOING with zero touches is stale.\n"
        f"4. Honor this card's Stop condition (the `stop` field): it marks the clean `/clear` point. "
        f"If it still carries the generic default, replace it with a real one: "
        f"`celeborn tasks edit {task_id} --stop \"<condition>\"`.\n"
        f"See references/multi-agent-editing.md."
    )


def _tasks_path(ctx: Path) -> Path:
    return ctx / TASKS_FILE


def _tasks_json_path(ctx: Path) -> Path:
    return ctx / TASKS_JSON


def _csv(v) -> list[str]:
    """Split a comma/space-separated string into a clean list."""
    return [x.strip() for x in re.split(r"[,\s]+", v or "") if x.strip()]


def _clamp_pct(v) -> int:
    """Coerce a value to an int percent in [0, 100] (0 on anything unparseable). The progress field
    that drives the In-Progress card's sand-fill bar (CELE-t106)."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def _parse_subtask_spec(spec: str) -> dict:
    """A `set`/`add` item like 'Wire the CLI *2' → {text, weight, done}. Trailing `*N` sets weight."""
    m = re.match(r"^(?P<t>.*?)(?:\s*\*(?P<w>\d+))?\s*$", spec or "")
    text = (m.group("t") or "").strip()
    weight = int(m.group("w")) if m and m.group("w") else 1
    return {"text": text, "weight": max(1, weight), "done": False}


def _split_subtasks(notes_lines: list[str]) -> tuple[list[dict], list[str]]:
    """Pull the `### Subtasks` checkbox block out of a card's body lines. Returns (subtasks, remaining
    note lines). Only the contiguous checkbox lines immediately under the heading are consumed."""
    idx = next((i for i, l in enumerate(notes_lines) if l.strip().lower() == SUBTASKS_HEADING.lower()), None)
    if idx is None:
        return [], notes_lines
    subs: list[dict] = []
    j = idx + 1
    while j < len(notes_lines):
        m = SUBTASK_RE.match(notes_lines[j].strip())
        if not m:
            break
        subs.append({
            "text": m.group("text").strip(),
            "weight": int(m.group("weight")) if m.group("weight") else 1,
            "done": m.group("done").lower() == "x",
        })
        j += 1
    return subs, notes_lines[:idx] + notes_lines[j:]


def _normalize_progress(t: dict) -> None:
    """Enforce the project rule: 100% means shipped to Done — and ONLY Done (CELE-t131). A card that
    has not yet shipped is capped at 99 no matter what was set manually (`--progress 100`) or derived
    from all-checked subtasks; moving the card to `done` fills it to 100. So the In-Progress sand-fill
    bar can crest to 99 ("ship it") but never reads 'complete' until the card actually leaves for Done."""
    pct = _clamp_pct(t.get("progress", 0))
    t["progress"] = 100 if t.get("state") == "done" else min(99, pct)


def _recompute_progress(t: dict) -> None:
    """When a card has subtasks, its `progress` is DERIVED: the weighted fraction of checked items
    (CELE-t106). No subtasks → progress is left as-is (explicit/manual). Either way the 100%=Done
    invariant is then enforced (CELE-t131): an unshipped card tops out at 99, never 100."""
    subs = t.get("subtasks") or []
    if subs:
        total = sum(max(1, int(s.get("weight", 1))) for s in subs)
        done = sum(max(1, int(s.get("weight", 1))) for s in subs if s.get("done"))
        if total:
            t["progress"] = _clamp_pct(round(100 * done / total))
    # CELE-t161: never below the persisted engine floor (claim/work/band). 0 on non-engine cards → the
    # pure-ratio CELE-t106 behavior is exactly preserved (incl. uncheck lowering the bar).
    floor = _clamp_pct(int(t.get("engine_floor", 0) or 0))
    if floor:
        t["progress"] = max(_clamp_pct(t.get("progress", 0)), floor)
    _normalize_progress(t)


# CELE-t176 — the "mandatory complete" gate. A card may not leave DOING for Done until its progress
# bar is crested to 99% (the ceiling an unshipped card can reach; see _normalize_progress). This makes
# the final "it's complete" step deliberate, so a card never silently disappears from DOING (in Fleet
# or board/) at partial progress. The operator crests (`--progress 99`, or all subtasks checked), THEN
# ships — ship fills the last 1% to 100. Enforced on every path a card can reach `done` by: `ship`,
# `tasks move … done`, and `tasks edit … --state done`.
CREST_PCT = 99


def _require_crest_for_done(ctx: Path, t: dict) -> None:
    """Refuse a transition into `done` unless the card's progress bar is crested to CREST_PCT (99).
    A no-op at/above the crest; otherwise `die`s with the exact commands to crest and ship."""
    pct = _clamp_pct(t.get("progress", 0))
    if pct >= CREST_PCT:
        return
    disp = _display_tid(ctx, t["id"])
    die(
        f"[{disp}] is at {pct}% — a card must be crested to {CREST_PCT}% before it can leave DOING for "
        f"Done (CELE-t176). Finish the work, then crest and ship:\n"
        f"  celeborn tasks edit {t['id']} --progress {CREST_PCT}\n"
        f"  celeborn ship {t['id']}\n"
        f"(100% is reserved for shipped cards.)"
    )


def _render_subtasks(subs: list[dict]) -> list[str]:
    out = [SUBTASKS_HEADING]
    for s in subs:
        box = "x" if s.get("done") else " "
        w = f" ×{int(s.get('weight', 1))}" if int(s.get("weight", 1)) != 1 else ""
        out.append(f"- [{box}] {s.get('text', '').strip()}{w}")
    return out


def _valid_task_id(tid: str) -> bool:
    """True when `tid` is a non-empty stored card key (e.g. t132). Id-less blocks are never cards."""
    return bool(tid and re.fullmatch(r"[A-Za-z0-9_-]+", tid))


def _validate_autonomy(value: str) -> list[str]:
    """Normalize a `--autonomy` grant list to canonical AUTONOMY_GRANTS order; die on any unknown
    token. Validation is strict at the CLI on purpose — grooming is where a typo would otherwise
    become a silently-denied overnight run (an unknown token grants nothing, t203 §3.4)."""
    toks = _csv((value or "").lower())
    unknown = [x for x in toks if x not in AUTONOMY_GRANTS]
    if unknown:
        die(f"unknown autonomy grant(s): {', '.join(unknown)} — the vocabulary is "
            f"{', '.join(AUTONOMY_GRANTS)} (t203 §3.4; `commit` = git-write, never implied)")
    return [g for g in AUTONOMY_GRANTS if g in toks]


def _parse_tasks(text: str) -> list[dict]:
    """Parse tasks.md into a list of task dicts. Each `## [id] title` block carries `- key: value`
    metadata lines; any remaining lines become the task's freeform notes."""
    tasks: list[dict] = []
    for blk in TASK_BLOCK_SPLIT_RE.split(text)[1:]:
        lines = blk.splitlines()
        head = lines[0].strip() if lines else ""
        m = TASK_HEADING_RE.match(head)
        if not m:
            continue  # malformed heading — never mint an id-less card
        tid, title = m.group("id"), m.group("title").strip()
        if not _valid_task_id(tid):
            continue
        meta: dict = {}
        notes_lines: list[str] = []
        in_meta = True  # only the `- key: value` run directly under the heading is card metadata
        for ln in lines[1:]:
            if in_meta:
                if not ln.strip():
                    continue
                mm = TASK_META_RE.match(ln)
                if mm:
                    meta[mm.group("key")] = mm.group("val").strip()
                    continue
                in_meta = False
            notes_lines.append(ln)
        subtasks, notes_lines = _split_subtasks(notes_lines)
        # Any state not in TASK_STATES (e.g. a legacy `blocked` card from before CELE-t135 retired
        # that column) falls back to `todo` so it stays visible rather than vanishing off the board.
        raw_state = (meta.get("state") or "todo").lower()
        t = {
            "id": tid,
            "title": title,
            "state": raw_state if raw_state in TASK_STATES else "todo",
            "owner": meta.get("owner", ""),
            "tags": _csv(meta.get("tags", "")),
            "blocked_by": _csv(meta.get("blocked-by", "")),
            "phase": meta.get("phase", ""),  # which plan phase card this task drills down from
            # Spine branding (CELE-t380): `spine` is the group slug shared by every card minted from
            # one plan; `emoji` is that spine's single purpose-emoji, unique per project across
            # distinct slugs. "" on unbranded/legacy cards. The JSON projection surfaces the slug as
            # `spine_id` (the `spine` JSON key is the CELE-t282 stamp object, kept distinct).
            "spine": meta.get("spine", ""),
            "emoji": meta.get("emoji", ""),
            # Logical Stop condition: a clearly-defined "this is a clean place to stop" marker, so the
            # model knows when the card is at a defensible `/clear` point (CELE-t81). Free text;
            # "" on legacy cards that predate the field — auto-filled with a default on `tasks add`.
            "stop": meta.get("stop", ""),
            # Percent complete (0-100) — drives the In-Progress card's sand-fill bar (CELE-t106).
            # Absent on legacy cards → 0. Explicit for now (`tasks edit --progress`); a context-derived
            # auto-estimate is the planned follow-up.
            "progress": _clamp_pct(meta.get("progress", 0)),
            # CELE-t161 progress-engine floor: a deterministic minimum the bar is held at (claim 5,
            # first-work 10, then the milestone band). 0/absent on non-engine cards → no effect.
            "engine_floor": _clamp_pct(meta.get("engine-floor", 0)),
            "jira": meta.get("jira", ""),    # linked Jira issue key (e.g. SCRUM-2); set by `jira pull`
            "github": meta.get("github", ""),  # linked GitHub Issue number (mirror repo); set by `github push/pull` (CELE-t214)
            # Autonomy grants (CELE-t212, t203 §3.4): what the owning session may do without a human,
            # set at grooming time. Tokens are kept as written (lowercased) — the gate ignores anything
            # outside AUTONOMY_GRANTS, so a hand-edited unknown token grants nothing rather than being
            # silently rewritten here. [] on ungroomed cards → most-restrictive under a promptless harness.
            "autonomy": _csv((meta.get("autonomy", "")).lower()),
            "created": meta.get("created", ""),
            "updated": meta.get("updated", ""),
            "subtasks": subtasks,  # checklist (CELE-t106); derives progress when present
            "notes": "\n".join(notes_lines).strip(),
        }
        _recompute_progress(t)  # subtasks (incl. hand-edited checkboxes) are the source of truth for %
        tasks.append(t)
    return tasks


DONE_ARCHIVE_FILE = "done-archive.md"
DONE_ARCHIVE_HEADER = (
    "# Done archive\n\n"
    "<!-- Celeborn auto-archives done cards that fall off the bottom of the Done column.\n"
    "     Cap: done_archive_keep_cards (default 100); oldest entries are dropped FIFO.\n"
    "     Still searchable via `celeborn search`. Regenerated by `celeborn tasks`. -->\n"
)


def _render_tasks(tasks: list[dict], *, header: str = TASKS_HEADER) -> str:
    out = [header]
    for t in tasks:
        out.append(f"## [{t['id']}] {t['title']}")
        out.append(f"- state: {t['state']}")
        out.append(f"- owner: {t['owner']}")
        out.append(f"- tags: {', '.join(t['tags'])}")
        out.append(f"- blocked-by: {', '.join(t['blocked_by'])}")
        out.append(f"- phase: {t['phase']}")
        if t.get("spine"):    # CELE-t380; only when set, so unbranded cards stay byte-identical
            out.append(f"- spine: {t['spine']}")
        if t.get("emoji"):    # CELE-t380; the spine's purpose-emoji brand
            out.append(f"- emoji: {t['emoji']}")
        out.append(f"- stop: {t.get('stop', '')}")  # logical Stop condition (CELE-t81); always rendered so every card advertises the slot
        if t.get("progress"):  # only when >0, so legacy cards stay byte-identical (CELE-t106)
            out.append(f"- progress: {_clamp_pct(t['progress'])}")
        if t.get("engine_floor"):  # CELE-t161; only when >0, so non-engine cards stay byte-identical
            out.append(f"- engine-floor: {_clamp_pct(t['engine_floor'])}")
        if t.get("jira"):
            out.append(f"- jira: {t['jira']}")
        if t.get("github"):
            out.append(f"- github: {t['github']}")
        if t.get("autonomy"):  # CELE-t212; only when set, so ungroomed cards stay byte-identical
            out.append(f"- autonomy: {', '.join(t['autonomy'])}")
        out.append(f"- created: {t['created']}")
        out.append(f"- updated: {t['updated']}")
        if t.get("subtasks"):  # checklist block (CELE-t106) — rendered between metadata and notes
            out.append("")
            out.extend(_render_subtasks(t["subtasks"]))
        if t["notes"]:
            out.append("")
            out.append(t["notes"])
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _tasks_doc(ctx: Path, tasks: list[dict]) -> dict:
    """JSON projection for the board viewer. Adds per-card agent_protocol (not stored in tasks.md)
    and joins the local agent registry so the owner chip can show family/model — tasks.md itself
    stays handle-only (the committed public contract is unchanged)."""
    agents = (_load_agents(ctx).get("agents") or {})
    alerts = _live_alerts(ctx)   # CELE-t195: an ended session's alert must not surface on the board
    cfg = load_config(ctx)
    slug = project_slug(ctx)
    qualified = bool(cfg.get("qualified_task_ids"))
    # Spine annotation (CELE-t282): position + READY stamp + why-not, computed HERE in code — the
    # rail and the PM render these verbatim, they never re-derive the predicate client-side.
    spine = _spine_doc(ctx, tasks, alerts=alerts)

    def _enrich(t: dict) -> dict:
        owner = (t.get("owner") or "").strip()
        reg = agents.get(owner) or {}
        model = reg.get("model") or ""
        return {
            **t,
            # `id` stays the canonical bare key the viewer's claim/move calls use; `display_id` is the
            # presentation form (qualified when the project opts in) the board chip can show instead.
            "display_id": _display_tid(ctx, t["id"], cfg=cfg),
            "agent_protocol": _agent_card_protocol(t["id"]),
            # Owner chip shows a session / human handle only — a model-derived owner is suppressed so
            # model text never lands on the board (CELE-t172). tasks.md itself is untouched.
            "owner": _display_owner(owner, model),
            "owner_family": reg.get("family") or "",
            "owner_model": model,
            # Live blocked-alert (CELE-t169) — only doing cards can be blocked; None means clear. Rides
            # the projection so the local board + hosted push both carry the badge; not a tasks.md field.
            "alert": alerts.get(t["id"]) if t.get("state") == "doing" else None,
            # Spine stamp (CELE-t282) — {pos, ready, why} on todo cards, None elsewhere. Projection-
            # only, never a tasks.md field: the stamp is derived state, recomputed on every save.
            "spine": spine.get(t["id"]),
            # Spine branding (CELE-t380): the group slug rides `spine_id` (not `spine`, which is the
            # stamp above) so the board can group/brand cards; `emoji` already arrives via **t.
            "spine_id": t.get("spine", ""),
        }

    enriched = [_enrich(t) for t in tasks]
    return {
        "generated_at": now_iso(),
        "project_slug": slug,
        "project_name": _project_name(ctx),
        "qualified_task_ids": qualified,  # board hint: render display_id (SLUG-tN) instead of id
        "id_prefix": slug.upper() if qualified else "",
        "states": TASK_STATES,
        "tasks": enriched,
    }


def _load_tasks(ctx: Path) -> list[dict]:
    p = _tasks_path(ctx)
    return _parse_tasks(p.read_text()) if p.is_file() else []


def _tasks_orient_summary(ctx: Path, tasks: list[dict]) -> str:
    """Compact task-board view for the Hot tier (Orient load): one count line, then the cards an
    agent resuming actually needs to see — what's in flight (doing), with any blocked_by deps flagged.
    Read-only; never touches tasks.md or the derived JSON. Returns "" when there are no tasks."""
    if not tasks:
        return ""
    counts = {s: sum(1 for t in tasks if t["state"] == s) for s in TASK_STATES}
    line = " · ".join(f"{counts[s]} {s}" for s in TASK_STATES)
    out = [f"{line}    (board: `celeborn tasks` · viewer: board/)"]
    actionable = [t for t in tasks if t["state"] == "doing"]
    cfg = load_config(ctx)
    for t in actionable:
        owner = f"  @{t['owner']}" if t["owner"] else ""
        blocked = f"  ⛔ {', '.join(t['blocked_by'])}" if t["blocked_by"] else ""
        disp = _display_tid(ctx, t["id"], cfg=cfg)
        stale = ""
        if t["state"] == "doing" and not _task_has_active_touches(ctx, t["id"]):
            stale = "  ⚠ stale (no touches) — `celeborn ship " + disp + "`"
        out.append(f"  {t['state']} → [{disp}] {t['title']}{owner}{blocked}{stale}")
    return "\n".join(out)


def _write_tasks_json(ctx: Path, tasks: list[dict]):
    _tasks_json_path(ctx).write_text(json.dumps(_tasks_doc(ctx, tasks), indent=2) + "\n")


def _save_tasks(ctx: Path, tasks: list[dict], *, autopush_ids: list[str] | None = None):
    """Persist the markdown source of truth, then refresh the derived JSON projection."""
    ctx = ctx.resolve()
    bad = [t for t in tasks if not _valid_task_id(t.get("id", ""))]
    if bad:
        sample = ", ".join(repr((t.get("title") or "")[:48]) for t in bad[:3])
        die(
            f"refusing to save {len(bad)} task(s) without valid id ({sample}). "
            "Use `###` for section headings inside card notes — only `## [tN] title` opens a card."
        )
    cfg = load_config(ctx)
    for t in tasks:
        _normalize_progress(t)  # 100%=Done invariant on every write (CELE-t131): ship→100, doing≤99
    tasks, archived = _archive_overflow_done(ctx, tasks, cfg)
    _tasks_path(ctx).write_text(_render_tasks(tasks))
    _write_tasks_json(ctx, tasks)
    if archived:
        info(f"Archived {len(archived)} done card(s) → {DONE_ARCHIVE_FILE} "
             f"(kept {cfg['done_keep_cards']} on board)")
    if autopush_ids:
        try:
            __import__("celeborn_jira").schedule_auto_push(ctx, tasks, autopush_ids)
        except Exception:
            pass  # auto-push is best-effort — never break tasks save
        try:
            __import__("celeborn_github").schedule_auto_push(ctx, tasks, autopush_ids)  # CELE-t214
        except Exception:
            pass  # GitHub mirror auto-push is best-effort too
        try:
            # Live-push the changed cards to the hosted board so celeborncode.ai updates in ~realtime
            # (detached + gated; a no-op when hosted sync isn't configured / signed in).
            __import__("celeborn_sync").schedule_hosted_push(ctx, autopush_ids)
        except Exception:
            pass  # hosted liveness is best-effort — never break tasks save


def _next_task_id(tasks: list[dict]) -> str:
    mx = 0
    for t in tasks:
        m = re.fullmatch(r"t(\d+)", t["id"])
        if m:
            mx = max(mx, int(m.group(1)))
    return f"t{mx + 1}"


# Project-qualified card ids. Stored ids stay bare `tN` (canonical key in tasks.md/.json + markers);
# qualification is presentation (display SLUG-tN) + input-acceptance (resolvers strip the qualifier).
# The qualifier is whatever precedes the final `-tN` / `/tN` — slugs may contain hyphens, so we anchor
# on the trailing t-number rather than splitting on the first separator.
_QUALIFIED_TID_RE = re.compile(r"^\s*(?:(?P<slug>.+)[-/])?(?P<tid>t\d+)\s*$", re.I)


def _split_qualified_tid(raw: str) -> tuple[str | None, str]:
    """Parse a (possibly) project-qualified card id → (slug_or_None, bare_tN). Accepts the displayed
    `SLUG-tN`, the marker form `slug/tN`, and bare `tN`. Returns (None, stripped_raw) when `raw` isn't
    a recognizable id, so callers fall back to an exact match. The bare id is lower-cased to match the
    stored `tN` key (display may upper-case the slug but never the t-number)."""
    m = _QUALIFIED_TID_RE.match(raw or "")
    if not m:
        return None, (raw or "").strip()
    return (m.group("slug") or None), m.group("tid").lower()


def _display_tid(ctx: Path | None, tid: str, *, cfg: dict | None = None, slug: str | None = None) -> str:
    """Render a card id for human output. Qualified → `SLUG-tN`; otherwise the bare `tN`. Qualify when
    an explicit `slug` is passed (cross-project views like fleet, where ambiguity is real) or when the
    local `qualified_task_ids` config is on. Never changes the stored id. `ctx` may be None ONLY when an
    explicit `slug` is supplied (the fleet path) — the local-config lookup is then skipped entirely."""
    if slug is None:
        cfg = cfg if cfg is not None else load_config(ctx)
        if not cfg.get("qualified_task_ids"):
            return tid
        slug = project_slug(ctx)
    return f"{slug.upper()}-{tid}" if slug else tid


def _resolve_task_arg(ctx: Path, raw: str) -> str:
    """Accept a project-qualified id (`SLUG-tN`, `slug/tN`) or bare `tN` from the CLI → bare `tN`.
    Warns (never fails) when the qualifier names a different project than this board — the local board
    only holds its own cards, so we resolve the bare id locally and let the caller's lookup decide."""
    slug, bare = _split_qualified_tid(raw)
    if slug:
        local = project_slug(ctx)
        if not _slug_matches(slug, local):
            warn(f"{raw!r}: project qualifier {slug!r} ≠ this board ({local!r}); resolving {bare} locally.")
    return bare


def _find_task(tasks: list[dict], tid: str) -> dict | None:
    _, bare = _split_qualified_tid(tid)
    return next((t for t in tasks if t["id"] == bare), None)


# --------------------------------------------------------------------------- progress engine (CELE-t161)
#
# Cards used to sit at 0% for their whole life because the working agent forgot to check off milestones
# and crest the bar. The fix is two-tier truth: the displayed bar = max(engine_floor, agent_set), capped
# 99 while doing. A DETERMINISTIC engine raises an honest floor from observable signals Celeborn already
# emits (commits carrying the `Celeborn-Task` trailer, touches, test runs, deploys); the working agent
# can always crest higher with semantic judgment (nudged via the UserPromptSubmit channel, never
# required). The engine is monotonic (only ever raises), idempotent, capped at 99 while doing, never
# overrides a higher manual value, and no-ops on todo/done. No LLM, no network — pure stdlib.
#
# State lives in `.context/progress.json` (local, gitignored, sibling of activity.md), one record per
# doing card. The bar ITSELF lives on the card (tasks.json `progress`), written through the normal
# task-save path so hosted autopush + the board just work. progress.json is engine bookkeeping only.
PROGRESS_NAME = "progress.json"
PROGRESS_SCHEMA = "celeborn-progress/1"
CLAIM_FLOOR = 5          # the instant a card goes doing
WORK_FLOOR = 10          # first observable work signal
SIGNAL_RAMP_STEP = 8     # no-milestone fallback: +8 per distinct hard signal …
SIGNAL_RAMP_CAP = 60     # … capped at 60 absolute (the band formula takes over once milestones exist)
NUDGE_T1, NUDGE_T2, NUDGE_T3 = 2, 4, 6   # turns-since-movement thresholds for the nudge ladder

# keyword↔signal map — data-driven so it's trivially extensible. A milestone's TEXT is matched by the
# same patterns against the evidence corpus: code ticks the machine-verifiable, the agent handles
# judgment ("reads cleanly", "UX feels right" — no pattern, left for the agent / Part C).
MILESTONE_SIGNALS = [
    (re.compile(r'\b(commit|committed)\b', re.I), 'commit'),
    (re.compile(r'\b(test|tests|suite|green|passing|tsc)\b', re.I), 'tests_green'),
    (re.compile(r'\b(deploy|deployed|ship|shipped|push|pushed|prod)\b', re.I), 'deploy'),
    (re.compile(r'\b(merge|merged|\bpr\b|pull request)\b', re.I), 'merge'),
]


def _progress_path(ctx: Path) -> Path:
    return ctx / PROGRESS_NAME


def _load_progress(ctx: Path) -> dict:
    p = _progress_path(ctx)
    if not p.is_file():
        return {"schema": PROGRESS_SCHEMA, "cards": {}}
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {"schema": PROGRESS_SCHEMA, "cards": {}}
    data.setdefault("schema", PROGRESS_SCHEMA)
    data.setdefault("cards", {})
    return data


def _save_progress(ctx: Path, data: dict) -> None:
    """Atomic write (tmp + os.replace) so a crash mid-write never corrupts the registry."""
    import os
    data["schema"] = PROGRESS_SCHEMA
    p = _progress_path(ctx)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, p)


def _progress_rec(data: dict, tid: str) -> dict:
    rec = (data.get("cards") or {}).get(tid)
    if rec is None:
        rec = {"engine_floor": 0, "last_progress": 0, "last_change_ts": "", "claimed_at": "",
               "work_started": False, "auto_ticked_idx": [], "auto_ticked": [],
               "nudge_level": 0, "turns_since_change": 0}
    return rec


def _commits_for_task(ctx: Path, tid: str, since_iso: str | None = None, limit: int = 200) -> list[dict]:
    """Commits whose message carries the `Celeborn-Task: tN` trailer for this card. Returns
    [{hash, ts(epoch int), subject, body}], newest first. Empty if not a git repo / no history."""
    import subprocess
    _, bare = _split_qualified_tid(tid)
    try:
        out = subprocess.run(
            ["git", "-C", str(ctx.parent), "log", f"-n{int(limit)}",
             "--format=%H%x1f%ct%x1f%s%x1f%b%x1e"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    since_epoch = None
    if since_iso:
        try:
            since_epoch = _dt.datetime.fromisoformat(since_iso).timestamp()
        except (ValueError, TypeError):
            since_epoch = None
    trailer = re.compile(rf"Celeborn-Task:\s*{re.escape(bare)}\b", re.I)
    rows = []
    for rec in out.stdout.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 4:
            continue
        h, ts, subject, body = parts[0], parts[1], parts[2], parts[3]
        if not trailer.search(subject + "\n" + body):
            continue
        try:
            ets = int(ts)
        except ValueError:
            ets = 0
        if since_epoch is not None and ets and ets < since_epoch - 1:
            continue
        rows.append({"hash": h, "ts": ets, "subject": subject, "body": body})
    return rows


def _activity_signal_corpus(ctx: Path) -> str:
    """The mechanical 'Recent commands' / 'Recent commits' sections of activity.md — evidence of work
    actually run this session. Deliberately excludes 'Last prompt' (which would create false signals)."""
    p = ctx / "activity.md"
    if not p.is_file():
        return ""
    try:
        text = p.read_text()
    except OSError:
        return ""
    out, keep = [], False
    for line in text.splitlines():
        if line.startswith("## Recent commands") or line.startswith("## Recent commits"):
            keep = True
            continue
        if line.startswith("## "):
            keep = False
            continue
        if keep:
            out.append(line)
    return "\n".join(out)


def _task_has_touch(ctx: Path, tid: str) -> bool:
    _, bare = _split_qualified_tid(tid)
    for recs in (_load_touches(ctx).get("files") or {}).values():
        for meta in recs:
            t = (meta or {}).get("task") or ""
            if t and (_split_qualified_tid(t)[1] == bare):
                return True
    return False


def _progress_signals(ctx: Path, card: dict, rec: dict) -> dict:
    """Gather observable evidence for the card since it was claimed. Returns
    {present: set(tokens), commits: int, touched: bool, corpus: str}."""
    since = rec.get("claimed_at") or ""
    commits = _commits_for_task(ctx, card["id"], since_iso=since or None)
    corpus = _activity_signal_corpus(ctx) + "\n" + "\n".join(
        c["subject"] + "\n" + c["body"] for c in commits)
    present = set()
    if commits:
        present.add("commit")
    for rx, token in MILESTONE_SIGNALS:
        if rx.search(corpus):
            present.add(token)
    return {"present": present, "commits": len(commits), "touched": _task_has_touch(ctx, card["id"]),
            "corpus": corpus}


def _auto_tick_milestones(card: dict, signals: dict, rec: dict) -> list[int]:
    """Tick each unchecked milestone whose text matches a present signal — once per milestone (a human
    uncheck is respected). Judgment milestones (no matching pattern) are left for the agent."""
    present = signals.get("present") or set()
    already = set(rec.get("auto_ticked_idx") or [])
    credited = list(rec.get("auto_ticked") or [])
    newly = []
    for i, s in enumerate(card.get("subtasks") or []):
        if s.get("done") or i in already:
            continue
        for rx, token in MILESTONE_SIGNALS:
            if token in present and rx.search(s.get("text", "")):
                s["done"] = True
                newly.append(i)
                already.add(i)
                if token not in credited:
                    credited.append(token)
                break
    rec["auto_ticked_idx"] = sorted(already)
    rec["auto_ticked"] = credited
    return newly


def _engine_floor(card: dict, signals: dict, rec: dict) -> int:
    """The monotonic floor formula. Lifecycle (5 on claim, 10 on first work) is baked into
    rec['engine_floor'] by the stamps; here we add the milestone band (or a signal ramp when a card has
    no milestones) and take the high-water mark. Capped 99 while doing — ship is the only path to 100."""
    base = rec.get("engine_floor", 0) or 0
    # Before the first work signal a doing card rests at the claim floor (5) — the 10→99 band and the
    # signal ramp only kick in once work has started (so claim=5, first work=10, then climb).
    if not rec.get("work_started"):
        return min(99, base)
    subs = card.get("subtasks") or []
    if subs:
        total = sum(max(1, int(s.get("weight", 1))) for s in subs)
        done = sum(max(1, int(s.get("weight", 1))) for s in subs if s.get("done"))
        ratio = (done / total) if total else 0
        derived = WORK_FLOOR + round(ratio * 89)            # 10 → 99 span
    else:
        n_sig = len(signals.get("present") or set())
        derived = min(SIGNAL_RAMP_CAP, WORK_FLOOR + n_sig * SIGNAL_RAMP_STEP)
    return min(99, max(base, derived))


def _progress_engine_tick(ctx: Path, card: dict, *, count_turn: bool = False) -> dict:
    """Orchestrate one engine pass for a DOING card: detect work, auto-tick signal-backed milestones,
    raise the monotonic floor, and set card['progress'] = max(floor, current) capped 99. Idempotent;
    no-op on todo/done. Persists progress.json. Does NOT save the card (caller saves via _save_tasks).
    Returns {moved, newly, floor, signals, rec}."""
    if card.get("state") != "doing":
        return {"moved": False, "newly": [], "floor": 0, "signals": {"present": set()}, "rec": {}}
    data = _load_progress(ctx)
    rec = _progress_rec(data, card["id"])
    if not rec.get("claimed_at"):
        rec["claimed_at"] = now_iso()
    # high-water across both stores; doing ⇒ at least the claim floor.
    rec["engine_floor"] = max(int(rec.get("engine_floor", 0) or 0),
                              int(card.get("engine_floor", 0) or 0), CLAIM_FLOOR)
    signals = _progress_signals(ctx, card, rec)
    # First observable work signal → raise the floor to WORK_FLOOR (sticky). A checked milestone counts:
    # ticking a box is itself unambiguous evidence work has started.
    any_done = any(s.get("done") for s in (card.get("subtasks") or []))
    if not rec.get("work_started") and (signals["commits"] or signals["touched"]
                                        or signals["present"] or any_done):
        rec["work_started"] = True
        rec["engine_floor"] = max(int(rec.get("engine_floor", 0) or 0), WORK_FLOOR)
    newly = _auto_tick_milestones(card, signals, rec)
    floor = _engine_floor(card, signals, rec)
    rec["engine_floor"] = max(int(rec.get("engine_floor", 0) or 0), floor)   # monotonic high-water
    card["engine_floor"] = rec["engine_floor"]                               # persist so reload-recompute respects it
    new_prog = max(rec["engine_floor"], int(card.get("progress", 0) or 0))   # never override higher manual
    card["progress"] = new_prog
    _normalize_progress(card)                                                # caps 99 unless done
    moved = card["progress"] != rec.get("last_progress")
    if moved or newly:
        rec["last_progress"] = card["progress"]
        rec["last_change_ts"] = now_iso()
        rec["nudge_level"] = 0
        rec["turns_since_change"] = 0
    elif count_turn:
        rec["turns_since_change"] = int(rec.get("turns_since_change", 0) or 0) + 1
    data.setdefault("cards", {})[card["id"]] = rec
    _save_progress(ctx, data)
    return {"moved": moved, "newly": newly, "floor": rec["engine_floor"], "signals": signals, "rec": rec}


def _progress_stamp_claim(ctx: Path, card: dict) -> None:
    """The instant a card goes doing (in the claim path): floor 5 + a claimed_at anchor. Mutates
    card['progress'] so the claim's own _save_tasks(autopush) carries the 5% to the hosted board."""
    if card.get("state") != "doing":
        return
    data = _load_progress(ctx)
    rec = _progress_rec(data, card["id"])
    rec.setdefault("claimed_at", now_iso())
    rec["engine_floor"] = max(int(rec.get("engine_floor", 0) or 0), CLAIM_FLOOR)
    data.setdefault("cards", {})[card["id"]] = rec
    _save_progress(ctx, data)
    card["engine_floor"] = max(int(card.get("engine_floor", 0) or 0), CLAIM_FLOOR)  # marks the card engine-tracked
    card["progress"] = max(int(card.get("progress", 0) or 0), CLAIM_FLOOR)
    _normalize_progress(card)


def _session_task_id(ctx: Path, session: str | None) -> str:
    """The doing card this session is signed in to (via agent_sessions), or ''."""
    sid = (session or "").strip()
    if not sid:
        return ""
    tid = ((_load_metrics(ctx).get("agent_sessions") or {}).get(sid) or {}).get("task") or ""
    tid = tid.strip()
    if not tid:
        return ""
    card = next((t for t in _load_tasks(ctx) if t["id"] == _split_qualified_tid(tid)[1]), None)
    return tid if (card is not None and card.get("state") == "doing") else ""


def _progress_nudge_line(ctx: Path, card: dict, res: dict) -> str:
    """Compute the escalation level from turns-since-movement + unaccounted signals, craft a
    copy-pasteable line (real id + the obvious milestone number), and persist the new level. Empty when
    there is nothing to say. Tagged for surfacing by _compose_user_prompt_envelope."""
    rec = res.get("rec") or {}
    if card.get("state") != "doing":
        return ""
    disp = _display_tid(ctx, card["id"])
    pct = int(card.get("progress", 0) or 0)
    turns = int(rec.get("turns_since_change", 0) or 0)
    signals = res.get("signals") or {}
    n_commits = int(signals.get("commits", 0) or 0)
    subs = card.get("subtasks") or []
    # The obvious move: first unchecked milestone whose text matches a present signal, else first unchecked.
    present = signals.get("present") or set()
    target = None
    for i, s in enumerate(subs):
        if s.get("done"):
            continue
        if any((token in present and rx.search(s.get("text", ""))) for rx, token in MILESTONE_SIGNALS):
            target = i + 1
            break
    if target is None:
        target = next((i + 1 for i, s in enumerate(subs) if not s.get("done")), None)
    check_cmd = f"celeborn tasks check {card['id']} {target}" if target else f"celeborn tasks edit {card['id']} --progress {min(99, pct + 10)}"

    level = 0
    if turns >= NUDGE_T3:
        level = 3
    elif turns >= NUDGE_T2 or (n_commits and target is not None):
        level = 2
    elif turns >= NUDGE_T1:
        level = 1
    rec["nudge_level"] = level
    data = _load_progress(ctx)
    data.setdefault("cards", {})[card["id"]] = rec
    _save_progress(ctx, data)
    if level == 0:
        return ""
    if level == 1:
        return f"🏹 Celeborn —> {disp} at {pct}% — tick any finished milestones: {check_cmd}"
    if level == 2:
        ev = f"{n_commits} commit{'s' if n_commits != 1 else ''} landed" if n_commits else "work is moving"
        return (f"🏹 Celeborn —> {disp}: {ev}, bar hasn't moved. Check off completed milestones now → "
                f"{check_cmd}")
    return (f"🏹 Celeborn —> {disp}: auto-advanced to {pct}% from commit/test signals. Crest higher if "
            f"more is done: celeborn tasks edit {card['id']} --progress {min(99, pct + 10)}")


def _progress_hook(ctx: Path, session: str | None) -> str:
    """UserPromptSubmit entry point: tick the session's doing card and return the nudge line (or '')."""
    tid = _session_task_id(ctx, session)
    if not tid:
        return ""
    tasks = _load_tasks(ctx)
    card = _find_task(tasks, tid)
    if card is None or card.get("state") != "doing":
        return ""
    res = _progress_engine_tick(ctx, card, count_turn=True)
    if res["moved"] or res["newly"]:
        card["updated"] = now_iso()
        _save_tasks(ctx, tasks, autopush_ids=[card["id"]])
    return _progress_nudge_line(ctx, card, res)


def cmd_progress(args):
    """`celeborn progress [<id>] [--explain]` — run the deterministic progress engine once for a card
    (or every doing card) and show the signals → floor derivation. Debug/inspection; also moves the bar."""
    ctx = require_context(args)
    tasks = _load_tasks(ctx)
    tid = getattr(args, "id", None)
    if tid:
        resolved = _split_qualified_tid(_resolve_task_arg(ctx, tid))[1]
        targets = [t for t in tasks if t["id"] == resolved]
        if not targets:
            die(f"no task with id {tid!r}")
    else:
        targets = [t for t in tasks if t.get("state") == "doing"]
    if not targets:
        info("no doing cards to evaluate.")
        return
    explain = getattr(args, "explain", False)
    for card in targets:
        disp = _display_tid(ctx, card["id"])
        if card.get("state") != "doing":
            info(f"[{disp}] is {card.get('state')} — the engine only runs on doing cards.")
            continue
        before = int(card.get("progress", 0) or 0)
        res = _progress_engine_tick(ctx, card)
        if res["moved"] or res["newly"]:
            card["updated"] = now_iso()
            _save_tasks(ctx, tasks, autopush_ids=[card["id"]])
        print(f"[{disp}] {before}% → {card['progress']}%  (engine floor {res['floor']})")
        if explain:
            sig = res["signals"]
            present = ", ".join(sorted(sig.get("present") or [])) or "none"
            subs = card.get("subtasks") or []
            done = sum(1 for s in subs if s.get("done"))
            rec = res["rec"]
            print(f"    signals present  : {present}")
            print(f"    commits (trailer): {sig.get('commits', 0)}    touched: {sig.get('touched', False)}")
            print(f"    milestones       : {done}/{len(subs)} checked"
                  + (f"  (auto-ticked this run: {res['newly']})" if res["newly"] else ""))
            print(f"    work_started     : {rec.get('work_started')}    nudge_level: "
                  f"{rec.get('nudge_level')}    turns_since_change: {rec.get('turns_since_change')}")


def cmd_alert(args):
    """`celeborn alert <id> [--message …] [--kind permission|idle|stopped] [--session …]` — the
    reusable "coding progress is blocked, the user's input is needed" service (CELE-t169). Raises a
    live alert on a DOING card so it surfaces on the board (locally + celeborncode.ai). The
    Notification/Stop hooks are its first callers; any external system can call it the same way.
      celeborn alert <id> --message "…"      raise/refresh an alert
      celeborn alert <id> --clear            drop it (also happens automatically when the user replies)
      celeborn alert --list                  show the live alerts on this board
    No focus-stealing OS dialog — the alert rides the card (dialogs rejected t47/t50/t62)."""
    ctx = require_context(args)
    if getattr(args, "list", False) or not getattr(args, "id", None):
        alerts = _load_alerts(ctx).get("alerts") or {}
        if not alerts:
            info("no live alerts.")
            return
        for tid, rec in sorted(alerts.items()):
            disp = _display_tid(ctx, tid)
            print(f"🔔 [{disp}] {rec.get('kind', 'idle')} — {rec.get('message') or '(no message)'}"
                  f"  ({rec.get('at', '')})")
        return
    resolved = _split_qualified_tid(_resolve_task_arg(ctx, args.id))[1]
    card = next((t for t in _load_tasks(ctx) if t["id"] == resolved), None)
    if card is None:
        die(f"no task with id {args.id!r}")
    disp = _display_tid(ctx, resolved)
    if getattr(args, "clear", False):
        cleared = _clear_alert(ctx, resolved)
        _refresh_alerted_card(ctx, resolved)
        info(f"cleared alert on [{disp}]" if cleared else f"[{disp}] had no alert")
        return
    if card.get("state") != "doing":
        die(f"[{disp}] is {card.get('state')} — only a doing card can be blocked.")
    kind = getattr(args, "kind", None) or "idle"
    if kind not in ALERT_KINDS:
        die(f"--kind must be one of {', '.join(ALERT_KINDS)}")
    rec = _set_alert(ctx, resolved, kind, getattr(args, "message", "") or "", getattr(args, "session", "") or "")
    _refresh_alerted_card(ctx, resolved)
    # Push the alert to the hosted board now (not on the throttled heartbeat) so a remote watcher
    # sees the block promptly. Best-effort; a no-op when hosted sync isn't configured.
    try:
        __import__("celeborn_sync").schedule_agents_push(ctx, min_interval_s=0)
    except Exception:  # noqa: BLE001
        pass
    ok(f"🔔 alerted [{disp}] — {rec['kind']}" + (f": {rec['message']}" if rec.get("message") else ""))


def cmd_ask(args):
    """`celeborn ask "<question>" [--session …] [--card …] [--options a,b,c] [--json]` — park a
    human-in-the-loop question so the board's dock can answer it (CELE-t280). The `ask_human`
    OpenCode tool is the primary caller: it files the ask, raises the card alert (so the question
    surfaces on the Stage dock), then polls `celeborn ask-status <id>` until answered. Prints the
    askId (or {id, card, …} with --json)."""
    ctx = require_context(args)
    question = (getattr(args, "question", "") or "").strip()
    if not question:
        die("ask requires a question")
    session = (getattr(args, "session", "") or _ambient_session_id()).strip()
    card_arg = getattr(args, "card", "") or ""
    card = (_split_qualified_tid(_resolve_task_arg(ctx, card_arg))[1] if card_arg
            else _split_qualified_tid(_session_task_id(ctx, session))[1])
    options = [o.strip() for o in re.split(r"[,\n]", getattr(args, "options", "") or "") if o.strip()]
    rec = _new_ask(ctx, session, card, question, options)
    # Surface it on the dock now by raising the card's blocked-alert (kind=permission → needs the
    # user). Only a DOING card carries an alert; a card-less ask still parks (the tool can poll).
    if card:
        t = _find_task(_load_tasks(ctx), card)
        if t and t.get("state") == "doing":
            _set_alert(ctx, card, "permission", question, session)
            _refresh_alerted_card(ctx, card)
            try:
                __import__("celeborn_sync").schedule_agents_push(ctx, min_interval_s=0)
            except Exception:  # noqa: BLE001
                pass
    if getattr(args, "json", False):
        print(json.dumps({"id": rec["id"], "card": card, "question": question, "options": options}))
    else:
        print(rec["id"])


def cmd_ask_status(args):
    """`celeborn ask-status <askId> [--json]` — the poll the `ask_human` tool loops on. Prints the
    answer once it lands (nothing while pending); with --json, {answered, answer}. Always exits 0 so
    a poller reads pending (answered=false) apart from a real error (die → non-zero)."""
    ctx = require_context(args)
    rec = _load_asks(ctx).get("asks", {}).get(args.ask_id)
    if rec is None:
        die(f"no such ask {args.ask_id!r}")
    answered = rec.get("answer") is not None
    if getattr(args, "json", False):
        print(json.dumps({"answered": answered, "answer": rec.get("answer")}))
    elif answered:
        print(rec["answer"])


def cmd_answer(args):
    """`celeborn answer <card> --kind permission|text --response "<a>" [--session …] [--question …]`
    — deliver a human's dock answer and journal it (CELE-t280). PERMISSION answers have already
    resumed the session live (the board POSTed once/always/reject to OpenCode); this only records
    them. TEXT answers fill the open `ask_human` ask (the blocking tool returns) or — if nothing is
    waiting — fall through to the outbox for the agent's next turn. Every answer is journaled to the
    card (compact note trail) and journal.md, and the card's blocked-alert is cleared."""
    ctx = require_context(args)
    resolved = _split_qualified_tid(_resolve_task_arg(ctx, args.card))[1]
    card = _find_task(_load_tasks(ctx), resolved)
    if card is None:
        die(f"no task with id {args.card!r}")
    disp = _display_tid(ctx, resolved)
    kind = (getattr(args, "kind", "") or "text").strip()
    answer = (getattr(args, "response", "") or "").strip()
    if not answer:
        die("answer requires --response")
    session = (getattr(args, "session", "") or "").strip()
    question = (getattr(args, "question", "") or "").strip()
    # Per-prompt model the human picked in the dock/prompt-line [model ▾] (CELE-t346). It rides the
    # journal + card trail, and — on the outbox path — the delivered message, so the next turn knows
    # which model the operator asked the agent to answer with.
    model = (getattr(args, "model", "") or "").strip()

    if kind == "permission":
        how = f"permission:{answer}"      # once | always | reject; the live resume already happened
    else:
        rec = _open_ask_for(ctx, session=session, card=resolved)
        if rec is not None:
            question = question or rec.get("question", "")
            _answer_ask(ctx, rec["id"], answer)
            how = "ask_human"
        else:
            # Nothing is blocking on an answer — deliver it as the agent's next-turn prompt.
            addressee = session[:6] if session else (card.get("owner") or "")
            msg = (f"💬 Board answer to your question — {question!r}:\n\n{answer}"
                   if question else f"💬 Message from the board:\n\n{answer}")
            if model:
                msg += f"\n\n(requested model: {model})"
            _outbox_queue(ctx, msg, addressee, tag=" dock-answer")
            how = "outbox"

    _append_card_qa(ctx, resolved, question, answer, kind, model)
    _journal_dock_qa(ctx, disp, question, answer, how, kind, model)
    _clear_alert(ctx, resolved)          # the session is no longer awaiting the user (CELE-t195)
    _refresh_alerted_card(ctx, resolved)
    try:
        __import__("celeborn_sync").schedule_agents_push(ctx, min_interval_s=0)
    except Exception:  # noqa: BLE001
        pass
    ok(f"answered [{disp}] via {how}: {answer}")


def _reorder_task(tasks: list[dict], tid: str, direction: str) -> list[dict]:
    """Reprioritize a task within its own column (state group). Display order within a column is
    list order, so we permute only the same-state siblings among the slots they already occupy —
    tasks in other states keep their absolute positions. `direction`: up | down | top | bottom."""
    target = _find_task(tasks, tid)
    if not target:
        return tasks
    slots = [i for i, t in enumerate(tasks) if t["state"] == target["state"]]
    sibs = [tasks[i] for i in slots]
    pos = next(i for i, t in enumerate(sibs) if t["id"] == tid)
    if direction == "up":
        new = max(0, pos - 1)
    elif direction == "down":
        new = min(len(sibs) - 1, pos + 1)
    elif direction == "top":
        new = 0
    elif direction == "bottom":
        new = len(sibs) - 1
    else:
        return tasks
    sibs.insert(new, sibs.pop(pos))
    out = list(tasks)
    for slot, sib in zip(slots, sibs):
        out[slot] = sib
    return out


def _bring_to_state_front(tasks: list[dict], tid: str) -> list[dict]:
    """Move task `tid` to the front of its own state group in the flat list, so it renders at the
    *top* of that column. Used when a task is completed: the newest-done card arrives on top and
    pushes older done cards down (design: card-assignment.md / done-column ordering)."""
    t = _find_task(tasks, tid)
    if not t:
        return tasks
    rest = [x for x in tasks if x["id"] != tid]
    idx = next((i for i, x in enumerate(rest) if x["state"] == t["state"]), len(rest))
    rest.insert(idx, t)
    return rest


def _done_tasks_ordered(tasks: list[dict]) -> list[dict]:
    """Done cards in board column order (top/newest first, bottom/oldest last)."""
    return [t for t in tasks if t["state"] == "done"]


def _append_done_archive(ctx: Path, cards: list[dict], cfg: dict) -> None:
    """Append overflow done cards to done-archive.md; drop oldest entries past the FIFO cap."""
    if not cards:
        return
    path = ctx / DONE_ARCHIVE_FILE
    existing = _parse_tasks(path.read_text()) if path.is_file() else []
    combined = existing + cards
    cap = int(cfg.get("done_archive_keep_cards", DEFAULTS["done_archive_keep_cards"]))
    if len(combined) > cap:
        combined = combined[len(combined) - cap:]
    path.write_text(_render_tasks(combined, header=DONE_ARCHIVE_HEADER))


def _archive_overflow_done(ctx: Path, tasks: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    """Move done cards past `done_keep_cards` off the board into done-archive.md."""
    keep = int(cfg.get("done_keep_cards", DEFAULTS["done_keep_cards"]))
    done_ordered = _done_tasks_ordered(tasks)
    if len(done_ordered) <= keep:
        return tasks, []
    overflow = done_ordered[keep:]
    overflow_ids = {t["id"] for t in overflow}
    remaining = [t for t in tasks if t["id"] not in overflow_ids]
    _append_done_archive(ctx, overflow, cfg)
    return remaining, overflow


# --------------------------------------------------------------------------- NEXT-UP selector (CELE-t219)
#
# A small PM model handed the raw board text cannot be trusted to pick the next card: in the
# 2026-07-04 test the Qwen-4b PM fixated on one card's agent-protocol block and answered "none"
# past a plainly ready card. So readiness is computed HERE, deterministically, and the PM's job
# collapses to invoking `celeborn next` and echoing the answer verbatim. Everything this path
# emits is data-only — card id + title, no notes, no protocol boilerplate.

def _strip_protocol(text: str) -> str:
    """Cut agent-protocol boilerplate out of emitted card text. A title (or any text) that had a
    protocol block pasted into it must never reach the NEXT-UP emitter's output — a small PM model
    mistakes it for card data (CELE-t219 evidence)."""
    if AGENT_PROTOCOL_MARKER in text:
        text = text.split(AGENT_PROTOCOL_MARKER, 1)[0]
    return text.strip()


def _archived_done_ids(ctx: Path) -> set[str]:
    """Ids of done cards FIFO-archived off the board (done-archive.md). The READY predicate treats
    them as Done — a blocker that shipped long ago must not wedge its dependents just because the
    Done column capped out and the card aged into the archive."""
    p = ctx / DONE_ARCHIVE_FILE
    return {t["id"] for t in _parse_tasks(p.read_text())} if p.is_file() else set()


def _ready_set(tasks: list[dict], archived_done: set[str], *,
               tags: list[str] | None = None,
               phase: str = "") -> tuple[list[dict], dict[str, list[str]]]:
    """READY = state todo AND every `blocked_by` id Done (on the board, or archived-done). Pure and
    deterministic: board order in, board order out — the todo column is already priority-ordered
    (`tasks reorder`), so callers take [0] as NEXT-UP with no judgment of their own (CELE-t219).

    A blocker found nowhere (not on the board, not in the archive) counts as satisfied: ids are
    never reused, so a vanished blocker was either archived-then-FIFO-dropped (it was Done) or
    deliberately `rm`'d — wedging the dependent forever helps no one. Those ids come back in the
    second return value, keyed by dependent card id, so the caller can flag them for a human.

    Filters: `tags` — the card must carry every listed tag; `phase` — exact match."""
    by_id = {t["id"]: t for t in tasks}
    ready: list[dict] = []
    unknown: dict[str, list[str]] = {}
    for t in tasks:
        if t["state"] != "todo":
            continue
        if tags and not set(tags) <= set(t["tags"]):
            continue
        if phase and t["phase"] != phase:
            continue
        blocked = False
        for b in t["blocked_by"]:
            bt = by_id.get(b)
            if bt is not None:
                if bt["state"] != "done":
                    blocked = True
                    break
            elif b not in archived_done:
                unknown.setdefault(t["id"], []).append(b)
        if not blocked:
            ready.append(t)
    return ready, unknown


def _ready_card_doc(ctx: Path, t: dict, cfg: dict) -> dict:
    """Machine form of one ready card for `next --json`. Deliberately thin — id, display id, title
    and the routing fields (tags/phase/blocked-by); never notes or protocol text."""
    return {
        "id": t["id"],
        "display_id": _display_tid(ctx, t["id"], cfg=cfg),
        "title": _strip_protocol(t["title"]),
        "tags": t["tags"],
        "phase": t["phase"],
        "blocked_by": t["blocked_by"],
    }


# --------------------------------------------------------------------------- spine discipline (CELE-t282)
#
# The Spine invariant (docs/plans/cele-t144-spine-and-stage.md §4): card ① — the first READY todo
# card in board order — must, at all times, be startable by a FRESH agent with zero project context
# beyond orient. `_ready_set` (CELE-t219) settles blockers; startability adds three more mechanical
# clauses: a real Stop condition, a brief in the note, no open question on the card. All of it is
# a field-by-field predicate in code — the no-think PM (t283) and the board rail render stamps,
# they never re-derive them.

SPINE_BRIEF_MIN_CHARS = 60   # a startable card carries a 3-8 line brief; below this it's a bare title


def _spine_audit(t: dict, *, alerts: dict | None = None) -> list[str]:
    """Startability violations for one spine card, beyond blocker-readiness (which is
    `_ready_set`'s job): real Stop condition, brief present, no open question. Returns [] when the
    card is startable verbatim. Pure field checks — evaluable by anything, reasoned by nothing."""
    why: list[str] = []
    stop = (t.get("stop") or "").strip()
    if not stop:
        why.append("no Stop condition")
    elif stop == DEFAULT_STOP:
        why.append("Stop condition is still the auto-filled default")
    brief = _strip_protocol(t.get("notes") or "")
    if len(brief) < SPINE_BRIEF_MIN_CHARS:
        why.append(f"brief too thin ({len(brief)}/{SPINE_BRIEF_MIN_CHARS} chars in the note)")
    rec = (alerts or {}).get(t["id"])
    if rec and rec.get("kind") != "spine":
        # Carry the question itself into the why so the rail's ✋ and the ship pre-flight show WHAT
        # is being asked, not just that it exists. A `spine`-kind alert is excluded on purpose: that
        # is the PM's own hand (CELE-t283) — an ECHO of the violations this function already found,
        # never an independent input. Feeding it back would let a stale hand wedge `ship --strict`
        # whenever the PM daemon isn't running to clear it.
        msg = (rec.get("message") or "").strip()
        why.append(f"open question on the card — {msg}" if msg else "open question on the card (alert raised)")
    return why


def _spine_doc(ctx: Path, tasks: list[dict], *, alerts: dict | None = None) -> dict[str, dict]:
    """Per-card spine annotation for the board projection: position (board order within the todo
    column — the spine is a total order, not a pool), the full READY stamp, and the why-not
    reasons. Keyed by card id; only todo cards appear. A blocker missing from the board counts
    done (`_ready_set` semantics — ids are never reused, so a vanished blocker shipped or was
    deliberately removed)."""
    by_id = {t["id"]: t for t in tasks}
    if alerts is None:
        alerts = _live_alerts(ctx)
    out: dict[str, dict] = {}
    pos = 0
    for t in tasks:
        if t["state"] != "todo":
            continue
        pos += 1
        waiting = [b for b in t["blocked_by"] if b in by_id and by_id[b]["state"] != "done"]
        why = (["waiting on " + ", ".join(waiting)] if waiting else []) + _spine_audit(t, alerts=alerts)
        # The PM's raised hand (CELE-t283): board-visible on the rail, appended AFTER the audit so
        # the predicate stays pure — the hand echoes violations, it never creates one.
        rec = (alerts or {}).get(t["id"])
        if rec and rec.get("kind") == "spine" and (rec.get("message") or "").strip():
            why.append(f"✋ {rec['message'].strip()}")
        out[t["id"]] = {"pos": pos, "ready": not why, "why": why}
    return out


def _spine_preflight(ctx: Path, tasks: list[dict], shipped_id: str) -> tuple[dict | None, list[str]]:
    """The ship ritual's gate (§4): you may not ship card N until the next spine card is startable
    verbatim. Simulates the post-ship board (the shipping card counted Done, so its dependents
    unblock), takes the head `_ready_set` would dispatch — t219 verbatim, never a blocked card —
    and audits it. Returns (head-card-or-None, violations); (None, []) means the spine is empty."""
    sim = [dict(t, state="done") if t["id"] == shipped_id else t for t in tasks]
    if not any(t["state"] == "todo" for t in sim):
        return None, []
    ready, _ = _ready_set(sim, _archived_done_ids(ctx))
    if not ready:
        return None, ["spine has no READY head — every todo card is blocked"]
    return ready[0], _spine_audit(ready[0], alerts=_live_alerts(ctx))


# --------------------------------------------------------------------------- spine branding (CELE-t380)
# Every spine (the group of cards minted from one plan, keyed by the `spine` slug) carries ONE
# purpose-emoji, unique per project across distinct slugs, so spines read as visually distinct on the
# board. The agent proposes the emoji at plan/mint time; these helpers enforce the two invariants:
#   A. all cards sharing a spine slug share one emoji;  B. distinct slugs never share an emoji.

def _norm_emoji(s: str) -> str:
    """Normalize a proposed brand glyph: strip surrounding whitespace. A brand is a single glyph
    (possibly a ZWJ/variation-selector sequence like ⚙️) — internal whitespace is rejected upstream."""
    return (s or "").strip()


def _spine_brand_conflict(tasks: list[dict], emoji: str, spine_slug: str) -> str:
    """The slug of a DIFFERENT spine already branded with `emoji` on this project, or "" if free.
    Enforces invariant B — the per-project uniqueness the collision check rejects on add/set."""
    emoji = _norm_emoji(emoji)
    if not emoji:
        return ""
    for t in tasks:
        other = (t.get("spine") or "").strip()
        if other and other != spine_slug and _norm_emoji(t.get("emoji", "")) == emoji:
            return other
    return ""


def _emoji_for_slug(tasks: list[dict], spine_slug: str) -> str:
    """The emoji currently branding `spine_slug` (invariant A means it's single-valued), or ""."""
    for t in tasks:
        if (t.get("spine") or "").strip() == spine_slug and _norm_emoji(t.get("emoji", "")):
            return _norm_emoji(t["emoji"])
    return ""


def _taken_emoji(tasks: list[dict]) -> dict[str, str]:
    """{emoji: slug} for every branded spine on the project — shown to the agent so it can pick an
    unused glyph on collision."""
    out: dict[str, str] = {}
    for t in tasks:
        slug, emoji = (t.get("spine") or "").strip(), _norm_emoji(t.get("emoji", ""))
        if slug and emoji:
            out.setdefault(emoji, slug)
    return out


def _spines_summary(tasks: list[dict]) -> list[dict]:
    """One row per distinct spine slug: {slug, emoji, counts:{todo,doing,done}, total}, ordered by
    first appearance. Drives `celeborn spine ls` and any board spine-header grouping."""
    order: list[str] = []
    agg: dict[str, dict] = {}
    for t in tasks:
        slug = (t.get("spine") or "").strip()
        if not slug:
            continue
        if slug not in agg:
            order.append(slug)
            agg[slug] = {"slug": slug, "emoji": "", "counts": {s: 0 for s in TASK_STATES}, "total": 0}
        row = agg[slug]
        if not row["emoji"] and _norm_emoji(t.get("emoji", "")):
            row["emoji"] = _norm_emoji(t["emoji"])
        st = t["state"] if t["state"] in TASK_STATES else "todo"
        row["counts"][st] += 1
        row["total"] += 1
    return [agg[s] for s in order]


def _apply_spine_brand(tasks: list[dict], spine_slug: str, emoji: str) -> int:
    """Set `emoji` on every card in `spine_slug` (invariant A: brand a spine atomically). Returns
    the number of cards restamped."""
    emoji = _norm_emoji(emoji)
    n = 0
    for t in tasks:
        if (t.get("spine") or "").strip() == spine_slug:
            if _norm_emoji(t.get("emoji", "")) != emoji:
                t["emoji"] = emoji
                t["updated"] = now_iso()
            n += 1
    return n


def _brand_error(tasks: list[dict], spine_slug: str, emoji: str) -> str:
    """Validate a proposed (spine, emoji) branding against both invariants; return a human-facing
    error message (with the taken-list so the agent can pick the next best fit), or "" if it's OK.
    Shared by `tasks add`, `tasks edit`, and `spine set`."""
    spine_slug, emoji = (spine_slug or "").strip(), _norm_emoji(emoji)
    if not emoji:
        return ""
    if any(ch.isspace() for ch in emoji) or len(emoji) > 8:
        return f"emoji {emoji!r} is not a single glyph — pass one purpose-emoji (e.g. ⚙️)"
    if not spine_slug:
        return "an --emoji needs a --spine slug to brand (the emoji belongs to a spine, not a lone card)"
    clash = _spine_brand_conflict(tasks, emoji, spine_slug)
    if clash:
        taken = ", ".join(f"{e} {s}" for e, s in sorted(_taken_emoji(tasks).items())) or "—"
        return (f"emoji {emoji} is already the brand for spine {clash!r} on this project — "
                f"pick another. Taken: {taken}")
    return ""


def _leading_emoji(title: str) -> str:
    """The leading emoji glyph of a title (incl. a VS16/ZWJ sequence), or "" if the title doesn't
    start with one. Used by `spine backfill` to adopt hand-typed title glyphs. Emoji ranges mirror
    `_disp_width`'s (same stdlib approach, no wcwidth dependency)."""
    import unicodedata
    s = title.lstrip()
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        is_emoji = (o in (0x200D, 0xFE0E, 0xFE0F)
                    or o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF
                    or unicodedata.category(ch) == "So")
        if is_emoji:
            out.append(ch)
        elif out:
            break              # first non-emoji after the glyph run ends it
        else:
            return ""          # title doesn't begin with an emoji
    return "".join(out).strip()


def cmd_spine(args):
    """Spine branding (CELE-t380): list / set / backfill the per-project purpose-emoji that brands a
    spine (the group of cards minted from one plan). The agent proposes the emoji; uniqueness across
    distinct spine slugs is enforced here."""
    ctx = require_context(args)
    tasks = _load_tasks(ctx)
    sub = getattr(args, "spine_cmd", None) or "ls"

    if sub == "ls":
        rows = _spines_summary(tasks)
        if getattr(args, "json", False):
            print(json.dumps({"spines": rows}, indent=2))
            return
        if not rows:
            print("No branded spines yet. Brand one:  celeborn spine set <slug> --emoji <glyph>")
            return
        print(f"Spines ({len(rows)}):")
        for r in rows:
            c = r["counts"]
            print(f"  {r['emoji'] or '·'}  {r['slug']:<22} {r['total']:>2} cards  "
                  f"({c['todo']} todo · {c['doing']} doing · {c['done']} done)")
        return

    if sub == "set":
        slug = (getattr(args, "slug", "") or "").strip()
        emoji = _norm_emoji(getattr(args, "emoji", "") or "")
        if not slug or not emoji:
            die("usage: celeborn spine set <slug> --emoji <glyph>")
        if not any((t.get("spine") or "").strip() == slug for t in tasks):
            die(f"no cards in spine {slug!r} yet — mint one first: "
                f"celeborn tasks add \"...\" --spine {slug} --emoji {emoji}")
        err = _brand_error(tasks, slug, emoji)
        if err:
            die(err)
        n = _apply_spine_brand(tasks, slug, emoji)
        _save_tasks(ctx, tasks)
        print(f"Branded spine {slug!r} {emoji} — {n} card(s) restamped")
        return

    if sub == "backfill":
        # Adopt existing hand-emoji spines: read the leading title glyph into the `emoji` field. Slugs
        # aren't invented — print suggested `tasks edit --spine` lines so a human/agent formalizes them.
        dry = getattr(args, "dry_run", False)
        found = [(t, g) for t in tasks
                 if not t.get("emoji") and (g := _leading_emoji(t["title"]))]
        if not found:
            print("No unbranded cards carry a leading emoji — nothing to backfill.")
            return
        for t, glyph in found:
            t["emoji"] = glyph
            # Migrate: the emoji field is now the brand's source of truth, so drop the leading glyph
            # from the title — otherwise every surface that renders emoji + title would double it.
            head = t["title"].lstrip()
            t["title"] = head[len(glyph):].lstrip()
            t["updated"] = now_iso()
        if not dry:
            _save_tasks(ctx, tasks)
        print(f"Backfilled emoji on {len(found)} card(s) from their title glyph"
              + (" (dry run — not saved)" if dry else "") + ":")
        for t, glyph in found:
            print(f"  {glyph}  [{_display_tid(ctx, t['id'])}] {_strip_protocol(t['title'])[:60]}")
        print("\nFormalize spine slugs so uniqueness is enforced, e.g.:")
        for glyph in dict.fromkeys(g for _, g in found):
            print(f"  celeborn tasks edit <id> --spine <slug> --emoji {glyph}")
        return

    die(f"unknown spine action {sub!r} — use: ls | set | backfill")


def cmd_tasks(args):
    ctx = require_context(args)
    tasks = _load_tasks(ctx)
    action = getattr(args, "task_cmd", None) or "list"
    # Accept project-qualified ids (SLUG-tN, slug/tN) anywhere a single card id is taken.
    if getattr(args, "id", None):
        args.id = _resolve_task_arg(ctx, args.id)

    if action == "add":
        tid = _next_task_id(tasks)
        stamp = now_iso()
        t = {
            "id": tid,
            "title": args.title.strip(),
            "state": args.state,
            "owner": (args.owner or "").strip(),
            "tags": _csv(args.tags),
            "blocked_by": _csv(args.blocked_by),
            "phase": (args.phase or "").strip(),
            # Spine branding (CELE-t380): mint the card into a spine slug with its purpose-emoji. The
            # emoji is validated for per-project uniqueness just below (before the card is appended).
            "spine": (getattr(args, "spine", "") or "").strip(),
            "emoji": _norm_emoji(getattr(args, "emoji", "") or ""),
            # Stop condition (CELE-t81): use the supplied --stop, else auto-fill the generic default so
            # no card is ever stop-less. The agent protocol nudges the owner to replace the default.
            "stop": (getattr(args, "stop", "") or "").strip() or DEFAULT_STOP,
            "autonomy": _validate_autonomy(getattr(args, "autonomy", "")),
            "progress": _clamp_pct(getattr(args, "progress", 0) or 0),
            "jira": "",
            "github": "",
            "created": stamp,
            "updated": stamp,
            "subtasks": [],
            "notes": (args.note or "").strip(),
        }
        # CELE-t380: reject a colliding brand before the card lands. Inherit the spine's existing
        # emoji when the slug is already branded and the agent didn't repeat it (keeps invariant A).
        if t["spine"] and not t["emoji"]:
            t["emoji"] = _emoji_for_slug(tasks, t["spine"])
        brand_err = _brand_error(tasks, t["spine"], t["emoji"])
        if brand_err:
            die(brand_err)
        tasks.append(t)
        tasks = _bring_to_state_front(tasks, tid)  # newest card lands on top of its column
        _save_tasks(ctx, tasks, autopush_ids=[tid])
        print(f"Added [{_display_tid(ctx, tid)}] {t['title']}  ({t['state']})")
        if getattr(args, "claim", False):
            by = _claim_identity(args)
            _claim_preflight(ctx, tasks, by, [tid], force=getattr(args, "force", False))
            t["owner"] = by
            if t["state"] == "todo":
                t["state"] = "doing"
            if t["state"] == "doing":
                _progress_stamp_claim(ctx, t)  # CELE-t161: engine floor 5 the instant a card goes doing
            t["updated"] = now_iso()
            tasks = _bring_to_state_front(tasks, tid)
            _save_tasks(ctx, tasks, autopush_ids=[tid])
            # Write the session→card link too (CELE-t194) so an add-and-claim from a Bash call gets a
            # context-token chip like a pasted claim — the fix isn't complete if only `claim` links.
            _record_agent_session(ctx, _resolve_session(args), by, [tid])
            print(f"Claimed [{_display_tid(ctx, tid)}] {t['title']} → {by or 'unassigned'}")
        return

    if action == "move":
        t = _find_task(tasks, args.id)
        if not t:
            die(f"no task with id {args.id!r}")
        prev = t["state"]
        if args.state == "done" and prev == "doing":
            _require_crest_for_done(ctx, t)   # CELE-t176: must be crested to 99 before leaving DOING
        t["state"] = args.state
        t["updated"] = now_iso()
        if args.state == "done" and prev != "done":
            tasks = _bring_to_state_front(tasks, t["id"])   # newest-done lands on top of the column
        _save_tasks(ctx, tasks, autopush_ids=[t["id"]])
        print(f"[{_display_tid(ctx, t['id'])}] {t['title']} → {args.state}")
        return

    if action == "reorder":
        t = _find_task(tasks, args.id)
        if not t:
            die(f"no task with id {args.id!r}")
        tasks = _reorder_task(tasks, args.id, args.dir)
        _save_tasks(ctx, tasks)
        print(f"[{_display_tid(ctx, t['id'])}] {t['title']} → {args.dir} (within {t['state']})")
        return

    if action == "edit":
        t = _find_task(tasks, args.id)
        if not t:
            die(f"no task with id {args.id!r}")
        prev_state = t["state"]
        if args.title is not None:
            t["title"] = args.title.strip()
        if args.state is not None:
            t["state"] = args.state
        if args.owner is not None:
            t["owner"] = args.owner.strip()
        if args.tags is not None:
            t["tags"] = _csv(args.tags)
        if args.blocked_by is not None:
            t["blocked_by"] = _csv(args.blocked_by)
        if args.phase is not None:
            t["phase"] = args.phase.strip()
        if getattr(args, "spine", None) is not None:
            t["spine"] = args.spine.strip()
        if getattr(args, "emoji", None) is not None:
            t["emoji"] = _norm_emoji(args.emoji)
        # CELE-t380: whenever this edit sets a spine or emoji, re-validate the resulting brand. Check
        # against the OTHER cards so a card can keep/adopt its own spine's emoji without self-clashing.
        if getattr(args, "spine", None) is not None or getattr(args, "emoji", None) is not None:
            others = [o for o in tasks if o["id"] != t["id"]]
            if t.get("spine") and not t.get("emoji"):
                t["emoji"] = _emoji_for_slug(others, t["spine"])  # inherit the spine's existing brand
            brand_err = _brand_error(others, t.get("spine", ""), t.get("emoji", ""))
            if brand_err:
                die(brand_err)
        if getattr(args, "stop", None) is not None:
            t["stop"] = args.stop.strip()
        if getattr(args, "autonomy", None) is not None:
            t["autonomy"] = _validate_autonomy(args.autonomy)
        if getattr(args, "progress", None) is not None:
            t["progress"] = _clamp_pct(args.progress)
        if args.note is not None:
            t["notes"] = args.note.strip()
        # CELE-t176: a DOING card edited to `done` must be crested. Honor any --progress set in the
        # SAME call (applied just above) before gating — so `edit --progress 99 --state done` works.
        if t["state"] == "done" and prev_state == "doing":
            _require_crest_for_done(ctx, t)
        t["updated"] = now_iso()
        if t["state"] == "done" and prev_state != "done":
            tasks = _bring_to_state_front(tasks, t["id"])   # newest-done lands on top of the column
        _save_tasks(ctx, tasks, autopush_ids=[t["id"]])
        print(f"Updated [{_display_tid(ctx, t['id'])}] {t['title']}")
        return

    if action in ("subtasks", "check", "uncheck"):
        t = _find_task(tasks, args.id)
        if not t:
            die(f"no task with id {args.id!r}")
        t.setdefault("subtasks", [])
        disp = _display_tid(ctx, t["id"])

        if action in ("check", "uncheck"):
            n = getattr(args, "n", 0)
            if not t["subtasks"]:
                die(f"[{disp}] has no subtasks — add some with `celeborn tasks subtasks {t['id']} add \"...\"`")
            if n < 1 or n > len(t["subtasks"]):
                die(f"subtask {n} out of range (1..{len(t['subtasks'])})")
            t["subtasks"][n - 1]["done"] = (action == "check")
        else:  # subtasks add | set | rm | list
            sub_cmd = getattr(args, "subtask_cmd", None) or "list"
            if sub_cmd == "add":
                spec = " ".join(args.text)
                item = _parse_subtask_spec(spec)
                if getattr(args, "weight", None):
                    item["weight"] = max(1, int(args.weight))
                t["subtasks"].append(item)
            elif sub_cmd == "set":
                t["subtasks"] = [_parse_subtask_spec(s) for s in args.items if s.strip()]
            elif sub_cmd == "rm":
                n = args.n
                if n < 1 or n > len(t["subtasks"]):
                    die(f"subtask {n} out of range (1..{len(t['subtasks'])})")
                t["subtasks"].pop(n - 1)
            # list falls through to the print below

        _recompute_progress(t)
        # CELE-t161: for an ENGINE-TRACKED doing card the engine owns the bar — its floor encodes the
        # milestone band and is monotonic, so re-assert it here (it never lowers, and preserves a higher
        # manual value). Gated to cards already tracked (claimed through the engine) so a plain
        # `add --state doing` card keeps the pure-ratio CELE-t106 behavior, uncheck included.
        if t.get("state") == "doing" and t["id"] in (_load_progress(ctx).get("cards") or {}):
            try:
                _progress_engine_tick(ctx, t)
            except Exception:  # noqa: BLE001 — progress is best-effort; never break a check
                pass
        if action != "subtasks" or getattr(args, "subtask_cmd", None):
            t["updated"] = now_iso()
            _save_tasks(ctx, tasks, autopush_ids=[t["id"]])
        # render the checklist
        subs = t["subtasks"]
        if not subs:
            print(f"[{disp}] no subtasks yet. Add: `celeborn tasks subtasks {t['id']} add \"<text>\" [--weight N]`")
            return
        done_w = sum(max(1, int(s.get("weight", 1))) for s in subs if s.get("done"))
        tot_w = sum(max(1, int(s.get("weight", 1))) for s in subs)
        print(f"[{disp}] {t['title']}  —  {t['progress']}%  ({done_w}/{tot_w} weighted)")
        for i, s in enumerate(subs, 1):
            box = "✓" if s.get("done") else "○"
            w = f"  ×{int(s.get('weight', 1))}" if int(s.get("weight", 1)) != 1 else ""
            print(f"  {i:>2}. {box} {s.get('text', '')}{w}")
        return

    if action == "rm":
        t = _find_task(tasks, args.id)
        if not t:
            die(f"no task with id {args.id!r}")
        removed = t["id"]
        tasks = [x for x in tasks if x["id"] != removed]
        # autopush the removed id: the live push drops the now-gone card from the hosted board too.
        _save_tasks(ctx, tasks, autopush_ids=[removed])
        print(f"Removed [{_display_tid(ctx, removed)}] {t['title']}")
        return

    if action == "archive":
        cfg = load_config(ctx)
        keep = args.keep if args.keep is not None else cfg["done_keep_cards"]
        done_n = len(_done_tasks_ordered(tasks))
        tasks, archived = _archive_overflow_done(ctx, tasks, {**cfg, "done_keep_cards": keep})
        if not archived:
            print(f"Done column has {done_n} card(s) (keep {keep}); nothing to archive.")
            return
        _tasks_path(ctx).write_text(_render_tasks(tasks))
        _write_tasks_json(ctx, tasks)
        print(f"Archived {len(archived)} done card(s) → {DONE_ARCHIVE_FILE}; kept {keep} on board.")
        print("Re-run `celeborn index` to refresh search.")
        return

    if action == "show":
        t = _find_task(tasks, args.id)
        if not t:
            die(f"no task with id {args.id!r}")
        print(f"[{_display_tid(ctx, t['id'])}] {t['title']}")
        print(f"  state:      {t['state']}")
        print(f"  owner:      {t['owner'] or '—'}")
        print(f"  tags:       {', '.join(t['tags']) or '—'}")
        print(f"  blocked-by: {', '.join(t['blocked_by']) or '—'}")
        print(f"  phase:      {t['phase'] or '—'}")
        if t.get("spine") or t.get("emoji"):   # CELE-t380: spine brand, shown only when set
            brand = f"{t.get('emoji') or '—'}  {t.get('spine') or '—'}".strip()
            print(f"  spine:      {brand}")
        print(f"  stop:       {t.get('stop') or '—'}")
        print(f"  autonomy:   {', '.join(t.get('autonomy') or []) or '— (ungroomed: most-restrictive under a promptless harness)'}")
        print(f"  jira:       {t.get('jira') or '—'}")
        print(f"  created:    {t['created'] or '—'}")
        print(f"  updated:    {t['updated'] or '—'}")
        if t["notes"]:
            print("\n" + t["notes"])
        print("\n" + _agent_card_protocol(t["id"]))
        return

    if action == "next":
        # Deterministic NEXT-UP / ready-set emitter (CELE-t219). The PM invokes this and echoes the
        # result verbatim — it must never enumerate or filter the raw board itself. stdout carries
        # data only (id + title); anomalies go to stderr so a verbatim echo stays clean.
        cfg = load_config(ctx)
        ready, unknown = _ready_set(
            tasks, _archived_done_ids(ctx),
            tags=_csv(getattr(args, "tag", "")),
            phase=(getattr(args, "phase", "") or "").strip())
        for tid, missing in unknown.items():
            # Explicit stderr (not warn(), which prints to stdout): the PM echoes stdout verbatim,
            # so anomaly flags must ride the other stream to keep the data channel clean.
            print(f"  ! [{_display_tid(ctx, tid, cfg=cfg)}] blocker(s) {', '.join(missing)} exist "
                  f"nowhere (board or {DONE_ARCHIVE_FILE}) — treated as done; verify before "
                  f"dispatching.", file=sys.stderr)
        if getattr(args, "json", False):
            print(json.dumps({
                "next": _ready_card_doc(ctx, ready[0], cfg) if ready else None,
                "ready": [_ready_card_doc(ctx, t, cfg) for t in ready],
            }, indent=2))
            return
        if getattr(args, "all", False):
            if not ready:
                print("READY: none")
                return
            print(f"READY ({len(ready)}):")
            for t in ready:
                print(f"  [{_display_tid(ctx, t['id'], cfg=cfg)}] {_strip_protocol(t['title'])}")
            return
        if not ready:
            print("NEXT-UP: none — no todo card has all blockers done")
            return
        t = ready[0]
        print(f"NEXT-UP: [{_display_tid(ctx, t['id'], cfg=cfg)}] {_strip_protocol(t['title'])}")
        return

    if action == "json":
        _write_tasks_json(ctx, tasks)
        if getattr(args, "out", None):
            Path(args.out).write_text(json.dumps(_tasks_doc(ctx, tasks), indent=2) + "\n")
            print(f"Wrote {_tasks_json_path(ctx)} and {args.out}")
        else:
            print(json.dumps(_tasks_doc(ctx, tasks), indent=2))
        return

    # default: list (the text board). Always refresh the derived JSON so the viewer is current.
    _write_tasks_json(ctx, tasks)
    if getattr(args, "json", False):
        print(json.dumps(_tasks_doc(ctx, tasks), indent=2))
        return
    if not tasks:
        print('No tasks yet. Add one:  celeborn tasks add "your first task"')
        return
    cfg = load_config(ctx)
    # Live context join for the DOING column (CELE-t206, closes t163): every in-flight card carries
    # its working session's tokens + /clear-nudge band + session id + coder model. Joined off the same
    # `_active_agents` feed the hosted band pill reads, so terminal and viewer never disagree. Skipped
    # entirely when nothing is DOING (keeps the idle-board render a pure tasks.md read).
    has_doing = any(t["state"] == "doing" for t in tasks)
    tokens_by_task, session_by_task, pressure_by_task = (
        _doing_context_join(ctx) if has_doing else ({}, {}, {}))
    reg = (_load_agents(ctx).get("agents") or {}) if has_doing else {}
    for s in TASK_STATES:
        col = [t for t in tasks if t["state"] == s]
        print(f"\n{TASK_STATE_LABELS[s]} ({len(col)})")
        for t in col:
            owner = f"  @{t['owner']}" if t["owner"] else ""
            blocked = f"  ⛔ {', '.join(t['blocked_by'])}" if t["blocked_by"] else ""
            brand = f"{t['emoji']} " if t.get("emoji") else ""  # CELE-t380: lead with the spine brand
            ann = ""
            if t["state"] == "doing":
                own = (t.get("owner") or "").strip()
                model = (reg.get(own) or {}).get("model") or ""
                ann = _doing_card_annotation(
                    tokens_by_task.get(t["id"]), session_by_task.get(t["id"], ""), model, own,
                    pressure_by_task.get(t["id"], "none"))
            print(f"  [{_display_tid(ctx, t['id'], cfg=cfg)}] {brand}{t['title']}{owner}{ann}{blocked}")


def _ambient_session_id() -> str:
    """The current session id, as the harness injects it into EVERY tool subprocess
    (`CLAUDE_CODE_SESSION_ID`). This is the linchpin of CELE-t194: it lets an agent-initiated
    `celeborn claim` / `tasks add --claim` run from a Bash tool call be session-owned WITHOUT the
    agent remembering to pass `--session` — the hook's stdin session id never reaches a Bash
    subprocess, but this env var does. It's per-window (each session's shell inherits its own id),
    so it's multi-agent-safe where a repo-wide cursor (`last_session_id`) would misattribute.
    `CELEBORN_SESSION_ID` is the harness-neutral alias (Claude's var wins when both are set):
    OpenCode's native celeborn_* tools set it on their CLI subprocesses (P6, CELE-t143) so a
    tool-call `tasks add --claim` links session→card even where no Claude env exists. Empty
    outside any harness (a plain terminal) — there a manual `--by` still attributes."""
    import os
    return (os.environ.get("CLAUDE_CODE_SESSION_ID")
            or os.environ.get("CELEBORN_SESSION_ID") or "").strip()


def _resolve_session(args) -> str:
    """The session id owning this command: explicit `--session` (the hook passes it) → the ambient
    `CLAUDE_CODE_SESSION_ID` the harness sets in every Bash tool call. Empty only outside a Claude
    window. Used for BOTH owner attribution (`_claim_identity`) and the session→card link
    (`_record_agent_session`) so an agent-typed claim tracks context exactly like a pasted one."""
    return (getattr(args, "session", None) or "").strip() or _ambient_session_id()


def _claim_identity(args) -> str:
    """Who owns the card. The SESSION is authoritative: whenever a real session id is resolvable —
    the `--session` the hook passes, OR the `CLAUDE_CODE_SESSION_ID` the harness injects into every
    Bash tool call — the card is owned by that session's short-id (6-char head), and the agent
    CANNOT rename it. The session IS the agent's name (CELE-t131); the code grabs it, not the model
    (CELE-t194 — this is what kills the recurring `@claude` / `@unknown` whimsy at the source). `--by`
    / `$CELEBORN_AGENT` attribute ONLY a session-less manual CLI run (a human at a plain terminal).

    Short head, not the full UUID, so it reads as a clean handle and the board never treats it as a
    raw session id. Mirrors _outbox_identity. Guard (CELE-t172): a model-shaped handle is never an
    owner — declare your model with `celeborn identify --model "…"`, not by stuffing it into --by."""
    explicit = (getattr(args, "by", None) or "").strip()
    sess = _resolve_session(args)
    import os
    env = (os.environ.get("CELEBORN_AGENT") or "").strip()
    if sess:
        short = sess[:6]
        # A live session owns the card, full stop. Surface (but ignore) any superseded --by so the
        # agent learns the code names the card, not it — with an extra nudge when the --by was a model
        # name (record that with `celeborn identify`, don't stuff it into the owner).
        if explicit and explicit != short:
            tail = (f" Record your model with `celeborn identify --model \"{explicit}\"`."
                    if _looks_like_model_handle(explicit) or explicit.lower() in _GENERIC_MODEL_FAMILIES
                    else "")
            warn(f"--by {explicit!r} is ignored — this card is owned by its session ({short}), not a "
                 f"name the agent chooses (CELE-t194).{tail}")
        return short
    handle = explicit or env
    if handle and _looks_like_model_handle(handle):
        warn(f"'{handle}' looks like a model, not an identity — a card should be owned by its "
             f"session or a handle, not a model. Record your model with `celeborn identify`.")
    return handle


def cmd_identify(args):
    """`celeborn identify --family <F> --model <M>` — declare who you are ONCE per session so every
    later touch/claim/ship shows your family + specific model (no per-command flags needed). Stored
    in the local `.context/.agents.json` registry, keyed by your handle (--as / $CELEBORN_AGENT).
    `--show` prints the known agents and exits."""
    ctx = require_context(args)
    if getattr(args, "show", False):
        agents = (_load_agents(ctx).get("agents") or {})
        if getattr(args, "json", False):
            print(json.dumps({"agents": agents}, indent=2))
            return
        if not agents:
            print("(no agents identified yet — run `celeborn identify --family … --model …`)")
            return
        for handle, e in sorted(agents.items()):
            label = _agent_label(e.get("family", ""), e.get("model", "")) or "(unknown model)"
            print(f"@{handle} — {label}")
        return
    handle = (getattr(args, "as_", None) or _claim_identity(args) or "").strip()
    if not handle:
        die("who are you? pass --as <handle> or set $CELEBORN_AGENT first.")
    family = (getattr(args, "family", None) or "").strip()
    model = (getattr(args, "model", None) or "").strip()
    if not family and not model:
        die("nothing to record — pass --family <Claude|Grok|GPT…> and/or --model \"<e.g. Opus 4.8>\".")
    entry = _register_agent(ctx, handle, family, model)
    label = _agent_label(entry.get("family", ""), entry.get("model", "")) or "(unknown model)"
    ok(f"identified @{handle} as {label}")


def cmd_claim(args):
    """`celeborn claim t13 [t14 …] [--by <agent>]` — claim-on-receipt. The act of a model receiving a
    card (its marker pasted into the chat, parsed by the UserPromptSubmit hook) assigns it: owner ←
    claimer, and a TODO card advances to DOING. Last claim wins — if a different owner held it, we
    reassign and say so (the board reflects the new owner; contention is surfaced, not silently lost)."""
    ctx = require_context(args)
    tasks = _load_tasks(ctx)
    by = _agent_identity(args, ctx)["handle"]  # resolves handle + records family/model for the board
    if getattr(args, "ids", None):  # accept project-qualified ids (SLUG-tN, slug/tN)
        args.ids = [_resolve_task_arg(ctx, x) for x in args.ids]
    claim_ids = list(getattr(args, "ids", None) or [])
    _claim_preflight(ctx, tasks, by, claim_ids, force=getattr(args, "force", False))
    results = []
    for tid in (getattr(args, "ids", None) or []):
        t = _find_task(tasks, tid)
        if not t:
            continue
        prev = (t.get("owner") or "").strip()
        was_todo = t["state"] == "todo"
        t["owner"] = by
        if was_todo:
            t["state"] = "doing"
        t["updated"] = now_iso()
        if t["state"] == "doing":
            _progress_stamp_claim(ctx, t)  # CELE-t161: engine floor 5 the instant a card goes doing
        tasks = _bring_to_state_front(tasks, tid)  # claimed card surfaces at top of its column
        who = by or "unassigned"
        disp = _display_tid(ctx, tid)
        if prev and prev != by:
            results.append(f"Reassigned [{disp}] {t['title']}: {prev} → {who} (last claim wins)")
        elif prev != by:
            results.append(f"Claimed [{disp}] {t['title']} → {who}")
        elif was_todo:
            # Already mine, but the claim still advanced it TODO → DOING — a card a PM `dispatch`
            # staged to this session (CELE-t213) lands here at pickup. Without a results line the
            # transition would never save (the guard below) and the board would lie.
            results.append(f"Claimed [{disp}] {t['title']} — staged for {who}, now DOING")
    if results:
        claimed = [tid for tid in (getattr(args, "ids", None) or []) if _find_task(tasks, tid)]
        _save_tasks(ctx, tasks, autopush_ids=claimed)
    # Active-agents bridge (CELE-t131/t194): remember which session owns which card so `celeborn
    # agents` (and the fleet's context-token chip) can attribute that session's live context window to
    # this DOING card. The session is the RESOLVED one — the `--session` the hook passes OR the
    # ambient `CLAUDE_CODE_SESSION_ID` every Bash tool call inherits — so an agent-typed `celeborn
    # claim` tracks context identically to a pasted marker (the fix for cards with no context chip).
    # Runs even on a re-claim of a card you already own (no owner change → no `results` line, but the
    # session is still linked).
    owned_now = [tid for tid in claim_ids if (_find_task(tasks, tid) or {}).get("owner") == by]
    _record_agent_session(ctx, _resolve_session(args), by, owned_now)
    print("\n".join(results))


def cmd_ship(args):
    """`celeborn ship t42` — P0 close-out: release all touches tagged with the task, move it to Done.
    Prevents stale DOING cards after an agent releases files but forgets the kanban move."""
    ctx = require_context(args)
    tid = (getattr(args, "id", None) or "").strip()
    if not tid:
        die("usage: celeborn ship <task-id> [--note <ship note>]")
    tid = _resolve_task_arg(ctx, tid)  # accept project-qualified ids (SLUG-tN, slug/tN)
    tasks = _load_tasks(ctx)
    t = _find_task(tasks, tid)
    if not t:
        die(f"no task with id {tid!r}")
    # CELE-t176: a card leaving DOING for Done must be crested to 99 first. Gate BEFORE any side
    # effect (touch release / note append) so a refused ship leaves the card exactly as it was.
    # Only DOING is gated — a todo/blocked card shipped as triage isn't "in-flight work vanishing".
    if t["state"] == "doing":
        _require_crest_for_done(ctx, t)
    # Spine discipline pre-flight (CELE-t282, design §4): you may not ship card N until the next
    # spine card is startable verbatim by a fresh agent. Audit the post-ship head BEFORE any side
    # effect so a --strict refusal leaves the card, touches and intents exactly as they were. The
    # shipping agent is the one entity with the context loaded to fix the spine cheaply — warn it
    # now (die under --strict), never leave the repair to the no-think PM.
    spine_head, spine_why = _spine_preflight(ctx, tasks, tid)
    if spine_why:
        head_ref = f"[{_display_tid(ctx, spine_head['id'])}] " if spine_head else ""
        for w in spine_why:
            warn(f"spine: {head_ref}{w}")
        if spine_head:
            print(f"  Fix:  celeborn tasks edit {spine_head['id']} --stop \"<clean /clear point>\" "
                  f"--note \"<3-8 line brief>\"   (design: docs/plans/cele-t144-spine-and-stage.md §4)")
        if getattr(args, "strict", False):
            die("ship --strict: the next spine card is not startable verbatim — sharpen it "
                "(or insert the right card at its spine position), then re-ship")
    who = _agent_identity(args, ctx)["handle"]  # resolves handle + records family/model for the board
    _clear_alert(ctx, tid)   # CELE-t195: a shipped card awaits nothing — drop any stale blocked-alert
    released = _release_touches_for_task(ctx, tid)
    # CELE-t303: a shipped card's planned commits are moot — withdraw them from the blackboard so
    # peers stop holding. Best-effort: a fleet-registry hiccup must never block a ship.
    try:
        dropped_intents = _drop_intents(ctx.parent.resolve(), task=tid)
    except Exception:  # noqa: BLE001
        dropped_intents = []
    note = (getattr(args, "note", None) or "").strip()
    if note:
        t["notes"] = f"{t['notes']}\n\n{note}".strip() if t.get("notes") else note
    if who and not (t.get("owner") or "").strip():
        t["owner"] = who
    prev = t["state"]
    t["state"] = "done"
    t["updated"] = now_iso()
    if prev != "done":
        tasks = _bring_to_state_front(tasks, tid)
    _save_tasks(ctx, tasks, autopush_ids=[tid])
    # Verify the write stuck — agents must not assume ship succeeded from exit code alone.
    saved = _find_task(_load_tasks(ctx), tid)
    if not saved or saved["state"] != "done":
        die(f"ship [{_display_tid(ctx, tid)}] failed — board still shows {saved['state'] if saved else 'missing'}; re-run or check for a parallel session overwrite")
    ok(f"Shipped [{_display_tid(ctx, tid)}] {t['title']} → done")
    if released:
        print(f"  released {len(released)} touch(es): {', '.join(released)}")
    elif prev == "doing":
        print("  (no active touches for this card)")
    if dropped_intents:
        print(f"  withdrew {len(dropped_intents)} commit intent(s) from the blackboard")
    # Name the follow-on (§4 ship ritual, clause 4): the ship message carries the new spine head so
    # the hand-off is explicit — "shipped tN → spine head is now tM (READY)".
    if spine_head:
        stamp = "READY" if not spine_why else "⚠ NOT startable: " + "; ".join(spine_why)
        print(f"  spine head is now [{_display_tid(ctx, spine_head['id'])}] "
              f"{_strip_protocol(spine_head['title'])}  ({stamp})")
    elif spine_why:
        print("  spine head: none — every todo card is blocked")
    else:
        print("  spine is empty — no todo cards to hand on")


# --------------------------------------------------------------------------- outbox (Phase 12)
#
# The prompt hand-off queue. The board's "Handoff" button (or `celeborn outbox push`) appends a
# prompt here; the UserPromptSubmit hook `drain`s pending entries each turn and injects them as the
# model's next instruction — the bridge from "I prioritized this card" to "the agent is now working
# on it". Local-only, gitignored, disposable: once drained, an entry moves to `outbox/sent.md` for
# provenance and the pending queue is emptied. No network — same machine, same .context/.
#
# Multi-agent routing (v0, design: references/card-assignment.md): the outbox is ONE FILE PER AGENT
# (`outbox/<agent>.md`), so several agents on one project can drain concurrently without clobbering
# each other (one writer, one reader per file). A card's `owner` is its assignee; pushing addresses
# the hand-off to that owner; an agent drains only its own file, its identity from $CELEBORN_AGENT.
# Unaddressed prompts land in `outbox/_unassigned.md` (today's single-queue behavior, claimable).
#
# PM→coder dispatch (CELE-t213): `celeborn dispatch <tid> --to <session>` is the PM's hand-off verb —
# it stages the card (owner ← the session's 6-char handle, card stays TODO) and queues the brief to
# `outbox/<sid6>.md`. Drain is session-aware (the hook passes the session id), so the coder's next
# turn receives the brief as its work instruction; the marker riding in it triggers claim-on-receipt
# (TODO → DOING + the t203 §1.3 agent_sessions link), with the CELE-t211 gate-time auto-claim as the
# backstop when the coder starts working without reading. The PM sees the board, the outbox, and the
# session registry — never coder transcripts: chain-of-thought stays opaque by design.

OUTBOX_DIR = "outbox"
OUTBOX_SENT_FILE = "sent.md"        # archive, lives inside OUTBOX_DIR
OUTBOX_UNASSIGNED = "_unassigned"   # agent slug for unaddressed prompts (any agent may claim them)


def _outbox_header(agent: str) -> str:
    who = agent or OUTBOX_UNASSIGNED
    return (
        f"# Prompt outbox · {who}\n\n"
        "<!-- Celeborn prompt hand-off queue (Phase 12). ONE FILE PER AGENT — the board's Handoff\n"
        "     button / `celeborn outbox push [--for <agent>]` appends here; that agent's UserPromptSubmit\n"
        "     hook drains it (`outbox drain`, identity from $CELEBORN_AGENT). One writer, one reader →\n"
        "     concurrency-safe across agents. Local-only, gitignored, disposable — drained entries move\n"
        "     to sent.md. One prompt per `##` block. -->\n"
    )


def _agent_slug(name: str) -> str:
    """A filesystem-safe agent id. Empty/blank → the shared unassigned queue."""
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return s or OUTBOX_UNASSIGNED


def _outbox_dir(ctx: Path) -> Path:
    return ctx / OUTBOX_DIR


def _outbox_file(ctx: Path, agent: str) -> Path:
    return _outbox_dir(ctx) / f"{_agent_slug(agent)}.md"


def _outbox_identity(args) -> str:
    """Who am I, for draining: explicit --for wins, else $CELEBORN_AGENT, else unassigned. This is
    how each concurrent agent pulls only the cards addressed to it (design: card-assignment.md)."""
    explicit = getattr(args, "for_", None)
    if explicit:
        return explicit.strip()
    import os
    return (os.environ.get("CELEBORN_AGENT") or "").strip()


CARD_REF_RE = re.compile(r"celeborn:\s*(?:(?P<slug>[\w.-]+)\s*/\s*)?(?P<tid>t\d+)", re.I)


def _card_marker(tid: str, slug: str) -> str:
    """Project-qualified card stamp: ⟨celeborn:slug/tN⟩. Prevents claiming tN in the wrong repo when
    a marker is pasted across projects. Parser still accepts legacy ⟨celeborn:tN⟩ in the same repo."""
    return f"⟨celeborn:{slug}/{tid}⟩"


def _find_card_refs(text: str, *, expected_slug: str | None = None) -> tuple[list[str], list[str]]:
    """Card ids to claim (first-seen order) and rejection lines for cross-project markers.
    Tolerant of stripped brackets; qualified markers must match expected_slug when it is set."""
    seen, out, rejects = set(), [], []
    for m in CARD_REF_RE.finditer(text or ""):
        slug, tid = (m.group("slug") or "").strip(), m.group("tid")
        if slug and expected_slug and slug.lower() != expected_slug.lower():
            rejects.append(
                f"  [{tid}] — marker project {slug!r} ≠ this repo {expected_slug!r} (not claimed)")
            continue
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out, rejects


# A project-qualified card id written in PROSE (the displayed `SLUG-tN` form, e.g. "continue with
# CELE-t131") — distinct from the pasted `celeborn:tN` marker that CARD_REF_RE catches. The literal
# `t` before the digits keeps it from matching Jira-style ids (SCRUM-115). Bare `tN` is deliberately
# NOT matched: a slug-less number in prose is too ambiguous to auto-claim.
PROSE_CARD_REF_RE = re.compile(r"\b(?P<slug>[A-Za-z][\w.]*)-(?P<tid>t\d+)\b", re.I)


def _find_prose_card_refs(text: str, *, expected_slug: str | None, claimable_ids: set[str]) -> list[str]:
    """Card ids (first-seen order) named in PROSE as `SLUG-tN` that belong to THIS board and are still
    open — the free-text counterpart to `_find_card_refs`'s pasted markers (CELE-t131). A human who
    types "continue with CELE-t131" into an unowned session is steering it onto that card; this lets
    the active-agents chip show the right owner+card without a manual `claim`. Mentions of another
    project's card, or of a shipped/unknown card, are silently skipped (no reject noise — unlike a
    pasted marker, a prose mention isn't an explicit claim attempt)."""
    seen, out = set(), []
    for m in PROSE_CARD_REF_RE.finditer(text or ""):
        slug, tid = m.group("slug"), m.group("tid").lower()
        if expected_slug and not _slug_matches(slug, expected_slug):
            continue                              # different project (or not a slug) — not ours
        if tid in seen or tid not in claimable_ids:
            continue                              # already taken this scan, or shipped/unknown card
        seen.add(tid)
        out.append(tid)
    return out


def _session_has_task(ctx: Path, session: str | None) -> bool:
    """Whether this session is already signed in to a still-live card. Gate for the prose sign-in: it
    only fills a VACUUM (a session not yet on a card), so a later casual mention of another card can't
    thrash the board. A shipped/abandoned card no longer counts, so the session can sign in to its next."""
    sid = (session or "").strip()
    if not sid:
        return False
    tid = ((_load_metrics(ctx).get("agent_sessions") or {}).get(sid) or {}).get("task")
    tid = (tid or "").strip()
    if not tid:
        return False
    card = next((t for t in _load_tasks(ctx) if t["id"] == tid), None)
    return card is not None and card.get("state") in ("doing",)


def _task_prompt(t: dict, ctx: Path) -> str:
    """Render a task into the prompt text that gets handed off / copied. Title is the instruction;
    notes ride along as detail. Kept deliberately plain so it reads as a natural user request — then
    the agent protocol block and a trailing card marker so the receiving session can claim it
    (design: card-assignment.md)."""
    body = t["title"].strip()
    if t.get("notes"):
        body += "\n\n" + t["notes"].strip()
    proto = t.get("agent_protocol") or _agent_card_protocol(t["id"])
    slug = project_slug(ctx)
    return body + "\n\n" + proto + "\n\n" + _card_marker(t["id"], slug)


def _outbox_blocks(text: str) -> list[str]:
    """Split outbox markdown into entry blocks (everything under each `## ` heading, heading included)."""
    return [("## " + b).rstrip() for b in re.split(r"(?m)^##[ \t]+", text)[1:]]


def _outbox_body(block: str) -> str:
    """The prompt text of one outbox block — the lines after the `## …` heading."""
    return "\n".join(block.splitlines()[1:]).strip()


def _outbox_queue(ctx: Path, prompt: str, addressee: str, tag: str = "") -> str:
    """Append one prompt block to an addressee's outbox file (creating dir/header as needed) and
    return the addressee slug. The single writer both `outbox push` and `dispatch` go through."""
    slug = _agent_slug(addressee)
    f = _outbox_file(ctx, addressee)
    _outbox_dir(ctx).mkdir(parents=True, exist_ok=True)
    existing = f.read_text() if f.is_file() else _outbox_header(addressee)
    forhint = f" for={slug}" if slug != OUTBOX_UNASSIGNED else ""
    entry = f"\n## queued {now_iso()}{tag}{forhint}\n{prompt}\n"
    f.write_text(existing.rstrip("\n") + "\n" + entry)
    return slug


def cmd_outbox(args):
    ctx = require_context(args)
    action = getattr(args, "outbox_cmd", None) or "list"
    d = _outbox_dir(ctx)

    if action == "push":
        if getattr(args, "task", None):
            t = _find_task(_load_tasks(ctx), args.task)
            if not t:
                die(f"no task with id {args.task!r}")
            prompt, tag = _task_prompt(t, ctx), f" [{_display_tid(ctx, t['id'])}]"
            # Addressing: explicit --for wins, else the card's owner (assigning the card addresses it).
            addressee = (getattr(args, "for_", None) or t.get("owner") or "").strip()
        elif getattr(args, "text", None):
            prompt, tag = args.text.strip(), ""
            addressee = (getattr(args, "for_", None) or "").strip()
        else:
            die("nothing to push — pass --task <id> or --text <prompt>")
        slug = _outbox_queue(ctx, prompt, addressee, tag)
        print(f"Queued prompt to outbox{tag} → {slug if slug != OUTBOX_UNASSIGNED else 'unassigned'}")
        return

    if action == "drain":
        # A drainer has up to TWO queues (CELE-t213): its name identity (--for / $CELEBORN_AGENT /
        # unassigned — today's behavior) AND, when a session id is resolvable (--session from the
        # hook, or the ambient CLAUDE_CODE_SESSION_ID), its session's 6-char handle — the address
        # a PM `dispatch` stages cards to. One writer, one reader per FILE still holds; one reader
        # just owns two files. Collect every block first, archive once, then empty the sources.
        idents, seen = [], set()
        sid = _resolve_session(args)
        for ident in (_outbox_identity(args), sid[:6] if sid else ""):
            slug = _agent_slug(ident)
            if slug not in seen:
                seen.add(slug)
                idents.append(ident)
        drained: list[tuple[Path, str, list[str]]] = []
        for ident in idents:
            f = _outbox_file(ctx, ident)
            blocks = _outbox_blocks(f.read_text()) if f.is_file() else []
            if blocks:
                drained.append((f, ident, blocks))
        if not drained:
            return
        # Archive raw blocks for provenance, then empty each drained pending queue.
        all_blocks = [b for _, _, blocks in drained for b in blocks]
        sent = d / OUTBOX_SENT_FILE
        prior = sent.read_text() if sent.is_file() else "# Prompt outbox — sent\n"
        sent.write_text(prior.rstrip("\n") + "\n\n" + "\n\n".join(all_blocks) + "\n")
        for f, ident, _ in drained:
            f.write_text(_outbox_header(ident))
        prompts = [_outbox_body(b) for b in all_blocks if _outbox_body(b)]
        print("\n\n---\n\n".join(prompts))
        return

    if action == "clear":
        target = getattr(args, "for_", None)
        if target:
            _outbox_file(ctx, target).write_text(_outbox_header(target))
            print(f"Cleared outbox for {_agent_slug(target)}")
            return
        if d.is_dir():
            for f in d.glob("*.md"):
                if f.name != OUTBOX_SENT_FILE:
                    f.unlink()
        print("Cleared the prompt outbox (all agents)")
        return

    # default: list pending entries, grouped by agent
    files = [f for f in sorted(d.glob("*.md")) if f.name != OUTBOX_SENT_FILE] if d.is_dir() else []
    groups, total = [], 0
    for f in files:
        blocks = _outbox_blocks(f.read_text())
        if not blocks:
            continue
        total += len(blocks)
        lines = [f"{f.stem} ({len(blocks)}):"]
        for b in blocks:
            head = b.splitlines()[0][3:].strip()
            first = next((ln for ln in b.splitlines()[1:] if ln.strip()), "")
            lines.append(f"  · {head} — {first[:60]}")
        groups.append("\n".join(lines))
    if total == 0:
        print("Outbox empty — nothing queued.")
        return
    print(f"{total} queued prompt(s):")
    for g in groups:
        print(g)


# A --to value that IS a session id (full UUID or a hex head) rather than a chosen handle. Session
# owners are always the 6-char head (mirrors _claim_identity), so a longer session-shaped string
# collapses to it; a real handle ("scotch-glass") passes through verbatim.
_SESSION_SHAPED_RE = re.compile(r"[0-9a-f][0-9a-f-]{6,}", re.I)


def _dispatch_card(ctx: Path, tasks: list[dict], t: dict, handle: str, *, force: bool = False) -> str:
    """Stage card `t` on coder `handle` and queue its brief — the CELE-t213 hand-off, shared by the
    `dispatch` CLI verb and the t283 PM loop so the two can never drift. Owner ← handle, card STAYS
    todo (DOING is earned at pickup), brief → `outbox/<handle>.md`. Returns the detail lines the
    caller prints. Raises ValueError when the card is not READY and `force` is off — the CLI turns
    that into `die`, the PM loop into a skip."""
    tid = t["id"]
    disp = _display_tid(ctx, tid)
    # Readiness (CELE-t219's predicate): dispatching a card whose blockers are still open hands the
    # coder work it cannot cleanly do. Unknown blockers count done there and get flagged here too.
    ready, unknown = _ready_set(tasks, _archived_done_ids(ctx))
    if tid in unknown:
        warn(f"[{disp}] names blocker(s) that exist nowhere: {', '.join(unknown[tid])} — treated as done")
    if tid not in {r["id"] for r in ready}:
        open_blockers = [b for b in t.get("blocked_by") or []
                         if (_find_task(tasks, b) or {}).get("state") not in (None, "done")]
        msg = f"[{disp}] is not READY — open blocker(s): {', '.join(open_blockers) or '?'}"
        if not force:
            raise ValueError(msg)
        warn(msg + " (--force — proceeding)")
    busy = _doing_for_owner(tasks, handle)
    if busy:
        warn(f"@{handle} already has {len(busy)} DOING card(s) — the brief still queues, but pickup "
             f"claim will wait for the coder to finish (one in-flight card per agent)")
    # An ungroomed card claimed at receipt would go DOING with no autonomy grants — under opencode
    # that denies everything (t212), stranding the coder the PM just dispatched. Default-fill the
    # same working set the t211 auto-provision uses; commit stays opt-in (t203 §3.4), and a groomed
    # card keeps whatever the operator granted.
    granted = ""
    if not t.get("autonomy"):
        t["autonomy"] = _autoprovision_grants()
        granted = f"  autonomy: {','.join(t['autonomy'])} (default-filled; commit stays OFF — t203 §3.4)\n"
    t["owner"] = handle
    t["updated"] = now_iso()
    _save_tasks(ctx, tasks, autopush_ids=[tid])
    slug = _outbox_queue(ctx, _task_prompt(t, ctx), handle, f" [{disp}]")
    return (f"  staged: owner @{handle}, still TODO — DOING is earned at pickup "
            f"(claim-on-receipt, or the t211 gate-time auto-claim)\n{granted}"
            f"  queued: .context/{OUTBOX_DIR}/{slug}.md — drained into that session's next turn")


def cmd_dispatch(args):
    """`celeborn dispatch t42 --to <session-id|handle>` — the PM hand-off (CELE-t213). Stages the
    card on the target coder (owner ← handle; the card STAYS todo — DOING is earned at pickup) and
    queues the card's brief into that coder's outbox. The coder's next turn drains the brief as its
    work instruction, and claim-on-receipt moves the card to DOING + writes the §1.3 agent_sessions
    link; a coder that starts working without reading it is caught by the CELE-t211 gate-time
    auto-claim (owner == sid6) — either path closes the loop. The PM orchestrates over the board,
    the outbox, and the session registry ONLY: coder chain-of-thought is opaque, by design."""
    ctx = require_context(args)
    tid = _resolve_task_arg(ctx, (getattr(args, "id", None) or "").strip())
    tasks = _load_tasks(ctx)
    t = _find_task(tasks, tid)
    if not t:
        die(f"no task with id {tid!r}")
    disp = _display_tid(ctx, tid)
    if t["state"] != "todo":
        die(f"[{disp}] is {t['state']} — only a TODO card can be dispatched"
            + (" (it is someone's in-flight work)" if t["state"] == "doing" else ""))
    target = (getattr(args, "to", None) or t.get("owner") or "").strip()
    if not target:
        die(f"no target — pass --to <session-id|handle> (or stage [{disp}]'s owner first)")
    handle = target[:6].lower() if _SESSION_SHAPED_RE.fullmatch(target) else target
    try:
        detail = _dispatch_card(ctx, tasks, t, handle, force=getattr(args, "force", False))
    except ValueError as e:
        die(f"{e}\n  Pass --force to dispatch anyway (not recommended).")
    ok(f"Dispatched [{disp}] {t['title']} → @{handle}")
    print(detail)


# --------------------------------------------------------------------------- PM loop (CELE-t283)
#
# The Qwen-4b Project Manager's march loop (design: docs/plans/cele-t144-spine-and-stage.md §4+§6):
# stamp READY, dispatch card ① to a free coder slot, raise a ✋ when ① is not startable, restamp
# when a ship moves the head. The PM VERIFIES AND FERRIES, NEVER INVENTS: every decision is a code
# predicate already on the board (`_ready_set` t219 · `_spine_audit` t282 · `_dispatch_card` t213);
# the model's only job is to phrase the board-visible line, and even that reply is validated with a
# code-formatted fallback — the loop runs identically with Ollama down. Everything the PM writes is
# a stamp (the tasks.json spine projection), a dispatch (owner + outbox brief) or a question (a
# `spine`-kind alert) — all board-visible. It reads the board, the outbox and the session registry
# ONLY; coder chain-of-thought stays opaque by design. `celeborn pm` is one pass (cron/hook
# friendly); `--watch` is the foreground loop CELE-t217 will keep always-on.

PM_STATE_NAME = ".pm-state.json"   # last pass's view of the board — how stamp/ship transitions are detected
PM_ALERT_SESSION = "pm"            # marks a spine hand as PM-raised; never collides with a session id
PM_SLOT_TOKENS_MAX = 100_000       # a fuller window is due to /clear — not a slot to hand new work


def _pm_model_line(cfg: dict, facts: dict, fallback: str) -> str:
    """Ask the local PM model to phrase ONE board line. The model FORMATS, never decides — and it
    REPHRASES rather than composes: it gets the code-formatted `fallback` sentence (correct by
    construction) plus the facts, and may only smooth the wording. Free composition from raw facts
    was tried first and a 4b model inverted meanings while keeping the ids ("t318 was restamped
    READY" for a t318 SHIP); rephrasing a correct sentence leaves parroting as the worst case.
    The reply is still validated — one line, sane length, every card id from the facts present
    verbatim — and anything else is discarded for the fallback."""
    import urllib.request
    url = str(cfg.get("pm_ollama_url") or DEFAULTS["pm_ollama_url"]).rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": cfg.get("pm_model") or DEFAULTS["pm_model"],
        "temperature": 0,
        "max_tokens": 120,
        "messages": [
            {"role": "system", "content":
                "You polish kanban-board announcements for a project manager. Rewrite LINE as ONE "
                "terse English sentence (max 25 words). Keep every card id and every stated fact "
                "exactly; you may only smooth the wording. No advice, no additions, no markdown. "
                "If LINE is already clear, return it unchanged."},
            {"role": "user", "content": json.dumps({"LINE": fallback, "facts": facts},
                                                   ensure_ascii=False)},
        ],
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            reply = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001 — model down/misconfigured: the loop marches on fallbacks
        return fallback
    line = next((ln.strip() for ln in str(reply).splitlines() if ln.strip()), "")
    ids = ([facts[k] for k in ("card", "head") if facts.get(k)]
           + list(facts.get("shipped") or []) + list(facts.get("ready") or []))
    if not line or len(line) > 240 or any(i not in line for i in ids):
        return fallback
    return line


def _pm_state_path(ctx: Path) -> Path:
    return ctx / PM_STATE_NAME


def _pm_load_state(ctx: Path) -> dict:
    p = _pm_state_path(ctx)
    try:
        return json.loads(p.read_text()) if p.is_file() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _pm_save_state(ctx: Path, state: dict) -> None:
    import os
    state["at"] = now_iso()
    tmp = _pm_state_path(ctx).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, _pm_state_path(ctx))


# --------------------------------------------------------------------------- PM wake queue (CELE-t216)
#
# The PM marches only when invoked (`celeborn pm`); nothing wakes it on its own yet. t216 wires the
# EVENT SIDE: a git post-commit hook, a github/jira pull that actually moved cards, a human kanban
# action on the board, and a human↔OpenCode turn each enqueue a lightweight "the board may have moved"
# event here. `celeborn pm` drains the queue and reports what woke it; the CELE-t217 daemon will watch
# this file to decide when to take a pass instead of polling blindly. Producers sit in hot paths (a
# hook, a board mutation, a git commit), so every entry point here is best-effort and never raises.

PM_WAKE_NAME = ".pm-wake.json"     # producers append wake events; the PM (and the t217 daemon) drain them
PM_WAKE_MAX = 200                  # cap the backlog so a never-drained queue can't grow without bound


def _pm_wake_path(ctx: Path) -> Path:
    return ctx / PM_WAKE_NAME


def _pm_wake_enqueue(ctx: Path, source: str, detail: str = "") -> bool:
    """Record one PM wake event (CELE-t216). Best-effort — returns False rather than raising, because
    every caller (a git hook, a board mutation, an OpenCode turn, a github/jira pull) must not break
    if a wake can't be written. The backlog is capped at PM_WAKE_MAX most-recent entries."""
    try:
        p = _pm_wake_path(ctx)
        try:
            data = json.loads(p.read_text()) if p.is_file() else {}
        except (json.JSONDecodeError, OSError):
            data = {}
        pending = data.get("pending")
        if not isinstance(pending, list):
            pending = []
        pending.append({"source": (source or "?").strip() or "?",
                        "detail": (detail or "").strip(), "at": now_iso()})
        import os
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"pending": pending[-PM_WAKE_MAX:]}, indent=2) + "\n")
        os.replace(tmp, p)
        return True
    except Exception:  # noqa: BLE001 — a wake is advisory; never break the caller's turn
        return False


def _pm_wake_peek(ctx: Path) -> list[dict]:
    """Pending wake events without clearing them (backs `pm wake --list` and the t217 daemon's poll)."""
    try:
        data = json.loads(_pm_wake_path(ctx).read_text())
        pending = data.get("pending")
        return pending if isinstance(pending, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _pm_wake_drain(ctx: Path) -> list[dict]:
    """Return the pending wake events and clear the queue (stamping when). The PM drains at the top of
    a pass so a march consumes its triggers; the t217 daemon will drain-then-march the same way."""
    pending = _pm_wake_peek(ctx)
    if not pending:
        return []
    try:
        import os
        p = _pm_wake_path(ctx)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"pending": [], "drained_at": now_iso(),
                                   "drained": len(pending)}, indent=2) + "\n")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass
    return pending


def _pm_free_slots(ctx: Path, tasks: list[dict], window_min: float | None = None) -> list[str]:
    """Live coder sessions the PM may dispatch to, emptiest context window first. A slot is a live
    session (the t131 registry) with no in-flight card, no staged todo card, no queued outbox brief
    and room left in its window — a session whose next turn is genuinely free."""
    spoken_for = {(t.get("owner") or "").strip() for t in tasks if t["state"] in ("todo", "doing")}
    slots: list[str] = []
    for r in sorted(_active_agents(ctx, window_min or AGENT_ACTIVE_WINDOW_MIN, False),
                    key=lambda row: row["tokens"]):
        handle = (r.get("agent") or "").strip()
        if not handle or handle in slots or r.get("task_id"):
            continue
        if handle in spoken_for or r["tokens"] > PM_SLOT_TOKENS_MAX:
            continue
        f = _outbox_file(ctx, handle)
        if f.is_file() and _outbox_blocks(f.read_text()):
            continue
        slots.append(handle)
    return slots


def _pm_pass(ctx: Path, *, window_min: float | None = None, slots: list[str] | None = None,
             dry_run: bool = False, fmt=None) -> list[str]:
    """One stamp → hand → dispatch → restamp cycle. Returns the announcement lines — the daemon's
    log; every state change they describe also landed on the board itself. Steady-state waiting
    ("READY, no free slot") is announced once per change, not per pass."""
    cfg = load_config(ctx)
    if fmt is None:
        fmt = lambda facts, fb: _pm_model_line(cfg, facts, fb)  # noqa: E731
    tasks = _load_tasks(ctx)
    alerts = _live_alerts(ctx)
    prev = _pm_load_state(ctx)
    out: list[str] = []
    disp = lambda tid: _display_tid(ctx, tid, cfg=cfg)  # noqa: E731

    # ── stamp: recompute the predicate; heal the projection when the stored stamps drifted (an
    # alert or the done-archive moved without a task save) and announce newly-READY cards.
    spine = _spine_doc(ctx, tasks, alerts=alerts)
    try:
        stored_tasks = json.loads(_tasks_json_path(ctx).read_text()).get("tasks") or []
        stored = {t["id"]: t.get("spine") for t in stored_tasks if t.get("state") == "todo"}
    except (json.JSONDecodeError, OSError):
        stored = None
    if stored != spine:
        if not dry_run:
            _write_tasks_json(ctx, tasks)
        out.append("stamped the spine — board projection refreshed")
    ready_ids = [tid for tid, s in spine.items() if s["ready"]]
    fresh = [tid for tid in ready_ids if tid not in set(prev.get("ready") or [])]
    if fresh:
        # Cap the roll-call: a first pass on a mature board stamps dozens at once (spine order kept).
        names, extra = [disp(tid) for tid in fresh[:8]], max(0, len(fresh) - 8)
        more = f" … +{extra} more" if extra else ""
        out.append(fmt({"event": "stamp", "ready": names, **({"more": extra} if extra else {})},
                       "stamped READY: " + ", ".join(f"[{n}]" for n in names) + more))

    # ── hand: audit the head _ready_set would dispatch (t219 verbatim — never a blocked card).
    # An unstartable ① gets a ✋ the PM cannot fix itself (design §4: it verifies and ferries);
    # a hand that no longer points at the unstartable head is lowered — fixed, shipped or reordered.
    ready, _unknown = _ready_set(tasks, _archived_done_ids(ctx))
    head = ready[0] if ready else None
    audit = _spine_audit(head, alerts=alerts) if head else []
    hand_tid = head["id"] if (head and audit) else None
    for tid, rec in sorted((alerts or {}).items()):
        if (rec or {}).get("kind") == "spine" and tid != hand_tid:
            if not dry_run:
                _clear_alert(ctx, tid)
                _refresh_alerted_card(ctx, tid)
            out.append(f"lowered the hand on [{disp(tid)}] — resolved")
    if hand_tid:
        prev_hand = prev.get("hand") or {}
        raised = (alerts.get(hand_tid) or {}).get("kind") == "spine"
        if not raised or prev_hand.get("tid") != hand_tid or prev_hand.get("why") != audit:
            fb = (f"[{disp(hand_tid)}] is spine head but not startable — {'; '.join(audit)} — "
                  f"whoever shipped the previous card owes the fix")
            msg = fmt({"event": "hand", "card": disp(hand_tid), "why": audit}, fb)
            if not dry_run:
                _set_alert(ctx, hand_tid, "spine", msg, session=PM_ALERT_SESSION)
                _refresh_alerted_card(ctx, hand_tid)
            out.append(f"✋ {msg}" if disp(hand_tid) in msg else f"✋ [{disp(hand_tid)}] {msg}")

    # ── dispatch: a startable ① goes to the emptiest free coder slot via the t213 verb. Once
    # staged (owner set, still todo) the PM waits for pickup — it never double-queues a brief.
    if head and not audit:
        head_disp, staged_owner = disp(head["id"]), (head.get("owner") or "").strip()
        if staged_owner:
            status = f"[{head_disp}] is READY — staged on @{staged_owner}, awaiting pickup"
        else:
            free = list(slots) if slots else _pm_free_slots(ctx, tasks, window_min)
            if not free:
                status = f"[{head_disp}] is READY — no free coder slot"
            elif dry_run:
                status = None
                out.append(f"would dispatch [{head_disp}] → @{free[0]}")
            else:
                status = None
                try:
                    detail = _dispatch_card(ctx, tasks, head, free[0])
                except ValueError as e:  # board changed under us — the next pass re-evaluates
                    out.append(f"dispatch skipped: {e}")
                else:
                    out.append(fmt({"event": "dispatch", "card": head_disp, "to": f"@{free[0]}"},
                                   f"dispatched [{head_disp}] {_strip_protocol(head['title'])} → @{free[0]}"))
                    out.append(detail)
        if status and status != prev.get("status"):
            out.append(status)
    else:
        status = None

    # ── restamp: ships since the last pass hand the spine on — announce the new head with its
    # stamp, the same signal `celeborn ship` prints to the shipping agent (§4 ritual, clause 4).
    done_ids = [t["id"] for t in tasks if t["state"] == "done"]
    shipped = [tid for tid in done_ids if tid not in set(prev.get("done") or [])] if prev else []
    if shipped:
        sh_names = [disp(s) for s in shipped]
        sh = ", ".join(f"[{n}]" for n in sh_names)
        if head is None:
            out.append(f"shipped {sh} → spine has no READY head — every todo card is blocked"
                       if any(t["state"] == "todo" for t in tasks)
                       else f"shipped {sh} → spine is empty — no todo cards to hand on")
        else:
            stamp = "READY" if not audit else "not startable: " + "; ".join(audit)
            out.append(fmt({"event": "restamp", "shipped": sh_names, "head": disp(head["id"]),
                            "stamp": stamp},
                           f"shipped {sh} → spine head is now [{disp(head['id'])}] "
                           f"{_strip_protocol(head['title'])} ({stamp})"))
    if not dry_run:
        _pm_save_state(ctx, {"ready": ready_ids, "done": done_ids,
                             "head": head["id"] if head else None,
                             "hand": {"tid": hand_tid, "why": audit} if hand_tid else None,
                             "status": status})
    return out


def cmd_pm(args):
    """`celeborn pm [--watch]` — the Qwen-4b PM march loop (CELE-t283). One pass stamps READY,
    dispatches the spine head to a free coder slot, raises/lowers the ✋ on an unstartable head and
    restamps after ships; `--watch` keeps it marching in the foreground (CELE-t217 wraps that in
    the always-on daemon). The model only phrases lines — with Ollama down the loop still runs on
    code-formatted text, and `--no-model` skips the call outright."""
    ctx = require_context(args)
    slots = _csv(getattr(args, "slots", None)) or None
    fmt = (lambda facts, fb: fb) if getattr(args, "no_model", False) else None

    def one_pass() -> list[str]:
        woke = [] if getattr(args, "dry_run", False) else _pm_wake_drain(ctx)
        lines = _pm_pass(ctx, window_min=getattr(args, "window_min", None), slots=slots,
                         dry_run=getattr(args, "dry_run", False), fmt=fmt)
        if woke:
            srcs = ", ".join(sorted({e.get("source") or "?" for e in woke}))
            lines = [f"woken by {len(woke)} event(s): {srcs}"] + lines
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        for ln in lines:
            # flush per line: the daemon's stdout is usually a pipe/log file (t217), and a
            # block-buffered watch loop shows nothing until exit — losing the log on a kill.
            print(ln if ln.startswith("  ") else f"{stamp} 🏹 PM · {ln}", flush=True)
        return lines

    if not getattr(args, "watch", False):
        if not one_pass():
            info("spine steady — nothing to do")
        return
    interval = max(5, int(getattr(args, "interval", None) or 15))
    info(f"PM watching every {interval}s (Ctrl-C stops; CELE-t217 owns the always-on daemon)")
    import time
    try:
        while True:
            one_pass()
            time.sleep(interval)
    except KeyboardInterrupt:
        info("PM stopped.")


def cmd_pm_wake(args):
    """`celeborn pm wake --source <s> [--detail <d>]` — enqueue one PM wake event (CELE-t216), or
    `--list` to show the pending queue. The producers (a git post-commit hook, the board's kanban
    mutations, an OpenCode user turn, a github/jira pull delta) call this so the next PM pass knows the
    board may have moved; `celeborn pm` drains the queue and CELE-t217's daemon will watch it."""
    ctx = require_context(args)
    if getattr(args, "list", False):
        pending = _pm_wake_peek(ctx)
        if not pending:
            info("no pending PM wake events")
            return
        print(f"{len(pending)} pending PM wake event(s):")
        for e in pending:
            det = f" — {e['detail']}" if e.get("detail") else ""
            print(f"  · {e.get('source') or '?'}{det}  ({e.get('at', '')})")
        return
    source = (getattr(args, "source", None) or "").strip()
    if not source:
        die("pm wake needs --source <s> (or --list to show the queue)")
    _pm_wake_enqueue(ctx, source, getattr(args, "detail", None) or "")
    ok(f"PM wake enqueued: {source}")


# --------------------------------------------------------------------------- argparse

GETTING_STARTED_EPILOG = """\
Getting started
  celeborn init            THE first-run command — wires Claude Code, scaffolds this
                           project's private memory, signs you in, and opens your board.
                           Run it once per project; re-run any time (it resumes).

  Everything runs locally and offline. Your .context/ (prompts, notes, working memory) is
  ALWAYS private — gitignored, never committed. Carry it across devices with a free account
  (celeborn init --github, or `celeborn login --github` later) — it syncs via your account,
  never git.

  celeborn board           open your kanban board (Celeborn's UI) in the browser
  celeborn scaffold        scaffold .context/ only (secondary — `init` already does this)
  celeborn status          show what an agent loads when it orients

Docs & help: https://celeborncode.ai   ·   `celeborn <command> --help` for any command.
"""

# --------------------------------------------------------------------------- architecture (CELE-t187)

INFRA_LOCAL_NAME = "infra-local.json"
INFRA_SCHEMA = "celeborn-architecture/1"

# Node id → default fields for a vendor detected from repo signals / env-var names. Detection is
# best-effort and non-authoritative: it seeds a starter map the human/agent then edits. We read only
# file existence and env-var NAMES — never any secret value.
_INFRA_ENV_VENDORS = [
    ("ANTHROPIC", {"id": "anthropic", "name": "Anthropic API", "kind": "vendor", "vendor": "Anthropic",
                   "control_surface": "https://console.anthropic.com"}),
    ("OPENAI", {"id": "openai", "name": "OpenAI API", "kind": "vendor", "vendor": "OpenAI",
                "control_surface": "https://platform.openai.com"}),
    ("STRIPE", {"id": "stripe", "name": "Stripe", "kind": "vendor", "vendor": "Stripe",
                "control_surface": "https://dashboard.stripe.com"}),
    ("SUPABASE", {"id": "db", "name": "Database", "kind": "database", "vendor": "Supabase",
                  "control_surface": "https://supabase.com/dashboard"}),
    ("VERCEL", {"id": "web", "name": "Hosted App", "kind": "app", "vendor": "Vercel",
                "control_surface": "https://vercel.com/dashboard"}),
    ("CHATWOOT", {"id": "chatwoot", "name": "Chatwoot", "kind": "vendor", "vendor": "Chatwoot",
                  "control_surface": ""}),
    ("JIRA", {"id": "jira", "name": "Jira", "kind": "vendor", "vendor": "Atlassian",
              "control_surface": "https://admin.atlassian.com"}),
]

_INFRA_NODE_FIELDS = ("id", "name", "kind", "vendor", "role", "endpoint", "ip", "control_surface", "notes")

# Dependency-name → node template (CELE-t201). A NEW dependency in a manifest is the clearest "a piece
# entered the stack" signal, so the auto-trace (and init) map distinctive package tokens to vendor nodes.
# We read dependency NAMES only (never lockfile hashes or any secret). Tokens are chosen to be distinctive
# enough that a substring match over the manifest text won't false-positive on unrelated packages.
_INFRA_DEP_VENDORS = [
    (("@anthropic-ai", "anthropic"), {"id": "anthropic", "name": "Anthropic API", "kind": "vendor",
        "vendor": "Anthropic", "control_surface": "https://console.anthropic.com", "notes": "detected: dependency"}),
    (("openai",), {"id": "openai", "name": "OpenAI API", "kind": "vendor", "vendor": "OpenAI",
        "control_surface": "https://platform.openai.com", "notes": "detected: dependency"}),
    (("openrouter",), {"id": "openrouter", "name": "OpenRouter", "kind": "vendor", "vendor": "OpenRouter",
        "control_surface": "https://openrouter.ai", "notes": "detected: dependency"}),
    (("stripe",), {"id": "stripe", "name": "Stripe", "kind": "vendor", "vendor": "Stripe",
        "control_surface": "https://dashboard.stripe.com", "notes": "detected: dependency"}),
    (("@supabase", "supabase"), {"id": "db", "name": "Database", "kind": "database", "vendor": "Supabase",
        "control_surface": "https://supabase.com/dashboard", "notes": "detected: dependency"}),
    (("@vercel/",), {"id": "web", "name": "Hosted App", "kind": "app", "vendor": "Vercel",
        "control_surface": "https://vercel.com/dashboard", "notes": "detected: dependency"}),
    (("@aws-sdk", "boto3", "aws-sdk"), {"id": "aws", "name": "AWS", "kind": "vendor", "vendor": "AWS",
        "control_surface": "https://console.aws.amazon.com", "notes": "detected: dependency"}),
    (("mongoose", "mongodb"), {"id": "mongo", "name": "MongoDB", "kind": "database", "vendor": "MongoDB",
        "control_surface": "https://cloud.mongodb.com", "notes": "detected: dependency"}),
    (("ioredis", "@upstash/redis"), {"id": "redis", "name": "Redis", "kind": "database", "vendor": "Redis",
        "control_surface": "", "notes": "detected: dependency"}),
    (("twilio",), {"id": "twilio", "name": "Twilio", "kind": "vendor", "vendor": "Twilio",
        "control_surface": "https://console.twilio.com", "notes": "detected: dependency"}),
    (("@sendgrid", "sendgrid"), {"id": "sendgrid", "name": "SendGrid", "kind": "vendor", "vendor": "SendGrid",
        "control_surface": "https://app.sendgrid.com", "notes": "detected: dependency"}),
    (("resend",), {"id": "resend", "name": "Resend", "kind": "vendor", "vendor": "Resend",
        "control_surface": "https://resend.com", "notes": "detected: dependency"}),
]

# Manifest basenames (lowercased) whose EDIT means a dependency may have been added → trace immediately.
# The same set is scanned (root + one dir level) for the dependency tokens above.
_INFRA_MANIFESTS = ("package.json", "requirements.txt", "pyproject.toml", "go.mod", "gemfile",
                    "cargo.toml", "composer.json", "pipfile")


def _infra_path(ctx: Path) -> Path:
    return ctx / INFRA_LOCAL_NAME


def load_infra(ctx: Path) -> dict:
    """Read .context/infra-local.json (gitignored, per-machine). {} when absent/invalid."""
    p = _infra_path(ctx)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        warn(f"{INFRA_LOCAL_NAME} is not valid JSON; treating as empty.")
        return {}


def _full_node(partial: dict) -> dict:
    """Fill a partial node dict out to the full field set (empty strings for missing fields)."""
    return {f: partial.get(f, "") for f in _INFRA_NODE_FIELDS}


def _detect_infra_nodes(root: Path) -> list[dict]:
    """Best-effort starter nodes from repo signals + env-var NAMES (never values). Deduped by id."""
    nodes: dict[str, dict] = {}
    # The local CLI is always a node (the client edge of every flow).
    nodes["cli"] = {"id": "cli", "name": "Celeborn CLI", "kind": "client", "vendor": "local",
                    "role": "developer machine", "endpoint": "localhost", "notes": ""}
    # File signals.
    if any((root / f).exists() for f in ("vercel.json", "vercel.ts")) or \
       any((root / d / "next.config.js").is_file() for d in (".", "web")):
        nodes.setdefault("web", {"id": "web", "name": "Hosted App", "kind": "app", "vendor": "Vercel",
                                 "control_surface": "https://vercel.com/dashboard", "notes": "detected: Vercel/Next"})
    if (root / "supabase").is_dir():
        nodes.setdefault("db", {"id": "db", "name": "Database", "kind": "database", "vendor": "Supabase",
                                "role": "postgres", "control_surface": "https://supabase.com/dashboard",
                                "notes": "detected: supabase/"})
    # Env-var NAME signals (from any .env* at the root — names only, never values).
    env_names: set[str] = set()
    try:
        for envf in root.glob(".env*"):
            if not envf.is_file():
                continue
            for line in envf.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                env_names.add(line.split("=", 1)[0].strip().upper())
    except OSError:
        pass
    for prefix, tmpl in _INFRA_ENV_VENDORS:
        if any(name.startswith(prefix) for name in env_names):
            nodes.setdefault(tmpl["id"], dict(tmpl))
    # Dependency-manifest NAME signals (CELE-t201): scan known manifests at the root and one dir level
    # for distinctive package tokens → vendor nodes. Names only; lockfiles and values are never read.
    manifest_text = ""
    seen_manifest: set[Path] = set()
    for name in _INFRA_MANIFESTS:
        for cand in (root / name, *root.glob(f"*/{name}")):
            if cand in seen_manifest or not cand.is_file():
                continue
            seen_manifest.add(cand)
            try:
                manifest_text += "\n" + cand.read_text(errors="ignore")[:200_000].lower()
            except OSError:
                pass
    if manifest_text:
        for tokens, tmpl in _INFRA_DEP_VENDORS:
            if any(tok in manifest_text for tok in tokens):
                nodes.setdefault(tmpl["id"], dict(tmpl))
    return [_full_node(n) for n in nodes.values()]


# Local install toolchain (CELE-t236) — the second half of the Stack view. The hosted diagram shows the
# HOSTED dependencies (vendors, databases, control surfaces); the `local` block shows what a developer
# must have INSTALLED to work on the repo: runtimes (Python, Node.js…), frameworks (Next.js, Django…),
# and package managers inferred from lockfiles. Detection reads manifest names + version SPECS only —
# never lockfile hashes, env values, or anything secret. It rides the same credential-stripped
# `project_architecture` push and renders below the hosted diagram on the Stack page.

# package.json dependency key → (display name, kind). Exact key match against dependencies/devDependencies.
_LOCAL_JS_DEPS = [
    ("next", "Next.js", "framework"),
    ("react", "React", "framework"),
    ("vue", "Vue", "framework"),
    ("svelte", "Svelte", "framework"),
    ("express", "Express", "framework"),
    ("typescript", "TypeScript", "language"),
    ("tailwindcss", "Tailwind CSS", "framework"),
]

# Python requirement token → (display name, kind). Word-boundary match over requirement lines.
_LOCAL_PY_DEPS = [
    ("fastapi", "FastAPI", "framework"),
    ("django", "Django", "framework"),
    ("flask", "Flask", "framework"),
]

# Lockfile basename → the package manager a developer needs installed to honor it.
_LOCAL_LOCKFILES = [
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "Yarn"),
    ("bun.lockb", "Bun"),
    ("package-lock.json", "npm"),
    ("uv.lock", "uv"),
    ("poetry.lock", "Poetry"),
    ("Pipfile.lock", "Pipenv"),
]

_LOCAL_KIND_RANK = {"runtime": 0, "language": 1, "framework": 2, "tool": 3}


def _dep_ver(spec) -> str:
    """Normalize a manifest version spec for display: strip npm range sigils ('^15.1.0' → '15.1.0',
    '~=3.8' → '3.8') but KEEP honest inequalities ('>=3.11' stays), and take the first clause of a
    comma range. Empty string when there's nothing usable."""
    s = str(spec or "").strip().split(",")[0].strip()
    return re.sub(r"^[\^~=v\s]+", "", s)


def _detect_local_deps(root: Path) -> list[dict]:
    """Best-effort local install toolchain from the repo's manifests (root + one dir level, same reach
    as the vendor detection above). Deduped by name — the first version spec found wins, a later
    version fills a blank. Ordered runtimes → languages → frameworks → tools, stable within a kind."""
    deps: dict[str, dict] = {}

    def add(name: str, kind: str, version: str = "", source: str = "") -> None:
        cur = deps.get(name)
        if cur is None:
            deps[name] = {"name": name, "kind": kind, "version": version, "source": source}
        elif version and not cur["version"]:
            cur["version"] = version
            cur["source"] = source or cur["source"]

    def manifests(name: str) -> list[Path]:
        return [p for p in (root / name, *sorted(root.glob(f"*/{name}"))) if p.is_file()]

    def read(p: Path) -> str:
        try:
            return p.read_text(errors="ignore")[:200_000]
        except OSError:
            return ""

    # Node.js + JS frameworks — JSON-parse each package.json (exact dependency keys, no substrings).
    for p in manifests("package.json"):
        try:
            d = json.loads(read(p))
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        rel = str(p.relative_to(root))
        engines = d.get("engines") if isinstance(d.get("engines"), dict) else {}
        add("Node.js", "runtime", _dep_ver(engines.get("node")), rel)
        packs: dict = {}
        for key in ("dependencies", "devDependencies"):
            if isinstance(d.get(key), dict):
                packs.update(d[key])
        for dep_key, label, kind in _LOCAL_JS_DEPS:
            if dep_key in packs:
                add(label, kind, _dep_ver(packs[dep_key]), rel)
    # Python — any of its manifests marks the runtime; pyproject's requires-python names the version.
    for name in ("pyproject.toml", "requirements.txt", "Pipfile", "setup.py"):
        for p in manifests(name):
            text = read(p)
            ver = ""
            if name == "pyproject.toml":
                m = re.search(r'(?m)^\s*requires-python\s*=\s*["\']([^"\']+)', text)
                ver = (m.group(1).strip() if m else "")
            add("Python", "runtime", ver, str(p.relative_to(root)))
            for token, label, kind in _LOCAL_PY_DEPS:
                if re.search(rf"(?im)^\s*[\"']?{token}\b", text):
                    add(label, kind, "", str(p.relative_to(root)))
    for p in manifests(".python-version"):
        lines = read(p).strip().splitlines()
        add("Python", "runtime", _dep_ver(lines[0]) if lines else "", str(p.relative_to(root)))
    # Other runtimes — presence of the manifest is the signal; version where the manifest states one.
    for p in manifests("go.mod"):
        m = re.search(r"(?m)^go\s+(\S+)", read(p))
        add("Go", "runtime", m.group(1) if m else "", str(p.relative_to(root)))
    for p in manifests("Cargo.toml"):
        m = re.search(r'(?m)^\s*rust-version\s*=\s*["\']([^"\']+)', read(p))
        add("Rust", "runtime", m.group(1) if m else "", str(p.relative_to(root)))
    for p in manifests("Gemfile"):
        add("Ruby", "runtime", "", str(p.relative_to(root)))
    for p in manifests("composer.json"):
        try:
            req = json.loads(read(p)).get("require") or {}
        except (json.JSONDecodeError, AttributeError):
            req = {}
        add("PHP", "runtime", _dep_ver(req.get("php")) if isinstance(req, dict) else "",
            str(p.relative_to(root)))
    for name in ("deno.json", "deno.jsonc"):
        for p in manifests(name):
            add("Deno", "runtime", "", str(p.relative_to(root)))
    # Package managers — a lockfile means the tool is part of the local install.
    for fname, label in _LOCAL_LOCKFILES:
        for p in manifests(fname):
            add(label, "tool", "", str(p.relative_to(root)))
            break
    ordered = sorted(deps.values(), key=lambda d: _LOCAL_KIND_RANK.get(d["kind"], 9))
    return ordered


def _architecture_init(ctx: Path, force: bool = False) -> None:
    p = _infra_path(ctx)
    if p.is_file() and not force:
        die(f"{INFRA_LOCAL_NAME} already exists (use --force to overwrite).")
    nodes = _detect_infra_nodes(ctx.parent)
    # Seed a naive flow from the CLI to the first detected server-side node so `sync` renders something.
    flows: list[dict] = []
    server_ids = [n["id"] for n in nodes if n["id"] != "cli"]
    if server_ids:
        flows.append({"from": "cli", "to": server_ids[0], "label": "sync push", "protocol": "https"})
    doc = {
        "schema": INFRA_SCHEMA,
        "updated": now_iso(),
        "_readme": ("Per-project architecture diagram (CELE-t187). Non-secret topology only. "
                    "`celeborn architecture sync` pushes nodes+flows to your hosted board with the "
                    "`credentials` block STRIPPED. Never put keys/tokens/passwords here — env NAMES only."),
        "nodes": nodes,
        "flows": flows,
        "local": _detect_local_deps(ctx.parent),
        "credentials": {"_note": "NEVER synced — store env-var NAMES only, never values."},
    }
    p.write_text(json.dumps(doc, indent=2) + "\n")
    ok(f"wrote {INFRA_LOCAL_NAME} with {len(nodes)} detected node(s).")
    print("  Edit it to add IPs, endpoints, control-surface URLs, and information flows, then")
    print("  `celeborn architecture sync` to push it to your hosted board (Pro). It's gitignored.")


def _architecture_show(ctx: Path) -> None:
    doc = load_infra(ctx)
    nodes = doc.get("nodes") or []
    flows = doc.get("flows") or []
    if not nodes:
        print(f"No {INFRA_LOCAL_NAME} yet. Run `celeborn architecture init`.")
        return
    print(f"Architecture — {len(nodes)} node(s), {len(flows)} flow(s):")
    for n in nodes:
        head = f"  [{n.get('kind', '?')}] {n.get('name') or n.get('id')}"
        if n.get("vendor"):
            head += f" · {n['vendor']}"
        print(head)
        for label, key in (("endpoint", "endpoint"), ("ip", "ip"), ("control", "control_surface")):
            if n.get(key):
                print(f"      {label}: {n[key]}")
    if flows:
        print("  flows:")
        for f in flows:
            arrow = f"    {f.get('from')} → {f.get('to')}"
            if f.get("label"):
                arrow += f"  ({f['label']})"
            print(arrow)
    local = doc.get("local") or []
    if local:
        print(f"  local toolchain ({len(local)}):")
        for d in local:
            line = f"    [{d.get('kind', '?')}] {d.get('name')}"
            if d.get("version"):
                line += f" {d['version']}"
            if d.get("source"):
                line += f"  · {d['source']}"
            print(line)


# --------------------------------------------------------------------------- auto-architecture-trace (CELE-t201)
#
# The Stack is captured once (`architecture init`) then kept current AUTOMATICALLY: a lightweight "trace"
# re-detects the topology and ADDITIVELY merges any newly-discovered pieces into infra-local.json, then
# remaps the hosted Stack. It runs on a cadence (once every N turns — not every turn; topology changes
# rarely) and immediately when a dependency manifest is edited (the "a piece entered the stack" event).
# Two hard rules keep it safe: (1) it is a NO-OP unless the project already opted in (infra-local.json
# exists) — it never auto-creates a diagram in a random repo; (2) the merge is purely additive — it only
# APPENDS newly-detected nodes, never overwriting or removing anything a human authored.
ARCH_TRACE_STATE_NAME = ".arch-trace.json"     # gitignored per-project trace bookkeeping (turn counter + pending)
ARCH_TRACE_EVERY_TURNS = 3                       # cadence: run a trace once every N user turns


def _arch_trace_state_path(ctx: Path) -> Path:
    return ctx / ARCH_TRACE_STATE_NAME


def _load_arch_trace_state(ctx: Path) -> dict:
    try:
        d = json.loads(_arch_trace_state_path(ctx).read_text())
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_arch_trace_state(ctx: Path, state: dict) -> None:
    try:
        _arch_trace_state_path(ctx).write_text(json.dumps(state) + "\n")
    except OSError:
        pass


def _merge_infra_nodes(doc: dict, detected: list[dict]) -> tuple[dict, list[str]]:
    """Additively merge detected nodes into the doc's node list. A detected node is ADDED only when
    neither its id NOR its (vendor, kind) pair already exists — so hand-authored nodes are never
    duplicated or overwritten. Returns (doc, added_display_names). Pure."""
    existing = list(doc.get("nodes") or [])
    ids = {str(n.get("id")) for n in existing}
    vendor_kinds = {(str(n.get("vendor")).lower(), str(n.get("kind"))) for n in existing if n.get("vendor")}
    added: list[str] = []
    for d in detected:
        nid = str(d.get("id"))
        vk = (str(d.get("vendor")).lower(), str(d.get("kind")))
        if nid in ids or (d.get("vendor") and vk in vendor_kinds):
            continue
        existing.append(_full_node(d))
        ids.add(nid)
        if d.get("vendor"):
            vendor_kinds.add(vk)
        added.append(d.get("name") or nid)
    doc["nodes"] = existing
    return doc, added


def _architecture_trace(ctx: Path, *, reason: str, allow_push: bool = True) -> list[str]:
    """Re-detect the topology, additively merge new pieces into infra-local.json, and refresh the
    machine-detected `local` toolchain block; on any change, remap the hosted Stack (detached
    best-effort push). NO-OP unless infra-local.json already exists (opt-in). Returns the display names
    of any NODES added this trace ([] when nothing changed — a silent toolchain refresh still pushes
    but never makes noise). Never raises."""
    try:
        if not _infra_path(ctx).is_file():
            return []                                    # not opted in — the trace stays silent
        doc = load_infra(ctx)
        if not doc:
            return []
        detected = _detect_infra_nodes(ctx.parent)
        doc, added = _merge_infra_nodes(doc, detected)
        # The local toolchain (CELE-t236) is machine-detected, never hand-authored, so unlike the
        # additive node merge it is simply REFRESHED — versions drift and pieces leave.
        local = _detect_local_deps(ctx.parent)
        local_changed = local != (doc.get("local") or [])
        if not added and not local_changed:
            return []
        doc["local"] = local
        doc["updated"] = now_iso()
        _infra_path(ctx).write_text(json.dumps(doc, indent=2) + "\n")
        if allow_push:
            try:
                __import__("celeborn_sync").schedule_architecture_push(ctx)
            except Exception:
                pass                                     # remap is best-effort; local capture already landed
        return added
    except Exception:
        return []


def _arch_trace_note(added: list[str], reason: str) -> str:
    """The SURFACE-THIS line for a trace that added pieces. Empty when nothing changed."""
    if not added:
        return ""
    what = ", ".join(added[:6]) + ("…" if len(added) > 6 else "")
    return (f"🏹 Celeborn —> architecture trace ({reason}): added {len(added)} new node(s) to the stack "
            f"— {what}. Remapped your hosted Stack.")


def _maybe_arch_trace_on_edit(ctx: Path, rel_path: str) -> str:
    """PostToolUse hook (CELE-t201): if the edited file is a dependency manifest, trace NOW (bypassing
    the cadence throttle) and reset the turn counter. Stashes the surface note so the next
    user-prompt-submit relays it reliably (PostToolUse output is model-only). Returns a note or ""."""
    if not _infra_path(ctx).is_file():
        return ""                                        # opt-in only — no footprint until `architecture init`
    base = Path(rel_path).name.lower()
    if base not in _INFRA_MANIFESTS:
        return ""
    added = _architecture_trace(ctx, reason=f"{base} edited")
    state = _load_arch_trace_state(ctx)
    state["turns_since_trace"] = 0                        # the manifest trace resets the cadence clock
    note = _arch_trace_note(added, f"{base} edited")
    if note:
        pending = state.get("pending") or []
        pending.append(note)
        state["pending"] = pending
    _save_arch_trace_state(ctx, state)
    return note


def _maybe_arch_trace_on_turn(ctx: Path) -> str:
    """UserPromptSubmit hook (CELE-t201): tick the turn counter; every ARCH_TRACE_EVERY_TURNS run a
    cadence trace. Also drain any pending note a manifest-edit trace stashed. Returns a SURFACE-THIS
    block (possibly several lines) or ""."""
    if not _infra_path(ctx).is_file():
        return ""                                        # opt-in only — no footprint until `architecture init`
    state = _load_arch_trace_state(ctx)
    notes: list[str] = list(state.get("pending") or [])
    state["pending"] = []
    n = int(state.get("turns_since_trace") or 0) + 1
    if n >= ARCH_TRACE_EVERY_TURNS:
        state["turns_since_trace"] = 0
        added = _architecture_trace(ctx, reason="cadence")
        note = _arch_trace_note(added, "every 3 turns")
        if note:
            notes.append(note)
    else:
        state["turns_since_trace"] = n
    _save_arch_trace_state(ctx, state)
    return "\n".join(notes)


def cmd_architecture(args):
    """`celeborn architecture [init|show|trace]` — capture non-secret infrastructure topology locally.
    `sync` is handled in celeborn_sync (needs the network); init/show/trace are pure-local and stay here."""
    ctx = require_context(args)
    sub = getattr(args, "arch_cmd", None)
    if sub == "init":
        _architecture_init(ctx, force=getattr(args, "force", False))
    elif sub == "trace":
        if not _infra_path(ctx).is_file():
            die(f"No {INFRA_LOCAL_NAME} yet. Run `celeborn architecture init` first.")
        before = load_infra(ctx).get("updated")
        added = _architecture_trace(ctx, reason="manual")
        if added:
            ok(f"trace added {len(added)} node(s): {', '.join(added)} — remapping hosted Stack.")
        elif load_infra(ctx).get("updated") != before:
            ok("trace refreshed the local toolchain — remapping hosted Stack.")
        else:
            print("trace: no new pieces detected — the stack is up to date.")
    else:
        _architecture_show(ctx)


# --------------------------------------------------------------------------- product federation (CELE-t190)
#
# Layer A of CELE-t188 (plan/cele-t188-multi-repo-oss-stewardship.md). A product spans several repo-facets
# with different roles + publish policies (client:public → PyPI; server:private → never; oss:* → fork+PR).
# The registry mirrors Celeborn's own authored-vs-machine split EXACTLY:
#   • product.md            — authored, COMMITTED. Product FACTS only: facet keys, roles, publish policy,
#                             canonical repo URLs, OSS provenance (Layer C). Portable across every clone.
#   • product-local.json    — gitignored, PER-MACHINE. Binds each facet key → this machine's checkout path.
#                             A facet with no binding here degrades gracefully to "not present on this machine".
# Layers B (git/PR ops) and C (OSS provenance + guard) read this registry; Layer D (README) is gated on C.
PRODUCT_MD_NAME = "product.md"
PRODUCT_LOCAL_NAME = "product-local.json"
PRODUCT_LOCAL_SCHEMA = "celeborn-product-local/1"
# The role vocabulary from the t188 plan (§3). Determines publish policy + guard posture (guards land on B/C).
PRODUCT_ROLES = ("client:public", "server:private", "oss:upstream", "oss:dependency", "oss:fork")


def _product_md_path(ctx: Path) -> Path:
    return ctx / PRODUCT_MD_NAME


def _product_local_path(ctx: Path) -> Path:
    return ctx / PRODUCT_LOCAL_NAME


def load_product_local(ctx: Path) -> dict:
    """Read .context/product-local.json (gitignored, per-machine). {} when absent/invalid."""
    p = _product_local_path(ctx)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        warn(f"{PRODUCT_LOCAL_NAME} is not valid JSON; treating as empty.")
        return {}


def parse_product(text: str) -> dict:
    """Parse product.md → {'name': str, 'facets': [{key, role, publish, repo, upstream, ...}],
    'provenance': [raw '- …' lines]}. Section-aware: facet lines live under a line beginning 'Facets',
    provenance (Layer C) under a line beginning 'Provenance'. HTML comments are stripped first so the
    managed header never parses as data. Never raises on malformed input — it returns what it can."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    name, facets, provenance, mode = "", [], [], None
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            h = s.lstrip("#").strip()
            if h.lower().startswith("product"):
                for sep in ("—", ":", " - ", "-"):
                    if sep in h:
                        name = h.split(sep, 1)[1].strip()
                        break
            mode = None
            continue
        low = s.lower()
        if low.startswith("facets"):
            mode = "facets"
            continue
        if low.startswith("provenance"):
            mode = "provenance"
            continue
        if not s.startswith("-"):
            continue
        body = s[1:].strip()
        if not body or body.lower().startswith("(none"):
            continue
        if mode == "facets":
            toks = body.split()
            facet = {"key": toks[0]}
            for t in toks[1:]:
                if "=" in t:
                    k, v = t.split("=", 1)
                    facet[k] = v
            facets.append(facet)
        elif mode == "provenance":
            provenance.append("- " + body)
    return {"name": name, "facets": facets, "provenance": provenance}


def load_product(ctx: Path) -> dict:
    """Parsed product.md plus an `exists` flag. Empty/absent → {'exists': False}."""
    p = _product_md_path(ctx)
    if not p.is_file():
        return {"name": "", "facets": [], "provenance": [], "exists": False}
    d = parse_product(p.read_text())
    d["exists"] = True
    return d


def _render_product(name: str, facets: list, provenance: list) -> str:
    """Serialize product.md canonically (Celeborn-maintained file). Provenance lines round-trip verbatim
    so a Layer-C write is preserved when Layer A rewrites the facet block."""
    lines = [f"# Product — {name}", ""]
    lines += [
        "<!-- Celeborn product registry (CELE-t190, Layer A of CELE-t188). Authored + COMMITTED — product",
        "     FACTS only: facet keys, roles, publish policy, canonical repo URLs, and OSS provenance",
        "     (Layer C). No local paths here — this machine's checkout paths live in product-local.json",
        "     (gitignored). Roles: client:public · server:private · oss:upstream · oss:dependency ·",
        "     oss:fork. Edit via `celeborn product add|bind`; the orient banner reads this file. -->",
        "",
        "Facets (key · role · publish · repo):",
    ]
    if facets:
        for f in facets:
            parts = [f"- {f['key']}", f"role={f.get('role', '')}"]
            if f.get("publish"):
                parts.append(f"publish={f['publish']}")
            if f.get("repo"):
                parts.append(f"repo={f['repo']}")
            if f.get("upstream"):
                parts.append(f"upstream={f['upstream']}")
            lines.append("   ".join(parts))
    else:
        lines.append("- (none yet — add with `celeborn product add <key> --role <role>`)")
    lines += ["", "Provenance (portions of the tree that are OSS — Layer C, CELE-t192):"]
    lines += provenance if provenance else ["- (none yet)"]
    lines.append("")
    return "\n".join(lines)


def _product_init(ctx: Path, name: str | None = None, force: bool = False) -> None:
    p = _product_md_path(ctx)
    if p.is_file() and not force:
        die(f"{PRODUCT_MD_NAME} already exists (use --force to overwrite).")
    nm = (name or _project_name(ctx) or ctx.parent.name).strip()
    p.write_text(_render_product(nm, [], []))
    ok(f"wrote {PRODUCT_MD_NAME} for product '{nm}'.")
    print("  Add a facet:  celeborn product add client --role client:public --repo github.com/you/app")
    print("  Bind it here: celeborn product bind client /path/to/checkout   (gitignored, per-machine)")


def _product_add(ctx: Path, key: str, role: str, publish: str | None,
                 repo: str | None, upstream: str | None) -> None:
    """Upsert a facet into product.md (add-or-edit by key). Scaffolds product.md if absent."""
    p = _product_md_path(ctx)
    if p.is_file():
        cur = parse_product(p.read_text())
        name, facets, provenance = cur["name"], cur["facets"], cur["provenance"]
    else:
        name, facets, provenance = (_project_name(ctx) or ctx.parent.name), [], []
    facet = {"key": key, "role": role}
    if publish:
        facet["publish"] = publish
    if repo:
        facet["repo"] = repo
    if upstream:
        facet["upstream"] = upstream
    existing = next((f for f in facets if f.get("key") == key), None)
    verb = "updated" if existing else "added"
    if existing:
        facets = [facet if f is existing else f for f in facets]
    else:
        facets.append(facet)
    p.write_text(_render_product(name, facets, provenance))
    ok(f"{verb} facet '{key}' (role={role}) in {PRODUCT_MD_NAME}.")


def _product_bind(ctx: Path, key: str, checkout: str) -> None:
    """Bind a facet key → this machine's checkout path in product-local.json (gitignored)."""
    prod = load_product(ctx)
    if prod["exists"] and not any(f.get("key") == key for f in prod["facets"]):
        warn(f"'{key}' is not a facet in {PRODUCT_MD_NAME} yet — binding it anyway "
             f"(add it with `celeborn product add {key} --role <role>`).")
    abspath = str(Path(checkout).expanduser().resolve())
    if not Path(abspath).is_dir():
        warn(f"{abspath} is not a directory on this machine — binding recorded, but the facet "
             f"shows as unbound (—) until the path exists.")
    local = load_product_local(ctx)
    if local.get("schema") != PRODUCT_LOCAL_SCHEMA:
        local["schema"] = PRODUCT_LOCAL_SCHEMA
    bindings = local.setdefault("bindings", {})
    bindings[key] = abspath
    _product_local_path(ctx).write_text(json.dumps(local, indent=2) + "\n")
    ok(f"bound '{key}' → {abspath} (product-local.json, gitignored).")


def _product_list(ctx: Path) -> None:
    prod = load_product(ctx)
    if not prod["exists"]:
        print(f"No {PRODUCT_MD_NAME} yet. Run `celeborn product init` to create the registry.")
        return
    facets = prod["facets"]
    bindings = (load_product_local(ctx).get("bindings") or {})
    name = prod["name"] or "product"
    print(f"Product — {name} · {len(facets)} facet(s)")
    if not facets:
        print("  (no facets — add with `celeborn product add <key> --role <role>`)")
    for f in facets:
        key, role = f.get("key", "?"), f.get("role", "?")
        path = bindings.get(key)
        bound = path and Path(path).is_dir()
        marker = "✓" if bound else "—"
        line = f"  [{role} {marker}] {key}"
        if f.get("repo"):
            line += f"   repo={f['repo']}"
        print(line)
        if path:
            print(f"        → {path}" + ("" if bound else "   (path missing — unbound)"))
        else:
            print("        (unbound on this machine — `celeborn product bind %s <path>`)" % key)
    if prod["provenance"]:
        print(f"  provenance (OSS — Layer C): {len(prod['provenance'])} entr(y/ies)")


def _product_banner(ctx: Path) -> str:
    """One-line orient banner: product name + facets with ✓ (bound + present here) / — (unbound) markers.
    '' when no product.md (silent for single-repo projects). Best-effort — never raises."""
    try:
        p = _product_md_path(ctx)
        if not p.is_file():
            return ""
        data = parse_product(p.read_text())
        facets = data.get("facets") or []
        if not facets:
            return ""
        bindings = load_product_local(ctx).get("bindings") or {}
        parts = []
        for f in facets:
            key, role = f.get("key", "?"), f.get("role", "?")
            path = bindings.get(key)
            marker = "✓" if (path and Path(path).is_dir()) else "—"
            parts.append(f"{key} ({role} {marker})")
        name = data.get("name") or "product"
        head = f"🏹 Celeborn product —> {name} · {len(facets)} facet{'s' if len(facets) != 1 else ''}: "
        shown, budget = [], 220
        for i, part in enumerate(parts):
            if shown and len(head) + len(" · ".join(shown + [part])) > budget:
                shown.append(f"+{len(parts) - i} more")
                break
            shown.append(part)
        return head + " · ".join(shown)
    except Exception:
        return ""


def cmd_product(args):
    """`celeborn product [list|init|add|bind]` — the product federation registry (Layer A of CELE-t188).
    Pure-local markdown/JSON maintenance; no network."""
    ctx = require_context(args)
    sub = getattr(args, "product_cmd", None)
    if sub == "init":
        _product_init(ctx, name=getattr(args, "name", None), force=getattr(args, "force", False))
    elif sub == "add":
        _product_add(ctx, args.key, args.role, getattr(args, "publish", None),
                     getattr(args, "repo", None), getattr(args, "upstream", None))
    elif sub == "bind":
        _product_bind(ctx, args.key, args.checkout)
    else:
        _product_list(ctx)


# --------------------------------------------------------------------------- Multi-repo git/PR ops (t191)
#
# Layer B of CELE-t188. `celeborn commit/push/pr --facet <key>` routes git (and a drafted `gh pr create`)
# to the facet's bound checkout, so a single board coordinates work across every repo of the product.
# Each op is attributed automatically — commits carry Celeborn-Task/-Agent/-Model trailers and register a
# cross-repo touch, exactly the multi-agent protocol the single-repo flow already uses. The publish guard
# (above) is the role enforcement; commit/push/pr are the routing. Reads the Layer A registry (t190).


def _facet_role_for_path(ctx: Path, path) -> tuple:
    """(key, role) of the bound facet whose checkout is `path` or an ancestor of it — longest match wins,
    so a nested facet resolves to the closest one. (None, None) when no product.md or no enclosing facet."""
    prod = load_product(ctx)
    if not prod.get("exists"):
        return (None, None)
    roles = {f.get("key"): f.get("role") for f in prod["facets"] if f.get("key")}
    bindings = load_product_local(ctx).get("bindings") or {}
    try:
        target = Path(path).expanduser().resolve()
    except Exception:
        return (None, None)
    best_key, best_role, best_len = None, None, -1
    for key, co in bindings.items():
        try:
            cop = Path(co).expanduser().resolve()
        except Exception:
            continue
        if target == cop or cop in target.parents:
            if len(str(cop)) > best_len:
                best_key, best_role, best_len = key, roles.get(key), len(str(cop))
    return (best_key, best_role)


def _publish_guard_targets(ctx: Path, cmd: str, project_dir: str) -> list:
    """The (key, role) of every forbidden-to-publish facet a publish command targets: a path token in the
    command that resolves into a bound checkout, else — when the command names no such path — the facet the
    command's own project resolves into. Resolving the command's OWN tokens (not string-matching the stored
    binding) makes detection symlink-robust (macOS /var → /private/var). Only server:private/oss:* facets
    are returned (client:public publishes are allowed), so an empty list means 'let it through'."""
    if not load_product(ctx).get("exists"):
        return []
    hits, seen = [], set()
    for tok in re.split(r"[\s'\"=]+", cmd):
        if "/" not in tok:
            continue                                   # only path-shaped tokens can name a checkout
        cand = tok.split("*", 1)[0].rstrip("/")        # drop a glob tail (dist/* → dist)
        if not cand:
            continue
        key, role = _facet_role_for_path(ctx, cand)
        if key and key not in seen and _role_forbids_publish(role):
            seen.add(key)
            hits.append((key, role))
    if hits:
        return hits
    key, role = _facet_role_for_path(ctx, project_dir)
    if key and _role_forbids_publish(role):
        return [(key, role)]
    return []


def _facet_resolve(ctx: Path, key: str) -> dict:
    """The facet dict (key/role/repo/upstream/publish) plus its resolved `checkout` Path on this machine.
    die()s with a corrective message when the facet is undeclared, unbound here, or the bound path is
    missing / not a git repo — the same graceful-degradation contract as the Layer A banner, but a hard
    stop because a git op has nowhere to run without a real checkout."""
    prod = load_product(ctx)
    if not prod.get("exists"):
        die("no product registry — run `celeborn product init` and declare facets first (CELE-t190).")
    facet = next((f for f in prod["facets"] if f.get("key") == key), None)
    if facet is None:
        declared = ", ".join(f.get("key", "?") for f in prod["facets"]) or "(none yet)"
        die(f"'{key}' is not a facet in product.md (declared: {declared}). "
            f"Add it: celeborn product add {key} --role <role>.")
    co = (load_product_local(ctx).get("bindings") or {}).get(key)
    if not co:
        die(f"facet '{key}' is not bound on this machine. Bind it: celeborn product bind {key} <checkout>.")
    cop = Path(co).expanduser()
    if not cop.is_dir():
        die(f"facet '{key}' is bound to {cop}, which is not a directory here — re-bind it "
            f"(`celeborn product bind {key} <checkout>`).")
    if not (cop / ".git").exists():
        die(f"facet '{key}' checkout {cop} is not a git repository.")
    return {**facet, "checkout": cop}


def _run_git(checkout: Path, git_args: list, timeout: int = 30):
    """Run `git -C <checkout> <args>` and return the CompletedProcess. die()s only if git can't be spawned
    at all; a non-zero git exit is left to the caller to report with context."""
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(checkout), *git_args],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        die(f"could not run git in {checkout}: {e}")


def _celeborn_trailers(ident: dict, task: str) -> list:
    """The commit trailers that attribute a facet-routed commit — bare tN per the machine-parsed
    convention (CLAUDE.md), agent handle, and model label. Omits any part that isn't known."""
    trailers = []
    bare = _split_qualified_tid(task)[1] if task else ""
    if bare:
        trailers.append(f"Celeborn-Task: {bare}")
    handle = (ident.get("handle") or "").strip()
    if handle and handle != "unknown":
        trailers.append(f"Celeborn-Agent: {handle}")
    label = _agent_label(ident.get("family", ""), ident.get("model", ""))
    if label:
        trailers.append(f"Celeborn-Model: {label}")
    return trailers


def _facet_touch(ctx: Path, key: str, filepath: str, ident: dict, task: str, why: str) -> None:
    """Register a cross-repo touch (path namespaced `<key>:<file>`) so agents sharing this .context/ see
    the facet activity on orient. The registry is this project's — a facet-routed op is coordinated on the
    one board, even though the file lives in another repo."""
    data = _load_touches(ctx)
    files = data.setdefault("files", {})
    recs = files.setdefault(f"{key}:{filepath}", [])
    _upsert_toucher(recs, {
        "by": ident.get("handle") or "unknown",
        "family": ident.get("family", ""),
        "model": ident.get("model", ""),
        "at": now_iso(),
        "task": _split_qualified_tid(task)[1] if task else "",
        "why": why,
    })
    _save_touches(ctx, data)


def cmd_commit(args):
    """`celeborn commit --facet KEY -m MSG [files…]` — route a git commit into a bound facet checkout,
    appending Celeborn-Task/-Agent/-Model trailers automatically and registering a cross-repo touch. Files
    are staged by name (never `git add -A`); omit them to commit what's already staged. Layer B of CELE-t188."""
    ctx = require_context(args)
    facet = _facet_resolve(ctx, args.facet)
    co = facet["checkout"]
    ident = _agent_identity(args, ctx)
    task = (getattr(args, "task", None) or "").strip() or _session_task_id(ctx, _resolve_session(args))
    files = list(getattr(args, "files", None) or [])
    trailers = _celeborn_trailers(ident, task)
    full = args.message.rstrip() + ("\n\n" + "\n".join(trailers) if trailers else "")
    if files:
        r = _run_git(co, ["add", "--", *files])
        if r.returncode != 0:
            die(f"git add failed in facet '{args.facet}' ({co}):\n{(r.stderr or r.stdout).strip()}")
    commit_args = ["commit", "-m", full] + (["--", *files] if files else [])
    r = _run_git(co, commit_args)
    if r.returncode != 0:
        die(f"git commit failed in facet '{args.facet}' ({co}):\n{(r.stderr or r.stdout).strip()}")
    for f in (files or ["(staged)"]):
        _facet_touch(ctx, args.facet, f, ident, task, f"committed to {args.facet}")
    ok(f"committed to facet '{args.facet}' ({co})" + (f" · {_split_qualified_tid(task)[1]}" if task else ""))
    print("  trailers: " + (", ".join(trailers) if trailers
                            else "(none — run `celeborn identify` so commits show who you are)"))
    head = _run_git(co, ["log", "-1", "--oneline"])
    if head.returncode == 0 and head.stdout.strip():
        print("  " + head.stdout.strip())


def cmd_push(args):
    """`celeborn push --facet KEY [remote] [branch]` — route `git push` to a bound facet checkout. A branch
    push (even into a private repo's own remote) is fine; a RELEASE push (`--tags`/`--follow-tags`) into a
    server:private/oss:* facet is refused under the same publish policy the PreToolUse guard enforces —
    caught here too because that guard can't see the git that runs inside celeborn. Layer B of CELE-t188."""
    ctx = require_context(args)
    facet = _facet_resolve(ctx, args.facet)
    co = facet["checkout"]
    tags = bool(getattr(args, "tags", False)) or bool(getattr(args, "follow_tags", False))
    if tags and _role_forbids_publish(facet.get("role", "")):
        die(_publish_policy_reason(args.facet, facet.get("role", ""), "a tag/release push"))
    git_args = ["push"]
    if getattr(args, "set_upstream", False):
        git_args.append("--set-upstream")
    if getattr(args, "follow_tags", False):
        git_args.append("--follow-tags")
    if getattr(args, "tags", False):
        git_args.append("--tags")
    if getattr(args, "remote", None):
        git_args.append(args.remote)
    if getattr(args, "branch", None):
        git_args.append(args.branch)
    r = _run_git(co, git_args, timeout=120)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        die(f"git push failed in facet '{args.facet}' ({co}):\n{out}")
    ok(f"pushed facet '{args.facet}' ({co})")
    if out:
        print("  " + out.replace("\n", "\n  "))


def cmd_pr(args):
    """`celeborn pr --facet KEY [--base B] [--title T] [--body B]` — DRAFT a pull request for a bound facet
    checkout: compute branch/base/commits, compose title+body with provenance, and print a ready-to-run
    `gh pr create` command. Celeborn NEVER auto-opens a PR — for oss:* facets it also prints the fork→PR
    steps. The human/agent reviews and sends it. Layer B of CELE-t188 (draft-don't-send)."""
    import shlex
    ctx = require_context(args)
    facet = _facet_resolve(ctx, args.facet)
    co, role = facet["checkout"], facet.get("role", "")
    repo, upstream = facet.get("repo", ""), facet.get("upstream", "")
    base = (getattr(args, "base", None) or "main").strip()
    br = _run_git(co, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = br.stdout.strip() if br.returncode == 0 else ""
    if not branch or branch == "HEAD":
        die(f"facet '{args.facet}' has no current branch (detached HEAD?) — check out a branch first.")
    log = _run_git(co, ["log", f"{base}..{branch}", "--oneline"])
    commits = [l for l in (log.stdout or "").splitlines() if l.strip()] if log.returncode == 0 else []
    task = (getattr(args, "task", None) or "").strip() or _session_task_id(ctx, _resolve_session(args))
    ident = _agent_identity(args, ctx)
    title = (getattr(args, "title", None) or "").strip()
    if not title:
        title = (commits[0].split(" ", 1)[1] if commits and " " in commits[0] else f"{branch} → {base}")
    body = (getattr(args, "body", None) or "").strip()
    if not body:
        lines = ["## Changes", ""] + (
            [f"- {c.split(' ', 1)[1] if ' ' in c else c}" for c in commits] or ["- (no commits ahead of base)"])
        body = "\n".join(lines)
    foot = []
    bare = _split_qualified_tid(task)[1] if task else ""
    if bare:
        foot.append(f"Celeborn-Task: {bare}")
    handle = (ident.get("handle") or "").strip()
    label = _agent_label(ident.get("family", ""), ident.get("model", ""))
    if handle and handle != "unknown":
        foot.append(f"Drafted-by: @{handle}" + (f" ({label})" if label else ""))
    if foot:
        body = body + "\n\n" + "\n".join(foot)

    print(f"🏹 Celeborn PR draft — facet '{args.facet}' ({role})")
    print(f"  repo:        {repo or '(no repo url in product.md — add `--repo` via product add)'}")
    print(f"  base ← head: {base} ← {branch}")
    print(f"  commits:     {len(commits)} ahead of {base}")
    print()
    print(f"  title: {title}")
    print("  body:")
    for line in body.splitlines():
        print("    " + line)
    print()
    if role.startswith("oss:"):
        print("  ⓘ Stewarded OSS — contribute via a fork, never publish/push as ours:")
        if upstream:
            print(f"      upstream: {upstream}")
        print(f"      1) gh repo fork {repo or upstream or '<upstream>'} --clone=false")
        print("      2) push this branch to YOUR fork, then open the PR against upstream.")
    ghr = f" -R {repo}" if repo else ""
    print("  Ready to send — Celeborn drafts, it never auto-opens a PR. Review the diff, then run:")
    print(f"      gh pr create{ghr} --base {base} --head {branch} \\")
    print(f"        --title {shlex.quote(title)} \\")
    print(f"        --body {shlex.quote(body)}")
    warn("drafted, not sent (CELE-t191) — send it yourself with the gh command above.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="celeborn", description="Celeborn Code — a long-term context substrate for coding agents "
        "(memory for Claude Code / Codex / Grok). Not Apache Celeborn (Spark shuffle) or the "
        "frkngksl/Celeborn Windows tool. `celeborn about` for identity + links.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=GETTING_STARTED_EPILOG)
    p.add_argument("--path", default=".", help="project dir to operate in (default: cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    # `scaffold` — the secondary, scaffold-only command (CELE-t228 renamed the old `init` here; the
    # everything-command `init` below now wires + scaffolds + signs in). `.context/` is private-only.
    ip = sub.add_parser("scaffold", help="scaffold .context/ only (private; `init` is the full first-run)")
    # `--private` is now the ONLY behavior (.context/ is always gitignored); kept as a hidden no-op so
    # old scripts/muscle-memory don't error. `--public` was removed — a public install is impossible.
    ip.add_argument("--private", action="store_true", help=argparse.SUPPRESS)
    ip.add_argument("--no-claude-md", dest="claude_md", action="store_false",
                    help="don't annotate CLAUDE.md (by default scaffold adds a managed block so Claude "
                         "Code, which auto-loads CLAUDE.md, knows Celeborn maintains context in .context/).")
    ip.add_argument("--no-agents-md", dest="agents_md", action="store_false",
                    help="don't annotate AGENTS.md (by default scaffold adds the same managed block for "
                         "Codex/Grok-style hosts that auto-load AGENTS.md).")
    ip.add_argument("--no-scan", dest="scan", action="store_false",
                    help="don't read the repo (README, build manifest, git log) to pre-seed the Hot "
                         "tier; leave the empty template for you to fill in by hand.")
    ip.add_argument("--no-cmm", dest="no_cmm", action="store_true",
                    help="don't auto-engage Codebase Memory (CMM) for this project. By default it "
                         "pre-clears CMM's read-only tools (fewer 'Allow' prompts) and indexes the repo "
                         "if the CMM binary is installed; reverse anytime with `celeborn cmm off`. "
                         "($CELEBORN_NO_CMM=1 opts out globally.)")
    ip.add_argument("--name", dest="name", default=None,
                    help="name this project for the kanban board (skips the interactive prompt). "
                         "Persisted as project_name in .celebornrc; defaults to the repo folder name.")
    ip.add_argument("--no-open", dest="open_board", action="store_false",
                    help="don't launch or open the kanban board after scaffolding (by default it seeds an "
                         "empty board and starts the localhost viewer — Celeborn's UI).")
    ip.add_argument("--no-browser", dest="open_browser", action="store_false",
                    help="start the kanban viewer but don't pop a browser tab (the board stays "
                         "reachable on localhost). Implied when not run from a terminal.")
    ip.set_defaults(func=cmd_scaffold, claude_md=True, agents_md=True, scan=True, no_cmm=False,
                    open_board=True, open_browser=True)
    sp = sub.add_parser("status", help="print the Hot tier (Orient load)")
    sp.add_argument("--full", action="store_true",
                    help="print the Hot tier unclipped (bypass the Orient-load size budgets)")
    sp.set_defaults(func=cmd_status)

    cp = sub.add_parser("checkpoint",
                        help="safely update session.json (focus/next/branch/status) — writes valid JSON, "
                             "clips over-long fields, repairs a corrupt file; replaces hand-editing")
    cp.add_argument("--focus", help="current focus (one line; long-form belongs in state.md/notes.md)")
    cp.add_argument("--next", help="next action")
    cp.add_argument("--branch", help="working branch")
    cp.add_argument("--status", help="session status (e.g. in-progress, blocked, green)")
    cp.add_argument("--stop-allowed", dest="stop_allowed", action="store_true",
                    help="mark the session safe to stop/clear")
    cp.add_argument("--no-stop-allowed", dest="no_stop_allowed", action="store_true",
                    help="mark the session NOT safe to stop (work in flight)")
    cp.add_argument("--for-clear", dest="for_clear", action="store_true",
                    help="pre-clear routine (CELE-t208): after writing session.json, regenerate handoff + "
                         "take a restorable snapshot, then verify-gate the Hot tier is fresh — exits "
                         "nonzero with a fix-list (and sets stop_allowed=false) if a /clear would lose work")
    cp.add_argument("--session", help="session id — with --for-clear, resolves the DOING card you own for "
                    "the Stop-condition check and tags the snapshot")
    cp.set_defaults(func=cmd_checkpoint)

    acp = sub.add_parser("autoclear",
                         help="opt-in OpenCode seamless clear-and-continue (CELE-t209): verify the "
                              "session is due (hard pressure + cooldown) and the t208 gate is clean, "
                              "then prep (handoff + snapshot + resume brief → outbox) and say 'ready' "
                              "so the plugin compacts — 'blocked' + fix-list (exit 1) when stale")
    acp.add_argument("--session", help="session id whose live window is under pressure (the plugin "
                                       "passes it; falls back to the ambient session)")
    acp.set_defaults(func=cmd_autoclear)

    sub.add_parser("index", help="(re)build the SQLite FTS index").set_defaults(func=cmd_index)

    sp = sub.add_parser("search", help="full-text recall")
    sp.add_argument("query")
    sp.add_argument("-n", "--limit", type=int, default=None, help="max results")
    sp.set_defaults(func=cmd_search)

    ap = sub.add_parser("archive", help="FIFO old journal entries + state.md history into cold archives")
    ap.add_argument("--what", choices=["all", "journal", "state"], default="all",
                    help="which tier to archive (default: all)")
    ap.add_argument("--keep", type=int, default=None, help="entries to keep in journal.md")
    ap.add_argument("--state-keep", type=int, default=None, dest="state_keep",
                    help="dated history bullets to keep in state.md's ## Now")
    ap.set_defaults(func=cmd_archive)

    pp = sub.add_parser("promote", help="distill a note to a higher tier")
    pp.add_argument("--to", choices=["learnings", "durable"], required=True)
    pp.add_argument("--title", required=True)
    pp.add_argument("--note", default="", help="body text")
    pp.add_argument("--doc", default=None, help="durable doc name (default: gotchas)")
    pp.set_defaults(func=cmd_promote)

    sub.add_parser("handoff", help="regenerate handoff.md").set_defaults(func=cmd_handoff)
    sub.add_parser("doctor", help="health check + memory-drift + secret scan").set_defaults(func=cmd_doctor)
    pgp = sub.add_parser("progress", help="run the deterministic progress engine for a card (debug + explain)")
    pgp.add_argument("id", nargs="?", help="card id (default: every doing card)")
    pgp.add_argument("--explain", action="store_true", help="show the signals → floor derivation")
    pgp.set_defaults(func=cmd_progress)

    al = sub.add_parser("alert", help="raise/clear a 'coding blocked — needs the user' alert on a card (surfaces on the board)")
    al.add_argument("id", nargs="?", help="card id to alert on (omit with --list)")
    al.add_argument("--message", "-m", default="", help="what is blocking (e.g. the permission request text)")
    al.add_argument("--kind", choices=list(ALERT_KINDS), default=None,
                    help="permission (needs approval) · idle (stalled) · stopped (turn ended, awaiting you)")
    al.add_argument("--session", default="", help="the blocked session's id (attribution; hooks pass this)")
    al.add_argument("--clear", action="store_true", help="clear the alert (also happens when the user replies)")
    al.add_argument("--list", action="store_true", help="list the live alerts on this board")
    al.set_defaults(func=cmd_alert)

    # Question-dock round-trip (CELE-t280): ask parks a human-in-the-loop question, ask-status is the
    # tool's poll, answer delivers + journals the human's reply.
    ak = sub.add_parser("ask", help="park a human-in-the-loop question for the board dock (ask_human backs onto this)")
    ak.add_argument("question", help="the question to ask the human")
    ak.add_argument("--session", default="", help="the asking session's id (default: ambient CLAUDE_CODE_SESSION_ID)")
    ak.add_argument("--card", default="", help="card to attach the ask to (default: the session's doing card)")
    ak.add_argument("--options", default="", help="comma-separated enumerable answers (quick chips)")
    ak.add_argument("--json", action="store_true", help="print {id, card, question, options}")
    ak.set_defaults(func=cmd_ask)

    aks = sub.add_parser("ask-status", help="poll a parked ask for its answer (used by the ask_human tool)")
    aks.add_argument("ask_id", help="the askId returned by `celeborn ask`")
    aks.add_argument("--json", action="store_true", help="print {answered, answer}")
    aks.set_defaults(func=cmd_ask_status)

    an = sub.add_parser("answer", help="deliver + journal a human's dock answer (CELE-t280 round-trip)")
    an.add_argument("card", help="the card the question is on")
    an.add_argument("--kind", choices=["permission", "text"], default="text",
                    help="permission (already resumed live) or free text (ask_human / alert)")
    an.add_argument("--response", required=True, help="the answer (permission: once|always|reject; text: free text)")
    an.add_argument("--session", default="", help="the answered session's id")
    an.add_argument("--question", default="", help="the question being answered (for the journal)")
    an.add_argument("--model", default="",
                    help="per-prompt model the human picked for this answer (CELE-t346) — journaled "
                         "and carried on the outbox message so the next turn answers with it")
    an.set_defaults(func=cmd_answer)

    # Spine branding (CELE-t380): the per-project purpose-emoji that brands each spine of cards.
    spn = sub.add_parser("spine", help="brand each spine (group of cards from one plan) with a unique purpose-emoji")
    spn.set_defaults(func=cmd_spine, spine_cmd=None, json=False)
    spnsub = spn.add_subparsers(dest="spine_cmd")
    spn_ls = spnsub.add_parser("ls", help="list spines on this project — emoji, slug, card counts")
    spn_ls.add_argument("--json", action="store_true", help="machine-readable output")
    spn_ls.set_defaults(func=cmd_spine, spine_cmd="ls")
    spn_set = spnsub.add_parser("set", help="brand/rebrand a spine's emoji (collision-checked per project)")
    spn_set.add_argument("slug", help="the spine group slug to brand")
    spn_set.add_argument("--emoji", default="", help="the purpose-emoji (unused by any other spine on this project)")
    spn_set.set_defaults(func=cmd_spine, spine_cmd="set")
    spn_bf = spnsub.add_parser("backfill", help="adopt existing hand-emoji spines: title glyph -> emoji field")
    spn_bf.add_argument("--dry-run", dest="dry_run", action="store_true", help="report only; don't write")
    spn_bf.set_defaults(func=cmd_spine, spine_cmd="backfill")

    tp = sub.add_parser("tasks", help="lightweight task/kanban board (tasks.md truth + derived tasks.json)")
    tp.set_defaults(func=cmd_tasks, task_cmd=None, json=False)
    tsub = tp.add_subparsers(dest="task_cmd")

    ta = tsub.add_parser("add", help="add a task")
    ta.add_argument("title")
    ta.add_argument("--state", default="todo", choices=TASK_STATES, help="initial state (default: todo)")
    ta.add_argument("--owner", default="", help="who owns it (e.g. an agent or person)")
    ta.add_argument("--tags", default="", help="comma/space-separated tags")
    ta.add_argument("--blocked-by", dest="blocked_by", default="", help="task id(s) blocking this one")
    ta.add_argument("--phase", default="", help="plan phase id this task belongs to (e.g. p11)")
    ta.add_argument("--spine", default="", help="spine group slug this card belongs to (cards minted from one plan)")
    ta.add_argument("--emoji", default="", help="the spine's purpose-emoji brand (must be unused by another spine on this project)")
    ta.add_argument("--stop", default="",
                    help="logical Stop condition — a clean `/clear` point for this card "
                         "(auto-filled with a generic default if omitted)")
    ta.add_argument("--autonomy", default="",
                    help="grooming-time autonomy grants (comma-separated subset of "
                         f"{','.join(AUTONOMY_GRANTS)}) — what the owning session may do without a "
                         "human; `commit` = git-write, off unless granted")
    ta.add_argument("--progress", type=int, default=None,
                    help="percent complete 0-100 (drives the In-Progress card's sand-fill bar)")
    ta.add_argument("--note", default="", help="freeform notes / body")
    ta.add_argument("--claim", action="store_true",
                    help="claim the new card immediately (avoids guessing the new id in a second command)")
    ta.add_argument("--by", default=None, help="claimer for --claim (default: $CELEBORN_AGENT)")
    ta.add_argument("--force", action="store_true", help="with --claim: claim even if you have other DOING cards")
    ta.set_defaults(func=cmd_tasks, task_cmd="add")

    tm = tsub.add_parser("move", help="move a task to a new state")
    tm.add_argument("id")
    tm.add_argument("state", choices=TASK_STATES)
    tm.set_defaults(func=cmd_tasks, task_cmd="move")

    tre = tsub.add_parser("reorder", help="reprioritize a task within its column (up | down | top | bottom)")
    tre.add_argument("id")
    tre.add_argument("dir", choices=["up", "down", "top", "bottom"])
    tre.set_defaults(func=cmd_tasks, task_cmd="reorder")

    te = tsub.add_parser("edit", help="edit task fields (only the flags you pass change)")
    te.add_argument("id")
    te.add_argument("--title", default=None)
    te.add_argument("--state", default=None, choices=TASK_STATES)
    te.add_argument("--owner", default=None)
    te.add_argument("--tags", default=None)
    te.add_argument("--blocked-by", dest="blocked_by", default=None)
    te.add_argument("--phase", default=None)
    te.add_argument("--spine", default=None, help="set the spine group slug (CELE-t380)")
    te.add_argument("--emoji", default=None, help="set the spine's purpose-emoji brand (collision-checked per project)")
    te.add_argument("--stop", default=None, help="set the logical Stop condition (clean `/clear` point)")
    te.add_argument("--autonomy", default=None,
                    help="set the autonomy grants (comma-separated subset of "
                         f"{','.join(AUTONOMY_GRANTS)}; empty string clears back to most-restrictive)")
    te.add_argument("--progress", type=int, default=None,
                    help="percent complete 0-100 (drives the In-Progress card's sand-fill bar)")
    te.add_argument("--note", default=None)
    te.set_defaults(func=cmd_tasks, task_cmd="edit")

    # Subtask checklist (CELE-t106): map a card's steps; checking them auto-derives the progress bar.
    tst = tsub.add_parser("subtasks", help="manage a card's subtask checklist (auto-derives the progress percent)")
    tst.add_argument("id")
    tst.set_defaults(func=cmd_tasks, task_cmd="subtasks", subtask_cmd=None)
    tstsub = tst.add_subparsers(dest="subtask_cmd")
    _sa = tstsub.add_parser("add", help="append a subtask (weight via --weight or a trailing '*N')")
    _sa.add_argument("text", nargs="+")
    _sa.add_argument("--weight", type=int, default=None, help="effort weight (default 1)")
    _ss = tstsub.add_parser("set", help="define the whole checklist at once; each item may end with '*N' for weight")
    _ss.add_argument("items", nargs="+")
    _sr = tstsub.add_parser("rm", help="remove subtask N (1-based)")
    _sr.add_argument("n", type=int)
    tstsub.add_parser("list", help="show the checklist (also the default)")

    tck = tsub.add_parser("check", help="mark subtask N done → pours the progress bar to the new level")
    tck.add_argument("id")
    tck.add_argument("n", type=int)
    tck.set_defaults(func=cmd_tasks, task_cmd="check")
    tuck = tsub.add_parser("uncheck", help="mark subtask N not-done → recomputes progress")
    tuck.add_argument("id")
    tuck.add_argument("n", type=int)
    tuck.set_defaults(func=cmd_tasks, task_cmd="uncheck")

    tr = tsub.add_parser("rm", help="remove a task")
    tr.add_argument("id")
    tr.set_defaults(func=cmd_tasks, task_cmd="rm")

    tarch = tsub.add_parser("archive", help="archive done cards past the column cap to done-archive.md")
    tarch.add_argument("--keep", type=int, default=None, help="done cards to keep on the board (default: done_keep_cards)")
    tarch.set_defaults(func=cmd_tasks, task_cmd="archive")

    tshow = tsub.add_parser("show", help="show one task in full")
    tshow.add_argument("id")
    tshow.set_defaults(func=cmd_tasks, task_cmd="show")

    tj = tsub.add_parser("json", help="(re)write .context/tasks.json (the board's data) and print it")
    tj.add_argument("--out", default=None, help="also write the JSON to this path")
    tj.set_defaults(func=cmd_tasks, task_cmd="json")

    tl = tsub.add_parser("list", help="show the text board (this is also the default)")
    tl.add_argument("--json", action="store_true", help="print tasks.json to stdout instead of the board")
    tl.set_defaults(func=cmd_tasks, task_cmd="list")

    # NEXT-UP selector (CELE-t219): readiness is computed here, deterministically, so a PM model
    # never has to enumerate the raw board itself. `celeborn next` is the top-level alias — it sits
    # in the PM loop next → claim → work → ship.
    def _next_flags(p):
        p.add_argument("--tag", default="", help="only cards carrying ALL of these comma-separated tags")
        p.add_argument("--phase", default="", help="only cards in this plan phase (e.g. p4)")
        p.add_argument("--all", action="store_true", help="emit the whole ready set instead of the single NEXT-UP")
        p.add_argument("--json", action="store_true", help="machine form {next, ready} — id/title/routing fields only")
        p.set_defaults(func=cmd_tasks, task_cmd="next")

    _next_flags(tsub.add_parser(
        "next", help="deterministic NEXT-UP: the first READY card (todo + every blocker done) — id + title only"))
    _next_flags(sub.add_parser(
        "next", help="surface the next READY card (alias of `tasks next`) — never a blocked one"))

    cl = sub.add_parser("claim", help="claim a card (owner ← you, TODO → DOING) — what receiving a pasted card does")
    cl.add_argument("ids", nargs="+", help="task id(s) to claim, e.g. t13")
    cl.add_argument("--by", default=None, help="claimer identity (default: $CELEBORN_AGENT)")
    cl.add_argument("--family", default=None, help="record your agent family (else `celeborn identify` / $CELEBORN_AGENT_FAMILY)")
    cl.add_argument("--model", default=None, help="record your specific model (else `celeborn identify` / $CELEBORN_AGENT_MODEL)")
    cl.add_argument("--force", action="store_true",
                    help="claim even if you already have other DOING cards (not recommended)")
    cl.add_argument("--session", default=None, help=argparse.SUPPRESS)  # active-agents bridge (t131)
    cl.set_defaults(func=cmd_claim)

    sh = sub.add_parser("ship", help="close out a card: release its touches + move to done")
    sh.add_argument("id", help="task id to ship, e.g. t42")
    sh.add_argument("--note", default=None, help="append a ship note to the card")
    sh.add_argument("--by", default=None, help="agent shipping (default: $CELEBORN_AGENT)")
    sh.add_argument("--family", default=None, help="record your agent family (else `celeborn identify` / $CELEBORN_AGENT_FAMILY)")
    sh.add_argument("--model", default=None, help="record your specific model (else `celeborn identify` / $CELEBORN_AGENT_MODEL)")
    sh.add_argument("--strict", action="store_true",
                    help="refuse the ship unless the next spine card is startable verbatim (CELE-t282 spine discipline)")
    sh.set_defaults(func=cmd_ship)

    idp = sub.add_parser("identify", help="declare your agent family + specific model once per session (multi-agent attribution)")
    idp.add_argument("--family", default=None, help="agent family, e.g. Claude / Grok / GPT / Gemini")
    idp.add_argument("--model", default=None, help='specific model, e.g. "Opus 4.8"')
    idp.add_argument("--as", dest="as_", default=None, help="handle to record under (default: $CELEBORN_AGENT / --by)")
    idp.add_argument("--by", default=None, help=argparse.SUPPRESS)
    idp.add_argument("--session", default=None, help=argparse.SUPPRESS)
    idp.add_argument("--show", action="store_true", help="list the agents already identified and exit")
    idp.add_argument("--json", action="store_true", help="with --show: JSON output")
    idp.set_defaults(func=cmd_identify)

    ob = sub.add_parser("outbox", help="prompt hand-off queue — drained into the live session each turn")
    ob.set_defaults(func=cmd_outbox, outbox_cmd=None)
    obsub = ob.add_subparsers(dest="outbox_cmd")
    obp = obsub.add_parser("push", help="queue a prompt (from a task card or raw text)")
    obp.add_argument("--task", default=None, help="render this task id into the queued prompt")
    obp.add_argument("--text", default=None, help="queue this literal prompt text")
    obp.add_argument("--for", dest="for_", default=None,
                     help="address the hand-off to this agent (default: the card's owner)")
    obp.set_defaults(func=cmd_outbox, outbox_cmd="push")
    obd = obsub.add_parser("drain", help="print + clear pending prompts (used by the UserPromptSubmit hook)")
    obd.add_argument("--for", dest="for_", default=None,
                     help="drain this agent's queue (default: $CELEBORN_AGENT, else unassigned)")
    obd.add_argument("--session", default=None,
                     help="also drain this session's queue (its 6-char handle — where `dispatch` "
                          "stages cards); the hook passes it, the CLI falls back to the ambient "
                          "CLAUDE_CODE_SESSION_ID")
    obd.set_defaults(func=cmd_outbox, outbox_cmd="drain")
    obsub.add_parser("list", help="show pending prompts (all agents)").set_defaults(func=cmd_outbox, outbox_cmd="list")
    obc = obsub.add_parser("clear", help="discard pending prompts (all agents, or one with --for)")
    obc.add_argument("--for", dest="for_", default=None, help="clear only this agent's queue")
    obc.set_defaults(func=cmd_outbox, outbox_cmd="clear")

    dp = sub.add_parser("dispatch",
                        help="PM hand-off (CELE-t213): stage a TODO card on a coder session and "
                             "queue its brief — the coder picks it up at its next turn")
    dp.add_argument("id", help="task id (tN or SLUG-tN); must be TODO and unblocked")
    dp.add_argument("--to", default=None,
                    help="target coder: a session id (collapses to its 6-char handle) or an agent "
                         "handle (default: the card's current owner)")
    dp.add_argument("--force", action="store_true",
                    help="dispatch even if the card still has open blockers")
    dp.set_defaults(func=cmd_dispatch)

    pmd = sub.add_parser("pm",
                         help="Qwen-4b PM loop (CELE-t283): stamp READY, dispatch the spine head to a "
                              "free coder, raise ✋ on an unstartable head; --watch = foreground daemon")
    pmd.add_argument("--watch", action="store_true", help="keep marching (foreground; Ctrl-C stops)")
    pmd.add_argument("--interval", type=int, default=None, help="seconds between watch passes (default 15)")
    pmd.add_argument("--window-min", dest="window_min", type=float, default=None,
                     help=f"live-session window for coder-slot discovery (default {int(AGENT_ACTIVE_WINDOW_MIN)}m)")
    pmd.add_argument("--slots", default=None,
                     help="comma-separated coder handles to dispatch to (skips live-session discovery)")
    pmd.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="report what a pass would do; write nothing")
    pmd.add_argument("--no-model", dest="no_model", action="store_true",
                     help="skip the Qwen formatting call; use code-formatted lines only")
    pmd.set_defaults(func=cmd_pm)
    # `pm wake` — the event side (CELE-t216): producers enqueue a wake, `celeborn pm` drains it. Bare
    # `celeborn pm` still marches (subparser is optional; the parent keeps func=cmd_pm).
    pm_sub = pmd.add_subparsers(dest="pm_cmd")
    pmw = pm_sub.add_parser("wake", help="enqueue a PM wake event (CELE-t216), or --list the queue")
    pmw.add_argument("--source", default=None,
                     help="what woke the PM (git-commit | github | jira | kanban | opencode | …)")
    pmw.add_argument("--detail", default=None, help="optional detail (a commit sha, an action name, …)")
    pmw.add_argument("--list", action="store_true", help="show pending wake events instead of enqueuing")
    pmw.set_defaults(func=cmd_pm_wake)

    vp = sub.add_parser("version", help="print version; --check looks back at GitHub for updates")
    vp.add_argument("--check", action="store_true",
                    help="check GitHub (cloud-dancer-labs/celeborn) for a newer Celeborn; offline-safe")
    vp.set_defaults(func=cmd_version)

    abp = sub.add_parser("about", help="identify Celeborn Code + canonical links (disambiguates the same-named projects)")
    abp.set_defaults(func=cmd_about)

    ip = sub.add_parser("integrity", help="verify the install matches the published release (detects in-place edits)")
    ip.add_argument("--write", action="store_true",
                    help="(re)generate the per-version checksum manifest — a release/build step, not for end users")
    ip.set_defaults(func=cmd_integrity)

    adv = sub.add_parser("advise", help="print the throughput/quality recommendations that apply right now")
    adv.add_argument("--harness", default=None, help="render for a specific harness adapter (default: autodetect)")
    adv.add_argument("--json", action="store_true", help="emit the recommendations as JSON")
    adv.add_argument("--dismiss", metavar="ID", default=None,
                     help="permanently silence one recommendation by id (e.g. reduce-permission-friction)")
    adv.add_argument("--restore", metavar="ID", default=None, help="un-silence a previously dismissed recommendation")
    adv.add_argument("--throughput", action="store_true",
                     help="also list on-demand throughput recommendations (spawn_task, /loop, /elves)")
    adv.set_defaults(func=cmd_advise)

    hsp = sub.add_parser("harness", help="show or pin the active harness adapter in .celebornrc (claude|grok|codex|neutral)")
    hsp.add_argument("name", nargs="?", default=None, metavar="claude|grok|codex|neutral",
                     help="harness to pin; omit to print the currently resolved adapter")
    hsp.set_defaults(func=cmd_harness)

    pmp = sub.add_parser("permissions",
                         help="generalize repeated permission approvals into reusable wildcard rules (Claude)")
    pmp.add_argument("--suggest", action="store_true", help="preview the proposed allow-list (default; read-only)")
    pmp.add_argument("--apply", action="store_true", help="write the generalized allow-list to the target file")
    pmp.add_argument("--shared", action="store_true",
                     help="target the committed settings.json (shared) instead of personal settings.local.json")
    pmp.add_argument("--yes", action="store_true", help="skip the confirm prompt on --apply / required to ARM the Danger Zone")
    pmp.add_argument("--harness", default=None, help="use a specific harness adapter (default: autodetect)")
    pmp.add_argument("--json", action="store_true",
                     help="emit the current permission state (baseline active-flags + Danger spectrum + resolved allow-list) as JSON")
    pmp.add_argument("--baseline", action="store_true",
                     help="apply the SAFE t100 auto-allow baseline (default target: global ~/.claude); pair with --remove to strip it")
    pmp.add_argument("--remove", action="store_true", help="with --baseline: remove the baseline rules instead of adding")
    pmp.add_argument("--danger-zone", dest="danger_zone", action="store_true",
                     help="arm (default, needs --yes) or --disarm the FULL UNSAFE auto-allow spectrum + bypassPermissions")
    pmp.add_argument("--disarm", action="store_true", help="with --danger-zone: remove the unsafe spectrum and restore safe defaults")
    pmp.add_argument("--add", metavar="RULE",
                     help="add a single per-rule permission (default --shared / project settings.json); pair with --kind")
    pmp.add_argument("--kind", choices=PERMISSION_RULE_KINDS, default="allow",
                     help="with --add: which list to add to (default: allow)")
    pmp.add_argument("--rm", metavar="RULE",
                     help="remove an exact per-rule permission from allow/ask/deny in the target file")
    pmp.add_argument("--set-mode", dest="set_mode", metavar="MODE",
                     help="set permissions.defaultMode (default|acceptEdits|plan|bypassPermissions; '' to unset)")
    pmp.add_argument("--local", action="store_true",
                     help="with --add/--rm/--set-mode: target this project's personal settings.local.json")
    pmp.add_argument("--global", dest="global_", action="store_true",
                     help="target the global ~/.claude/settings.json instead of the project file")
    pmp.set_defaults(func=cmd_permissions)

    aup = sub.add_parser("autonomy",
                         help="show or set the fleet's default autonomy grants + night-run knobs (.celebornrc)")
    aup.add_argument("--json", action="store_true",
                     help="emit the current autonomy state (consumed by the board Settings page)")
    aup.add_argument("--set-grants", dest="set_grants", metavar="LIST",
                     help=f"default grants stamped onto groomed cards, CSV of {'/'.join(AUTONOMY_GRANTS)} "
                          "(commit is never implied)")
    aup.add_argument("--night-questions", dest="night_questions",
                     choices=[k for k, _ in AUTONOMY_NIGHT_QUESTIONS],
                     help="what a raised hand does overnight")
    aup.add_argument("--elves", type=int, metavar="N",
                     help=f"max concurrent night-run sessions the PM may dispatch ({AUTONOMY_ELVES_MIN}..{AUTONOMY_ELVES_MAX})")
    aup.add_argument("--pm-model", dest="pm_model", choices=[k for k, _ in AUTONOMY_PM_MODELS],
                     help="the head-elf model that stamps READY / dispatches / raises hands")
    aup.set_defaults(func=cmd_autonomy)

    skp = sub.add_parser("skills",
                         help="list Celeborn / recommended / Matt-Pocock skills; install the Matt Pocock suite")
    skp.add_argument("skills_cmd", nargs="?", choices=["list", "install-mattpocock", "update"], default="list",
                     help="list (default), install-mattpocock, or update (re-pull the Matt Pocock suite @latest)")
    skp.add_argument("--json", action="store_true", help="emit JSON (consumed by the board Settings page)")
    skp.add_argument("--global", dest="global_", action="store_true", help="install into the global ~/.claude scope")
    skp.set_defaults(func=cmd_skills)

    rp = sub.add_parser("record", help="record a memory event for the economy estimate")
    rp.add_argument("event", choices=["orient", "compaction", "handoff", "turn", "clear", "tokens"])
    rp.add_argument("--session", default=None, help="session id (dedupes repeat orients; required for `tokens`)")
    rp.add_argument("--tokens", type=int, default=None,
                    help="for `turn`: tokens to add to the rolling context estimate; "
                         "for `tokens`: the session's REAL live context window, absolute (P4, CELE-t141)")
    rp.set_defaults(func=cmd_record)

    mp = sub.add_parser("metrics", help="show the tokens-saved / restarts-avoided estimate")
    mp.add_argument("--json", action="store_true", help="emit raw metrics JSON")
    mp.set_defaults(func=cmd_metrics)

    agp = sub.add_parser("agents", help="live per-session context windows (who's working + how full) — the board's /clear-nudge chips")
    agp.add_argument("--json", action="store_true", help="emit the active-agents snapshot as JSON (for the board /api/agents route)")
    agp.add_argument("--window-min", dest="window_min", type=float, default=None,
                     help=f"a transcript touched within this many minutes counts as live (default {int(AGENT_ACTIVE_WINDOW_MIN)})")
    agp.add_argument("--all", action="store_true", help="include idle sessions (ignore the window)")
    agp.add_argument("action", nargs="?", choices=["forget"],
                     help="`agents forget <session>`: wipe a ghost chip — tombstone a session so it leaves the board")
    agp.add_argument("session", nargs="?", help="session id (full or 8-char) to forget")
    agp.set_defaults(func=cmd_agents)

    for _kind, _help, _dd in (("standup", "what happened recently (done cards + commits + journal)", 1),
                              ("changelog", "a wider-window changelog of recent progress", 7)):
        _sp = sub.add_parser(_kind, help=_help)
        _sp.add_argument("--days", type=int, default=None, help=f"window in days (default {_dd})")
        _sp.add_argument("--tweet", action="store_true", help="emit a build-in-public X post (≤280 chars) instead")
        _sp.add_argument("--json", action="store_true", help="emit the raw aggregated activity as JSON")
        _sp.set_defaults(func=cmd_standup, kind=_kind)

    bdp = sub.add_parser("board", help="open this project's kanban board (Celeborn's UI) in the browser; "
                                       "ensures the viewer is running first")
    bdp.add_argument("--json", action="store_true", help="emit {port,url,live} as JSON (report-only — no launch/open)")
    bdp.add_argument("--port", dest="port_only", action="store_true", help="print just the resolved port (no launch/open)")
    bdp.add_argument("--url", dest="url_only", action="store_true", help="print just the URL (no launch/open)")
    bdp.add_argument("--start", action="store_true", help="ensure-on-orient: launch the viewer (detached) if its port is down")
    bdp.add_argument("--no-open", dest="no_open", action="store_true",
                     help="ensure the viewer is up but don't pop a browser tab (implied on a non-interactive shell)")
    # Hidden: the detached restart-loop entrypoint `_spawn_board` re-invokes (keeps `next dev` alive).
    bdp.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    bdp.add_argument("--supervise-port", type=int, help=argparse.SUPPRESS)
    bdp.add_argument("--supervise-tasks", help=argparse.SUPPRESS)
    bdp.set_defaults(func=cmd_board)

    flp = sub.add_parser("fleet", help="live multi-project agent dashboard (register projects, then watch who's working/stuck)")
    flp.add_argument("fleet_action", nargs="?", default="", metavar="register|unregister|repair",
                     help="register this repo (or --path) / unregister <dir> / repair (re-dedup all slugs)")
    flp.add_argument("fleet_target", nargs="?", default=None, metavar="project-dir",
                     help="project directory to unregister (or register when no --path)")
    flp.add_argument("--path", dest="fleet_path", default=None,
                     help="project directory for register (default: the orienting repo)")
    flp.add_argument("--json", action="store_true", help="emit the fleet snapshot as JSON (for the board viewer)")
    flp.add_argument("--dry-run", dest="dry_run", action="store_true",
                     help="for `repair`: preview the slug changes without writing the registry or .celebornrc files")
    flp.set_defaults(func=cmd_fleet)

    rp = sub.add_parser("run", help="real-time tracker for ONE multi-agent swarm (the Elves): per-worker heartbeat, progress, and a shared learning blackboard")
    rsub = rp.add_subparsers(dest="run_cmd")
    r_start = rsub.add_parser("start", help="begin a run (clears prior workers + blackboard)")
    r_start.add_argument("run_id", nargs="?", default=None, help="stable run id (default: run-<ts>)")
    r_start.add_argument("--goal", default=None, help="one-line goal of the run")
    r_start.add_argument("--shards", type=int, default=0, help="number of shards/workers")
    r_start.add_argument("--units", type=int, default=0, help="total units of work (e.g. records)")
    r_start.add_argument("--keep", action="store_true", help="do NOT clear prior workers/blackboard")
    r_start.set_defaults(func=cmd_run, run_cmd="start")
    r_beat = rsub.add_parser("beat", help="heartbeat + progress upsert for one worker (call often)")
    r_beat.add_argument("--worker", required=True, help="worker id (e.g. ik_07)")
    r_beat.add_argument("--shard", default=None, help="shard label this worker owns")
    r_beat.add_argument("--phase", default=None, help="phase label (e.g. Crosswalk)")
    r_beat.add_argument("--item", default=None, help="current item being worked")
    r_beat.add_argument("--done", type=int, default=None, help="units completed so far")
    r_beat.add_argument("--total", type=int, default=None, help="units in this worker's shard")
    r_beat.add_argument("--found", type=int, default=None, help="units resolved (a hit)")
    r_beat.add_argument("--missed", type=int, default=None, help="units that resolved to nothing")
    r_beat.add_argument("--source-ok", dest="source_ok", default=None, help="increment ok for a source (e.g. wikidata)")
    r_beat.add_argument("--source-fail", dest="source_fail", default=None, help="increment fail for a source")
    r_beat.add_argument("--source-rl", dest="source_rl", default=None, help="increment rate-limited for a source")
    r_beat.add_argument("--quiet", action="store_true", help="suppress the per-beat echo")
    r_beat.set_defaults(func=cmd_run, run_cmd="beat")
    for _act, _help in (("done", "mark a worker finished"), ("fail", "mark a worker failed")):
        _rp = rsub.add_parser(_act, help=_help)
        _rp.add_argument("--worker", required=True, help="worker id")
        _rp.add_argument("--found", type=int, default=None)
        _rp.add_argument("--missed", type=int, default=None)
        _rp.add_argument("--done", type=int, default=None)
        _rp.add_argument("--total", type=int, default=None)
        if _act == "fail":
            _rp.add_argument("--error", default=None, help="failure reason")
        _rp.set_defaults(func=cmd_run, run_cmd=_act)
    r_learn = rsub.add_parser("learn", help="append a deduped lesson to the shared blackboard")
    r_learn.add_argument("lesson", help="the lesson (short, reusable)")
    r_learn.add_argument("--worker", default=None, help="who learned it")
    r_learn.add_argument("--quiet", action="store_true")
    r_learn.set_defaults(func=cmd_run, run_cmd="learn")
    r_lrn = rsub.add_parser("learnings", help="print recent blackboard lessons (elves read this at shard-start)")
    r_lrn.add_argument("-n", "--limit", type=int, default=30)
    r_lrn.add_argument("--json", action="store_true")
    r_lrn.set_defaults(func=cmd_run, run_cmd="learnings")
    r_st = rsub.add_parser("status", help="print the live run snapshot (also the default)")
    r_st.add_argument("--json", action="store_true", help="emit the snapshot as JSON (for the board)")
    r_st.set_defaults(func=cmd_run, run_cmd="status")
    r_w = rsub.add_parser("watch", help="live-refreshing terminal dashboard until all workers finish")
    r_w.add_argument("--interval", type=float, default=2.0)
    r_w.set_defaults(func=cmd_run, run_cmd="watch")
    rp.set_defaults(func=cmd_run, run_cmd=None)

    fp = sub.add_parser("flex", help="the shareable 🏹💪 '$ Wrapped' brag card (tokens→$ saved + restarts avoided)")
    fp.add_argument("--tweet", action="store_true", help="emit a ≤280-char build-in-public X post instead of the card")
    fp.add_argument("--json", action="store_true", help="emit the raw figures as JSON")
    fp.set_defaults(func=cmd_flex)

    sv = sub.add_parser("savings", help="running savings totals (this project + whole fleet) — the board's economy bar (t68)")
    sv.add_argument("--json", action="store_true", help="emit {generated_at, project, fleet} as JSON")
    sv.set_defaults(func=cmd_savings)

    bp = sub.add_parser("blame", help='git blame for the "why" — commits on a file + linked Celeborn memory')
    bp.add_argument("path_arg", metavar="file", help="repo-relative path (or absolute path inside the project)")
    bp.add_argument("-n", "--limit", type=int, default=8, help="max git commits to show (default 8)")
    bp.add_argument("--memory", type=int, default=5, help="max memory sections to show (default 5)")
    bp.add_argument("--json", action="store_true", help="emit {file, commits, memory} as JSON")
    bp.set_defaults(func=cmd_blame)

    wp = sub.add_parser("why", help='decision archaeology — why "<topic>"? (decision + date + rationale)')
    wp.add_argument("query", metavar="topic", help="topic / keyword to recall the reasoning for")
    wp.add_argument("-n", "--limit", type=int, default=None, help="max results (default 5)")
    wp.add_argument("--json", action="store_true", help="emit {query, hits} as JSON")
    wp.set_defaults(func=cmd_why)

    tch = sub.add_parser("touch", help="register who is editing which file (multi-agent; see references/multi-agent-editing.md)")
    tch.add_argument("words", nargs="*", metavar="file|command",
                     help="<file> to register; or: list | clear | release <file>")
    tch.add_argument("--by", default=None, help="agent id (default: $CELEBORN_AGENT)")
    tch.add_argument("--task", default=None, help="kanban task id (e.g. t28)")
    tch.add_argument("--why", default=None, help="short reason you're editing this file (shown on orient)")
    tch.add_argument("--family", default=None, help="agent family override (else `celeborn identify` / $CELEBORN_AGENT_FAMILY)")
    tch.add_argument("--model", default=None, help="specific model override (else `celeborn identify` / $CELEBORN_AGENT_MODEL)")
    tch.add_argument("--json", action="store_true", help="JSON output (list)")
    tch.add_argument("--force", action="store_true", help="release even if another agent owns the touch")
    tch.set_defaults(func=cmd_touch)

    itp = sub.add_parser("intent", help="declare a planned commit on the fleet blackboard "
                                        "(third channel: touch=where, board=what, intent=about-to; "
                                        "peers editing the same files are warned before they commit)")
    itp.add_argument("words", nargs="*", metavar="what|command",
                     help='"<what you will commit>" (declaring REQUIRES --task); or: list | done | clear')
    itp.add_argument("--files", default=None,
                     help="comma-separated paths the commit will touch (default: your active touches for --task)")
    itp.add_argument("--task", default=None, help="kanban card id this commit is for — REQUIRED to declare, must be a real card (e.g. t42)")
    itp.add_argument("--eta", default=None, help="how soon you expect to commit (e.g. 20, 45m, 1h) — shown to peers")
    itp.add_argument("--session", default=None, help="session id owning this intent (default: the ambient CLAUDE_CODE_SESSION_ID)")
    itp.add_argument("--all-agents", dest="all_agents", action="store_true",
                     help="clear: wipe EVERY agent's intent for this project (default clear releases only your own)")
    itp.add_argument("--by", default=None, help="agent id for a session-less manual run (a live session always wins)")
    itp.add_argument("--family", default=None, help="agent family override (else `celeborn identify` / $CELEBORN_AGENT_FAMILY)")
    itp.add_argument("--model", default=None, help="specific model override (else `celeborn identify` / $CELEBORN_AGENT_MODEL)")
    itp.add_argument("--all", action="store_true", help="list: show intents machine-wide, not just this project")
    itp.add_argument("--json", action="store_true", help="JSON output (list)")
    itp.set_defaults(func=cmd_intent)

    rmp = sub.add_parser("remind", help="reassuring checkpoint-and-renew reminder (host supplies --tokens)")
    rmp.add_argument("--tokens", type=int, default=None, help="current context size in tokens (the host supplies this)")
    rmp.add_argument("--every", type=int, default=100_000, help="reminder increment in tokens (default 100k)")
    rmp.add_argument("--last", type=int, default=None, help="token count at last reminder; stay silent unless a new increment is crossed")
    rmp.add_argument("--auto", action="store_true", help="use Celeborn's own rolling context estimate (metrics.context_estimate) instead of --tokens; tracks its own last-reminded mark")
    rmp.add_argument("--transcript", default=None, help="path to a Claude Code transcript (JSONL); read the live context size from its latest usage record. Overrides --tokens/--auto and persists the reading to metrics")
    rmp.add_argument("--soft-limit", type=int, default=None, help="soft context-pressure threshold in tokens; crossing it speaks a ⚠ warning (default: context_soft_tokens in .celebornrc, else 100000)")
    rmp.add_argument("--hard-limit", type=int, default=None, help="hard context-pressure threshold in tokens; crossing it speaks an urgent ⛔ stop-and-checkpoint warning (default: context_hard_tokens in .celebornrc, else 125000)")
    rmp.add_argument("--session", default=None, help="read the live window from this session's capture cursor (`record tokens`, transcript-less harnesses like OpenCode); tracks its own last-reminded mark")
    rmp.add_argument("--clear-cmd", default=None, help="host-specific clear instruction to display")
    rmp.add_argument("--force", action="store_true", help="print even if no new increment was crossed")
    rmp.set_defaults(func=cmd_remind)

    psp = sub.add_parser("panic-save", help="snapshot the authored tiers to a restore point + print a visible '🏹 Celeborn saved your session' (runs automatically pre-compaction)")
    psp.add_argument("--reason", default="manual", help="why the save fired (compaction / alarm / manual); recorded in the snapshot meta.json")
    psp.add_argument("--session", default=None, help="session id to record in the snapshot meta")
    psp.add_argument("--keep", type=int, default=PANIC_KEEP, help=f"FIFO retention: keep the most recent N snapshots (default {PANIC_KEEP})")
    psp.add_argument("--quiet", action="store_true", help="save but print nothing")
    psp.add_argument("--json", action="store_true", help="print the snapshot record as JSON")
    psp.set_defaults(func=cmd_panic_save)

    rsp = sub.add_parser("restore", help="bring back a pre-compaction panic-save (most recent by default); current state is backed up first")
    rsp.add_argument("--from", dest="from_", default=None, help="restore a specific snapshot by stamp (see --list); default is the most recent")
    rsp.add_argument("--list", action="store_true", help="list available panic-saves (newest first) instead of restoring")
    rsp.add_argument("--keep", type=int, default=PANIC_KEEP, help=f"FIFO retention for the pre-restore backup (default {PANIC_KEEP})")
    rsp.add_argument("--json", action="store_true", help="print the restore result as JSON")
    rsp.set_defaults(func=cmd_restore)

    cap = sub.add_parser("capture", help="mechanically ingest a Claude Code transcript into the local Automatic Context Record (no model)")
    cap.add_argument("--transcript", required=True, help="path to the Claude Code transcript JSONL")
    cap.add_argument("--session", default=None, help="session id (from the Stop-hook stdin); cursor resets when it changes")
    cap.add_argument("--quiet", action="store_true", help="suppress the summary line (for hooks)")
    cap.add_argument("--global", dest="global_", action="store_true",
                     help="force the global ~/.context sink even inside a repo (the hybrid fallback "
                          "used for sessions run outside any .context/ repo)")
    cap.add_argument("--note", action="store_true",
                     help="also print a per-turn `{\"systemMessage\": ...}` heartbeat (for the Stop "
                          "hook): kept unique each turn (growing session total, or 'idle ×K') so "
                          "Claude Code can't suppress it as a duplicate. Terminal-only — see `heartbeat`.")
    cap.set_defaults(func=cmd_capture)

    hbp = sub.add_parser("heartbeat", help="print the per-turn capture heartbeat to plain stdout "
                                           "(for the UserPromptSubmit hook; visible in the Claude app)")
    hbp.add_argument("--session", default=None, help="session id (from the hook stdin); reads that "
                                                     "session's cursor instead of the most-recent one")
    hbp.set_defaults(func=cmd_heartbeat)

    slp = sub.add_parser("statusline", help="render Celeborn's Claude Code statusLine (persistent, "
                                            "can't be suppressed like a hook systemMessage)")
    slp.add_argument("--transcript", default=None, help="transcript JSONL; adds the live context size")
    slp.add_argument("--session", default=None, help="session id (from the hook stdin); reads that "
                                                     "session's cursor instead of the most-recent one")
    slp.set_defaults(func=cmd_statusline)

    wp = sub.add_parser("wire", help="merge Celeborn's `celeborn hook <event>` hooks + statusLine into a "
                                     "Claude Code settings.json (idempotent; migrates a legacy bash install)")
    wp.add_argument("--global", dest="global_", action="store_true",
                    help="write ~/.claude/settings.json (every session) instead of the project's .claude/settings.json")
    wp.add_argument("--force", action="store_true", help="replace an existing non-Celeborn statusLine")
    wp.add_argument("--no-permission-baseline", dest="no_permission_baseline", action="store_true",
                    help="with --global: do NOT merge the safe read-only permission baseline (the "
                         "'big three') into ~/.claude/settings.json")
    wp.add_argument("--no-skills", dest="no_skills", action="store_true",
                    help="with --global: do NOT install the Matt Pocock skill suite (on by default)")
    wp.add_argument("--grok", action="store_true",
                    help="also wire Grok Build hooks + .grok/rules/celeborn.md for this project")
    wp.add_argument("--opencode", action="store_true",
                    help="also install the OpenCode wiring (event plugin + Qwen-4b PM agent + "
                         "provider block) into this project's .opencode/ + opencode.json")
    wp.set_defaults(func=cmd_wire)

    # CELE-t228 — `init` is THE first-run command: one guided everything-command over wire + scaffold +
    # login (renamed from the old `setup`; `setup` stays a hidden back-compat alias, registered below).
    stp = sub.add_parser("init", help="THE first-run command: wire Claude Code + scaffold this project's "
                                      "private memory + sign in + open your board. Idempotent — re-run to resume.")
    stp.add_argument("--github", action="store_true",
                     help="sign in with GitHub (the default) — enables private cross-device sync of your "
                          ".context/ via your free account. `--no-login` skips sign-in entirely.")
    stp.add_argument("--project", action="store_true",
                     help="wire the project's .claude/settings.json instead of ~/.claude (the default is a "
                          "global wire, so every session is covered)")
    stp.add_argument("--force", action="store_true", help="replace an existing non-Celeborn statusLine when wiring")
    stp.add_argument("--no-permission-baseline", dest="no_permission_baseline", action="store_true",
                     help="don't merge the safe read-only permission baseline (passed through to `wire`)")
    stp.add_argument("--no-skills", dest="no_skills", action="store_true",
                     help="don't install the Matt Pocock skill suite (passed through to `wire`)")
    stp.add_argument("--no-init", dest="no_init", action="store_true",
                     help="skip the per-project scaffold step (wire/sign-in only — e.g. a machine with no project yet)")
    stp.add_argument("--no-cmm", dest="no_cmm", action="store_true",
                     help="don't auto-engage Codebase Memory for this project (passed through to `scaffold`)")
    stp.add_argument("--name", dest="name", default=None,
                     help="name this project for the board, skipping the prompt (passed through to `scaffold`)")
    stp.add_argument("--no-open", dest="no_open", action="store_true",
                     help="don't launch/open the kanban board after scaffolding (passed through to `scaffold`)")
    stp.add_argument("--no-browser", dest="no_browser", action="store_true",
                     help="start the board but don't pop a browser tab (passed through to `scaffold`)")
    stp.add_argument("--no-weave", dest="no_weave", action="store_true",
                     help="skip the local-engine weave step (OpenCode + Ollama + Pippin, CELE-t374). "
                          "The weave is consent-gated anyway; this suppresses even the offer.")
    stp.add_argument("--no-orientation", dest="no_orientation", action="store_true",
                     help="skip the first-run Orientation tutorial project (ORIE, CELE-t387). By default "
                          "a fresh install creates a dedicated onboarding board so you don't land on an "
                          "empty one; this opts out.")
    stp.add_argument("--no-login", dest="no_login", action="store_true",
                     help="skip the sign-in step. Login is on by default (Modal parity); this is the "
                          "documented opt-out for a purely local-first install.")
    stp.add_argument("--email", help="sign in with email + password instead of the GitHub browser flow")
    stp.set_defaults(func=cmd_init)
    # Hidden back-compat alias: `celeborn setup` routes to the same everything-command as `init`,
    # without appearing in --help (CELE-t228 — agents used to flip a coin between init and setup).
    sub._name_parser_map["setup"] = stp

    wq = sub.add_parser("wire-quality", help="opt-in deterministic quality gates: auto-test-on-edit + "
                                             "board `tsc --noEmit` (PostToolUse + Stop hooks; AGENTS.md fallback)")
    wq.add_argument("--local", action="store_true",
                    help="write the personal settings.local.json instead of the shared settings.json")
    wq.set_defaults(func=cmd_wire_quality)

    gkp = sub.add_parser("grok", help="Grok Build integration — wire hooks + per-project orient rules")
    gkp.add_argument("grok_action", nargs="?", default="wire", metavar="wire|sync-rules",
                     help="wire = install hooks + bootstrap; sync-rules = refresh .grok/rules/celeborn.md")
    gkp.set_defaults(func=cmd_grok)

    ocp = sub.add_parser("opencode", help="OpenCode integration — wire the plugin/PM agent (CELE-t204), "
                                          "or inspect/steer the engine from Settings (CELE-t352)")
    ocp.add_argument("opencode_action", nargs="?", default="wire", metavar="wire|status|set",
                     help="wire = install/refresh .opencode/{plugin,agent}/ + merge opencode.json; "
                          "status = live engine state; set = persist an engine setting")
    ocp.add_argument("--json", action="store_true", help="JSON output (status/set)")
    ocp.add_argument("--no-probe", action="store_true",
                     help="status: skip the live `opencode serve` network probe (config only)")
    ocp.add_argument("--default-model", metavar="ID", help="set: model a fresh session starts with")
    ocp.add_argument("--serve-url", metavar="URL", help="set: `opencode serve` REST base")
    ocp.add_argument("--compaction-hijack", choices=("on", "off"), dest="compaction_hijack",
                     help="set: replace OpenCode's blind summary with the Hot tier (CELE-t142)")
    ocp.add_argument("--card-gate", choices=("on", "off"), dest="card_gate",
                     help="set: deny writes/research without a claimed card (CELE-t131/t140)")
    ocp.set_defaults(func=cmd_opencode)

    olp = sub.add_parser("ollama", help="Local Ollama daemon — installed models, keep-alive, pull/rm "
                                        "(runs the Qwen-4b PM + night-run local models, CELE-t352)")
    olp.add_argument("ollama_action", nargs="?", default="status", metavar="status|pull|rm|set",
                     help="status = live daemon state; pull/rm <model>; set --host/--keep-alive")
    olp.add_argument("model", nargs="?", help="model tag for pull/rm (e.g. qwen3:8b)")
    olp.add_argument("--json", action="store_true", help="JSON output")
    olp.add_argument("--host", metavar="URL", help="set: Ollama daemon base URL")
    olp.add_argument("--keep-alive", metavar="MIN", type=int, dest="keep_alive",
                     help="set: minutes a model stays warm after its last call")
    olp.set_defaults(func=cmd_ollama)

    wvp = sub.add_parser("weave", help="the sovereign weave: detect/install OpenCode + Ollama from "
                                       "their official upstream channels, pull Pippin (Qwen3-4b), "
                                       "wire plugin + config — a free local agent stack (CELE-t374)")
    wvp.add_argument("weave_action", nargs="?", default="install", metavar="install|status",
                     help="install (default) = idempotent detect → consented upstream install → "
                          "Pippin pull → wire + config merge; status = pure read of every component "
                          "against the pins (references/weave-pin.json)")
    wvp.add_argument("--yes", action="store_true",
                     help="pre-consent to the official upstream installers (scripted installs; "
                          "interactive runs are prompted per component)")
    wvp.add_argument("--no-models", dest="no_models", action="store_true",
                     help="skip the ~2.5 GB Pippin model pulls (binaries + wiring only)")
    wvp.add_argument("--json", action="store_true", help="status: JSON output")
    wvp.set_defaults(func=cmd_weave)

    erp = sub.add_parser("engine-room", help="the Engine Room: start/stop/restart/health for the two "
                                             "local engines (Local Code + Local Model) the weave installs")
    erp.add_argument("engine_action", nargs="?", default="status", metavar="status|up|down|restart",
                     help="status (default) = Scotty-style health of both engines; up/down/restart = "
                          "lifecycle (Celeborn only ever touches a process it started itself)")
    erp.add_argument("target", nargs="?", default="all", metavar="code|model|all",
                     help="which engine to act on (default: all)")
    erp.add_argument("--json", action="store_true", help="JSON output (board Settings + Stage)")
    erp.set_defaults(func=cmd_engine_room)

    hp = sub.add_parser("hook", help="in-process Claude Code hook entry point (reads the event JSON on "
                                     "stdin); what `wire` points every hook at")
    hp.add_argument("event", choices=list(HOOK_EVENTS),
                    help="which hook event to run: " + ", ".join(HOOK_EVENTS))
    hp.add_argument("--harness", default=None,
                    help="translate the stdin payload from this harness's native shape before "
                         "dispatch (currently: opencode); default: Claude-shaped, or $CELEBORN_HARNESS")
    hp.set_defaults(func=cmd_hook)

    cp = sub.add_parser("consent", help="review the click-reducing automations (all opt-out) + record "
                                        "your agreement to the Celeborn User Agreement")
    cp.add_argument("--name", help="your full name — records agreement non-interactively")
    cp.add_argument("--opt-out", dest="opt_out",
                    help="comma-separated item numbers or keys to DISABLE (e.g. '5' or 'cd-redirect-autoallow')")
    cp.add_argument("--yes", action="store_true",
                    help="skip the interactive opt-out prompt (keep all enabled unless --opt-out given)")
    cp.add_argument("--show", action="store_true", help="print the recorded consent and exit")
    cp.set_defaults(func=cmd_consent)

    # Account + premium Supabase-backed sync (Phase 8b). Lazily imported so the core stays
    # network-free. Identity is Supabase Auth (email+password, TOTP MFA, GitHub OAuth); the free
    # account is OPTIONAL — the local core never needs it.
    rgp = sub.add_parser("register", help="create a free Celeborn account (email + password + username)")
    rgp.add_argument("--email", help="account email (prompted if omitted)")
    rgp.add_argument("--username", help="display username (prompted if omitted)")
    rgp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_register(a))

    lgp = sub.add_parser("login", help="sign in (email+password +TOTP, or --github) to enable hosted sync")
    lgp.add_argument("--github", action="store_true", help="sign in with GitHub (browser PKCE) instead of a password")
    lgp.add_argument("--email", help="account email (prompted if omitted)")
    lgp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_login(a))

    sub.add_parser("logout", help="revoke the session and delete local credentials").set_defaults(
        func=lambda a: __import__("celeborn_sync").cmd_logout(a))
    whp = sub.add_parser("whoami", help="show the signed-in account (email, username, MFA, tier)")
    whp.add_argument("--json", action="store_true", help="emit a thin identity snapshot for the board Settings Account section")
    whp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_whoami(a))
    acp = sub.add_parser("account", help="account: show identity (default), or `migrate` to heal a CLI/GitHub split")
    acp.add_argument("--json", action="store_true", help="emit a thin identity snapshot for the board Settings Account section")
    acp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_whoami(a))
    acsub = acp.add_subparsers(dest="account_cmd")
    amp = acsub.add_parser("migrate",
                           help="move hosted projects from an old account into the one you're signed in as (CELE-t107)")
    amp.add_argument("--email", help="source (old) account email; prompted if omitted")
    amp.add_argument("--yes", action="store_true", help="skip the 'not signed in with GitHub' confirmation")
    amp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_account_migrate(a))

    upg = sub.add_parser("upgrade", help="start a Celeborn Pro/Team subscription (opens Stripe Checkout)")
    upg.add_argument("--tier", choices=["pro", "team"], default="pro", help="plan to subscribe to (default: pro)")
    upg.add_argument("--annual", action="store_true", help="bill annually (≈2 months free) instead of monthly")
    upg.add_argument("--seats", type=int, default=1, help="number of seats to purchase (default: 1)")
    upg.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_upgrade(a))

    sub.add_parser("billing", help="manage your subscription (opens the Stripe billing portal)").set_defaults(
        func=lambda a: __import__("celeborn_sync").cmd_billing(a))

    mfp = sub.add_parser("mfa", help="manage TOTP MFA (Google Authenticator)")
    mfp.add_argument("action", nargs="?", default="status", choices=["enroll", "status", "disable"],
                     help="enroll a new TOTP factor, show status, or disable (default: status)")
    mfp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_mfa(a))

    syp = sub.add_parser("sync", help="push/pull .context/ to the hosted (Supabase) backend")
    syp.add_argument("--watch", action="store_true", help="keep syncing on an interval instead of once")
    syp.add_argument("--interval", type=int, default=5, help="seconds between syncs in --watch mode (default 5)")
    syp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_sync(a))

    # config (CELE-t355): the read/write seam behind the board Settings sections (context bands, fleet
    # liveness TTLs, board display toggles, hosted-sync + integration flags). Project keys land in
    # .celebornrc; machine-global keys (--fleet) land in ~/.config/celeborn/fleet.json's settings block.
    cfp = sub.add_parser("config", help="read/write board Settings knobs (.celebornrc + fleet.json)")
    cfp.add_argument("--json", action="store_true", help="emit the resolved settings state as JSON (for the board)")
    cfp.set_defaults(func=lambda a: __import__("celeborn_config").cmd_config(a))
    cfsub = cfp.add_subparsers(dest="config_cmd")
    cfset = cfsub.add_parser("set", help="write one key: celeborn config set <key> <value> [--fleet]")
    cfset.add_argument("key", help="settings key (see `celeborn config` for the list)")
    cfset.add_argument("value", help="new value (bool: true/false · int · str · int_list: '50,75,100,125')")
    cfset.add_argument("--fleet", action="store_true", help="write to the machine-global fleet.json settings (for fleet-scoped keys)")
    cfset.add_argument("--json", action="store_true", help="emit the write report as JSON")
    cfset.set_defaults(func=lambda a: __import__("celeborn_config").cmd_config(a))

    # Architecture (CELE-t187): capture NON-SECRET infrastructure topology (vendor names, IPs,
    # control-surface URLs, DB endpoints) into .context/infra-local.json (gitignored). init/show are
    # local; sync pushes the topology (credentials stripped) to the hosted architecture diagram (Pro).
    arp = sub.add_parser("architecture",
        help="capture infrastructure topology for the hosted architecture diagram")
    arsub = arp.add_subparsers(dest="arch_cmd")
    ari = arsub.add_parser("init", help="scaffold .context/infra-local.json (auto-detects vendors)")
    ari.add_argument("--force", action="store_true", help="overwrite an existing infra-local.json")
    ari.set_defaults(func=cmd_architecture)
    arsub.add_parser("show", help="print the captured topology + control-surface links").set_defaults(
        func=cmd_architecture)
    arsub.add_parser("sync", help="push the topology (credentials stripped) to the hosted board").set_defaults(
        func=lambda a: __import__("celeborn_sync").cmd_architecture_sync(a))
    arsub.add_parser("trace", help="re-detect the stack + additively merge new pieces, then remap the hosted "
                                   "Stack (runs automatically every 3 turns + on a dependency-manifest edit)").set_defaults(
        func=cmd_architecture)
    arp.set_defaults(func=cmd_architecture, arch_cmd=None)  # bare `architecture` → show

    # Product federation registry (CELE-t190, Layer A of CELE-t188): name the repo-facets of one product,
    # their roles + publish policy (committed product.md) and this machine's checkout paths (gitignored
    # product-local.json). The orient banner reads it; Layers B/C/D build on it.
    pdp = sub.add_parser("product",
        help="product federation registry — repo-facets, roles, publish policy + orient banner (CELE-t190)")
    pdsub = pdp.add_subparsers(dest="product_cmd")
    pdsub.add_parser("list", help="print the facet table (roles · publish · bound/unbound here)").set_defaults(
        func=cmd_product, product_cmd="list")
    pdi = pdsub.add_parser("init", help="scaffold .context/product.md (the committed registry)")
    pdi.add_argument("--name", default=None, help="product name (default: project name / folder)")
    pdi.add_argument("--force", action="store_true", help="overwrite an existing product.md")
    pdi.set_defaults(func=cmd_product, product_cmd="init")
    pda = pdsub.add_parser("add", help="add or update a facet in product.md (product FACTS only, no paths)")
    pda.add_argument("key", help="facet key (e.g. client, server)")
    pda.add_argument("--role", required=True, choices=list(PRODUCT_ROLES), help="facet role")
    pda.add_argument("--publish", default=None, help="publish policy (e.g. never, pypi, fork+PR)")
    pda.add_argument("--repo", default=None, help="canonical remote URL (portable — no local checkout path)")
    pda.add_argument("--upstream", default=None, help="upstream remote (for oss:* facets)")
    pda.set_defaults(func=cmd_product, product_cmd="add")
    pdb = pdsub.add_parser("bind", help="bind a facet → this machine's checkout path (gitignored, per-machine)")
    pdb.add_argument("key", help="facet key to bind")
    pdb.add_argument("checkout", help="absolute path to the local checkout on this machine")
    pdb.set_defaults(func=cmd_product, product_cmd="bind")
    pdp.set_defaults(func=cmd_product, product_cmd=None)  # bare `product` → list

    # Multi-repo git/PR ops (CELE-t191, Layer B of CELE-t188): route git + a drafted `gh pr create` to a
    # bound facet checkout, auto-attributing each op (touch + Celeborn-Task/-Agent/-Model trailers). The
    # publish guard (PreToolUse) enforces role policy; these route. All read the Layer A registry (t190).
    cmp_ = sub.add_parser("commit",
        help="facet-routed git commit into a bound checkout, with auto touch + trailers (CELE-t191)")
    cmp_.add_argument("--facet", required=True, help="facet key to route the commit to (must be bound here)")
    cmp_.add_argument("-m", "--message", required=True,
                      help="commit message (Celeborn-Task/-Agent/-Model trailers appended automatically)")
    cmp_.add_argument("--task", default=None,
                      help="task id for the Celeborn-Task trailer (default: this session's doing card)")
    cmp_.add_argument("--by", default=None, help="agent handle (default: this session)")
    cmp_.add_argument("--family", default=None, help="agent family for attribution (e.g. Claude)")
    cmp_.add_argument("--model", default=None, help="agent model for attribution (e.g. Opus 4.8)")
    cmp_.add_argument("--session", default=None, help=argparse.SUPPRESS)
    cmp_.add_argument("files", nargs="*",
                      help="files to stage+commit (paths relative to the facet checkout); omit to commit staged")
    cmp_.set_defaults(func=cmd_commit)

    psp_ = sub.add_parser("push",
        help="facet-routed git push to a bound checkout (release/tag push guarded by role) (CELE-t191)")
    psp_.add_argument("--facet", required=True, help="facet key to route the push to (must be bound here)")
    psp_.add_argument("remote", nargs="?", default=None, help="git remote (e.g. origin); default: git's default")
    psp_.add_argument("branch", nargs="?", default=None, help="branch/refspec to push; default: git's default")
    psp_.add_argument("-u", "--set-upstream", dest="set_upstream", action="store_true",
                      help="pass --set-upstream to git push")
    psp_.add_argument("--tags", action="store_true",
                      help="push tags too (a RELEASE push — refused on server:private/oss:* facets)")
    psp_.add_argument("--follow-tags", dest="follow_tags", action="store_true",
                      help="pass --follow-tags (a RELEASE push — refused on server:private/oss:* facets)")
    psp_.set_defaults(func=cmd_push)

    prd_ = sub.add_parser("pr",
        help="DRAFT a facet-routed pull request (prints a gh command; never auto-sends) (CELE-t191)")
    prd_.add_argument("--facet", required=True, help="facet key to route the PR to (must be bound here)")
    prd_.add_argument("--base", default=None, help="base branch for the PR (default: main)")
    prd_.add_argument("--title", default=None, help="PR title (default: the top commit subject)")
    prd_.add_argument("--body", default=None, help="PR body (default: a bullet list of the commits)")
    prd_.add_argument("--task", default=None, help="task id for the PR provenance (default: session's doing card)")
    prd_.add_argument("--by", default=None, help="agent handle for the drafted-by line (default: session)")
    prd_.add_argument("--family", default=None, help=argparse.SUPPRESS)
    prd_.add_argument("--model", default=None, help=argparse.SUPPRESS)
    prd_.add_argument("--session", default=None, help=argparse.SUPPRESS)
    prd_.set_defaults(func=cmd_pr)

    # Manage hosted projects on celeborncode.ai (t97): list them, or remove one (incl. an orphan whose
    # repo was deleted — removal is hosted-only, no local .context/ needed). RM cascades server-side.
    prp = sub.add_parser("project", help="manage hosted projects (list / remove) on celeborncode.ai")
    prsub = prp.add_subparsers(dest="project_cmd")
    prsub.add_parser("list", help="list your hosted projects (name · id)").set_defaults(
        func=lambda a: __import__("celeborn_sync").cmd_project(a))
    prr = prsub.add_parser("rm", help="remove a hosted project by name or id (cascades; PERMANENT)")
    prr.add_argument("name", metavar="name|id", help="project name (exact) or its uuid")
    prr.add_argument("--yes", action="store_true", help="skip the type-the-name confirmation")
    prr.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_project(a))
    prp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_project(a))  # bare `project` → list

    # Hidden: best-effort live push of changed cards to the hosted board, spawned detached by a local
    # task mutation so celeborncode.ai updates in ~realtime. Not for direct use.
    hpp = sub.add_parser("hosted-push", help=argparse.SUPPRESS)
    hpp.add_argument("--ids", default="", help="comma-separated task ids to push")
    hpp.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_hosted_push(a))

    # Hidden: best-effort live push of the active-agents windows to the hosted board, spawned detached
    # by the per-turn capture so hosted token chips track the live local windows (CELE-t131). Not for
    # direct use.
    hpa = sub.add_parser("hosted-push-agents", help=argparse.SUPPRESS)
    hpa.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_hosted_push_agents(a))

    # GitHub App: bind a repo so the App ingests its PR/issue threads (capture free; pull = Pro sync).
    ghp = sub.add_parser("github", help="GitHub integration: App ingest (link) + board→Issues mirror (CELE-t214)")
    ghsub = ghp.add_subparsers(dest="github_cmd", required=True)
    ghl = ghsub.add_parser("link", help="link <owner/repo> to this project for GitHub App ingest")
    ghl.add_argument("repo", metavar="owner/repo", help="GitHub repository as <owner>/<repo>")
    ghl.add_argument("--installation", help="App installation id (shown on the App's post-install page)")
    ghl.set_defaults(func=lambda a: __import__("celeborn_sync").cmd_github_link(a))

    # Board → GitHub Issues mirror (CELE-t214). Operator-credential direct REST API (Bearer token via
    # --token/env/`gh auth token`), separate from the read-only ingest App. See celeborn_github.py.
    ghc = ghsub.add_parser("connect", help="connect a mirror repo (token via --token/CELEBORN_GITHUB_TOKEN/gh)")
    ghc.add_argument("repo", metavar="owner/repo", nargs="?", help="mirror repo as <owner>/<repo> (prompted if omitted)")
    ghc.add_argument("--token", help="GitHub token (prefer CELEBORN_GITHUB_TOKEN env or `gh auth login` — never commit tokens)")
    ghc.add_argument("--json", action="store_true", help="print connection JSON after success")
    ghc.set_defaults(func=lambda a: __import__("celeborn_github").cmd_github(a))
    ghst = ghsub.add_parser("status", help="verify the stored GitHub mirror connection")
    ghst.add_argument("--json", action="store_true", help="print JSON (for the board API)")
    ghst.set_defaults(func=lambda a: __import__("celeborn_github").cmd_github(a))
    ghrec = ghsub.add_parser("reconcile", help="audit GitHub vs Celeborn (Celeborn wins); --apply pushes outward")
    ghrec.add_argument("--apply", action="store_true", help="push all Celeborn cards → GitHub (no orphan import)")
    ghrec.add_argument("--json", action="store_true", help="print JSON report")
    ghrec.set_defaults(func=lambda a: __import__("celeborn_github").cmd_github(a))
    ghpull = ghsub.add_parser("pull", help="pull GitHub Issues → tasks (idempotent; links via issue number/marker)")
    ghpull.add_argument("--dry-run", dest="dry_run", action="store_true", help="preview without writing tasks.md")
    ghpull.set_defaults(func=lambda a: __import__("celeborn_github").cmd_github(a))
    ghpush = ghsub.add_parser("push", help="push tasks → GitHub Issues (PREVIEW by default; --apply to write)")
    ghpush.add_argument("ids", nargs="*", help="task ids to push (default: all cards)")
    ghpush.add_argument("--apply", action="store_true", help="actually write to GitHub (default is a safe preview)")
    ghpush.set_defaults(func=lambda a: __import__("celeborn_github").cmd_github(a))
    ghflush = ghsub.add_parser("flush", help="drain the auto-push queue now (also runs after capture)")
    ghflush.add_argument("--force", action="store_true", help="ignore per-task debounce")
    ghflush.set_defaults(func=lambda a: __import__("celeborn_github").cmd_github(a))

    # Bidirectional Jira Cloud integration (Phase 10/11). Lazily imported; transport is the Jira REST
    # API + an API token (works headless, unlike the OAuth MCP server). See celeborn_jira.py.
    jp = sub.add_parser("jira", help="bidirectional Jira Cloud sync (issues ↔ tasks/phases)")
    jsub = jp.add_subparsers(dest="jira_cmd", required=True)
    jc = jsub.add_parser("connect", help="connect a Jira Cloud site (hidden API-token prompt)")
    jc.add_argument("--site", help="https://yourname.atlassian.net (prompted if omitted)")
    jc.add_argument("--email", help="Atlassian account email (prompted if omitted)")
    jc.add_argument("--project", help="project key to sync, e.g. CEL (prompted if omitted)")
    jc.add_argument("--token", help="API token (prefer CELEBORN_JIRA_TOKEN env — never commit tokens)")
    jc.add_argument("--json", action="store_true", help="print connection JSON after success")
    jc.set_defaults(func=lambda a: __import__("celeborn_jira").cmd_jira(a))
    js = jsub.add_parser("status", help="verify the stored Jira connection")
    js.add_argument("--json", action="store_true", help="print JSON (for the board API)")
    js.set_defaults(func=lambda a: __import__("celeborn_jira").cmd_jira(a))
    jrec = jsub.add_parser("reconcile", help="audit Jira vs Celeborn (Celeborn wins); --apply pushes outward")
    jrec.add_argument("--apply", action="store_true", help="push all Celeborn cards → Jira (no orphan import)")
    jrec.add_argument("--json", action="store_true", help="print JSON report")
    jrec.set_defaults(func=lambda a: __import__("celeborn_jira").cmd_jira(a))
    jpull = jsub.add_parser("pull", help="pull Jira issues → tasks (idempotent; links via the issue key)")
    jpull.add_argument("--dry-run", dest="dry_run", action="store_true", help="preview without writing tasks.md")
    jpull.set_defaults(func=lambda a: __import__("celeborn_jira").cmd_jira(a))
    jpush = jsub.add_parser("push", help="push tasks → Jira (PREVIEW by default; --apply to write)")
    jpush.add_argument("ids", nargs="*", help="task ids to push (default: all cards)")
    jpush.add_argument("--apply", action="store_true", help="actually write to Jira (default is a safe preview)")
    jpush.add_argument("--type", help="issue type for NEW issues (default: Task)")
    jpush.add_argument("--sprint", help="where new issues land: active | backlog | <sprint-id> (default: active)")
    jpush.set_defaults(func=lambda a: __import__("celeborn_jira").cmd_jira(a))
    jflush = jsub.add_parser("flush", help="drain the auto-push queue now (also runs after capture)")
    jflush.add_argument("--force", action="store_true", help="ignore per-task debounce")
    jflush.set_defaults(func=lambda a: __import__("celeborn_jira").cmd_jira(a))

    # codebase-memory-mcp (CMM) integration (Sprint 1 "Zero Prompts"). Lazily imported; all glue
    # lives in celeborn_cmm.py (interface-level, depends on CMM's public surface, never internals).
    cmp_ = sub.add_parser("cmm", help="codebase-memory-mcp: pre-clear permissions + engage structural memory")
    cmsub = cmp_.add_subparsers(dest="cmm_cmd", required=True)
    cme = cmsub.add_parser("engage", help="pre-clear CMM's read-only tools, register MCP, index, install the flow-first North Star")
    cme.add_argument("--global", dest="global_", action="store_true",
                     help="write the allow-list to ~/.claude/settings.json (default: project .claude/settings.json)")
    cme.add_argument("--force", action="store_true", help="re-engage even if this project is opted out")
    cme.add_argument("--no-provision", dest="no_provision", action="store_true",
                     help="skip auto-provisioning the pinned CMM binary (S2)")
    cme.set_defaults(func=lambda a: __import__("celeborn_cmm").cmd_cmm(a))
    cmo = cmsub.add_parser("off", help="disengage CMM for this project (revert added entries; sticky opt-out)")
    cmo.add_argument("--global", dest="global_", action="store_true", help="revert the global allow-list")
    cmo.set_defaults(func=lambda a: __import__("celeborn_cmm").cmd_cmm(a))
    cms = cmsub.add_parser("status", help="report engaged/indexed/version/allow-list state")
    cms.add_argument("--global", dest="global_", action="store_true", help="inspect the global allow-list")
    cms.add_argument("--json", action="store_true", help="print JSON")
    cms.set_defaults(func=lambda a: __import__("celeborn_cmm").cmd_cmm(a))
    # S2 "Zero Touch": provisioning + upstream tracking. Glue lives in celeborn_cmm_provision.py.
    cmpv = cmsub.add_parser("provision", help="fetch + checksum-verify + cache the pinned CMM binary (S2)")
    cmpv.add_argument("--force", action="store_true", help="re-download even if a valid cached copy exists")
    cmpv.set_defaults(func=lambda a: __import__("celeborn_cmm_provision").cmd_provision(a))
    cmct = cmsub.add_parser("contract", help="run the CMM interface contract test (14 tools + ids); exits non-zero on drift")
    cmct.add_argument("--json", action="store_true", help="print JSON")
    cmct.set_defaults(func=lambda a: __import__("celeborn_cmm_provision").cmd_contract(a))
    cmsc = cmsub.add_parser("sync-check", help="watch upstream for a newer pinned release; gate it behind the contract test, plan a PR (S2)")
    cmsc.add_argument("--apply", action="store_true", help="execute a green plan as a branch + gh PR (default: dry-run plan)")
    cmsc.set_defaults(func=lambda a: __import__("celeborn_cmm_provision").cmd_sync_check(a))

    # Encrypted secrets manager for Pro (CELE-t224, Infisical). Lazily imported; the whole family is
    # Pro-gated (the vault itself is Infisical's free tier — Pro gates the wrapper + discipline
    # enforcement). CLI-first over the pinned, auto-provisioned `infisical` binary; see celeborn_secrets.py.
    sec = sub.add_parser("secrets", help="Pro: encrypted secrets vault (Infisical) — store keys once, inject at run time")
    secsub = sec.add_subparsers(dest="secrets_cmd", required=True)
    _sec_f = lambda a: __import__("celeborn_secrets").cmd_secrets(a)  # noqa: E731
    sc_setup = secsub.add_parser("setup", help="one-command onboarding: pinned CLI + browser login + hands-off project provisioning")
    sc_setup.add_argument("--host", help="Infisical host for self-hosters (default: Infisical Cloud)")
    sc_setup.add_argument("--project", help="vault project name (default: the repo folder name)")
    sc_setup.set_defaults(func=_sec_f)
    sc_set = secsub.add_parser("set", help="put a secret in the vault (hidden prompt — the value never touches this repo's disk)")
    sc_set.add_argument("name", help="secret name, e.g. ANTHROPIC_API_KEY")
    sc_set.add_argument("--env", help="vault environment (default: rc default_env, usually dev)")
    sc_set.add_argument("--stdin", action="store_true", help="read the value from stdin instead of a prompt (automation)")
    sc_set.set_defaults(func=_sec_f)
    sc_get = secsub.add_parser("get", help="print one secret value (for scripting — prefer `secrets run`)")
    sc_get.add_argument("name")
    sc_get.add_argument("--env", help="vault environment (default: rc default_env)")
    sc_get.set_defaults(func=_sec_f)
    sc_list = secsub.add_parser("list", help="list secret NAMES in the current env (never values)")
    sc_list.add_argument("--env", help="vault environment (default: rc default_env)")
    sc_list.set_defaults(func=_sec_f)
    sc_run = secsub.add_parser("run", help="run a command with vault secrets injected as env vars: secrets run -- <cmd …>")
    sc_run.add_argument("--env", help="vault environment (default: rc default_env)")
    sc_run.add_argument("cmd", nargs=argparse.REMAINDER, metavar="-- <command …>")
    sc_run.set_defaults(func=_sec_f)
    sc_status = secsub.add_parser("status", help="provider, host, project link, login + binary state")
    sc_status.add_argument("--json", action="store_true", help="print JSON (for the board API)")
    sc_status.set_defaults(func=_sec_f)
    sc_doc = secsub.add_parser("doctor", help="secrets-discipline check: live secret values in repo .env files")
    sc_doc.set_defaults(func=_sec_f)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
