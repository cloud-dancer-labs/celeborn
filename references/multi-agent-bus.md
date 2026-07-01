# Multi-agent channel/bus — design spec

> Status: **draft / north-star**. Not yet built. This is the design for turning Celeborn's shared
> context substrate into a coordination fabric for *many agents working one project at once*.
> © Cloud Dancer, all rights reserved; distributed by Thot Technologies LLC.

## 1. The idea in one line

Celeborn already turns context into durable, shared, on-disk state. Point that at *concurrent* agents
and the substrate becomes a **blackboard / message bus**: agents coordinate by reading and writing
shared, append-only channels rather than by direct RPC. Communication is durable, replayable, and
restart-proof — every "conversation" is on disk.

This is the **blackboard architecture** (and its cousin, the Linda *tuple space*): no agent messages
another directly; coordination *emerges* from shared state.

## 2. What already exists (the bones)

| Primitive | Today | Reuse for the bus |
|-----------|-------|-------------------|
| Durable shared store | `.context/*.md` + `celeborn sync` | the channels live here |
| Per-reader cursors | per-session capture cursor (byte offset into transcript) | "what's new since I last read channel X" |
| Cross-channel recall | SQLite FTS (`celeborn search`) | query across all channels |
| Realtime transport | hosted `context_files` is in the Supabase realtime publication, **but the CLI only polls** | flip to subscribe → push |

Three of the four pieces exist. The missing work is **channels + write-coordination + the realtime flip**.

## 3. Channel layout

Channels are append-only markdown logs under a reserved bus root:

```
.context/bus/
  <channel>/
    <agent-id>.md        # ONE writer per file — the agent that owns it only appends
  agents.md              # registry: agent-id → role, status, last-seen heartbeat
  topics.md              # channel registry: name, purpose, subscribers
```

Two channel shapes:
- **Topic channels** (`bus/<topic>/<agent>.md`) — many agents publish to a shared topic, each into
  *its own file*. The topic is the union of its per-agent files.
- **Direct channels** (`bus/dm-<a>-<b>/`) — scoped to a pair, same per-writer-file rule.

**The load-bearing rule: one writer per file, append-only.** Each agent only ever appends to files it
owns. This is what makes concurrency safe (see §5).

### Message format

Each appended entry is an H2 block (consistent with journal convention — H2, oldest-first, tail =
recent), with a machine-readable front matter line:

```
## 2026-06-04T18:42:11Z · agent=planner · seq=412 · reply_to=builder#388
<body — freeform markdown, may reference files, decisions, other messages>
```

`seq` is per-agent monotonic. `reply_to` is `<agent>#<seq>` for threading. HTML comments are stripped
before indexing (existing convention), so structured hints can ride in comments without polluting FTS.

## 4. Cursor protocol (read side)

Each agent keeps a cursor per channel-file it subscribes to: the **byte offset** it has consumed up to.
This is the *same mechanism* `capture` already uses against the transcript (`_iter_transcript` tracks
`tell()` as an exact resumable offset).

```
.context/bus/.cursors/<reader-agent>.json
  { "bus/design/planner.md": 10241, "bus/design/builder.md": 5120, ... }
```

Read loop per agent:
1. For each subscribed channel-file, read from `cursor[file]` to EOF.
2. Parse new H2 entries; hand them to the agent as "messages since last check."
3. Advance `cursor[file]` to the new EOF (only past fully-parsed entries — partial trailing writes are
   left for next pass, exactly as capture already does).

Because reads are offset-based and writes are append-only, **a reader never needs a lock** — it can read
a file another agent is mid-append to; it just stops at the last complete entry.

## 5. Write-coordination — why append-only per-writer solves the clobber

The current sync model is **last-writer-wins on whole files** (`build_push_rows` uploads each file with
`version = mtime_ns`; PostgREST upsert merges by `(project_id, path)`). Two agents editing the same
file silently clobber each other. That is fatal for concurrent agents.

**Fix without locks or CRDTs:** partition the write space so no two agents ever write the same file.
- Each agent owns `bus/<channel>/<its-id>.md` and *only appends*.
- A channel's content is the *merge of its per-agent files*, ordered by entry timestamp — a merge that
  is associative and commutative because no file is ever co-written. No conflict can arise.
