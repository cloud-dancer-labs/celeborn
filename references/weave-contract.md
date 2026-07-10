# The sovereign weave — contract (CELE-t373)

How Celeborn blends **OpenCode** (harness) + **Ollama** (model runtime) + **Qwen3-4b**
(local model, persona *Pippin*) into one working, free, local agent stack **without ever
owning, vendoring, or rebranding any of them.** This is the engineering form of the
operator's sovereignty requirement and the first spine card of the CELE-t372 epic
([plan/cele-t372-t144-realization.md](../plan/cele-t372-t144-realization.md) §2). CELE-t374
(sovereign install) and CELE-t375 (engine lifecycle) build to this contract; marketing copy
quotes it.

**Golden rule (restated from CELE-t256):** *Celeborn orchestrates; upstream delivers.* Every
component stays visibly and mechanically an independent upstream project. Celeborn owns only
two surfaces — the **pin-of-record** ([weave-pin.json](weave-pin.json)) and the **adapter
glue** (the plugin, the config merge, the install/lifecycle code). Zero Celeborn edits ever
land inside an upstream tree.

---

## 1. The pins

Machine-readable source: [weave-pin.json](weave-pin.json). Human summary:

| Component | Pin | Publisher | License | Official channel |
|---|---|---|---|---|
| **OpenCode** | `1.17.13` | anomalyco | MIT | `curl -fsSL https://opencode.ai/install \| bash` |
| **Ollama** | floor `0.31.1` (floats above) | Ollama | MIT | `curl -fsSL https://ollama.com/install.sh \| sh` · `brew install ollama` |
| **Pippin · PM** (non-thinking) | `qwen3:4b-instruct` (2.5 GB) | Qwen team, Alibaba Cloud | Apache-2.0 | `ollama pull qwen3:4b-instruct` |
| **Pippin · ghost** (thinking) | `qwen3:4b` (2.5 GB) | Qwen team, Alibaba Cloud | Apache-2.0 | `ollama pull qwen3:4b` |

**OpenCode version — one source of truth.** The pin *is* the `@opencode-ai/plugin` version
declared in [`opencode/package.json`](../opencode/package.json) (today `1.17.13`) — the exact
release the plugin is typechecked against. `weave-pin.json` mirrors it for machine reads; bump
both together, never independently. OpenCode moves ~43 commits/day, so the pin is a real tested
tag, never "latest."

**Ollama is a floor, not an exact pin.** Ollama's API surface (`/api/tags`, `/api/pull`,
`/v1/chat/completions`) is stable; we require a minimum (`0.31.1`, the tested version) and let
it float upward. No sha256 gate — Ollama installs from its own official channel.

**Qwen — the `qwen-4b` correction (CELE-t373 finding).** The old default `qwen-4b`
([scripts/celeborn.py](../scripts/celeborn.py) `pm_model`, the
[opencode.json](../opencode/opencode.json) provider key, `project-manager.md`) is **not a
pullable registry tag** — it only ever resolved through a hand-made local alias (`ollama cp`),
so `ollama pull qwen-4b` fails on a fresh machine. It is retired in favor of the two real
upstream tags above. Migrating those code refs is CELE-t374's first task. We do **not** re-create
the alias (see rule 1).

---

## 2. The five sovereignty rules

