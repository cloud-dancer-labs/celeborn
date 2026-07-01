# Executable app — daemon architecture & packaging plan

> Status: **draft / plan**. Not yet built. Ship Celeborn as a compiled, self-contained executable built
> around a **warm local daemon + thin per-turn hooks (Option A)**, with a **local-first fallback** that
> keeps every turn correct even when the daemon is down. © Cloud Dancer, all rights reserved; distributed by Thot Technologies LLC.

## 0. Goal & why now

Three problems, one move:
- **Install friction** — replace `uv tool install` + clone + `celeborn wire` with one signed download.
- **Source protection** — the relicense made the code proprietary, but we ship readable `.py`. The
  daemon (sync protocol + optimizer + its prompt — the crown-jewel IP) ships compiled.
- **Per-turn latency** — hooks fire every turn; a cold-spawned interpreter per turn is the enemy. A
  warm daemon collapses per-turn cost to an IPC round-trip.

## 1. Architecture (chosen): warm daemon + thin clients

```
            ┌─────────────────────────── celeborn daemon (compiled, persistent) ───────────────────────────┐
            │  • hosted SYNC — holds the realtime websocket WARM (the whole point of "telepathy")           │
            │  • OPTIMIZER — Haiku .md husbandry in the background, against warm in-memory .context state    │
            │  • owns: warm state, auth tokens, sync cursor, the optimizer prompt (IP)                       │
            └───────────────────────────────────────────▲──────────────────────────────────────────────────┘
                                                         │ unix domain socket (tight-timeout request/reply)
   per turn:  hook fires ──► thin client ───────────────┘   happy path: relay warm answer, sub-ms
                                   └────────────────────────► FALLBACK (§2): run the work locally itself
```

- **Daemon = everything stateful, networked, latency-tolerant, and IP-sensitive.** Sync holds a warm
  connection (realtime requires it); the optimizer runs off the hot path against warm state; both are
  the valuable logic, so compiling them protects them.
- **Hooks = thin clients.** On the happy path they relay a warm answer from the daemon — no interpreter
  cold-start, no file re-reads, no Haiku call in the hot path. Per-turn cost ≈ socket round-trip.
- **IPC:** unix domain socket at a fixed path (`$XDG_RUNTIME_DIR/celeborn.sock`, fallback
  `~/.cache/celeborn/daemon.sock`). Request carries the hook event + stdin JSON + client version.

> This is what makes the per-turn path "fast and nimble": not a bundled Python re-launched each turn,
> but a daemon that is *already warm*. "Manage the python" = hold warm state, not re-spawn.

## 2. The fallback contract (the load-bearing part)

**Design philosophy: the daemon is a *cache/accelerator* over a path that is always complete on its own.
Celeborn is local-first — every turn is correct without the daemon; the daemon only adds realtime sync
and optimization *freshness*.** A turn must NEVER break because the daemon is down, slow, restarting, or
version-skewed.

### 2.1 What the client does every turn (decision tree)
1. **Try the daemon** — connect + request under a hard timeout (~50ms connect, ~100ms total). Include
   the client's version in the request.
2. **Hit** → use the daemon's warm answer. Done. Sub-millisecond.
3. **Miss** — *any* of: no socket, connect/read timeout, error reply, **version mismatch** — then:
   a. **Do the job locally, now.** Run the self-contained per-turn path against disk (the same code the
      daemon runs warm — see §3). The turn is fully correct.
   b. **Self-heal, non-blocking.** Fire-and-forget a daemon (re)start so the *next* turn is warm. Never
      wait on it.
   c. Never emit an error or hang. Worst case is "this one turn was a few ms slower and sync/opt were
      one interval stale."

### 2.2 What degrades, and why it's safe
| Subsystem | Daemon up | Daemon down (fallback) | Safe because |
|-----------|-----------|------------------------|--------------|
| Capture / statusline / nudge | warm, instant | run locally vs. disk | deterministic, same code path |
| **Hosted sync** | realtime | **pauses**, resumes when daemon returns | local-first; nothing is lost, just not yet pushed |
| **Optimizer (.md husbandry)** | fresh each interval | **uses last-optimized Hot tier** | last optimization is still valid, just not re-trimmed |

So fallback sacrifices only **realtime latency** and **optimization freshness** — never correctness,
never data.

### 2.3 Liveness, skew, and thundering herd
- **Liveness:** socket responds to `ping` within timeout. Secondary signal: a daemon heartbeat/pidfile
  whose mtime the client can stat cheaply.
- **Version skew** (binary upgraded, old daemon still running): version travels in the handshake;
  mismatch → client uses fallback this turn and kicks a daemon restart. Never talk to a stale daemon.
- **No thundering herd:** many hooks firing while the daemon is down must not each spawn a daemon.
  Guard the (re)start behind a lockfile / idempotent `launchctl kickstart` so exactly one start happens.
- **No-`.context` no-op preserved:** in repos without `.context/`, both daemon and fallback no-op
  exactly as the hooks do today — the safe-to-enable-globally property is kept.

### 2.4 Fallback must be tested as a first-class path
CI runs the whole hook suite **twice**: once with the daemon up, once with it forcibly down/killed
mid-turn/version-skewed. Identical observable output (modulo sync freshness). The fallback is not a
catch block — it's a supported mode.

