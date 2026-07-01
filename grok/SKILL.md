---
name: celeborn-grok
description: >
  Grok Build integration for Celeborn long-term memory. Use when working in Grok Build with a
  .context/ directory, or when the user asks to set up Celeborn hooks for Grok, wire celeborn for
  grok, or fix celeborn capture/remind in Grok sessions. Complements the core celeborn skill.
metadata:
  version: "0.1.0"
  requires: celeborn
---

# Celeborn × Grok Build

This skill bridges Grok Build to the stock `celeborn` CLI. Canonical source in the
Celeborn repo: `grok/` (installed to `~/.grok/skills/celeborn-grok/` by `scripts/install.sh`).
Does **not** modify Celeborn core — see `../grok_handoff.md` for integration plan.

## Setup (automatic on `celeborn init`)

**`celeborn init` wires Grok for you** when `grok` is on PATH and `~/.grok` exists: global hooks,
`.grok/rules/celeborn.md` in the project (Grok auto-loads it), and an orient bootstrap. No separate
step for new projects.

Manual re-wire anytime:

```bash
celeborn grok wire
# or
celeborn wire --grok
```

One-shot install (same as init's wire):

```bash
bash grok/scripts/install.sh --project /path/to/your-project
```

**Launch Grok from the project** so hooks bind to the right `.context/` (not a parent home dir):

```bash
grok --cwd /path/to/your-project
```

**No manual hook reload.** Global hooks in `~/.grok/hooks/` load on every new Grok session. After
`/clear`, SessionStart writes `.context/.grok-orient-pending.md` and UserPromptSubmit nudges the
agent to read it before replying.

Only edge case: wiring **while Grok is already open** on that project — type `/clear` once.

Verify: `python3 ~/.grok/skills/celeborn-grok/scripts/grok_celeborn.py doctor`

## What the adapter does

| Grok hook | Adapter behavior |
|-----------|------------------|
| `SessionStart` | `celeborn record orient` + writes `.context/.grok-orient-pending.md` |
| `UserPromptSubmit` | Reads `signals.json` token count → `celeborn remind`; prints heartbeat |
| `Stop` | Converts `chat_history.jsonl` → Claude JSONL → `celeborn capture` |
| `PreCompact` | Checkpoint reminder + `celeborn record compaction` |
| `SessionEnd` | `celeborn handoff` |

Grok does not inject `SessionStart` stdout into the model. **Every session in a Celeborn project:**

1. If `.context/.grok-orient-pending.md` exists → read it once, orient from that, delete it.
2. Otherwise run `celeborn status`.

`celeborn init` also writes the same orient + **multi-agent kanban** rules into `AGENTS.md` (and
`CLAUDE.md` for Claude Code). Read that block if present — it explains claim-on-receipt and picking
cards that won't interrupt in-flight work.

## Context renewal in Grok

Grok Build supports **`/clear`** (alias for `/new`) — the direct equivalent of Claude Code's `/clear`.
It wipes the conversation; the adapter's `SessionStart` hook re-orients from `.context/` via
`.grok-orient-pending.md`.

When the adapter prints a Celeborn nudge, offer **`/clear`** first. Optionally mention **`/compact`**
as a lighter in-session alternative (compresses history without a full session reset).

## Manual fallback (always works)

```bash
celeborn status
celeborn remind --tokens <from /context command> --every 50000 --clear-cmd /clear
celeborn capture --transcript <converted.jsonl> --session <id>
```

Convert a Grok transcript without hooks:

```bash
python3 ~/.grok/skills/celeborn-grok/scripts/grok_celeborn.py convert \
  ~/.grok/sessions/<encoded-cwd>/<session-id>/chat_history.jsonl \
  --session <session-id>
```

## Claude settings conflict

If `~/.claude/settings.json` still has legacy `$CELEBORN_HOME` hooks, they **fail in Grok**
(`hook not executed: required env var(s) not set`). The Grok hooks in `~/.grok/hooks/celeborn.json`
replace them for Grok Build. Leave Claude settings alone for Claude Code; Grok uses its own hook file.

## Protocol (same as Celeborn)

Follow the core **celeborn** skill for Orient / Checkpoint / Forget / Promote / Handoff.
This skill only covers Grok-specific wiring and the orient-pending file workaround.