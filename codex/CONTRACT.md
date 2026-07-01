# Harness adapter contract — validation (t70)

The Skill Advisor engine in core (`scripts/celeborn.py`) is **harness-neutral**: it speaks canonical
friction signals + neutral intents and never names a slash command or a `.claude/` path itself. Each
harness fulfills four contract responsibilities (plan/t70-skill-advisor.md §"Adapter contract"):

| Responsibility | Claude Code (core adapter) | Grok Build (`grok/` bridge) | Codex CLI (`codex/` bridge) |
|---|---|---|---|
| **friction_signals** — raw harness friction → canonical signal | `ClaudeAdapter.friction_signals`: counts generalizable `Bash(...)` literals in `.claude/settings*.json` → `permission-friction` | transcript normalized for capture (`_TOOL_MAP`, `convert_grok_transcript`); **permission friction: not implemented** — Grok's approval store is unverified (honest gap) | `codex_friction_signal`: reads `~/.codex/config.toml` — interactive `approval_policy` + workspace **not** trusted → `permission-friction` |
| **permission_target** — the friction lever | `(.claude/settings*.json, "per-command-allow")` | none productized — Grok approval mechanism unverified (matches core's "coarse approval, not yet productized" degrade) | `(~/.codex/config.toml, "approval-config")` — coarse: `approval_policy`/`sandbox_mode` + `[projects."<root>"] trust_level` |
| **render** — intent → harness idiom | `celeborn permissions --suggest/--apply` (+ `/fewer-permission-prompts`), channel `orient` | orient + kanban text (`ORIENT_NUDGE`, pending-file body), channel `pending-file` | `render_permission_hint`: the config.toml trust/approval block, channel `config` |
| **inject** — how a notice reaches the model | SessionStart `hookSpecificOutput` (`_advisor_notice` in `dispatch_hook`) | `.context/.grok-orient-pending.md` + `.grok/rules/celeborn.md` (auto-loaded) — **now also carries the advisor block** (`advisor_block`) | `.context/.codex-orient-pending.md` + managed `AGENTS.md` block (auto-loaded) — carries the advisor block |

## Grok validation result

- **Signal normalization ✓** — `convert_grok_transcript` + `_TOOL_MAP` map Grok's transcript/tool
  shape into the canonical Claude-JSONL form `celeborn capture` consumes (the same mechanic the
  engine relies on for every harness).
- **Injection carries the advisor ✓** — deliverable #6 asked whether the existing pending-file
  injection can carry an advisor notice as well as the orient load. It can, and now does:
  `hook_session_start` appends `advisor_block(ctx_root)` (a harness-neutral `celeborn advise --json`
  read) to the orient file, degrading to silence when there is nothing to recommend.
- **Permission lever — honest gap.** Grok's approval/permission storage is **unverified**, so the
  Grok bridge does not yet detect permission-friction or productize a permission fix. Core's
  `celeborn permissions` already degrades correctly for such a harness ("its lever is coarse
  approval/sandbox config, not yet productized"). When Grok's mechanism is confirmed, add a
  `friction_signals` + `permission_target` to the bridge exactly as the Codex bridge does for
  `config.toml`.

## Codex adapter result

Built as a `codex/` bridge mirroring `grok/`, with **zero core changes**:

- **Transcript bridge ✓** — `convert_codex_transcript` parses rollout `.jsonl`
  (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`): `response_item` → `message` /
  `function_call` / `function_call_output` / `local_shell_call`; tool args (a JSON-encoded string)
  normalized; `shell`/`shell_command`/`local_shell`/`exec_command` → `Bash`, `apply_patch` →
  `Edit` (pure-create → `Write`), `view_image` → `Read`.
- **Permission lever ✓** — `codex_permission_status` reads `approval_policy` / `sandbox_mode` /
  `[projects."<root>"] trust_level` without a TOML parser (stdlib-only). `permissions --suggest`
  prints the trust hint; `--apply` appends the trust block idempotently (refuses to duplicate a
  table).
- **Injection ✓** — `AGENTS.md` managed block (Codex auto-loads it every session) +
  `.context/.codex-orient-pending.md`, both carrying the advisor recommendation.
- **Hooks ✓** — `SessionStart` / `UserPromptSubmit` / `Stop` / `PreCompact` / `SessionEnd` (Codex's
  hook system shares Claude Code's event names) + the legacy `notify` (`agent-turn-complete` → Stop).

Tests: `tests/test_codex_celeborn.py` (19) + `tests/test_grok_celeborn.py` advisor-injection tests.
