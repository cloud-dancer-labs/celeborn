# CMM upstream tracking (Sprint 2 "Zero Touch")

How Celeborn rides `DeusData/codebase-memory-mcp` (CMM) updates **without ever maintaining its
code**. This is the whole of §4.1 of [`plan/cmm-celeborn.md`](../plan/cmm-celeborn.md), made
operational. Golden rule, restated: **zero Celeborn edits ever land inside the CMM tree.** We depend
only on CMM's *interface* — its release artifacts, its 14 MCP tool names, and its binary CLI.

## The two surfaces we own

| Surface | What it is | Where | Maintained how |
|---|---|---|---|
| **Pin-of-record** | the pinned version + per-platform artifact URLs/checksums + the source tag/commit | [`references/cmm-pin.json`](cmm-pin.json) | advanced **only** through the gated sync routine |
| **Adapter glue** | provisioning, the contract test, the sync planner | [`scripts/celeborn_cmm_provision.py`](../scripts/celeborn_cmm_provision.py) | ordinary Celeborn code, outside the CMM tree |

There is no third surface. We never vendor, build, or patch CMM source.

## Runtime artifact — the signed release binary (CMM-6)

`celeborn cmm provision` (and `engage`, automatically) resolves this platform's entry in the pin,
downloads the artifact, **verifies its SHA256 against the pin**, and caches it under
`~/.cache/celeborn/cmm/<version>/` (honoring `$CELEBORN_CMM_CACHE` / `$XDG_CACHE_HOME`). A
tampered, truncated, or missing artifact **fails safe** — nothing is written to the cache and the
session degrades to episodic-only (CMM-10). No build step, no C toolchain, reproducible.

`cmm_binary()` resolves in this order: `$CELEBORN_CMM_BIN` → the verified provisioned cache → `cmm`
on `PATH`. That is what makes engage need *no manual install*.

> **SHA256 is the mandatory integrity gate.** Stdlib has no ed25519 verifier, so detached-signature
> verification is an optional best-effort hook (`minisign`/`signify` on `PATH`); it never blocks.

## Source presence — the read-only mirror (CMM-7)

The `source` block in the pin (`repo` + `tag` + `commit`) **is** the mirror reference: it records
exactly which upstream commit the pinned binary and the 14-tool contract correspond to. It exists
for transparency, contract-testing, and reproducibility — **never as a build dependency**.

A literal `git submodule` of `DeusData/codebase-memory-mcp` at the pinned tag is an *optional*
vendoring step. It is deferred until the upstream repo + release assets are reachable, because (a)
the binary is the runtime artifact regardless, and (b) the pin's `source` already pins the exact
commit. If/when added, it must be checked out read-only at the pinned commit and **never edited**.

## Pin discipline

**Never "latest" at runtime.** The pin only advances deliberately, through the gate below. This
protects vibe coders: an upstream change can never break their flow mid-session.

## Interface contract test (CMM-8)

`celeborn cmm contract` asserts the interface Celeborn relies on still holds:

1. exactly **14 CMM tools**;
2. the `allow`/`ask` partition is disjoint and covers all 14;
3. every identifier matches the Claude Code permission-id format;
4. *(when a binary is present)* the tool names CMM actually exposes still equal those 14.

It exits non-zero on any drift — wire it into CI. A renamed/removed tool fails loudly.

## Scheduled upstream sync (CMM-9)

`celeborn cmm sync-check` watches upstream releases and **gates every bump behind the contract
test**:

- **up to date** → nothing to do.
- **newer release, contract PASSES** → produces a bumped-pin PR plan (branch `cmm-sync/<tag>`,
  title, body, the new `cmm-pin.json`). Default is a **dry run**; `--apply` writes the branch and
  opens the PR via `gh`.
- **newer release, contract FAILS** → **flags** it and opens **no PR**. Reconciling the adapter with
  the renamed/removed tool is the *only* maintenance the sync routine ever asks of us — tiny, and
  interface-level.

Artifacts whose checksum the release feed didn't provide are bumped as `"pending": true`, so a
half-known PR can never ship an unverifiable pin (provision refuses pending artifacts).

To run it on a schedule, point a routine/cron at `celeborn cmm sync-check --apply` (e.g. weekly).
The schedule itself is environment-specific and intentionally not committed.

## Graceful degrade (CMM-10)

Every entry point above returns a status dict and **never raises**. Provision failure, an
unreachable feed, an absent binary, a failed index — all degrade to episodic-only memory, are
logged, and never surface a mid-flow error. The permission pre-clear (S1, the flow headline) lands
regardless.
