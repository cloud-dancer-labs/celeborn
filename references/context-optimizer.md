# Seamless agentic context — design spec

> Status: **draft / north-star**. Not yet built. Two coupled bets that turn Celeborn from a context
> *store* into a context *operating system*: (1) a per-turn **Orient-payload optimizer** that keeps every
> rehydration minimal-but-sufficient, and (2) **seamless multi-agent telepathy** over the hosted bus
> (detailed separately in [`multi-agent-bus.md`](multi-agent-bus.md)).
> © Cloud Dancer, all rights reserved; distributed by Thot Technologies LLC.

## 0. Product decisions baked into this spec

- **Hosted-only sync & bus.** Cross-device sync and the multi-agent bus run *only* on the hosted
  backend. No self-host: the git-daemon-only and bring-your-own-Supabase paths are not the product
  direction. **Local single-machine memory stays free and works offline** — that is the on-ramp.
- **The user still clears manually.** We do not try to defeat `/clear`. We make the *cost* of clearing
  approach zero by guaranteeing a fresh session rehydrates tight and current with no manual work.
- **Clear early, clear often — and train the user to.** The strategy is to push the user to `/clear`
  frequently rather than let one session balloon. This reorders priorities: the **`/clear` surface
  (SessionStart Orient payload) is primary**; the compact surface is secondary (a session that clears
  often rarely reaches auto-compaction). Celeborn already ships the training mechanism — the heartbeat
  nudge (`🏹 Celeborn —> … Good to /clear …?`). The optimizer makes that nudge *safe to obey*: clearing
  is free only because rehydration is tight and lossless.
- **Optimizer model: Haiku at high reasoning effort.** Cheap enough per token to run every turn,
  strong enough to follow precise distillation instructions. The instructions are the product (§3.5).

## 1. The load-bearing constraint (read this first)

The original framing was "rewrite CLAUDE.md every turn so each turn starts fresh." Claude Code's
context model means that *specific* mechanism cannot shrink a live session — but the instinct is right,
and there is a version that bites hard. The facts (confirmed against Claude Code docs):

| Fact | Consequence |
|------|-------------|
| CLAUDE.md is read **once, at session start** — not re-read between turns | Editing it mid-session has **zero** effect on the running window |
| A live context window is **append-only** | Nothing but `/clear` or `/compact` removes tokens already in it |
| `/compact` **re-injects project-root CLAUDE.md, MEMORY.md, and unscoped rules from disk** | A tight CLAUDE.md *does* shrink every compaction |
| The `SessionStart` hook fires on a fresh session (`/clear`, restart) — **not** on `/compact` | The Orient hook payload pays off at `/clear`; it does **not** survive a compaction |

**Two boundaries, two delivery channels, one optimizer:**

- **`/clear` (and restart)** → hydrated by the `SessionStart` hook (`celeborn status` / the Hot tier).
  Keep *this* minimal → cheap clears.
- **`/compact`** → hydrated by disk-backed CLAUDE.md / MEMORY.md / unscoped rules. Keep *these*
  minimal → cheap compactions.

So the user's "optimize CLAUDE.md" instinct is *correct for compaction* and the "fresh start every
turn" instinct is *correct for clears* — it just lands at the boundary, not mid-turn. The optimizer
maintains both surfaces continuously so whichever boundary the user hits next, they land tight.

## 2. What already exists (the bones)

- **Hot tier / Orient load** — `state.md` headline + `activity.md`, injected by the `SessionStart` hook.
  This *is* the `/clear` payload. Today it is hand-trimmed and bounded by char budgets (`_clip()`); the
  optimizer replaces hand-trimming with continuous, intelligent distillation.
- **Tiered store** — deeper detail (`notes.md`, `journal.md`, `durable/`) stays out of the Hot tier and
  is pulled on demand + searchable via FTS. The optimizer's job is deciding the *Hot/cold boundary* per
  turn, not inventing new storage.
- **Per-turn capture** — `celeborn capture` already runs every turn and knows what changed (touched
  files, last prompt, transcript offset). That signal is the optimizer's trigger and input.

## 3. Feature A — the Orient-payload optimizer

### 3.1 Goal
Each turn, in the background, distill **only the words needed to maintain current flow** into the two
hydration surfaces, so a fresh session (`/clear`) or a compaction resumes the active task with the
fewest possible tokens and no manual rehydration.

"Current flow" = the minimum to keep working *right now*: active focus, next action, live constraints,
and *pointers* (not content) to the deeper tiers. Everything else stays cold, on-demand, and searchable.

> **Where it runs:** the optimizer is a **daemon subsystem**, not a per-turn spawn — see
> [`executable-app.md`](executable-app.md) §1–§3. It runs against warm in-memory `.context` state off
> the hot path; the per-turn hooks never wait on it. If the daemon is down, the Hot tier simply stays
> at its last-optimized state (the fallback in `executable-app.md` §2.2) — safe, just not freshly
> trimmed.

### 3.2 Where output lands (never mutate the human's CLAUDE.md body)
The committed `CLAUDE.md` is user-authored and git-tracked; rewriting it wholesale every turn would
churn a shared file and fight the author. Apply the bus's **one-writer-per-file** rule to the optimizer:

