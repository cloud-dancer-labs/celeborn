# Celeborn — GitHub App: Marketplace listing copy & operator notes

Source copy for the GitHub Marketplace listing and the App's settings page. Pairs with the manifest in
[`github-app-manifest.json`](github-app-manifest.json) and the single receiver
[`supabase/functions/gh-webhook`](../supabase/functions/gh-webhook/index.ts). Sprint plan: §3 / §9 of
[`plan/t53-github-app-sprint.md`](../plan/t53-github-app-sprint.md).

---

## Name & tagline

**App name:** `Celeborn Memory` (slug `celeborn-memory`; "Celeborn" alone was unavailable). Display tagline:

**Celeborn Memory — memory drift checks & thread capture for AI-agent repos**

> Keeps your repo's committed memory honest and your team's local context fed — without ever writing to
> your code, PRs, or issues.

## Short description (≤ 140 chars)

> Celeborn flags drift in your committed `.context/` memory on every PR and captures PR/issue threads
> for your team. Read-only by design.

## Detailed description

Celeborn is the persistent memory layer for AI coding agents. The free local CLI gives every agent a
tiered `.context/` store so it resumes with full awareness instead of re-priming from amnesia. **This
GitHub App is the free cloud peripheral** around that store:

- **Memory-drift checks (free).** When you open or update a PR, Celeborn reads your *committed*
  `.context/state.md` and `notes.md` at the head commit and posts a neutral Check Run listing memory
  references to files the repo no longer has. It is **always `neutral`** — drift never fails your CI,
  it just surfaces rot before it misleads the next agent.
- **Thread capture (free to capture, Pro to pull).** Celeborn captures PR review threads and issue
  comments into your project's backlog. Anyone on a linked repo can run `celeborn sync` to pull that
  history into their **local** `.context/journal.md` — pulling is part of Celeborn Pro; capture costs
  you nothing and accrues from day one, so upgrading later unlocks the whole backlog.

## The guarantee — Celeborn never writes to your repo

This is enforced by **permissions, not promises**. The App requests **read-only** access to Contents,
Issues, and Pull requests. Its *only* write scope is **Checks** — the neutral drift Check Run. There is
no Contents-write, no Issues-write, no Pull-requests-write, so the App is **structurally incapable** of
pushing memory, comments, or commits back into your repository. Data flows **inward only**: GitHub →
your team's local context. Nothing Celeborn reads is ever sent back out to GitHub.

App private keys and webhook secrets live only in the backend's environment and are never logged. Every
inbound webhook is HMAC-verified (`X-Hub-Signature-256`); bad signatures are rejected.

---

## Permissions requested (least privilege)

| Scope          | Level        | Why |
|----------------|--------------|-----|
| Metadata       | Read         | Baseline (required by GitHub). |
| Contents       | Read         | Drift reads committed `.context/{state,notes}.md` + the repo tree at the PR head SHA. |
| Issues         | Read         | Ingest reads issue comment bodies. |
| Pull requests  | Read         | Ingest reads review threads; drift reads the PR head SHA. |
| Checks         | **Read & Write** | Drift posts its neutral Check Run — **the only write the App can make.** |

**Webhook events:** `pull_request`, `check_run`, `check_suite` (drift); `issue_comment`,
`pull_request_review`, `pull_request_review_comment` (ingest); `marketplace_purchase`, `installation`,
`installation_repositories` (lifecycle/billing).

**One webhook URL.** All events post to a single receiver, `gh-webhook`, which HMAC-verifies and then
dispatches by `X-GitHub-Event` to the drift / ingest / lifecycle handlers — one secret, one signature
path. (`marketplace_purchase` is **record-only**: Stripe remains the single source of entitlement;
installing or "purchasing" the free plan never grants Pro on its own.)

---

## Pricing

Listed as a **free** plan. The App itself costs nothing — drift checks and thread capture are free for
everyone. *Pulling* captured threads into local context is part of **Celeborn Pro** (billed in the CLI,
via Stripe), not gated through GitHub billing. See [`freemium-billing.md`](freemium-billing.md).

---

## ⚠ Operator checklist before listing (NOT code — org/process)

GitHub Marketplace requires more than a working webhook. Tracked here so it isn't forgotten:

- [ ] **Verified publisher.** Move App ownership to a GitHub **org**, enable org 2FA, verify the
      `celeborn.dev` domain via DNS TXT, add a contact email. **Currently NEEDS WORK** — see
      [`CELEBORN_PATH1_EVALUATION.md`](../CELEBORN_PATH1_EVALUATION.md) (verified-publisher + merchant-of-record rows).
- [ ] **Billing webhook that 200s** even for a free plan — covered by the record-only
      [`marketplace-webhook`](../supabase/functions/marketplace-webhook/index.ts) (reachable via `gh-webhook`).
- [ ] Replace `<your-project>` in [`github-app-manifest.json`](github-app-manifest.json) with the real
      Supabase project ref, and set `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET`
      in the Edge Function environment.
- [ ] Listing assets: logo, screenshots (a sample drift Check Run), category, and this description.
- [ ] Merchant-of-record / tax: a free listing defers it, but a future paid Marketplace plan would map
      `marketplace_purchase` → entitlement and reopen the MoR question (Path-1 eval §). Out of scope here.

> **Eligibility status (2026-06-17):** the engineering gating is **PASS** — the App, manifest, and
> record-only billing webhook are built. Remaining items are org/process only (verified publisher, domain
> verification, assets). See the corrected [`CELEBORN_PATH1_EVALUATION.md`](../CELEBORN_PATH1_EVALUATION.md)
> — it is now a **GO** for the free listing; the old NO-GO verdict on that page is superseded.

---

## Submission fields (GitHub-required — fill before the listing can go live)

These are mandatory on the Marketplace listing form and are **not** yet pinned anywhere:

| Field | Value to use |
|---|---|
| **Primary category** | **Code quality** (the drift Check Run is a code-quality signal). Secondary: **Project management** (thread capture → backlog). |
| **Listing name** | Celeborn Memory |
| **Very short description** | The short (≤140) line above. |
| **Logo / feature card** | Square logo (min 200×200, transparent PNG) + a feature-card background. Reuse the thot.ai Celeborn mark. |
| **Screenshots (1–5)** | A sample "Celeborn / memory drift" Check Run on a PR (metadata only — counts + filenames, no memory prose). |
| **Privacy policy URL** | `https://celeborn.dev/privacy` *(must exist — the App reads repo Contents/PRs/Issues, so a privacy policy is required).* |
| **Support / docs URL** | `https://github.com/cloud-dancer-labs/celeborn#github-app` (or `https://celeborn.dev/support`). |
| **Support email** | A monitored contact under the verified org. |
| **Pricing plan** | Single **Free** plan. (Pro is billed in the CLI via Stripe — not a Marketplace plan.) |

> **Privacy policy is a hard gate.** Because the App holds `contents/issues/pull_requests: read`, GitHub
> requires a published privacy policy URL before approval. Stand up `celeborn.dev/privacy` (a short page
> stating: read-only ingestion, inward-only data flow, no resale, secrets never logged) as part of the same
> domain-verification step used for verified-publisher.
