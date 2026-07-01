# Multi-agent editing — protocol & shorthand

> Status: **v2 built** (`celeborn touch` + orient surfacing — `2026-06-10`; agent identity
> family/model/why via `celeborn identify` — `2026-06-14`). How concurrent agents
> avoid stepping on each other's uncommitted edits **without** polluting source with per-line tags.
> Complements [card-assignment.md](card-assignment.md) (task-level claims) and
> `celeborn blame` (post-commit why).
> © Cloud Dancer, all rights reserved; distributed by Thot Technologies LLC.

## The problem

Kanban claims broadcast **which card** is in flight (`t28 @ grok`). Git broadcasts **who committed
what** after the fact. Neither answers: *"who is editing `scripts/celeborn.py` right now?"*

Uncommitted overlap is the failure mode — two agents edit the same hot file, neither knows until
someone complains or a merge goes sideways.

## What we do NOT do

**Never tag every line of code** with model id, date, and timezone. That was CVS `$Author$` thinking;
it bloats diffs, goes stale on refactor, and duplicates what git blame already provides for committed
code. See the analysis in the design discussion that led to this doc.

## The protocol (all agents)

0. **Identify** yourself once per session so touches name *who* you are, not just a handle:
   `celeborn identify --family <Claude|Grok|GPT|Gemini…> --model "<e.g. Opus 4.8>"`. Stored in the
   local `.context/.agents.json` registry (keyed by handle) and auto-attached to every later
   touch/claim/ship. Two threads of the *same* model? give each a distinct handle (`--by` /
   `$CELEBORN_AGENT`).
1. **Claim** the kanban card before substantive work (`celeborn claim <id> --by <your name>`).
2. **Touch** each shared file before you edit it
   (`celeborn touch <file> --task <id> --why "<reason>"`). The `--why` is what lets the next agent
   tell a deliberate edit from a collision; your identity (family/model) rides along automatically.
3. **Orient** — on every wake, read the touches block in `celeborn status`. If another agent's touch
   is fresh on a file you need, coordinate (wait, split work, or ask the human) before editing.
4. **Checkpoint** — one journal line when you start and finish a file edit (human-readable backup).
5. **Ship** when the card is finished (`celeborn ship <id>` — releases all touches for that task and
   moves it to Done). Releasing the last touch on a still-DOING card triggers a nudge to ship.
6. **Release** individual files when pausing mid-card (`celeborn touch release <file>`).
7. **Commit** with trailers so `celeborn blame` can link code → memory (see shorthand below).
8. **Never** embed per-line agent metadata in source.

Touches live in `.context/touches.json` (local, gitignored, seconds-fresh). Stale entries expire
after `touch_ttl_hours` (default 2) in `.celebornrc`.

## Shorthand reference

Use these in journal entries, commit messages, and touch notes — not in code.

| Token | Meaning | Example |
|-------|---------|---------|
| `@touch A/D/T` | Agent **A** registered file touch at UTC **D** for task **T** | `@touch grok/2026-06-10T10:16Z/t28` |
| `@done A/T` | Agent **A** released touch for task **T** | `@done grok/t28` |
| `CA:A` | Commit trailer: `Celeborn-Agent: A` | `CA:grok` |
| `CM:M` | Commit trailer: `Celeborn-Model: M` (family/model) | `CM:Claude Opus 4.8` |
| `CT:T` | Commit trailer: `Celeborn-Task: T` | `CT:t28` |
| `CF:path` | Commit touched **path** (optional, in body) | `CF:scripts/celeborn.py` |

**Date format:** ISO-8601 UTC with `Z` suffix (Greenwich mean time). Compact, sortable, no ambiguity:
`2026-06-10T10:16Z` (minute precision is enough for journal; touch registry stores full seconds).

**Agent id (A):** prefer a human-readable name (`grok`, `claude`, `opus-a`) via `--by` or
`$CELEBORN_AGENT`. Session UUIDs are a fallback only — they read poorly on the board and in touches.

**Family + model (who, specifically):** a bare handle can't tell a Claude from a Grok, or one
Claude model from another. `celeborn identify --family Claude --model "Opus 4.8"` records both in the
local registry; resolution precedence per command is **flag → env (`CELEBORN_AGENT_FAMILY` /
`CELEBORN_AGENT_MODEL`) → registry**. Touches embed the resolved values inline (self-describing even
if the cache is wiped); the derived `tasks.json` joins the registry so the board owner chip can show
`@claude · Opus 4.8` while committed `tasks.md` stays handle-only.

### Journal one-liners (copy-paste patterns)

```
@touch grok/2026-06-10T10:16Z/t28 — editing scripts/celeborn.py (celeborn blame)
```

```
@done grok/t28 — released scripts/celeborn.py; 274 lines, tests green
```

### Commit trailer block (append to commit message)

```
Celeborn-Agent: grok
Celeborn-Model: Grok 4
Celeborn-Task: t28
```

Shorthand in the body: `CA:grok CM:Grok 4 CT:t28 CF:scripts/celeborn.py`

## CLI

```bash
celeborn identify --family <F> --model "<M>"          # once per session (--as <handle> / --show)
celeborn touch <file> [--task <id>] [--why <reason>]  # register before editing (identity inherited)
celeborn touch release <file> [--by <agent>]          # drop your touch
celeborn touch list [--json]                          # all active touches (shows family · model — why)
celeborn touch clear [--by <agent>]                   # wipe (yours, or all)
```

`--family` / `--model` can also be passed directly to `touch` / `claim` / `ship` to override or
seed the registry without a separate `identify` call.

`celeborn status` (Orient) prints active touches alongside the task board so every agent sees them on
wake.

## Layering

| Layer | Granularity | Latency | Mechanism |
|-------|-------------|---------|-----------|
| Kanban claim | Task / card | Seconds | `tasks.md` owner + DOING |
| File touch | Path | Seconds | `.context/touches.json` |
| Journal | Human narrative | Minutes | `journal.md` `@touch` / `@done` lines |
| Git + blame | Committed lines | On commit | trailers + `celeborn blame` |

## Open questions

- Auto-touch from capture when an agent's Write/Edit targets a path (reduces forget-to-touch).
- Warn (not block) when touching a file another agent already holds.
- Bus channel for touch events when [multi-agent-bus.md](multi-agent-bus.md) lands.

---

*Pointers:* kanban claims → [card-assignment.md](card-assignment.md); post-commit why → `celeborn blame`;
install rules → `CLAUDE.md` / `AGENTS.md` managed blocks (regenerated by `celeborn init`).