- **Clear surface:** the `SessionStart` Orient payload (Hot tier) — already optimizer-owned territory.
- **Compact surface:** an **optimizer-owned, disk-backed rule file** (an unscoped rule, or a delimited
  `<!-- celeborn:optimized -->` block the optimizer exclusively owns) — re-injected on `/compact`.
  The human's CLAUDE.md prose is never touched.

### 3.3 Loop
1. `capture` fires (every turn). If no *material* change since last optimization → skip (diff-gate).
2. Optimizer agent (**Haiku at high reasoning effort**) reads: current Hot tier, recent
   activity/transcript delta, the current optimized block. Budget-capped per run.
3. Emits a fresh minimal payload for both surfaces. Writes only if it differs (stability over churn).
4. Records what it *demoted* to cold tiers (so nothing is silently dropped — auditable).

### 3.4 Guardrails (the ways this goes wrong)
- **Cost/latency:** Haiku-at-high-effort makes per-turn affordable, but still diff-gate hard and cap the
  per-run token budget. The optimizer runs in the background — never block the user's turn on it.
- **Thrash:** rewriting on tiny deltas destabilizes the cache and the file. Require a materiality
  threshold; prefer append-to-cold + headline-edit over full rewrites.
- **Silent loss:** distillation must *demote*, never *delete* — everything cut stays in a cold tier and
  stays searchable. Log the demotion.
- **Correctness drift:** the optimizer can hallucinate a tighter-but-wrong state. The mechanical
  `activity.md` (never LLM-touched) remains the ground-truth backstop, exactly as today.

### 3.5 The instructions are the product
A Haiku optimizer is only as good as the prompt it follows — that prompt *is* the core craft, and a
versioned, tested artifact, not an afterthought. It must encode, precisely and elegantly:

- **What "current flow" means** — focus, next action, live constraints, pointers (not content). A tight
  rubric for what earns a place in the Hot tier vs. what gets demoted.
- **Demote, never delete** — the cut goes to a named cold tier; output the demotion so it's auditable.
- **Stability** — prefer the smallest edit that keeps the headline true; don't rewrite what hasn't
  changed (protects the prompt cache and avoids churn).
- **Format discipline** — emit exactly the Hot-tier shape the hooks expect, within the char budgets.
- **Honesty** — never invent a tidier state than the mechanical `activity.md` supports.

Drafting and eval'ing this prompt is the natural next planning artifact (no code): write it, then judge
its output against real `.context` snapshots before wiring any runtime.

## 4. Feature B — seamless multi-agent telepathy (hosted)

Full design in [`multi-agent-bus.md`](multi-agent-bus.md). Hosted-only direction changes the emphasis:

- **The realtime flip (§6 there) is now core, not optional v2.** "Seamless telepathy" = sub-second
  delivery over the hosted Supabase Realtime channel the table already publishes to. Polling is only the
  degraded fallback.
- **No self-host tier for the bus.** The append-tail sync and channel store live on the hosted backend;
  there is no BYO-Supabase bus. Local memory remains free; coordination across agents is the paid wedge.
- **Coupling to Feature A:** every agent on the bus has its *own* Hot tier and its own optimizer. The
  optimizer must subscribe agents only to relevant channels and keep bus chatter in cold/searchable
  tiers — otherwise telepathy inflates exactly the context the optimizer is trying to shrink. The two
  features share one budget: total loaded context per agent.

## 5. Required copy change (not code — flag, not done here)

The hosted-only decision contradicts the README sync section I currently ship
([`README.md`](../README.md) ~line 205): it advertises a free **git-daemon + bring-your-own-Supabase**
self-host path. Under "hosted sync only, local stays free" that becomes:

- Local single-machine memory: free, offline, no account.
- Sync + bus: hosted only (paid — Pro for sync, Team for the bus), no self-host.

Drop the "Free, no account: git-daemon / BYO Supabase" bullet's self-host framing. Left as a follow-up
to apply on request.

## 6. Phasing

1. **Optimizer v0 (local, clear-surface only).** Diff-gated background optimizer that maintains the
   Orient payload. Measurable: tokens-at-rehydration after `/clear`, before vs after. No bus, no compact
   surface yet.
2. **Optimizer v1 (compact surface).** Optimizer-owned disk-backed rule block re-injected on `/compact`;
   measure post-compaction context size.
3. **Bus v0–v2** per `multi-agent-bus.md`, hosted realtime promoted to core.
4. **Convergence.** Per-agent optimizer + bus subscriptions share one context budget; telepathy stays
   cold-by-default.

## 7. Open questions

- Materiality threshold for re-optimizing — what counts as "enough changed"? (file-touch count? a
  cheap classifier? semantic diff of the headline?)
- Which model tier for the optimizer, and is per-turn even affordable vs. on-threshold-only?
- Compact surface: unscoped rule file vs. a delimited optimizer-owned block inside CLAUDE.md — which
  does Claude Code re-inject most reliably? (verify before building v1)
- Does running the optimizer as a Celeborn subagent vs. an external process change the cost model?

---

*Pointers:* multi-agent bus → [`multi-agent-bus.md`](multi-agent-bus.md); sync internals →
[`sync-design.md`](sync-design.md); substrate/tier model → `.context/durable/architecture.md`.
