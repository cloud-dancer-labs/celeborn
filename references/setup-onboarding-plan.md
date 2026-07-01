# Install like Modal — `celeborn setup` + onboarding plan (CELE-t120)

> **BUILT 2026-06-23** — `celeborn setup` shipped (scripts/celeborn.py: `cmd_setup` + helpers,
> `setup` subparser; `TestSetup`, 8 tests) and the README `## Install` now leads with the two-step
> flow. **One refinement from the plan below:** the implemented order is **wire → init → login** (not
> wire → login → init) so the local-first project is fully scaffolded even if the browser sign-in is the
> one step that doesn't complete; login is the final, gated step, and a failed interactive login WARNS
> and lets setup finish rather than aborting (wire+init are already usable). Still outstanding: the
> celeborn.thot.ai install block (built under CELE-t104, spec below).

> Status (original): **plan, not built.** Captures how to collapse Celeborn's current 3–4 step install into a
> Modal-clean flow: a package-manager install followed by one guided `celeborn setup` command, mirrored
> by a copy-paste install block on the celeborn.thot.ai home page. No code in this card — the build is a
> follow-up. Auth decision is locked: **browser login is a required step of `setup`** (Modal parity).
>
> **Coordinates with [CELE-t104]** (celeborn.thot.ai as the home page — "Get started with Celeborn for
> free →"). The website install-guidance section below is *specified here* but *built under CELE-t104*
> so the two cards don't both edit `web/`. This doc is the source of truth for the install copy CELE-t104
> renders.

## The goal — what "like Modal" means

Modal's first-run experience is two commands and a browser tab:

```bash
pip install modal
python3 -m modal setup     # opens a browser, creates an API token, you're done
```

Then you run code. That's the whole onboarding. Celeborn today is good but longer — install the command,
`wire --global`, `init` per project, and (separately) `login --github` if you want the hosted board. The
goal of CELE-t120 is to make Celeborn's first run feel like Modal's: **one install + one `setup`**, with
the browser auth folded in as a required step, and the same two-step block shown prominently on the site.

## Current install (the baseline we're compressing)

Four moving parts today (see [README.md](../README.md#install)):

1. **Install the command** — `brew` / `winget` / `scoop` (native binaries) or `uv tool install` / `pip`.
   *(Stays as-is — this is the package-manager step, the analog of `pip install modal`.)*
2. **`celeborn wire --global`** — merges statusLine + the 5 hook groups into `~/.claude/settings.json`,
   plus the safe permission baseline and the Matt Pocock skill suite. Idempotent, ask-wins.
3. **`celeborn init`** (per project) — scaffolds `.context/`, annotates CLAUDE.md, prompts for a project
   name, opens the localhost board (CELE-t121), and engages CMM if present.
4. **`celeborn login --github`** (separate, optional today) — browser PKCE auth for the hosted board at
   celeborn.thot.ai.

The pieces already exist and are individually solid. The cost is *sequencing and discovery*: a new user
has to know to run three commands in the right order, and login is easy to miss.

## Proposed flow — two steps, Modal-clean

```bash
# 1 — install the command (pick your package manager; unchanged)
brew install cloud-dancer-labs/celeborn/celeborn        # or winget / scoop / uv / pip

# 2 — one guided setup (new)
celeborn setup
```

`celeborn setup` is a new top-level command that orchestrates the existing verbs in order, each
idempotent and skippable-if-already-done, narrating as it goes:

1. **Wire hooks** → calls the existing `wire --global` path (statusLine + hooks + permission baseline +
   skills). Detects an existing wire and reports "already wired" instead of re-doing it.
2. **Browser login (required, Modal parity)** → calls the existing `login --github` (browser PKCE).
   Opens a tab, user authorizes, token lands. This is the locked decision: setup does not complete
   without an authenticated account. *(Escape hatches below for CI/headless.)*
3. **Scaffold the current project** → calls `init` if run inside a project dir (prompts for name, opens
   the board). If not in a project, prints the one-liner to run later (`cd your-project && celeborn init`).
4. **Print a Modal-style "you're ready" next-step** — the board URL (localhost:3141) and the single
   command to begin, so the terminal ends on a clear call-to-action, not a wall of output.

### Design constraints / open questions for the build card

- **Idempotent + resumable.** Re-running `celeborn setup` after a partial run must no-op the done steps
  and resume the rest — same contract as `wire`. No duplicate hooks, no second login if a valid session
  exists (`whoami` check first).
- **Headless / CI escape hatch.** The "required browser login" is a *human-install* rule. `setup` must
  detect non-interactivity (reuse `_init_is_interactive()`, the TTY gate from CELE-t121) and in
  CI/headless mode either skip login with a clear notice or accept a token via env
  (`CELEBORN_TOKEN`/device-code). Decide which in the build card; do not block automated installs on a
  browser that can't open.
- **Flags** to mirror the underlying verbs: `--no-skills`, `--no-permission-baseline` (pass-through to
  wire), `--name`/`--no-open`/`--no-browser` (pass-through to init), and a `--no-login` opt-out for users
  who only want the local-first experience despite the Modal-parity default. (The default *prompts/runs*
  login; `--no-login` is the documented way out.)
- **Keep the individual verbs.** `wire` / `init` / `login` stay as first-class commands; `setup` is a
  thin orchestrator over them, not a reimplementation. This keeps the advanced/manual path intact and
  keeps `setup` a small, testable shell.
- **Naming.** `celeborn setup` reads closest to `modal setup`. `onboard` / `quickstart` are alternatives;
  recommend `setup` for the muscle-memory match. (`init` is taken and means per-project scaffold.)

## README rewrite (deliverable 2 — docs)

Rewrite [README.md](../README.md#install) `## Install` to lead with the two-step flow, the way Modal's
"Create your first app" page reads — short, linear, copy-pasteable — then keep the current detailed
breakdown below a fold for power users:

- **Top:** "Two steps" — the package-manager install, then `celeborn setup`. Mirror Modal's terse voice.
- **Below:** the existing per-step detail (what `wire` merges, the permission baseline, skills scope, CMM)
  stays as "What `setup` does / advanced & manual install" reference, so nothing is lost for the people
  who want to wire by hand.
- Cross-link the website Get-Started block so README and site say the same two commands verbatim.

## Website install guidance (deliverable 3 — specified here, built under CELE-t104)

celeborn.thot.ai (CELE-t104) is being rebuilt as the home page whose every CTA is "Get started with
Celeborn for free →". This plan supplies the **install block** that page should render:

- A prominent, Modal-styled **"Create your first project"** panel near the top: the two commands
  (`brew install …` / `celeborn setup`) in a dark code block with **copy buttons**, matching the site's
  `:root` dark tokens.
- A one-line explainer under each command (what it does), exactly like Modal's annotations
  ("install the client" / "authenticate through your browser").
- The "Get started with Celeborn for free →" button (CELE-t104's recurring CTA) points at this block /
  the GitHub-app install.
- **Boundary:** CELE-t104 owns the page build and `web/` edits; this card owns the *content/spec* of the
  install block. When CELE-t104 is built, lift the copy from here. Note in CELE-t104 that the install
  block content is specified in this doc.

## Suggested build sequencing (for the follow-up card)

1. Land `celeborn setup` orchestrator + tests (idempotency, TTY gate, flag pass-through, resume). CLI only.
2. Rewrite README `## Install` to lead with the two-step flow.
3. Feed the install block spec into CELE-t104 when the home page is built.

Each is independently shippable; (1) is the keystone.
