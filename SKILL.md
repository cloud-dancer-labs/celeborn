---
name: celeborn
description: Long-term context substrate for coding agents. Use to give a repository a small, persistent on-disk memory so sessions stay light, survive compaction, and resume cheaply across days. Trigger when the user says "set up long-term memory", "I keep losing context", "remember this across sessions", "make this project resumable", "rehydrate", "checkpoint the work", "hand this off", or when starting/resuming work in a repo that has a .context/ directory. Also use when context is getting heavy and you should offload state to disk.
license: LicenseRef-Proprietary
metadata:
  version: "0.1.0"
  homepage: https://github.com/cloud-dancer-labs/celeborn
---

# Celeborn

You are working in a project that keeps its memory on disk, in a `.context/` directory, instead of
in the conversation. This lets the session stay light and resume cheaply after compaction. Your job
is to **read from that memory cheaply** and **write back to it with discipline** so the next session
(or the next thread, or you after a compaction) lands oriented instead of lost.

The mechanical work is done by a CLI: `celeborn` (alias `cel`), or
`python3 <celeborn>/scripts/celeborn.py`. You bring the judgment — what to write, what to promote,
what to forget. See `references/memory-protocol.md` and `references/tiers.md` for the full model.

## The one rule

**Only the Hot tier enters context by default, and it is size-bounded no matter how much total
memory exists.** Everything deeper is reached on demand by targeted search returning snippets — never
by loading whole files "to be safe." That is the entire context economy. Don't break it by reading
the whole journal or every durable doc.

## Single source of truth

`.context/` is the **only** home for durable project context. Do not create or maintain parallel
context files — `CLAUDE_CONTEXT.md`, `NOTES.md`, `PROGRESS.md`, `TODO.md`, scratch hand-off docs.
They drift, they aren't tiered, and they aren't searchable. Every fact that should outlive the
conversation goes into a tier (`state.md` / `journal.md` / `learnings.md` / `durable/*`), and **every
session hydrates from Celeborn** via ORIENT — never from an ad-hoc file.

