# The memory economy estimate

Celeborn keeps a running, **honest** estimate of what the substrate buys you, in
`.context/metrics.json`. Two headline numbers, both deliberately conservative and clearly labelled
as estimates.

## Tokens saved

Every time the agent **Orients** (or bridges a compaction), it loads the **Hot tier** —
`state.md` + `session.json` + `durable/manifest.md` — instead of all of `.context/`. The estimate
for that load event is:

```
saved = tokens(all of .context/)  −  tokens(Hot tier)
```

where `tokens(text) ≈ len(text) / chars_per_token` (default 4, configurable in `.celebornrc`).
"All of `.context/`" is every committed `.md`/`.json` **except** the derived index, the config, and
the regenerated `handoff.md`. The cumulative figure is the sum across load events.

This is explicitly a counterfactual: *"if you had naively loaded all project memory into context on
every session, you'd have spent this many more tokens."* It is **not** a claim about tokens the model
didn't generate. It grows as memory accumulates (the gap between Hot and total widens) and as
sessions recur.

## Restarts avoided

A "restart" is a point where, **without** persistent on-disk memory, the agent would have started
cold. Celeborn counts two real ones:

- **session resume** — a *new* session Oriented onto existing memory (a cold start avoided). Deduped
  by session id so re-running `status` or re-Orienting within one session doesn't inflate it.
- **compaction bridged** — the `PreCompact` hook fired and the memory carried the session through the
  window summarization.

```
restarts avoided = session resumes + compactions bridged
```

## How events are recorded

Recording is event-driven, never a side effect of *looking*:

- `celeborn status` and `celeborn metrics` are **read-only** — inspecting never changes the numbers.
- The `SessionStart` hook runs `celeborn record orient --session <id>` (the id dedupes repeats).
- The `PreCompact` hook runs `celeborn record compaction`.
- `celeborn handoff` increments the handoff counter.

On tools without hooks (Codex, Claude.ai), the agent can run `celeborn record orient` once at the
start of a session to keep the estimate alive; if it doesn't, only the token-saving *capability*
goes unmeasured — the substrate still works.

## Caveats (stated plainly)

- It's a **character-ratio estimate**, not a tokenizer. Good enough for a trend, not for billing.
- The token-saved baseline assumes the naive alternative is "load everything," which is the strawman
  Celeborn exists to beat — so read it as "context cost avoided vs. no memory discipline."
- The numbers are local to one repo's `.context/` and travel with it in git.
