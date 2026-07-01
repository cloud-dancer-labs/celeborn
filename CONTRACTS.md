# Celeborn — public contract & stability

Celeborn is open core. Because the client code is readable and installed on your machine, this document
draws the line between the **stable public contract** (what you can build on and what tooling depends
on) and **internals** (which change freely between versions). The integrity self-check
(`celeborn integrity` / `celeborn doctor`) detects in-place edits to the installed CLI; if you need
different behavior, change it through a PR — not by editing your install (see `CONTRIBUTING.md`).

## Stable — the public contract

These are versioned surfaces. We avoid breaking them without a deprecation path.

1. **File formats (the `.context/` tiers + `tasks.md`).** The on-disk layout is the real product and the
   thing other tools (and the hosted server) read:
   - `.context/state.md`, `notes.md`, `journal.md`, `decisions.md`, `learnings.md` — Markdown tiers.
   - `.context/session.json` — `{schema, focus, next_action, branch, status, updated_at, …}`.
   - `.context/tasks.md` — the task board (truth). `## [tN] title` blocks with `state/owner/tags/
     blocked-by/phase/jira/created/updated` fields. `tasks.json` is a **derived, disposable** projection.
   - These formats are **forward-tolerant**: unknown fields are ignored, missing fields default, and a
     malformed file degrades to empty rather than crashing (see "Defensive parsing" below).

2. **CLI verbs.** The documented commands and their core flags — `init`, `status`, `capture`, `record`,
   `search`, `tasks`, `claim`/`ship`, `touch`, `identify`, `panic-save`/`restore`, `doctor`, `wire`,
   `version`, `integrity`, the account/sync verbs — are the public API. Internal helper functions
   (anything `_underscore`-prefixed) are **not** a contract and may change shape at any time.

3. **The hook protocol.** How the CLI talks to Claude Code: the events it wires
   (`SessionStart`/`UserPromptSubmit`/`PreCompact`/`SessionEnd`/`Stop`), the `celeborn hook <event>`
   entrypoint, and the stdout/JSON envelope shape each event emits. Hosts and wrappers can rely on this.

## Internal — not a contract

Free to change between versions, no notice: the module layout and function names inside `celeborn.py` /
`celeborn_sync.py` / `celeborn_jira.py`, the SQLite index schema (it is fully regenerable from the
markdown — never authoritative), the local-only registries (`.context/touches.json`,
`.context/.agents.json` — disposable caches, never authoritative; family/model are also embedded in
each touch record), output phrasing/formatting, and any `_`-prefixed helper.

## Defensive parsing (why partial breakage degrades, not corrupts)

Celeborn reads tolerantly on purpose, so a hand-edited or partially-written file never takes the tool
down:

- JSON reads are wrapped (`try/except json.JSONDecodeError, OSError`) and fall back to a safe default
  (e.g. `{}` / `[]`), never an unhandled crash.
- Schema fields are read with defaults (`.get(...)`, `setdefault`) — missing keys are tolerated and
  unknown keys are ignored, so older and newer files interoperate.
- **Hooks never raise.** Every hook path degrades to silence (a no-op turn) rather than breaking the
  session — one bad read can't wedge your agent loop.

If you extend the formats, preserve these properties: tolerate the unknown, default the missing, and
never let a read of authored memory crash a turn.
