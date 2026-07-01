# The memory protocol

Five verbs. The agent runs them; the `celeborn` CLI makes the mechanical parts cheap and
deterministic. Judgment (what to write, what to promote) stays with the agent; bookkeeping
(indexing, archiving by threshold, search, health checks) is offloaded to the CLI.

---

## ORIENT — read (cheap rehydration)

At session start, after compaction, or when picking up a fresh thread, read in this fixed order and
**stop**:

1. `.context/session.json` — the machine-readable state (focus, next action, stop-state)
2. `.context/state.md` — the live brief
3. `.context/durable/manifest.md` — pointers to durable docs (read a body only if the next action needs it)

If this project uses Celeborn tasks, the Orient load also includes in-flight/blocked cards from
`tasks.md`. Read that slice before claiming new work — pick a TODO that won't interrupt another
agent's card.

That is the whole default load. Pull more **only when the task requires it**:
- the relevant `durable/*.md` body (by pointer from the manifest)
- the tail of `journal.md` (recent history) if you're resuming in-progress work
- `celeborn search "<query>"` for anything older or uncertain — it returns snippets with
  `file:anchor` pointers, so you open only the section you need

`celeborn status` prints exactly the Orient load in one shot. The `SessionStart` hook runs it for you.

---

## CHECKPOINT — write (after each meaningful unit of work)

1. **Rewrite `state.md` in place** so "Now / Next action" reflect reality. Never append history here.
2. **Append one entry to the bottom of `journal.md`** (what you did + evidence + next).
3. **Update `session.json`** (`focus`, `next_action`, `branch`, `status`, `updated_at`).

Checkpoint whenever you finish something you'd hate to re-derive after a compaction. The `PreCompact`
hook reminds you to checkpoint before the window is summarized.

---

## FORGET — compress (keep the Hot tier small)

Strategic forgetting is what keeps rehydration cheap — *"chats are for execution, handoff docs are
for memory, archives are for history, fresh threads are for speed."*

- `journal.md` past `journal_keep_entries`? → `celeborn archive` moves the oldest entries to
  `journal-archive/`. They stay searchable; they leave the Warm path.
- `state.md` past `state_max_lines`? → condense it. Move detail to the journal or a durable doc.
- A learning superseded or proven wrong? → delete it. Don't keep dead memory.

`celeborn doctor` flags when any of these budgets is exceeded.

---

## PROMOTE — distill (move knowledge up as it stabilizes)

```
journal (transient)  →  learnings (reusable)  →  durable/* + manifest line (stable truth)
```

Promote a journal observation to `learnings.md` once it generalizes. Promote a learning into
`durable/*` once it's a stable repo truth, and add a one-line `manifest.md` pointer so Orient knows
it exists. `celeborn promote` appends the formatted entry for you; you still decide what's worth
promoting. Demotion and deletion are equally valid — memory you stop trusting comes back out.

---

## HANDOFF — restart cheaply

Before ending a long session, or to escape a bloated context, run `celeborn handoff`. It regenerates
`handoff.md` from `state.md` + `session.json`: branch/status, the next required action, open risks,
and a ready-to-paste resume prompt. Starting a **fresh thread** from a good handoff is the cheapest
possible compaction — zero history cost, full continuity.

---

## Single source of truth

There is exactly one home for durable context: `.context/`. No parallel files (`CLAUDE_CONTEXT.md`,
`PROGRESS.md`, `NOTES.md`, ad-hoc hand-off docs) — they drift out of sync, escape the tier budgets,
and aren't searchable. All writes land in a tier; all reads (and every new-session hydration) go
through ORIENT. A pre-existing context file is migrated into the tiers and reduced to a pointer, never
maintained alongside. This is what lets the index stay authoritative and the project stay portable:
the markdown is the whole truth, and it travels with the repo.

---

## The discipline in one line

Write the smallest true thing to the highest-leverage file, archive what aged, promote what
generalized, and never carry in context what a pointer can fetch on demand.
