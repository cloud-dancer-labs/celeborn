# Rehydration & compaction recovery

After any compaction, restart, or fresh thread, your conversation history is gone — but your memory
isn't. It lives in `.context/` on disk. Recover deterministically:

## Read order (do not improvise)

1. `.context/session.json` — fastest signal: current `focus`, `next_action`, `status`, `stop_allowed`.
2. `.context/state.md` — the live brief: Now, Active constraints, Open threads, Pointers.
3. `.context/durable/manifest.md` — what durable knowledge exists (read bodies only as needed).

If several agents share this project, check the Orient **tasks** block (or `celeborn tasks`) for
who's `doing` what before claiming new work, and the **touches** block for who is editing which
files (`celeborn touch` — see `references/multi-agent-editing.md`). Celeborn is the live source of
truth for in-progress cards; external issue trackers are downstream reporting only.

Then, **only if** resuming specific in-progress work:

4. The tail of `.context/journal.md` — the last few entries, for what was just happening.
5. `celeborn search "<topic>"` — for anything older, uncertain, or referenced by a pointer.

Stop once you can state the current focus and the next action. Do not read the whole journal,
the archive, or every durable doc "to be safe" — that is exactly the context bloat Celeborn exists
to avoid.

## Then

6. Identify the single next action from `session.json.next_action` (or `state.md` → Now → Next action).
7. Resume. Do not re-do completed work — `journal.md` shows what's done.
8. If anything you read is stale (a file moved, a flag renamed), trust the repo over the memory and
   fix the note as part of your next checkpoint.

## If `.context/` is missing or thin

- Missing entirely → the project isn't Celeborn-enabled yet; `celeborn init` to scaffold it.
- Present but `index.db` missing/stale → `celeborn index` to rebuild; it's derived and cheap.
- `state.md` empty but journal full → reconstruct the brief from the journal tail, then checkpoint.

## Quick path

`celeborn status` prints steps 1–3 in one shot. On Claude Code, the `SessionStart` hook runs it
automatically, so a recovered session lands pre-hydrated.

## Grok Build

Grok renews context with **`/clear`** (alias `/new`), the equivalent of Claude Code's `/clear`. Grok
does **not** inject `SessionStart` hook output into the model, so the [`grok/`](../grok/) adapter writes
the Orient load to `.context/.grok-orient-pending.md` instead: on each new session, **read that file
once, orient from it, then delete it** (or run `celeborn status` if it's absent). See
[`grok/SKILL.md`](../grok/SKILL.md).