### 1 — Install from upstream official channels only
OpenCode via its official installer at the pinned version; Ollama via its official
installer/brew; Qwen via `ollama pull` of the pinned tags. **Never vendor, mirror, rebrand, or
locally rename** an upstream binary or model. Celeborn's installer *orchestrates* the upstream
installers with the same consent style [`/install`](https://celeborncode.ai/install) uses
today — it never ships a copy. *(This is why the `qwen-4b` local alias is retired rather than
re-created: `ollama cp qwen3:4b qwen-4b` is a local rebrand of an upstream artifact.)*

### 2 — Attribution at install time
Each step announces the project, its publisher, and its license, with a link, before it acts.
The exact lines (verbatim, reused in the Settings OpenCode/Ollama/Model sections):

> Installing **OpenCode** — an independent open-source AI coding agent by **anomalyco**, **MIT**
> license · https://opencode.ai
>
> Installing **Ollama** — an independent open-source model runtime by **Ollama**, **MIT**
> license · https://ollama.com
>
> Pulling **Qwen3-4b** — an open-weight model by the **Qwen team, Alibaba Cloud**, **Apache-2.0**
> license · https://qwenlm.github.io/blog/qwen3 · Celeborn runs it locally as **Pippin**.

The word "Pippin" is Celeborn's persona name for our *use* of the model; it never overwrites or
hides the upstream identity, which is always shown alongside.

### 3 — Pinned, tested versions; pins move only via a Celeborn release
The versions in §1 are what the plugin and board are tested against. They advance **only**
deliberately, in a Celeborn release — never "latest" at runtime, so an upstream change can never
break a vibe-coder's flow mid-session. When upstream ships something newer, the bump is a
reviewed change to `weave-pin.json` + `opencode/package.json` in a release, mirroring the gated
`celeborn cmm sync-check` discipline in [cmm-upstream.md](cmm-upstream.md).

### 4 — Upstream update paths stay intact (doctor explains drift, never blocks)
A user who updates OpenCode or Ollama themselves must never be broken silently. `celeborn
doctor` compares installed-versus-pin and **explains** any drift (§4 wording below). It never
pins them down, never reverts them, never refuses to run. Their tools remain theirs.

### 5 — Uninstall independence, both directions
Removing Celeborn leaves OpenCode/Ollama/Qwen fully working standalone (Celeborn only ever
*added* a plugin file, a config block, and pulled models — all inert without Celeborn). Removing
OpenCode/Ollama leaves Celeborn fully working with Claude Code or Grok Build. The weave is a
convenience, never a lock-in.

---

## 3. Pippin — the local-model persona

Continuing the Tolkien theme (Celeborn → Pippin), the local Qwen3-4b presence is named **Pippin**:
helpful and eager, but *not too smart* — occasionally, innocently troublesome without meaning to
be. That is simply the nature of a 4b model, and the persona owns it honestly rather than
pretending to be an oracle. Pippin has two modes, backed by the two pinned tags:

| Mode | Model | Character | Job |
|---|---|---|---|
| **Pippin · PM** | `qwen3:4b-instruct` (non-thinking) | decisive, literal, zero-reasoning | the wired, highly-specified work already built + planned: the PM march loop / board formatter that **phrases board lines, never decides** (CELE-t283). Determinism is the feature. |
| **Pippin · ghost in the machine** | `qwen3:4b` (thinking) | curious, chatty, sometimes wrong | a general local helper — answers "how do I…", points at [celeborncode.ai/faq](https://celeborncode.ai/faq), analyzes dependencies, does small local tasks. Never trusted with decisions; always cheap and offline. |

**Why two tags, not one.** The PM role must be deterministic and non-thinking — reasoning is a
*bug* there. The ghost role wants light reasoning to be conversational. Same 2.5 GB family, two
tags, one persona. (`qwen3:4b`'s thinking mode is used for the ghost only.)

**Pippin gets smarter over time — by instruction, not by swapping the model.** More capable
models (Opus, Fable) author instruction sets / prompt scaffolds that make Pippin *seem* more
intelligent and be more genuinely helpful within its size. The model stays the pinned 4b; the
scaffolding around it improves. This keeps the local stack free and small while its usefulness
compounds. *(Building the ghost agent, its FAQ-reference tool, and the instruction-set loop is
downstream work — CELE-t374+ / a follow-on card — not this contract. This contract only names
it and pins its model.)*

---

## 4. Drift policy — the wording `celeborn doctor` prints

Doctor is advisory (rule 4). It prints one of these and **always exits without blocking the
weave**:

**OpenCode drift** (installed ≠ pin):
```
⚠ OpenCode <installed> is installed; Celeborn's plugin is tested against <pinned>.
  This is fine — nothing is blocked. If the board Stage or PM misbehaves, run
  `celeborn opencode wire` to re-pin the plugin, or install the tested version from
  https://opencode.ai. Celeborn never changes your OpenCode for you.
```

**Ollama below floor** (installed < floor):
```
⚠ Ollama <installed> is below Celeborn's tested floor <floor>. The engine may still work;
  if `ollama pull` or serve behaves oddly, update Ollama from https://ollama.com. Not blocked.
```

**Pippin model not pulled** (pinned tag absent from `ollama list`):
```
• Pippin's model <tag> isn't pulled yet (~2.5 GB). Run `celeborn ollama pull <tag>` to enable
  the local PM/assistant. Optional — Celeborn runs fine on Claude Code or Grok without it.
```

**Stale `qwen-4b` alias detected** (legacy local alias present):
```
• A local `qwen-4b` alias is present — that name is retired. Celeborn now uses the upstream
  tags qwen3:4b-instruct (Pippin·PM) and qwen3:4b (Pippin·ghost). The alias is harmless; you
  may `ollama rm qwen-4b` once nothing references it.
```

**All aligned:**
```
✓ Sovereign weave aligned: OpenCode <v>, Ollama <v>, Pippin qwen3:4b-instruct + qwen3:4b.
```

---

## 5. What this contract binds

- **CELE-t374 (sovereign install)** detects/installs each component from the channels in §1,
  prints the §2 attribution lines, migrates `qwen-4b` → the real tags, and pulls both Pippin
  models. It never touches an upstream tree.
- **CELE-t375 (engine lifecycle)** implements the §4 doctor checks and the start/stop/health of
  `opencode serve` + the Ollama daemon.
- **Marketing / FAQ (CELE-t377/t378)** quote §1–§3 as the answer to "I don't have an AI coding
  assistant" — the sovereign weave *is* that answer: a complete, free, local, honestly-attributed
  stack.

Bumping any pin is a reviewed change to [weave-pin.json](weave-pin.json) (+ `opencode/package.json`
for OpenCode) landed in a Celeborn release. Nothing else in this file changes without operator
review.
