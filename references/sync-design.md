# Sync / "code anywhere" — design (Phase 8, future)

> Status: **design, not built.** Captures the architecture for keeping a project's `.context/` in
> sync across devices and agents in near-real-time. Phases 0–7 do not depend on any of this; sync is
> purely additive.
>
> **Commercial boundary:** *all* sync (8a **and** 8b) is a **premium feature**. The open-source CLI is
> fully functional **locally**; anything that moves `.context/` between machines lives behind a
> license/account gate. See [Free / premium boundary & gating](#free--premium-boundary--gating).

## The goal

Make a Celeborn project *transportable in real time*. Concretely, the north-star scenario:

> I'm at a friend's house with only my phone, running Claude Code on the phone. My laptop at home is
> running a multi-agent batch on the same project, also using Celeborn. I see the laptop's context
> updates on my phone **live**, I can steer the project by editing context from my phone, and I can
> even code on the phone in parallel **without poisoning** the laptop's run.

## The one principle that makes it cheap

**Sync the markdown; never sync the SQLite index.** The index is *derived and disposable* — every
device rebuilds its own from the markdown in ~35 ms. So the sync substrate only ever moves
`.context/**/*.md` and `session.json`/`metrics.json` (small, text, diffable). Transferring a live
binary `.sqlite` would be wasteful and conflict-prone, and is never done. This is the same design
choice (markdown = truth, index = regenerable) that already makes a Celeborn repo portable via git.

## Two independent planes

The "without poisoning" requirement is really a request to keep two *different* things in sync with
two *different* tools:

| Plane | What syncs | Owned by | Conflict model |
|---|---|---|---|
| **Context plane** | `.context/**` (markdown) | **Celeborn** (this design) | structured, merge-friendly |
| **Code plane** | the actual source tree | **git** (branches/worktrees) | normal git review/merge |

The phone "manages" (writes context) while the laptop "executes" (runs agents). Parallel *coding* on
the phone happens on its **own branch/worktree** and is reconciled by git later — Celeborn never
shares the laptop's working tree. Celeborn's only job is to keep the *context* coherent so each side
knows what the other is doing.

## Why sync exists: privacy manufactures the demand

The strongest reason to build sync isn't "code anywhere" convenience — it's a direct consequence of the
privacy model. The moment we tell users *"a public repo means a public `.context/`, so gitignore it"*
(see [`celeborn init --private`](#)), **git stops being the transport** for their working memory. Now
that memory lives on exactly one machine. Getting it to a second device — or a teammate, or a phone —
requires another channel. **That channel is Celeborn sync.**

So the free tool legitimately *creates the need it then sells*: privacy (a real reason, not artificial
friction) is what drives demand for sync. This is the cleanest possible "monetize the complement"
flywheel — and it makes *private long-term memory, available everywhere* the headline value, not a
convenience add-on.

## Phase 8a — `celeborn sync` git daemon (free, no-cloud)

A lazy background process, **out of the agent's critical path**. **This path is free** — it rides on
git/GitHub you already have, no Celeborn account, no server.

1. A filesystem watcher on `.context/` (e.g. `watchdog`/`fswatch`), debounced.
2. On change → commit **markdown only** to a dedicated context ref (a `celeborn-context` branch or a
   separate sync remote) → push.
3. On an interval or webhook → pull; a `post-merge` hook runs `celeborn index` so the local DB rebuilds.

- **Pros:** zero new infra; every change is an auditable commit; degrades to plain `git pull`/`push`.
- **Cons:** latency seconds–minutes (not sub-second); git on a phone is clunky.
- Good enough to *manage from a phone*; for a true real-time experience use 8b.

## Phase 8b — Supabase-backed sync (real-time; the "code anywhere" layer)

The hosted real-time path is built **entirely on Supabase** rather than a hand-rolled REST+SSE service —
Supabase supplies every primitive the original design needed, as managed infrastructure:

| Design need | Supabase primitive |
|---|---|
| GitHub OAuth login | **Auth** (native GitHub provider) — proves identity; links to the Stripe customer |
| Store `.context/*.md` | **Postgres** rows `(project_id, path, content, version, updated_at)` — text, diffable |
| Real-time pull | **Realtime** (Postgres change broadcasts) — replaces hand-rolled SSE |
| Server-side search for thin clients | **Postgres full-text search** (`tsvector`) — replaces a server SQLite index |
| Per-project authorization | **Row Level Security** — declarative policies, not hand-coded checks |
| Premium / subscription gating | `entitlements` table + RLS, fed by a Stripe webhook → **Edge Function** |
| Server-side secret backstop | Postgres trigger / Edge Function re-scan on write |

- The laptop runs `celeborn sync --remote supabase`: the watcher upserts changed `.context/*.md` rows;
  a Realtime subscription writes remote changes down; re-index locally on write.
- The phone is a **thin client** (web / Claude Code mobile): Realtime gives it the laptop's run live,
  Postgres FT search lets it recall with no local Python/SQLite, and its writes land within ~a second.
- **Invariants preserved:** markdown is still the source of truth; the **local SQLite index is still
  derived locally and never synced**; Postgres FTS is a *separate* server-side index for thin clients.
- **E2E mode:** the client encrypts markdown before upsert → Postgres stores ciphertext → server FT
  search is disabled (the documented tradeoff); thin clients fall back to local indexing.

### Two hosting models (open-core)

- **Celeborn-hosted Supabase = the premium product.** Zero setup; **subscription-gated** via RLS +
  entitlements — a *genuinely enforceable* gate (unlike git). This is the paid convenience.
- **Bring-your-own Supabase = free self-host.** Supabase is open source, so the privacy-conscious or
  enterprise can point `celeborn sync` at their own Supabase project. Ungated by definition (it's your
  infra) — and that's fine; it's free.

## Concurrency — how to not corrupt context with two writers

Handle it per-file, exploiting each tier's natural shape:

- **`journal.md`** — append-only. Concurrent appends merge by timestamp order → effectively
  conflict-free. This is the main write path, so most concurrent writing is already safe.
- **`session.json`** — tiny. Last-writer-wins guarded by a version/ETag.
- **`state.md`** — rewrite-in-place; the one genuine conflict point. v1 mitigation: treat phone edits
  as a **directive channel** — the phone *proposes* ("set Next action to X"), and the laptop's running
  agent *integrates* it into `state.md` on its next checkpoint. A clean producer/consumer split avoids
  simultaneous full-file rewrites.
- **Eventual robust path:** a **CRDT** (Automerge/Yjs) over the markdown for true free-for-all
  multi-writer editing. Heavier than v1 needs; revisit only if the directive-channel model proves too
  limiting.

Cross-ref: this supersedes the "concurrent writers" open question in [`../plan/PLAN.md`](../plan/PLAN.md) §11.

## Security & access control

Sync moves `.context/` markdown off the local machine, so it inherits responsibility for that data.
Context files routinely contain project-sensitive detail (architecture, roadmap, occasionally
credentials a user shouldn't have pasted — see the project's own secrets incident). Security is
therefore a **first-class requirement of the premium layer, not an afterthought.**

- **Authentication.** Every endpoint requires a credential — a Celeborn account session or a
  per-machine **license key / API token** (same credential that unlocks premium; see gating below).
  No anonymous access to `/p/{id}` of any kind.
- **Authorization.** A project `{id}` is owned by an account; access is an explicit per-project grant
  (owner + invited collaborators). The laptop and phone in the north-star scenario authenticate as the
  *same* account. SSE streams and `PUT` writes are both scoped to granted projects.
- **In transit.** TLS everywhere; tokens sent as bearer headers, never in query strings (which leak
  into logs — again, see the incident).
- **At rest.** Markdown encrypted at rest server-side; tokens stored hashed.
- **End-to-end option (premium-plus).** For teams that don't want the server to read context at all,
  an optional **client-side encryption** mode: the client encrypts `.context/*.md` before `PUT`. The
  tradeoff is explicit — E2E mode **disables server-side FTS** (`/search` and the JSON `/status`
  projection), so thin clients fall back to syncing ciphertext and indexing locally. Default mode is
  server-readable (so phones get server-side search); E2E is opt-in for the security-conscious.
- **Secret hygiene.** Secrets must *never* leave the origin machine via sync. This is a hard guarantee
  enforced by defense in depth — see [Secrets must never sync](#secrets-must-never-sync-defense-in-depth)
  below. It is the single most important security property of the substrate.

## Secrets must never sync (defense in depth)

The strongest guarantee in the design: **no secret ever leaves the origin machine** — not via the
hosted service, not via the git daemon, and (the gap we hit in practice) not via the *normal* git
commit of `.context/` either, since `.context/` is itself a tracked, often-public directory. A single
prefix scan is not enough; "never" requires multiple independent layers, each **fail-closed**.

**Layer 0 — Prevention (don't let secrets in).** Documented hard rule: `.context/` never stores
credentials; reference secrets by env-var *name* only. Reinforced by the SessionStart reminder and
agent guidance. The cheapest secret to never sync is one never written.

**Layer 1 — Broad detection (engine).** A curated, versioned ruleset — **not just prefixes**:
- Provider patterns: AWS `AKIA…`, Stripe `sk_live_…`, Slack `xox[baprs]-…`, Google `AIza…`,
  GitHub `ghp_…`/`github_pat_…`, OpenAI/Anthropic-style `sk-…`, `xai-…`, `sbp_…`, JWTs `eyJ…`,
  and `-----BEGIN … PRIVATE KEY-----` blocks.
- A **Shannon-entropy** heuristic to catch unknown high-entropy blobs the regexes miss.
- **Engine:** a stdlib built-in ruleset is the always-on core (zero-dependency promise preserved); if
  **`gitleaks`/`trufflehog`** is installed, the client also runs it for best-in-class coverage. External
  is an enhancement, never a requirement. (The scanner must not flag its *own* pattern file — patterns
  are anchored to match real secret bodies, and `.celebornrc` is self-excluded.)

**Layer 2 — Auto-redact on the way out (default behavior).** When detection fires, the client does
**not** block sync; it syncs a **redacted copy** with each secret replaced by `[REDACTED:<type>]`,
while **leaving the local file byte-for-byte intact**. So the real value never crosses the machine
boundary, but context keeps flowing. Every redaction is **logged and surfaced** to the user (the marker
names the type so the receiving device knows something was elided). A per-project **allowlist** prevents
over-redaction of confirmed false positives. Redaction is a safety net, not a license to store
secrets — the user is still nudged to fix the source (Layer 0).

**Layer 3 — Two enforcement points, both fail-closed.**
- **Sync path:** redaction runs client-side *before* encrypt/PUT (8b) and *before* commit to the
  context ref (8a) — identical logic, so both sync mechanisms have the same guarantee.
- **Commit path:** the *same* scanner runs as a **git `pre-commit` hook covering `.context/`**, so a
  secret can't enter the normal repo (public!) in the first place — this automates the manual
  sanitization that was otherwise needed by hand.
- Sync **refuses to run if the scanner is disabled or missing** — the guard cannot be silently bypassed.

**Layer 4 — Server-side backstop.** In server-readable mode the server re-runs detection on ingest and
rejects/redacts, so a buggy or tampered client still can't land a plaintext secret.

**Layer 5 — E2E complement.** Client-side encryption keeps the *server* from reading context, but other
authorized devices still receive it — so E2E is complementary to, not a substitute for, Layers 0–4.

**Layer 6 — Audit + regression tests.** Every detection/redaction/override is logged; a shipped
known-secret **fixture corpus** regression-tests the scanner so coverage never silently degrades.

**Honest limit (stated, not papered over):** no scanner catches 100% of custom/opaque secrets, and
entropy has both false positives and negatives. "Never" is achieved by the *combination* —
prevention + broad detection + fail-closed redaction at two points + server backstop + E2E — not by
any single layer.

## Free / premium boundary & gating

**The premium product is one thing: Celeborn-*hosted* real-time sync.** Everything else is free — the
local CLI, the git-daemon (8a), and bring-your-own Supabase. This is sharper than the earlier
"all sync is premium" framing (now superseded): you only pay for the *managed convenience*, which is
also the only thing that can be genuinely gated.

| | Free (OSS) | Premium |
|---|---|---|
| Local memory tiers, search, handoff (`init`/`status`/`search`/`index`/`handoff`/`archive`/`promote`/`record`/`metrics`/`remind`/`doctor`) | ✅ | ✅ |
| `celeborn sync` 8a git-backed daemon (no-cloud) | ✅ | ✅ |
| Bring-your-own Supabase (self-hosted 8b) | ✅ | ✅ |
| **Celeborn-hosted Supabase** — zero-setup real-time sync | — | ✅ |
| Phone/thin-client access to the hosted service, server-side FT search | — | ✅ |

**Gating model — subscription/account, enforced server-side.** The gate lives where it can actually be
enforced: the **Celeborn-hosted** Supabase backend, via RLS + an `entitlements` table. No
entitlement → no project rows on the hosted server. The CLI, the git daemon, and self-hosted
Supabase need no account and aren't gated (you can't gate someone's own git or their own Supabase — and
we don't try). This is cleaner than gating "all sync": the paid thing is the hosted convenience, and
that gate is real, not honor-system.

### Billing = Stripe subscriptions (Free / Pro / Team / Enterprise)

Billing is a standard freemium SaaS on **Stripe**. **Supabase Auth (GoTrue) proves identity** —
email+password, TOTP MFA, or GitHub OAuth (see [`supabase-auth-setup.md`](supabase-auth-setup.md)); an
active **Stripe subscription** unlocks premium sync, with the **tier** (Pro $8/seat, Team $12/seat,
Enterprise) read from the subscription's price. Full design → [`freemium-billing.md`](freemium-billing.md).

- **Mechanism.** User registers/signs in via **Supabase Auth** (identity only; the optional free account
  has no entitlement). `celeborn upgrade` opens a Stripe Checkout Session carrying their `auth.users` id
  (`client_reference_id`); on payment, a **Stripe webhook** links the Stripe customer to the user and
  writes the `entitlements` row (tier, seats, status, `current_period_end`). The same RLS
  `is_entitled()` check now reads that row.
- **Liveness.** Subscribe to Stripe `customer.subscription.*` / `invoice.payment_failed` webhooks to
  grant, downgrade, and revoke in near-real-time; back it with an entitlement **TTL** / `current_period_end`
  so a lapsed subscription loses access without depending on webhook delivery.
- **Why Stripe.** Real per-seat subscriptions, defined tiers, proration, invoices, tax, dunning, and a
  self-serve Billing Portal — the table stakes the old GitHub-Sponsors model lacked. Tier gates more than
  a binary: Team-only features (shared projects, the agent bus) check `entitlement_tier()`.
- **Migration.** Existing GitHub sponsors are grandfathered into a comp `pro` entitlement (or a 100%-off
  Stripe coupon) at cutover — see freemium-billing.md.

## Non-goals (for now)

- Syncing the SQLite index (never — it's derived).
- Syncing or merging the *code* tree (that's git's job; use branches/worktrees).
- Offline-first CRDT editing in v1 (directive-channel model first).
- Real-time *collaborative cursor* editing of markdown (out of scope; this is memory sync, not a doc editor).
- A free hosted tier (none planned; free = local only). A time-limited trial is a business decision, not an architectural one.