## 3. One logic, two invocation modes (resolves "dumb client" vs. "great fallback")

The per-turn work (capture, statusline render, nudge) lives in **one Python module**. The daemon imports
it and runs it against warm state; the fallback runs the *same* module cold against disk. No duplicated
logic — two invocation modes of one implementation. The client is "dumb" on the happy path (just relay)
but **complete** on the fallback path (it carries the hot-path logic). The only things it genuinely
cannot do without the daemon are realtime sync and fresh optimization — and those degrade per §2.2.

This also subsumes the earlier "two Python dependencies" finding: today every hook shells out to
`python3 -c` to parse JSON and requires a `hooks/` dir. Collapsing the bash hooks into the
client/daemon entrypoints (`celeborn hook <event>` reading JSON stdin in-process) removes the inline
`python3`, the `bash` scripts, the `hooks/` dir, and `$CELEBORN_HOME`. **Worth doing even before any
compilation** — it's the most fragile part of the install today.

## 4. Compiler — Nuitka (recommended)

Celeborn is stdlib-only with a lazy `sqlite3` import → trivial to freeze. Differentiator is source
protection: **PyInstaller** ships decompilable `.pyc`; **Nuitka** compiles source → C → native machine
code (strong protection, often faster, no bytecode to lift). Recommend **Nuitka**; PyInstaller is the
fallback if cross-platform Nuitka builds prove too costly. Both are build-time only → the
**zero-runtime-dependency convention holds**. The daemon bundles its own runtime; the user installs no
Python/uv.

## 5. Daemon lifecycle (belt and suspenders)

- **Supervised start:** `launchd` (macOS, `KeepAlive=true` for crash-restart) / `systemd --user`
  (Linux) starts the daemon at login and revives it if it dies.
- **Lazy start backstop:** the client also (idempotently) starts the daemon on a cache-miss (§2.1b), so
  it's warm even before the first login-service tick or right after a crash.
- **Clean shutdown / upgrade:** new binary version → kick a restart (§2.3); daemon flushes the sync
  cursor and exits.

## 6. macOS signing & notarization (hard requirement)

A downloadable macOS binary that isn't **Developer-ID signed + notarized** trips Gatekeeper. Needs an
Apple Developer account, `codesign` → `notarytool` → `stapler`, wired into CI. Windows: optional
Authenticode signing. Linux: none.

## 7. Cross-platform build matrix (CI)

Native compilation can't cross-compile — build per target on its own runner:
- **macOS** (primary; dev env is darwin) — arm64 + x86_64, signed + notarized.
- **Linux** — x86_64 (+ arm64).
- **Windows** — the real gap (hooks assumed bash; daemon + `celeborn hook` subcommands mostly close it,
  but verify Claude Code hook invocation + unix-socket equivalent / named pipe there).
- Tag-driven release → signed artifacts + Homebrew tap update.

## 8. Optional later layer — GUI

Out of scope. (An earlier native-dialog path — `_gui_alert` → osascript/zenity/kdialog/notify-send — was
removed in t62: focus-stealing modal alert windows were repeatedly flagged as annoying, so heartbeats now
ride text channels only.) A menubar app (Tauri) could later surface metrics/nudges/sync-status, driving
the *same daemon* as its backend. Decoupled from shipping the executable.

## 9. Phasing

1. **Hook-collapse (no daemon, no compile).** `celeborn hook <event>` subcommands replace the bash
   hooks + inline `python3`; `wire` emits binary/script-invoking commands. The per-turn logic becomes
   one importable module (§3). Independently valuable; de-risks everything else.
2. **Daemon + fallback.** Stand up the local daemon (unix socket, warm state) and the full fallback
   contract (§2). Test both modes in CI. Still uncompiled — proves the architecture.
3. **Sync + optimizer into the daemon.** Move the warm realtime connection and the Haiku optimizer
   in as daemon subsystems (ties [`context-optimizer.md`](context-optimizer.md) here).
4. **Freeze (Nuitka, macOS).** Verify warm-path latency and fallback on a machine with no system Python.
5. **Sign + distribute; lifecycle (launchd).** Homebrew tap; installer wires + prompts `celeborn login`.
6. **Cross-platform** (Linux, then Windows). 7. **(Optional) GUI.**

## 10. Open questions

- **Latency budget:** measure warm-path IPC round-trip *and* fallback cold-path per hook. What's the
  ceiling we'll accept per turn before it's felt?
- IPC on Windows (named pipe) — does it slot into the same client contract?
- Daemon memory footprint holding warm `.context` for many projects — per-project daemon, or one daemon
  multiplexing projects?
- Auto-update: self-update vs. Homebrew vs. installer re-run, and how it coordinates the daemon restart.
- Keep a source (pip/uv editable) install path for internal dev, or binary-only?

---

*Pointers:* hook wiring → `hooks/` + `celeborn wire` (scripts/celeborn.py); optimizer / telepathy →
[`context-optimizer.md`](context-optimizer.md); bus → [`multi-agent-bus.md`](multi-agent-bus.md).
