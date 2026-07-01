# The memory tiers

Celeborn's entire context economy comes from one rule: **only the Hot tier enters context by
default, and the Hot tier is size-bounded regardless of how much total memory exists.** Everything
else is reached on demand.

| Tier | File(s) | Loaded at session start? | Write discipline |
|---|---|---|---|
| **Hot** | `state.md`, `session.json`, `durable/manifest.md` | **Yes, always** | rewrite-in-place; hard size budget (`state_max_lines`) |
| **Warm** | `journal.md` (tail only) | On demand, when resuming work | append at bottom; archive past threshold |
| **Cold** | `journal-archive/*` | Only via `celeborn search` | write-once |
| **Distilled** | `learnings.md`, `decisions.md` | On demand / when referenced from `state.md` | promote in, prune when superseded |
| **Durable** | `durable/*.md` | Manifest always; bodies on demand | stable repo truths only |
| **Index** | `index.db` | Queried, never read into context | derived; regenerable; gitignored |

## Why this bounds context cost

A two-day-old project and a two-year-old project rehydrate at roughly the same token cost, because
Orient reads only `state.md` + `session.json` + `durable/manifest.md` — three small, deliberately
capped files. The journal grows, but it archives. The durable layer grows, but only its one-line
manifest is auto-loaded; the bodies are pulled by pointer when relevant. Cold history is never
loaded wholesale — it is queried, and search returns snippets, not files.

## What goes where

- **state.md** — the *only* place for current status and the next action. If you're tempted to write
  "update:" or a dated note here, it belongs in the journal.
- **journal.md** — what happened, with evidence. Transient by nature; archived as it ages.
- **learnings.md** — lessons general enough to help a future, unrelated task.
- **decisions.md** — choices that shouldn't be re-litigated.
- **durable/** — slow-moving repo truths (architecture, conventions, gotchas). Promote here only when
  something is stable.
- **handoff.md** — the fresh-thread resume prompt.

See `memory-protocol.md` for the verbs that move knowledge between these tiers, and `rehydration.md`
for the exact read-order on session start / after compaction.
