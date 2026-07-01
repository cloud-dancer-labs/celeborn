# Card → model assignment — design

> Status: **v1 built** (claim-on-receipt + per-agent outbox — `2026-06-09`). **v2 = semi-telepathic
> kanban** (this doc) — agents orient through Celeborn, see what everyone else is doing, pick a card
> that won't step on in-flight work, and **claim** it so the whole fleet knows it's in progress.
> © Cloud Dancer, all rights reserved; distributed by Thot Technologies LLC.

## The mental model — human kanban, semi-telepathic agents

On a human team, nobody assigns cards from a central registry. People walk up to the board, see what's
**To Do**, see who's already **Doing**, and pull the next card they can do without getting in
someone's way. Coordination is **shared visibility**, not RPC.

Celeborn gives agents the same thing — with a twist. They are **semi-telepathic**: every agent that
wakes in a Celeborn project is hooked into the same `.context/` substrate. On orient they see the
task board summary (who owns what, what's blocked), recent activity, and session focus. They don't
need a human to enumerate which models are live; they read the **durable board state** and infer what
others are probably working on from `owner`, `state`, `updated`, tags, and notes.

Celeborn still cannot see which chat window is active until a model **acts** in the monitored repo
(first hook fire, first capture, first claim). That is fine — the board is the coordination surface,
not a process list. An agent discovers the fleet by reading Celeborn, not by scanning the desktop.

**Claiming a card is the broadcast.** When an agent claims `t13`, `tasks.md` immediately records
`owner ← claimer` and `todo → doing`. Every other agent's next orient, board poll, or hook turn sees
that card as taken. No separate "I'm working on this" channel is required for v2 — the kanban *is* the
bus until the full [multi-agent bus](multi-agent-bus.md) lands.

## Two sources of truth — Celeborn live, Jira reporting

| Layer | Role | Latency | Who cares |
|-------|------|---------|-----------|
| **Celeborn** (`tasks.md` / board) | **Real-time work** — what's actually being done *right now*, by which agent, in which repo | Seconds (hook + board poll) | Agents, elves, the human watching the board |
| **Jira** | **Secondary report** — governance, stakeholders, sprint history, people outside the agent loop | Minutes (cron / `jira push`) | PMs, execs, ticket consumers |

Jira does not drive agent execution. Agents do not wait for a Jira transition to start work. Flow:

1. Work is **planned and prioritized** on the Celeborn board (or pulled once from Jira into a card).
2. An agent **claims** and executes against live repo context.
3. Status **fans out to Jira** on a schedule or at milestones (`celeborn jira push`) — comments,
   transitions, fleet report — so humans who live in Jira stay informed without Jira being in the
   hot path.

`jira` on a task is a **link**, not ownership. The `owner` field is written by **claim**, not by
issue assignee sync. Pull imports title/state; push exports progress — it does not overwrite a live
claim.

## v2 — semi-telepathic claim (target behavior)

### What the agent sees (orient + each turn)

On `session-start`, the Orient load already includes a compact board slice via `_tasks_orient_summary`:

- Column counts (`N todo · M doing · …`)
- Every **doing** and **blocked** card with `[id]`, title, `@owner`, blockers

That is the agent's "who's on the board right now" view. Deeper detail: `celeborn tasks`, the browser
board, or `tasks.md` directly.

Each `user-prompt-submit` turn also runs heartbeat, optional outbox drain, and **claim-on-receipt**
when the inbound prompt carries a card marker (see v1 below).

### How an agent should pick a card (conflict-aware)

When the user says "pick up the next card" (or the agent is idle and the board has unclaimed TODOs),
follow human kanban etiquette:

1. **Read the board** — `celeborn tasks` or the Orient summary. Note every `doing` card and its
   `@owner`.
2. **Skip non-starters** — do not claim a card that is `doing` with a different `owner`, `blocked`,
   or `done`. Do not steal in-flight work unless the user explicitly reassigns.
3. **Prefer non-interrupting work** — among `todo` cards with no owner (or owned by you), pick one
   least likely to collide:
   - Different tags / area than cards already `doing` (e.g. don't grab a second `ui` card if `@grok`
     is already doing `t12` tagged `ui`).
   - No `blocked_by` dependency on an unstarted predecessor.
   - Highest priority within the column (top of To Do = first in list order).
4. **Identify yourself** — before or as part of the claim, declare who you are in plain language
   (`I'm Grok in the celeborn repo`) and write that identity into the claim via `--by` or
   `$CELEBORN_AGENT`. Opaque session ids are a fallback, not the preferred board label.
5. **Claim** — `celeborn claim <id> --by <you>`. This is the assignment broadcast: TODO → DOING,
   `owner ← you`, `updated ← now`.
6. **Set the Stop condition** — every card carries a logical **Stop condition** (`stop`): the
   clearly-defined "this is a clean place to stop" marker that tells you when the card is at a
   defensible `/clear` point. `tasks add` auto-fills a generic default; on claim, read it and replace
   the default with a real one for this card — `celeborn tasks edit <id> --stop "<condition>"`.
7. **Work the card** — treat the card body (title + notes) as the task spec; move to `done` or
   `blocked` when finished. Reaching the Stop condition is the signal that you may cleanly `/clear`.

If every TODO is owned by someone else or would clearly overlap (same file area, same epic, explicit
"wait for tN" in notes), **do not claim** — tell the user what's in flight and what would be safe to
start, like a teammate at the standup.

### Claim paths (both valid)

| Path | Trigger | Assignment mechanism |
|------|---------|----------------------|
| **Human route** | User copies 📋 from board, pastes into a chat | `UserPromptSubmit` parses `⟨celeborn:tN⟩` → `celeborn claim` |
| **Agent route** | User says "take a card" / agent self-starts from orient | Agent runs `celeborn claim` (or pastes marker itself) after conflict check |

Both paths use the same primitive. **Last claim wins** if two agents grab the same card — the hook
surfaces `Reassigned [tN] alice → bob` rather than hiding contention.

### Identity resolution (who owns the card on the board)

Order for `celeborn claim`:

1. `--by <agent>` — explicit, human-readable (preferred for autonomous claims).
2. `$CELEBORN_AGENT` — per-shell env (`CELEBORN_AGENT=grok`).
3. Session id from the hook — stable for one session, opaque on the board.

**v2 addition (planned):** `celeborn identify <name>` — one-time friendly alias persisted in
`.context/sessions.json`, so orient can show `grok (session …)` without re-prompting every turn.

On claim, the `UserPromptSubmit` envelope tells the model:

> You now own the following board card(s); they have been moved to DOING under your name.

The model should **repeat its identity** in the first line of its reply when claiming autonomously, so
the human can correlate board `@owner` with the chat they're in.

## ✅ v1 — claim-on-receipt (built)

Mechanical assignment when a card marker arrives in the prompt:

- Every copied/handed-off prompt carries `⟨celeborn:tN⟩` (CLI `_card_marker` / board `taskPrompt`).
  Parser (`_find_card_refs`) tolerates stripped/ASCII variants.
- **Paste into a session** → that session's hook runs `celeborn claim tN`.
- **Owner is learned at claim time**, not chosen from a dropdown (Celeborn cannot enumerate live
  models; the human or the claiming agent decides by acting).
- **Handoff (push) is demoted, not deleted** — 🏹 only appears once a card has an owner; it
  re-notifies that owner. First contact is Copy → paste or autonomous claim.

## ✅ v0 — per-agent outbox (built)

Per-agent files under `.context/outbox/<agent>.md`, `sent.md` archive, `_unassigned.md` for
unaddressed prompts. Push addresses to `owner`; each agent drains only its queue via
`$CELEBORN_AGENT`.

```bash
celeborn outbox push --task tN [--for <agent>]
celeborn outbox drain [--for <agent>]
celeborn outbox list
celeborn outbox clear [--for <agent>]
```

## Data model (unchanged)

### Task fields that matter for claims

| Field | Set by | Meaning |
|-------|--------|---------|
| `state` | claim / move | `todo` → `doing` on claim |
| `owner` | claim / edit | **Who is working this right now** (live, Celeborn-owned) |
| `stop` | `tasks add` (default) / edit | **Logical Stop condition** — the clean `/clear` point for the card; auto-filled with a generic default, replace with a card-specific one on claim |
| `updated` | claim / move | Last state change (proxy for claim time) |
| `jira` | `jira pull` | Linked issue key — reporting only |
| `blocked_by` | edit | Dependencies — agent must respect before claiming |
| `tags` | edit | Heuristic for conflict avoidance |

No `claimed_at` in v2 — `updated` on a fresh `doing` is enough.

### Outbox

Unchanged from v0. Autonomous agents rarely need push for first contact; push remains for human
re-nudges and future bus dispatch.

## Board UI

- **Copy** — primary human routing verb; embeds marker.
- **Owner chip** — read-only, reflects claim within seconds.
- **Handoff** — gated on `owner`; re-notify only.
- **No assignee picker** — assignment is claim-shaped, not pre-assigned.

Future: tint/filter cards by owner, show "claimed 2m ago" from `updated`, optional "Suggest next
card" that runs the conflict heuristic server-side.

## CLI surface

```
celeborn claim tN [tN …] [--by <agent>]   # broadcast: owner ← claimer, todo → doing
celeborn tasks                            # full board — use before autonomous claim
celeborn tasks move tN done|blocked|todo  # close or unblock
celeborn identify <name>                  # (v2 planned) persist friendly agent id
celeborn outbox push --task tN [--for <a>]
celeborn jira push [--apply]              # secondary report — after real work on the board
```

Hook (`user-prompt-submit`): drain outbox → parse markers → `celeborn claim` → inject claim envelope.

## Scope ladder

1. **v0:** per-agent outbox + `$CELEBORN_AGENT`. ✅
2. **v1:** `⟨celeborn:tN⟩` marker, `celeborn claim`, hook parse, board Copy/Handoff gating. ✅
3. **v2 (this doc):** semi-telepathic kanban — orient-driven conflict-aware pick, self-identify on
   claim, Celeborn-as-live-SoT / Jira-as-report framing, prompt guidance in Orient + claim envelope.
4. **v3 → bus:** claim events as append-only bus entries; realtime board; `agents.md` heartbeats for
   stale-owner detection and automatic unclaim.

## Prompt contract (what we tell models)

**On orient (session-start):** the tasks summary is not decorative — it is the team board. Before
starting new work, check what's `doing` and who owns it.

**On claim (hook envelope):** you own these cards; they are DOING under your name; execute now.

**At install (built):** `celeborn init` injects this guidance into the managed blocks in
`CLAUDE.md` and `AGENTS.md`, and the templates `state.md` / `handoff.md` plus
`references/rehydration.md` and `memory-protocol.md` point at the board. File-level edit
coordination (`celeborn touch`) is in [multi-agent-editing.md](multi-agent-editing.md). Re-run
`celeborn init` to refresh an existing project's managed blocks after upgrades.

**In SKILL / project rules (recommended):**

> Celeborn is the live coordination layer for this repo. The kanban board in `.context/tasks.md` is
> the real-time source of truth for what agents are doing. Jira is downstream reporting. When picking
> work, read the board, avoid cards that would interrupt another agent's in-flight task, claim with
> `celeborn claim <id> --by <your name>`, then move the card when done.

## Open questions

- **Double-claim window.** Last-claim-wins is correct at the data layer; two agents acting on the same
  card before either refreshes orient is a human/autonomy coordination problem. Board visibility +
  conflict heuristics reduce it; bus heartbeats (v3) address stale owners.
- **Marker robustness.** Claim-on-receipt depends on the marker surviving into `payload.prompt`.
  Autonomous `celeborn claim` bypasses this.
- **Jira drift.** If someone moves an issue in Jira but Celeborn still shows `doing`, Celeborn wins
  for agents; `jira push` reconciles outward. Pull is idempotent and does not stomp `owner`.
- **Cross-repo agents.** Same agent id in two repos is fine — `owner` is per `.context/`.
- **Elves / headless agents.** Set `CELEBORN_AGENT=elves-watchdog` (or `--by`) so board chips are
  readable; elves should claim before long autonomous batches.

---

*Pointers:* board viewer → `board/README.md`; bus north-star → `references/multi-agent-bus.md`;
Jira transport → `scripts/celeborn_jira.py`; hook implementation → `dispatch_hook` in
`scripts/celeborn.py`.