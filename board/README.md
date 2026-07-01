# Celeborn Board

An **optional** kanban board for a Celeborn project: the flat four-column task board
(`.context/tasks.json`) with FLIP column animations via the `motion` library, done tasks
strike through in place, Done newest-first. A **Fleet** tab shows live agents across
registered projects. Views poll diff-aware (skip re-render when `generated_at` is unchanged).

`tasks.md` stays the single source of truth — the same relationship the SQLite index has to
the markdown. Reads come from the derived `tasks.json` (polled for live updates); writes go
*through* the CLI (`celeborn tasks` / `celeborn outbox`), so the markdown is always
authoritative and the board never parses or rewrites it directly. The board is **not** part
of the Celeborn core (which stays stdlib-only, zero runtime deps) — it is a separate
Node/Next.js subproject you run on demand. Its write actions shell out to `celeborn` on your
PATH (override with `CELEBORN_BIN`).

## Run

```bash
cd board
npm install
npm run dev          # http://localhost:3141 (override with PORT, e.g. PORT=3696 npm run dev)
```

The port is **per-project** so multiple Celeborn boards don't collide: the scripts honor `$PORT`
(default 3141). Ask Celeborn for this project's resolved port with `celeborn board` (or
`celeborn board --port` / `--url`) — it's an explicit `board_port` in `.celebornrc`, else a stable
hash of the project path (3141–3940 band).

**Ensure-on-orient (auto-start).** Once `npm install` has been run in `board/`, you rarely need to
start it by hand: at every `SessionStart` the Celeborn hook probes this project's resolved port and,
if nothing is listening, launches the viewer **detached** (its own session, stdio to
`.context/.board.log`), pointed at this repo via `PORT` + `CELEBORN_TASKS_JSON`. So the board is
effectively always running on its localhost port. A local-only `.context/.board.pid` records the
launch so a still-booting server isn't double-launched by the next orient. Trigger it yourself with
`celeborn board --start`, or turn it off with `"board_autostart": false` in `.celebornrc` (it's also
a quiet no-op when `board/` has no `node_modules`, `npm` isn't on PATH, or the project has no
`tasks.md`).

By default the board reads `../.context/tasks.json` (it assumes it lives at
`<repo>/board/`). To point it at a different repo's context, set an absolute path:

```bash
CELEBORN_TASKS_JSON=/path/to/your/repo/.context/tasks.json npm run dev
```

## Feeding it data

**Tasks (agent cards):**

```bash
celeborn tasks add "Build the kanban board viewer" --state doing --owner claude --tags ui
celeborn tasks move t1 done
celeborn tasks                  # text board in the terminal; also refreshes tasks.json
```

Every `celeborn tasks` command rewrites the derived JSON; the board picks
changes up within a few seconds (`/api/tasks`).

## Working from the board

**Tasks view** — the flat four-column kanban for every card.

**Fleet view** — live multi-project agent dashboard (Pro headline). Register repos with
`celeborn fleet register`, then open the **🛰 Fleet** tab to watch who's working, stuck, or idle
across every registered project on this machine (polls `celeborn fleet --json` every 5s). Hosted
sync extends the same view across devices.

You don't have to drop to the terminal — the board can drive the same CLI:

- **Add a card** — *+ Add card* at the top of the To Do column (Enter to save, Esc to cancel).
- **Prioritize** — the ▲ / ▼ buttons on each card reorder it within its column (top = highest
  priority). Order within a column *is* the priority, and it persists to `tasks.md`.
- **Copy** — 📋 copies the card's prompt (title + notes + a `⟨celeborn:tN⟩` marker) to your
  clipboard. This is the primary way to assign work: **paste it into whichever model you want**, and
  that session *claims* the card (see below). The marker lets the receiving hook identify the card by
  id — no title matching.
- **Handoff** — 🏹 re-sends the card to the model that **already claimed it** (its owner). It only
  appears once a card has an owner — before that, there's no target to push to, so Copy + paste is how
  first contact happens. Done cards have no Handoff.

### Keyboard shortcuts

Click a card to select it (or use the arrows), then:

| Key | Action |
| --- | --- |
| `↑` / `↓` | move the selection across the board |
| `Enter` or `h` | hand the selected card off to the model |
| `⌘C` / `Ctrl+C` | copy the selected card's prompt |
| `⇧↑` / `⇧↓` | reprioritize the selected card within its column |
| `u` or `⇧←` | on a **Blocked** card: move to To Do and clear blocked-by |
| drag | drag a **Blocked** card onto the **To Do** column to unblock |

Blocked cards also show an **✅ Unblock → To Do** button.

## How handoff reaches the model

Handoff appends the card's prompt to a local **per-agent** outbox (`.context/outbox/<agent>.md`).
Celeborn's `UserPromptSubmit` hook **drains** that agent's queue at the start of each turn and injects
the queued prompt into the session as a work instruction — so a handed-off card automatically becomes
the model's next prompt. Drained entries are archived to `.context/outbox/sent.md`; the whole
`.context/outbox/` directory is gitignored (local working state). You can drive the same queue from
the terminal:

```bash
celeborn outbox push --task t3   # queue a card (what the Handoff button does)
celeborn outbox list             # see what's pending, grouped by agent
celeborn outbox drain            # print + clear pending (what the hook does each turn)
celeborn outbox clear            # discard everything pending
```

### Routing a card to a specific model (multi-agent) — claim-on-receipt

Several agents can work one project at once (it's the same `.context/`). You don't pick a model *on
the card* — Celeborn can't know which models are live, but you do (it's which windows are open). So
**routing is where you paste**:

1. **Copy** the card (📋). The clipboard now holds its prompt plus a `⟨celeborn:t3⟩` marker.
2. **Paste it into the model you want.** That session's `UserPromptSubmit` hook reads the marker and
   runs `celeborn claim t3` — **owner ← that session, and the card moves To Do → Doing.** The
   board reflects the new owner within a few seconds.

Owner is *learned* from the claim, not chosen up front. **Last claim wins** — paste the same card into
two windows and the later one takes it (the hook says so). A session's identity comes from
`CELEBORN_AGENT` (else the session id):

```bash
CELEBORN_AGENT=grok claude     # this session claims as "grok"; you can also: celeborn claim t3 --by grok
```

Once a card has an owner, 🏹 **Handoff** re-notifies that owner via the outbox (`outbox push --task
tN`, addressed to the owner) — a follow-up nudge, not the first assignment. Cards with **no owner**
that you push instead land in a shared `_unassigned` queue any unaddressed agent drains, so the
single-agent default still "just works."

> Design + roadmap (semi-telepathic kanban, claim broadcast, Celeborn-live / Jira-report):
> `references/card-assignment.md`.