- Curated shared docs (`state.md`, `notes.md`) stay single-owner as today; the bus is the concurrent
  surface.

This is the minimal coordination model. CRDT/OT is explicitly **not** needed for v1 and should be
resisted (stdlib-only, markdown-is-truth conventions).

### Sync changes required
- Append-aware upload: push only the **tail** of each owned channel-file past the last synced offset,
  not the whole file. (Also fixes the egress blow-up documented in `references/sync-design.md` / the
  cost analysis — full-file re-push every interval is the current cost killer.)
- Server stores channel entries append-only; pull is `entries where seq > last_seen` per channel.

## 6. The realtime flip (continuous comms)

Polling (`sync --watch --interval 5`) makes "continuous" mean "every few seconds." The hosted
`context_files` table is **already in the `supabase_realtime` publication** — the CLI just doesn't
subscribe. To make agents "talk continuously":

1. Client opens a Realtime websocket, subscribes to `INSERT`s on the bus channels it cares about
   (filtered by `project_id` + channel).
2. On an insert, the agent pulls just that entry (or the tail past its cursor) — sub-second delivery.
3. Polling stays as the offline/degraded fallback (the existing `--watch` loop), so the system works
   with no realtime too.

Latency model: **near-real-time async**, not synchronous RPC. Good for many agents loosely coordinating;
not a substitute for a fast request/response round-trip between two agents.

## 7. Consistency & ordering

- **Within one agent's channel-file:** total order (append-only, monotonic `seq`).
- **Across channels:** eventual, timestamp-merged. No global transaction, no global lock.
- **Causality:** best-effort via `reply_to`; not a vector clock in v1.
- Clocks: entries carry wall-clock UTC for ordering; agents must tolerate small skew (merge is stable
  on `(timestamp, agent-id, seq)`).

## 8. Failure modes & guards

- **Feedback storms / chatter explosion** — agents echoing each other inflate context *and* sync egress.
  Guard: agents subscribe only to relevant channels; the Hot-tier char budget still bounds what loads on
  Orient; channels are searched on-demand, not auto-loaded.
- **Runaway cost** — every writer adds egress. Append-tail sync (§5) + channel-scoped subscriptions keep
  it bounded. This spec and the sync cost model are the *same* problem; solve together.
- **Stale cursor after crash** — cursors are advisory; on resume an agent re-reads from its last durable
  cursor, at worst re-seeing a few entries (idempotent consumers required).
- **Orphaned agents** — `agents.md` heartbeat lets others detect a dead participant and reassign work.
- **Poison entry** — a malformed block is skipped (parse-fail tolerated, like capture); never blocks the
  channel.

## 9. Phased build

1. **v0 — local bus, polling.** `bus/` layout, append API (`celeborn post <channel> <body>`), cursor
   read API (`celeborn read <channel> --since-cursor`), FTS over channels. No sync, single machine.
2. **v1 — append-tail sync.** Teach `sync` to push/pull channel tails by offset (fixes egress too).
   Multi-machine, polling cadence.
3. **v2 — realtime flip.** Subscribe to Supabase Realtime inserts; sub-second delivery; polling fallback.
4. **v3 — coordination primitives.** `agents.md` heartbeats, work-claim entries (claim/ack to avoid two
   agents grabbing the same task), `reply_to` threading surfaced in tooling.

## 10. Open questions

- Channel GC / archival (channels are journals; do they archive like `journal.md` → `journal-archive/`?).
- Auth granularity in the hosted tier: can a teammate be scoped to *some* channels? (RLS per channel.)
- Do we expose the bus as MCP tools so non-Celeborn agents can participate?
- Back-pressure: cap entries/sec per agent to protect cost?

## 11. Why this is the paid wedge

A solo dev surviving `/clear` won't pay — local memory is free and works offline, and that's the point.
But a **team or fleet of agents sharing one coherent, durable, replayable memory across machines, with
no infra to stand up** is worth real money. The bus is the feature that makes Celeborn a *team* product,
not a personal one. It rides the same hosted backend the sync tier already uses.

---

*Pointers:* sync internals → `references/sync-design.md`; hosted backend → `references/supabase-setup.md`;
substrate/tier model → `.context/durable/architecture.md`.
