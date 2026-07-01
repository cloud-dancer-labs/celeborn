---
name: celeborn-codex
description: >
  OpenAI Codex CLI integration for Celeborn long-term memory. Use when working in Codex with a
  .context/ directory, or when the user asks to set up Celeborn hooks for Codex, wire celeborn for
  codex, or fix celeborn capture/remind in Codex sessions. Complements the core celeborn skill.
metadata:
  version: "0.1.0"
  requires: celeborn
---

# Celeborn × Codex CLI

This skill bridges the OpenAI **Codex CLI** to the stock `celeborn` CLI. Canonical source in the
Celeborn repo: `codex/` (installed to `~/.codex/skills/celeborn-codex/` by `scripts/install.sh`).
Does **not** modify Celeborn core — it mirrors the Grok bridge (`grok/`) on the harness seam.

## Setup

```bash
bash codex/scripts/install.sh --project /path/to/your-project
```

This installs `~/.codex/hooks/celeborn.json`, writes the managed Celeborn block into the project's
`AGENTS.md` (Codex auto-loads AGENTS.md every session), and bootstraps an orient file.

Re-sync the AGENTS.md block anytime:

```bash
python3 ~/.codex/skills/celeborn-codex/scripts/codex_celeborn.py sync-agents --path .
```

> **Hooks location.** Codex's lifecycle-hooks system uses the same event names as Claude Code
> (`SessionStart` / `UserPromptSubmit` / `Stop` / `PreCompact` / `SessionEnd`). Depending on your
> Codex build, hooks live either in `~/.codex/hooks/*.json` or in a `[hooks]` table in
> `~/.codex/config.toml` — both share the schema in `hooks/celeborn.json`. The **AGENTS.md orient
> block works regardless of hooks**, so memory is never lost even if hooks aren't wired.

Verify: `python3 ~/.codex/skills/celeborn-codex/scripts/codex_celeborn.py doctor`

## What the adapter does

| Codex hook | Adapter behavior |
|-----------|------------------|
| `SessionStart` | `celeborn record orient` + refresh AGENTS.md block + write `.context/.codex-orient-pending.md` (with the advisor recommendation, if any) |
| `UserPromptSubmit` | Read rollout token usage → `celeborn remind`; nudge to read the orient file; heartbeat |
| `Stop` | Convert the rollout `.jsonl` → Claude JSONL → `celeborn capture` |
| `PreCompact` | Checkpoint reminder + `celeborn record compaction` |
| `SessionEnd` | `celeborn handoff` |
| `notify` (legacy) | `agent-turn-complete` → treated as Stop (convert + capture) |

Codex does not inject `SessionStart` stdout into the model. **Every session in a Celeborn project:**

1. If `.context/.codex-orient-pending.md` exists → read it once, orient from it, delete it.
2. Otherwise run `celeborn status`.

The managed AGENTS.md block states exactly this, plus the multi-agent kanban shorthand.

## Permission friction (Codex's coarse lever)

Codex's approval lever is **coarse** — `approval_policy` / `sandbox_mode` in `~/.codex/config.toml`
plus per-project trust — not per-command allow-rules like Claude Code. So instead of generalizing
hundreds of literal rules, the advisor recommends trusting the workspace once:

```bash
# Read-only hint:
python3 ~/.codex/skills/celeborn-codex/scripts/codex_celeborn.py permissions --path .
# Append the [projects."<root>"] trust_level = "trusted" block:
python3 ~/.codex/skills/celeborn-codex/scripts/codex_celeborn.py permissions --apply --path .

# The friction recommendation that applies right now:
python3 ~/.codex/skills/celeborn-codex/scripts/codex_celeborn.py advise --path .
```

## Rollout transcript bridge

Codex persists each session to `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<conversation_id>.jsonl`.
The bridge converts those rollout lines (`response_item` → `message` / `function_call` /
`function_call_output` / `local_shell_call`) into the Claude Code JSONL shape `celeborn capture`
consumes, normalizing Codex's tool vocabulary:

| Codex tool | Canonical |
|---|---|
| `shell` · `shell_command` · `local_shell` · `exec_command` · `write_stdin` | `Bash` |
| `apply_patch` | `Edit` (pure-create patch → `Write`) |
| `view_image` | `Read` |

Manual convert: `codex_celeborn.py convert <rollout.jsonl> --session <id> --dest <out.jsonl>`.

## Manual fallback (always works)

Even with no hooks: run `celeborn status` at the start of a Codex session to orient, and
`celeborn checkpoint` / edit `.context/state.md` before `/new` or compaction. The AGENTS.md block
keeps these instructions in front of the model every session.