If a repo already has such a file when you arrive: migrate its live content into the right tiers, then
replace the file with a one-line pointer (e.g. *"Context now lives in `.context/` — run `celeborn
status`."*) so nothing links into a void. One source of truth, one place to read, one place to write.

## First thing, every session: ORIENT

Run `celeborn status` (the `SessionStart` hook may have already done this for you). It prints the Hot
tier: `session.json`, `state.md`, and `durable/manifest.md`. Read those, and **stop**. Pull more only
when the task needs it:

- a `durable/*.md` body — by pointer from the manifest
- the tail of `journal.md` — if resuming in-progress work
- `celeborn search "<query>"` — for anything older or uncertain (returns `file#anchor` + snippet)

Now you know the current focus and the next action. Begin.

## As you work: CHECKPOINT

After each meaningful unit of work — anything you'd hate to re-derive after a compaction:

1. **Rewrite `.context/state.md` in place** so "Now / Next action / Open threads" are current.
   Never append history here; exactly one current state.
2. **Append one entry to the bottom of `.context/journal.md`** (what you did + evidence pointer + next).
3. **Update `session.json` via `celeborn checkpoint`** — `celeborn checkpoint --focus "…" --next "…"
   --status "…"` writes valid JSON, stamps `updated_at`, and clips over-long fields. **Never hand-edit
   the raw `session.json`** — that is the recurring corruption source; the verb is the only safe writer
   (and repairs a file that's already broken). Keep `focus`/`next_action` to one line — long-form goes
   in `state.md`/`notes.md`.

If `PreCompact` fires, checkpoint immediately — the window is about to be summarized. Celeborn also
**panic-saves** automatically the moment compaction is imminent: the `PreCompact` hook snapshots the
authored tiers to `.context/.panic/<stamp>/` and surfaces a panic-save line (file count + stamp path
are live values, not hardcoded) — a recoverable restore point (bring one back with `celeborn restore`)
on top of your own checkpoint.

## Keep memory lean: FORGET & PROMOTE

- Journal over budget? → `celeborn archive` (moves old entries to `journal-archive/`, still searchable).
- `state.md` bloating? → condense; push detail to the journal or a durable doc.
- A reusable lesson emerged? → `celeborn promote --to learnings --title "..." --note "..."`.
- A lesson became a stable repo truth? → `celeborn promote --to durable --doc <name> --title "..." --note "..."`
  (also writes a `manifest.md` pointer). Delete memory you stop trusting.

Promotion flow: `journal → learnings → durable`.

## Renewing: REMIND (keep the window light)

As the live context grows, invite the user to renew it — clearing is safe because the memory is on
disk. Roughly every ~100k tokens, surface `celeborn remind --tokens <current> --last <previous>` (it
stays silent unless a new increment is crossed) and offer the host's clear command. On Claude Code
that's `/clear`, which — with the `SessionStart` hook wired — wipes history *and* reloads the Hot
tier in one step. If you can't supply a live count, feed turn deltas with `celeborn record turn
--tokens <delta>` and call `celeborn remind --auto`, which fires off Celeborn's own rolling estimate.
Portable across all coding systems — see `references/reminders.md`.

## Ending or restarting: HANDOFF

Before ending a long session, or to escape a bloated context, run `celeborn handoff`. It regenerates
`handoff.md` (branch, status, next action, risks, and a paste-ready resume prompt) from `state.md` +
`session.json`. Starting a **fresh thread** from a good handoff is the cheapest compaction there is.

## After compaction

Your history is gone but your memory isn't. Follow `references/rehydration.md`: read `session.json`
→ `state.md` → `durable/manifest.md`, then resume from the next action. Don't re-do completed work
(`journal.md` shows what's done). Trust the repo over a stale note; fix the note on your next checkpoint.

## Tracking work: TASKS & PLAN

Celeborn carries its own lightweight, agent-native task board so the work-in-flight lives in
`.context/` alongside the rest of the memory — not in a throwaway `TODO.md`. `tasks.md` is the
markdown source of truth (`celeborn tasks` edits it; one task per `## [id]` block, states
`todo | doing | blocked | done`); `.context/tasks.json` is the derived projection the optional
board viewer reads — regenerated on every `tasks` command, gitignored, disposable. The same
markdown-truth / derived-index relationship the SQLite index has.

**It loads on Orient.** `celeborn status` prints a compact board block in the Hot tier — one
count line plus the cards that need attention (everything `doing` or `blocked`). So a resuming
session sees what's in flight and what's stuck without opening anything. Pull the full board with
`celeborn tasks` (text kanban in the terminal; also refreshes `tasks.json`).

Drive it from the terminal — or let the browser board do it (`cd board && npm run dev`; reads
`tasks.json`, writes *through* the CLI so the markdown stays authoritative):

- `celeborn tasks add "<title>" [--state doing] [--owner <who>] [--tags a,b] [--phase pN]`
- `celeborn tasks move <id> <state>` · `celeborn tasks reorder <id> up|down|top|bottom`
  (order within a column **is** the priority, and it persists)
- `celeborn tasks edit <id> [--title …] [--state …] [--owner …] …` · `celeborn tasks rm <id>`
  · `celeborn tasks show <id>`

**Multi-agent kanban (semi-telepathic claims).** Several models share one `.context/` and see the same
board on orient — who's `doing` what, what's blocked. **Celeborn is the live source of truth for work;
Jira is downstream reporting.** Before claiming, read the board and pick a TODO that won't interrupt
in-flight cards (different area/tags, no `blocked_by` conflicts). Claiming broadcasts: `owner ← you`,
TODO → DOING, visible to every other agent on their next hook turn.

Two paths: (1) human copies 📋 and pastes — the `⟨celeborn:tN⟩` marker triggers `celeborn claim` on
`UserPromptSubmit`; (2) you run `celeborn claim <id> --by <your name>` after a conflict check.
Identify yourself with `--by` or `CELEBORN_AGENT` (else session id). Last claim wins. To re-notify an
owner later, push to their outbox — the hook drains it as that agent's next prompt:

**Identify your model (once per session):** a bare handle can't tell a Claude from a Grok — run
`celeborn identify --family <Claude|Grok|GPT|Gemini…> --model "<e.g. Opus 4.8>"` on orient and every
later touch/claim/ship carries it (resolution: flag → `$CELEBORN_AGENT_FAMILY`/`_MODEL` → registry).

**File touches (before editing shared code):** register with `celeborn touch <file> --task <id> --why
"<reason>"` (identity inherited) so other agents see *who* you are and *why* on orient; `celeborn touch
release <file>` when done. Never tag every line with agent metadata — see
`references/multi-agent-editing.md` for shorthand (`@touch`, `CA:`, `CM:`, `CT:`).

- `celeborn claim <id> [--by <agent>]` — take ownership (what the marker triggers on receipt)
- `celeborn outbox push --task <id> [--for <agent>]` — queue a card for an agent (the board's 🏹)
- `celeborn outbox list | drain | clear` — inspect / drain (the hook does this each turn) / discard

Full design + roadmap: `references/card-assignment.md`.

## Setup (if `.context/` doesn't exist yet)

`celeborn init` scaffolds it from templates and gitignores the derived `index.db`. **Smart init reads
the repo on first run** — it pulls the project name + one-line description (README / build manifest),
the stack and top languages, and the recent git commits, so the *first* Orient already knows what the
project is (a tiny `## Now` headline in `state.md` + a fuller "Repo snapshot" in `notes.md`; it never
clobbers files you've already written). Pass `--no-scan` to skip it. Then refine `state.md` with your
real focus + next action, `celeborn index`, and `celeborn doctor` to confirm health. Never put secrets
in `.context/` — `celeborn doctor` scans for them.

**Public repo → private memory.** By default `.context/` is committed (it travels with a *private*
repo for free). In a **public** repo that would publish your working journal, so run
`celeborn init --private` to gitignore `.context/` and move it across machines with `celeborn sync`
instead. `init` auto-detects a public repo (via `gh`) and defaults to private there; `--public` forces
the committed behavior.

## Running under Grok Build

This protocol is host-agnostic, but **Grok Build** needs a small adapter — it has its own hook format
and doesn't inject `SessionStart` output into the model. Install it with
`bash grok/scripts/install.sh --project .`; then follow the **celeborn-grok** skill. The one difference
to know: on each new Grok session, read `.context/.grok-orient-pending.md` once to Orient, then delete
it (the adapter writes it because Grok can't hand you the Orient load directly). Everything else —
ORIENT / CHECKPOINT / FORGET / PROMOTE / HANDOFF — is identical. See [`grok/SKILL.md`](grok/SKILL.md).

## Command reference

| Command | Use |
|---|---|
| `celeborn status` | Orient — print the Hot tier |
| `celeborn checkpoint [--focus … --next … --branch … --status …]` | Safe writer for `session.json` — valid JSON, stamps `updated_at`, clips over-long fields, repairs a corrupt file. **Use this instead of hand-editing the raw JSON.** |
| `celeborn index` | Rebuild the search index (derived; do after edits) |
| `celeborn search "<q>"` | Recall — snippets + `file#anchor` pointers |
| `celeborn archive` | Forget — move old journal entries to cold storage |
| `celeborn promote --to learnings\|durable ...` | Distill knowledge up a tier |
| `celeborn handoff` | Write the fresh-thread resume prompt |
| `celeborn tasks [add\|move\|reorder\|edit\|rm\|show\|list]` | Task board — `tasks.md` truth + derived `tasks.json`; loads on Orient |
| `celeborn identify --family <F> --model "<M>"` | Declare your agent family + specific model once per session (`--as <handle>` / `--show`); auto-attached to touches/claims so the board shows *who* — Claude vs Grok, and which model |
| `celeborn claim <id> [--by <agent>]` | Take ownership of a card (what a pasted `⟨celeborn:tN⟩` marker triggers) |
| `celeborn ship <id> [--note …]` | Close out a card: release its touches + move to Done (prevents stale DOING) |
| `celeborn outbox [push\|drain\|list\|clear]` | Per-agent prompt queue for handing a card to a model; the `UserPromptSubmit` hook drains it |
| `celeborn doctor` | Health check + secret scan (also reports install integrity) |
| `celeborn integrity` | Verify the installed core modules match the published release — **detects** in-place edits (a source/editable checkout reports `unverified` and stays quiet). See [`CONTRACTS.md`](CONTRACTS.md) for the public-contract line; change behavior via a PR, not by editing your install |
| `celeborn version [--check]` | Print version; `--check` looks back at GitHub for a newer Celeborn (offline-safe) |
| `celeborn capture --transcript <p>` | Mechanically ingest a Claude Code transcript into the local Automatic Context Record (`auto/*.md` + a refreshed `activity.md`) — deterministic, no model; the `Stop` hook runs it every turn |
| `celeborn panic-save [--reason …\|--quiet\|--json]` | Snapshot the authored tiers to a restore point under `.context/.panic/<stamp>/` and print the panic-save line (dynamic file count + path; see `references/memory-protocol.md`) — the `PreCompact` hook runs it automatically when compaction is imminent |
| `celeborn restore [--list\|--from <stamp>]` | Bring back a pre-compaction panic-save (most recent by default); the current files are backed up first, so a restore is reversible |
| `celeborn metrics` | Show estimated tokens saved + restarts avoided |
| `celeborn standup` / `changelog` | Mechanical "what happened" digest (done cards + commits + journal; `standup` = 1 day, `changelog` = 7); `--tweet` for a build-in-public X post |
| `celeborn flex [--tweet\|--json]` | The shareable 🏹💪 "$ Wrapped" card — tokens→$ saved + restarts avoided; `--tweet` = ≤280-char post |
| `celeborn blame <file> [--json]` | Git blame for the *why* — recent commits on a file + Celeborn memory (decisions/journal/learnings/notes) that cite it |
| `celeborn why "<topic>" [-n N\|--json]` | Decision archaeology — the decision, its date, and rationale for a topic (ranked across decisions/learnings/journal/notes); the "it remembered why from weeks ago" one-liner |
| `celeborn touch <file> [--task <id>] [--why …]` | Register a file edit with who+why (`release` / `list` / `clear`) — multi-agent protocol in `references/multi-agent-editing.md` |
| `celeborn board [--port\|--url\|--json\|--start]` | Show this project's kanban URL + de-collided per-project port (and whether it's live); `--start` launches the viewer if its port is down (the ensure-on-orient that SessionStart runs every orient) |
| `celeborn fleet [register\|unregister] [--json]` | Live multi-project agent dashboard — who's working, stuck, or idle across registered repos (`~/.config/celeborn/fleet.json`); `--json` feeds the board viewer's **Fleet** tab; hosted sync (Pro) extends across devices |
| `celeborn grok [wire\|sync-rules]` | Grok Build — `init` runs `wire` automatically when `grok` is installed; refreshes `.grok/rules/celeborn.md` (orient + kanban binding); `wire --grok` also available on `celeborn wire` |
| `celeborn advise [--json\|--dismiss ID\|--restore ID]` | Skill advisor — the throughput/quality recommendations that apply right now (e.g. permission friction); also the engine the SessionStart nudge calls. `--dismiss <id>` permanently silences one, `--restore <id>` brings it back. Harness-neutral (renders per active adapter) |
| `celeborn permissions [--suggest\|--apply] [--shared\|--yes]` | Collapse repeated permission approvals into reusable wildcard rules (Claude). `--suggest` previews (read-only); `--apply` writes the personal `settings.local.json` (`--shared` → committed `settings.json`). Safe families (read-only inspection + the trusted Celeborn CLI + test runners) are widened; un-widenable literals stay verbatim and are tallied as remaining bottlenecks |
| `celeborn record <orient\|compaction\|handoff>` | Record a memory event (hooks call this; run `record orient` manually at session start on tools without hooks) |
| `celeborn init [--private\|--public\|--no-scan]` | Scaffold `.context/` (`--private` gitignores it; auto-private in public repos). Smart init reads the repo (README/manifest/git) to pre-seed the Hot tier; `--no-scan` opts out |
| `celeborn login` | Premium: GitHub sign-in to unlock hosted sync (Pro subscription) |
| `celeborn sync [--watch]` | Premium: push/pull `.context/` to Supabase (secrets redacted out; local SQLite index never synced) |

`status` and `metrics` are read-only — inspecting never changes the numbers. See
`references/metrics.md` for how the estimate is computed.

## Two layers: authored + automatic

Celeborn keeps memory at two layers, so it never depends solely on the agent remembering to write:

- **Authored (judgment):** you rewrite `state.md`/`session.json` in real time and append to `journal.md`
  on every meaningful change. High-signal, curated. This is still your job, in the same beat as the work.
- **Automatic (mechanical, deterministic, no model):** the `Stop` hook runs `celeborn capture` every
  turn, which reads the Claude Code transcript and records structured facts — your prompt, files
  edited, commands run, commits, test results — into the local-only Automatic Context Record (`auto/*.md`,
  searchable) and an always-current `activity.md` digest that loads on Orient. Secrets are redacted; the
  authored tiers are never touched. So even if the authored Hot tier goes stale, `activity.md` +
  `celeborn search` recover what actually happened. The Automatic Context Record is **local-only**
  (gitignored — it holds verbatim prompts) but rides `celeborn sync` across your own devices.
