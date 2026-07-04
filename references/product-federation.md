# Product federation (`celeborn product`) — CELE-t190

A Celeborn "project" is one repo = one `.context/`. But the real unit of work is often a **product**
that spans several repos with different rules — a public client, a private server, vendored/forked OSS.
The **product registry** names each repo-**facet**, its **role**, and its **publish policy**, so every
agent knows on orient what facets exist and which are present on this machine.

This is **Layer A** of CELE-t188 (`plan/cele-t188-multi-repo-oss-stewardship.md`). Layer B (multi-repo
git/PR ops), Layer C (OSS provenance + guard, CELE-t192), and Layer D (the public-README stewardship
claim, CELE-t193) all build on this registry.

## The two files (authored-vs-machine split)

Mirrors Celeborn's existing `.context/` split exactly — product facts travel via git, machine paths never do.

| File | Committed? | Holds |
|---|---|---|
| `.context/product.md` | ✅ committed | Product **facts**: facet keys, roles, publish policy, canonical repo URLs, OSS provenance (Layer C). Portable across every clone. |
| `.context/product-local.json` | ❌ gitignored | This machine's **checkout paths** — one binding per facet key. Resolved fresh per machine. |

A facet declared in `product.md` with **no binding on this machine** degrades gracefully to *"not
present here"* (marker `—`) rather than following a dead path. That is the property that makes the split
correct: the same committed `product.md` works on every machine, bound or not.

## Roles

| Role | Meaning | Publish policy | We own it? |
|---|---|---|---|
| `client:public` | Published client | Publish to PyPI (gated: BUSL relicense, CELE-t168) | ✅ |
| `server:private` | Private server | **Never publish** — full rights reserved | ✅ |
| `oss:upstream` | External OSS we track / forked from | Contribute via fork → PR | ❌ |
| `oss:dependency` | Vendored dep / submodule in our tree | Local vendor OR upstream PR | ❌ |
| `oss:fork` | Our fork of an upstream | Push to fork; PR back upstream | ❌ (steward) |

Publish/provenance **guards** that read these roles are specified in the t188 plan (§6) and land on the
Layer B/C cards — never ship a guard before the data it reads is real.

## Commands

```sh
celeborn product init [--name <n>]            # scaffold .context/product.md (committed)
celeborn product add <key> --role <role> \    # add/update a facet (product FACTS only, no local paths)
    [--publish <policy>] [--repo <url>] [--upstream <url>]
celeborn product bind <key> <checkout>        # bind a facet → this machine's checkout (gitignored)
celeborn product [list]                        # print the facet table (roles · publish · bound/unbound here)
```

`add` is an upsert — re-running it for an existing key edits that facet. `bind` writes only to the
gitignored `product-local.json`; it never touches `product.md`.

## Orient banner

When a `product.md` exists, orient leads with a single budget-safe line (silent otherwise — no nag for
single-repo projects):

```
🏹 Celeborn product —> Celeborn · 2 facets: client (client:public ✓) · server (server:private —)
```

`✓` = bound + the checkout exists on this machine; `—` = declared but unbound (or the bound path is
missing) here. Overflow truncates with `+N more`.

## Layer B — multi-repo git/PR ops (CELE-t191)

Once facets are declared (Layer A) and **bound** on this machine, three commands route git — and a
*drafted* `gh pr create` — to the bound checkout, so a single board coordinates work across every repo
of the product:

```sh
celeborn commit --facet <key> -m "<msg>" [files…]   # git commit in the facet's checkout
celeborn push   --facet <key> [remote] [branch]     # git push, routed to the checkout
celeborn pr     --facet <key> [--base <b>]           # DRAFT a PR (prints a gh command; never sends)
```

- **`commit`** stages the named files (never `git add -A`), commits them in the facet's checkout, and
  appends the attribution trailers automatically: `Celeborn-Task: <tN>` (bare id — the machine-parsed
  convention), `Celeborn-Agent: <handle>`, `Celeborn-Model: <family · model>`. The task defaults to this
  session's `doing` card. It also registers a **cross-repo touch** (`<key>:<file>`) in *this* project's
  `.context/`, so other agents see the facet activity on orient even though the file lives in another repo.
  Omit the files to commit what's already staged.
- **`push`** routes `git push`. A branch push (even to a private repo's own remote) is fine; a **release**
  push (`--tags` / `--follow-tags`) into a `server:private`/`oss:*` facet is refused in-command under the
  publish policy (see below) — Celeborn enforces it here because the PreToolUse guard can't see the git that
  runs *inside* `celeborn`.
- **`pr`** is **draft-only** — Celeborn never auto-opens a PR against anyone's repo. It computes the branch,
  base, and commits ahead; composes a title + body with provenance; and prints a ready-to-run
  `gh pr create …` for you to review and send. For `oss:*` facets it also prints the fork → PR steps
  (`gh repo fork`, push to your fork, PR upstream).

Any of these `die`s with a corrective hint if the facet is undeclared, unbound on this machine, or its
bound path is missing / not a git repo — the same graceful-degradation contract as the orient banner, but
a hard stop because a git op has nowhere to run without a real checkout.

## The publish guard (CELE-t191, t188 §6)

A new **PreToolUse lever** on the existing guard rail (alongside the `cd … > file` redirect guard), with
the same soft/hard-DENY vocabulary and escape-hatch convention. A publish/release action — `twine upload`,
`python -m twine`, `flit`/`poetry`/`hatch`/`maturin publish`, `npm`/`pnpm`/`yarn`/`bun publish`,
`cargo publish`, `gh release create`, or a tag push (`git push --tags`/`--follow-tags`) — is checked
against the registry:

- targeting a **`server:private`** facet → **hard-DENY** (private, full rights reserved; never publishes);
- targeting any **`oss:*`** facet → **hard-DENY** (stewarded OSS; contribute via fork → PR, never
  publish-as-ours);
- targeting a **`client:public`** facet → allowed (still honoring the BUSL relicense gate elsewhere).

The guard resolves the target facet from a path in the command that lands inside a bound checkout, or —
when the command names no such path — from the project the command runs in. It is **silent** for
single-repo projects (no `product.md`) and for any non-publish command; the cheap publish-action regex runs
first, so only publish-shaped Bash calls ever pay the registry lookup. The accepted-risk override mirrors
`# celeborn:allow-redirect`: a trailing `# celeborn:allow-publish: <why>` comment auto-ALLOWs the command.

## Example

See `examples/sample-.context/product.md` for Celeborn's own two-facet registry (Celeborn's live
`.context/` is gitignored, so the format is demonstrated there).
