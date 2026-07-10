# Elves × Celeborn — integration overlay

> This document is the **Celeborn edition's override** of how Elves persists its working memory.
> Where [`SKILL.md`](SKILL.md) describes saving state via constant git pushes, **follow this instead.**
> Elves' loop is unchanged; only its memory substrate moves onto Celeborn.

## Division of labour

- **Elves drives the work** — the Ralph loop, batch planning, multi-agent execution, testing, and
  PR-based code review. This is the role Celeborn deliberately does *not* implement.
- **Celeborn holds the memory** — the tiered `.context/` store, the bounded Hot tier, search-based
  recall, archiving, and cheap rehydration after compaction. This is the role Elves no longer has to
  improvise with ad-hoc files and frequent pushes.

One sentence: **Elves is the night shift; Celeborn is the memory it works from.**

## Surface mapping (Elves → Celeborn tier)

| Elves working surface | Celeborn home | Celeborn verb |
|---|---|---|
| Survival Guide (run control, next exact batch, constraints) | `.context/state.md` (Hot) | rewrite in place each checkpoint |
| Execution Log (chronological proof) | `.context/journal.md` (Warm) | append one entry per batch |
| Learnings (reusable lessons) | `.context/learnings.md` (Distilled) | `celeborn promote --to learnings` |
| `.ai-docs/*` (durable architecture/conventions/gotchas) | `.context/durable/*` + `manifest.md` | `celeborn promote --to durable` |
| Plan (authoritative scope/batches) | `.context/durable/plan.md` (or repo `PLAN.md`) | durable doc, manifest-linked |
| Handoff to the next shift / fresh thread | `.context/handoff.md` | `celeborn handoff` |

Elves' own promotion flow — *execution log → learnings → .ai-docs* — **is** Celeborn's
*journal → learnings → durable*. They were the same idea; now they are the same files.

## What replaces "constant git pushes"

Elves historically pushed often so a compaction or crash couldn't lose state. In the Celeborn edition:

- **Context checkpointing is local and cheap.** After each batch, the agent rewrites `state.md`,
  appends to `journal.md`, updates `session.json`, and runs `celeborn index`. No push is needed for
  the *memory* to survive — it's on disk and searchable immediately.
- **`celeborn handoff`** writes the fresh-thread resume prompt; the `SessionStart` hook re-hydrates
  the Hot tier after `/clear` or compaction. The `PreCompact` hook forces a checkpoint first.
- **Git/PRs are reserved for code review**, which is their real job in Elves — *not* for context
  bookkeeping. The user still merges; Elves still never merges. Push code on the batch's review
  cadence, not on every state change.
- **Portability for free.** Because the memory is markdown + a regenerable SQLite index, an Elves run
  is transportable (clone → `celeborn index` → resume) — and ready for the Phase 8 sync layer so a
  run on one machine can be watched/steered from another.

## Operating contract for the agent

When running Elves inside a Celeborn project:

1. **Orient** via `celeborn status` (the Hot tier) before starting or resuming a batch — not by
   re-reading the whole execution log.
2. **Checkpoint** to the tiers after every batch (state + journal + session), then `celeborn index`.
3. **Forget** with `celeborn archive` when the journal grows past budget; nothing is lost (cold tier
   stays searchable).
4. **Promote** stable lessons up the tiers rather than letting `state.md` bloat.
5. **Renew** the window when it grows — surface `celeborn remind` (~100k increments) and `/clear`;
   you'll rehydrate from the Hot tier, not from history.
6. Use **git only** for the code itself and its PR review.

If a Celeborn `.context/` is absent, run `celeborn init` first; if Celeborn is unavailable entirely,
fall back to the original Elves file conventions in `SKILL.md`.
