# Contributing to Celeborn

Celeborn is deliberately small and dependency-free. Please keep it that way.

## Change behavior through a PR — not by editing your install

Celeborn is open core: the code is readable on purpose. But **editing the installed CLI in place is
unsupported** — a local patch breaks silently on the next update, and "I edited `celeborn.py` and it
broke" is not a reportable bug. The installed copy is meant to be immutable; `celeborn doctor` /
`celeborn integrity` will flag a modified install and tell you to reinstall to reset.

If you want different behavior, that energy is welcome — **fork and open a PR.** That's the supported
path to change Celeborn, and it benefits everyone instead of forking your one machine off the update
track. Contributions are accepted under the project's DCO/CLA (sign-off required on commits). To work
on the code, use an **editable** install so your tree *is* the install:

```bash
uv tool install --editable .     # or: pip install -e .   → `celeborn` / `cel` runs your tree
```

(An editable/source checkout ships no integrity manifest, so the self-check stays silent for you.)

## Principles

- **Markdown is the source of truth.** The SQLite index is derived and disposable; never make the
  DB authoritative or required for the markdown to be useful.
- **Stdlib only.** The CLI uses `argparse`, `sqlite3`, `json`, `pathlib`, `re` — nothing else. New
  runtime dependencies need a strong justification.
- **The Hot tier stays bounded.** Any feature that would grow what loads on Orient is suspect.
  Reach for on-demand search instead.
- **Boring technology.** Prefer stable, well-understood approaches over clever ones.

## Layout

- `scripts/celeborn.py` — the entire CLI (single file).
- `references/` — the protocol docs, SQL schema, and the templates `init` scaffolds.
- `hooks/` — optional Claude Code hooks (no-op in non-Celeborn repos).
- `SKILL.md` — the agent-facing instructions.
- `examples/sample-.context/` — a curated, schema-correct reference instance.

## Developing

```bash
pip install -e .                 # gives `celeborn` / `cel`
python3 scripts/celeborn.py --help
```

Run the test suite (stdlib `unittest`, no extra deps):

```bash
python3 -m unittest tests.test_celeborn          # full suite
python3 -m unittest tests.test_celeborn.TestIntegrity -v   # one class
```

New behavior should come with tests. The suite covers init → populate → archive → promote → index →
search → doctor, idempotency, the secret scan, and the install integrity self-check.

## Stability contract

Before changing on-disk formats, CLI verbs, or the hook protocol, read **`CONTRACTS.md`** — those are
the public, versioned surfaces other tools (and the hosted server) depend on. Internals (the module
layout, `_`-prefixed helpers, the regenerable SQLite index) are free to change. Preserve the defensive
parsing properties: tolerate unknown fields, default the missing, and never let a read of authored
memory crash a turn (hooks must degrade to silence).

## Releasing

The published package must ship a per-version integrity manifest so installs can self-verify. The build
**must** run `celeborn integrity --write` so the generated `integrity.json` is bundled into the data
package (`celeborn_refs`). It is intentionally **git-ignored** — never commit it; it is generated fresh
per release from the exact files being shipped.

## Conventions

- Match the existing style in `celeborn.py`. Keep functions small and single-purpose.
- If you change `references/schema.sql`, make sure `celeborn index` still drops and rebuilds cleanly
  (the index must remain fully regenerable).
- Update `SKILL.md`, `README.md`, and `PLAN.md` when behavior changes. Stale docs are debt.
