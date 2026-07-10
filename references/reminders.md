# Reminders — the reassuring "renew your context" nudge

Celeborn can surface a gentle, reassuring reminder as a session's context grows — inviting you to
clear and start fresh, *reassured that nothing is lost* because the memory lives on disk. The voice
is Celeborn's own (the silver tree that remembers what the wind carries away).

## What's portable, and what isn't (honest split)

The reminder must work across **every** coding agent — Claude Code, Codex, Cursor, Aider, Windsurf,
and whatever comes next — so it's split into a portable core and thin per-host adapters:

| Piece | Where it lives | Portable? |
|---|---|---|
| **The message + milestone logic** | `celeborn remind` (this repo) | ✅ Fully — it's just text any system can print |
| **The live token count** | The host agent | ❌ Host-specific — the CLI can't see it; the host passes `--tokens` |
| **The "button" / clear action** | The host agent's UI + clear command | ❌ Host-specific — no universal clickable button or programmatic clear exists |

So: **Celeborn owns the *when* (milestone math) and the *what* (the voice). The host supplies the
*trigger* (its token count) and the *affordance* (a button, or a one-key command).** This is the same
philosophy as the rest of Celeborn — a portable markdown/CLI core, with hooks as per-host accelerators.

## The portable command

```bash
celeborn remind --tokens <current_context_tokens> [--every 100000] [--last <prev_tokens>] [--soft-limit <n>] [--hard-limit <n>] [--session <id>] [--clear-cmd "<text>"]
```

- Prints a reassuring, Tolkien-voiced verse that escalates gently by milestone (≈100k, 200k, 300k+).
- `--every` sets the **band size** (default 100k): the verse escalates and re-speaks once per band.
- `--last` makes it **idempotent per increment**: pass the token count at the previous reminder and
  it stays silent unless a new `--every`-sized boundary was crossed. A host can therefore call it on
  every turn/render and the user only sees it once per band.
- `--soft-limit <n>` / `--hard-limit <n>` set the **context-pressure thresholds** (CELE-t207): newly
  crossing one (measured against the tracked last-reminded mark) replaces the calm verse with an
  explicit warning — ⚠ at soft ("wrap the current step, checkpoint, then clear"), ⛔ at hard ("stop
  and checkpoint NOW"; the future auto-clear trigger). Defaults live in `.celebornrc`
  (`context_soft_tokens` 100k / `context_hard_tokens` 125k — the same "clear now"/"clear urgent"
  lines the board bands draw); the flags override per call, and ≤ 0 disables a threshold. Crossing
  detection needs a tracked mark, so a bare one-shot `--tokens` without `--last` keeps the calm
  milestone wording.
- `--session <id>` reads the live window a transcript-less harness (OpenCode) reported onto that
  session's capture cursor via `celeborn record tokens`. The cursor tracks its own last-reminded
  mark, quietly re-arms when the window shrinks (post-clear/compaction), and carries a
  machine-readable `pressure` field (`none`/`soft`/`hard`) that the board chips — and a future
  auto-clear — read without re-deriving tokens.
- `--clear-cmd` sets the wording of the action line (e.g. `/clear` in Claude Code).
- With no `--tokens`, it prints the generic verse — useful for a human or an agent invoking it by hand.
- The reassurance line names the **Hot-tier cost** and that it reloads automatically (e.g. "your
  context rehydrates automatically (~922 tok)"), so the user sees concretely how cheap and safe
  renewal is.

### `--auto`: Celeborn's own rolling estimate

Celeborn can't *see* the host's live window, but it can carry a **rolling estimate** so the host
doesn't have to pass `--tokens` every call:

```bash
celeborn record turn --tokens <delta>   # accumulate (host feeds each turn's growth)
celeborn remind --auto                   # fires off the accumulated estimate; silent until a new band
```

`record turn` adds to `metrics.context_estimate`; a new session, `record clear`, or `record
compaction` resets it to roughly the Hot-tier load (the window after a renewal). `remind --auto`
reads that estimate and tracks its own last-spoken mark, so it nudges once per increment with no
host bookkeeping beyond feeding turn deltas. **It is an approximation** — a fed lower-bound proxy,
not a measurement — and is refined the moment a host can supply a real number via `--tokens`. (Note:
this is distinct from the `tokens_saved_estimate` metric, which measures rehydration economy, not
live-window growth.)

## Per-host triggers (adapters)

**Claude Code** — wire the token count through a `statusLine` command (it receives context/usage info
on stdin), or surface it from the `SessionStart`/turn flow. The clear action is the native `/clear`,
which — with Celeborn's `SessionStart` hook wired — wipes history *and* re-injects the Hot tier in one
step (see `hooks/`). There is no clickable button in the Claude Code TUI and no auto-`/clear` at a
threshold; the affordance is the single `/clear` keystroke, and auto-compaction is the safety net.

**Agents without statuslines/hooks (Codex, Cursor, Aider, …)** — the agent itself follows `SKILL.md`:
watch the context meter, and at ~100k intervals call `celeborn remind --tokens <n>` and show the
result, then offer that host's clear/reset command. Where a host *does* expose a button API, an
adapter can render the verse with a real button; where it doesn't, it's a one-line command.

**A human, anywhere** — just run `celeborn remind`. It works with zero integration.

## Why a "button that clears, on every system" can't be a single feature

Two hard limits, stated plainly so no one expects otherwise:
1. **No portable token introspection.** The size of the live window is private to each host; a
   standalone tool cannot observe it. Hence `--tokens` is supplied by the host.
2. **No portable clear/button.** Each agent has its own UI and its own reset (`/clear`, `/reset`,
   etc.), and most cannot be cleared programmatically by an external tool. So the reminder *invites*
   the clear; it cannot *perform* it universally.

What Celeborn guarantees everywhere is the part that actually needed solving: a calm, trustworthy
nudge — in one consistent voice — that makes clearing feel safe, because the silver tree remembers.
