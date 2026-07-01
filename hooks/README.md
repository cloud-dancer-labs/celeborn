# Celeborn hooks (Claude Code)

These make the substrate automatic on Claude Code. They are **optional sugar** — the
[`SKILL.md`](../SKILL.md) protocol works without them on any tool (Codex, Claude.ai), where the
agent runs the Orient read and checkpoints manually.

Every hook is now a single **in-process** `celeborn hook <event>` command — it reads the host's
event JSON on stdin and does the work directly (no bash control flow, no inline `python3`, no
`$CELEBORN_HOME` resolver). See [`references/executable-app.md`](../references/executable-app.md) §3.

| Event | Command | Does |
|---|---|---|
| `SessionStart` | `celeborn hook session-start` | Prints the Hot tier (Orient load) so the session lands oriented |
| `UserPromptSubmit` | `celeborn hook user-prompt-submit` | Reads the live context size from the transcript each turn; nudges a safe `/clear` once per ~50k band, urgent past 150k, native alarm at 200k |
| `Stop` | `celeborn hook stop` | Mechanically captures structured facts (prompts, files, commands, commits, tests) from the finished turn into the local-only Automatic Context Record. No model. |
| `PreCompact` | `celeborn hook pre-compact` | Reminds the agent to checkpoint before the window is summarized |
| `SessionEnd` | `celeborn hook session-end` | Refreshes `handoff.md` (the fresh-thread resume prompt) |
| `statusLine` | `celeborn hook statusline` | Paints the persistent per-turn capture line in the host UI chrome |

All of these **no-op in repos without a `.context/` directory** (capture/statusline fall through to a
global `~/.context` sink so no session goes unrecorded), so they are safe to enable globally.

## Install

1. Put the `celeborn` CLI on PATH: `pip install -e /path/to/celeborn` (or
   `uv tool install --editable /path/to/celeborn`). This gives `celeborn` / `cel`.
2. `celeborn wire` — merges the hooks + statusLine into your Claude Code `settings.json`
   (`--global` for `~/.claude/settings.json`, default is the project's `.claude/settings.json`).
   It's idempotent and **migrates an older bash-based install** in place. Or hand-merge
   [`settings.snippet.json`](settings.snippet.json).

That's it. New sessions auto-hydrate; compactions get a checkpoint nudge; sessions end with a fresh
handoff written.

## The `*.sh` shims

The `*.sh` files are thin one-line shims that `exec` the matching `celeborn hook <event>` against the
clone's own `celeborn.py`. They exist **only** so an install previously wired to a script path keeps
working through the same in-process logic — re-run `celeborn wire` to migrate to the bare
`celeborn hook <event>` commands and you won't need them.

## Other hosts

These hooks are **Claude Code-specific** (they wire into `~/.claude/settings.json`). Grok Build has its
own lifecycle-hook format and does *not* read this directory — its adapter lives in
[`grok/`](../grok/), installs hooks to `~/.grok/hooks/celeborn.json`, and shells out to the same stock
`celeborn` CLI (no `$CELEBORN_HOME`, no in-process `celeborn hook`). A legacy `$CELEBORN_HOME` bash hook
left in `~/.claude/settings.json` will *fail* under Grok — use the `grok/` install path there instead.
See [`grok/SKILL.md`](../grok/SKILL.md) and [`../grok_handoff.md`](../grok_handoff.md).
