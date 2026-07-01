# Freemium billing — Stripe migration plan (supersedes the GitHub Sponsors model)

> Status: **test mode LIVE & verified end-to-end (cutover steps 1–3 done).** Moving the hosted-sync gate
> from "active GitHub Sponsor of `cloud-dancer-labs`" to a **standard freemium SaaS** billed through **Stripe**.
> Shipped: migration `0004_stripe_billing.sql` (applied to remote); Edge Functions `create-checkout`,
> `billing-portal`, `stripe-webhook` (+ `_shared/stripe.ts`, all deployed); CLI `celeborn upgrade` /
> `celeborn billing`; `scripts/stripe_bootstrap.sh`. **Verified (2026-06-08):** Pro/Team Products + 4
> Prices minted in test mode, `price_…` IDs + `STRIPE_WEBHOOK_SECRET` captured into both `.env` and
> Supabase secrets; a registered dashboard webhook endpoint points at the deployed `stripe-webhook`; an
> end-to-end test (test user → trial subscription with `metadata.user_id`) confirmed the webhook writes a
> correct `entitlements` row (`tier=pro, status=trialing, seats=1, period_end` from the line item) on
> `customer.subscription.created`, and flips it to `status=canceled` on `customer.subscription.deleted`.
> **Remaining = cutover steps 4–7** (grandfather sponsors, flip docs, drop the Sponsors webhook, then
> repeat in LIVE mode). Companion to [`supabase-setup.md`](supabase-setup.md) (legacy Sponsors backend)
> and [`sync-design.md`](sync-design.md).
>
> **Identity note:** the free-version launch already moved identity to **GoTrue-native** (`celeborn
> register`/`login` mint the session JWT directly). So `auth-exchange` is **no longer on the billing
> path** — `create-checkout`/`billing-portal` authenticate with the user's GoTrue JWT directly, and the
> webhook keys entitlements by `auth.users.id`. The "modify auth-exchange" row below is therefore moot
> for this build; it stays legacy and untouched.

## Why the change

GitHub Sponsors was the v1 billing backend: any active sponsor (any tier) unlocked hosted sync. It was
fast to ship but is a poor commercial fit — pay-what-you-want, GitHub-account-coupled, no real seats,
tiers, trials, invoicing, tax, or dunning. Moving to Stripe gives us per-seat subscriptions, defined
tiers, a customer portal, proration, tax, and reliable webhooks — the table stakes of a SaaS.

**What does NOT change:** the local CLI stays free and fully functional with no account; the
git-daemon (8a) and bring-your-own-Supabase paths stay free; secrets are still redacted before upload;
RLS in Postgres is still the real, enforceable gate. Only the **entitlement source** changes: "is this
GitHub login a sponsor?" → "does this user have an active Stripe subscription, and at what tier?"

> **Identity update (free-version launch).** Identity is now **Supabase Auth (GoTrue)** — email+password,
> TOTP MFA, and GitHub OAuth — **not** the old GitHub device-flow + hand-signed JWT. See
> [`supabase-auth-setup.md`](supabase-auth-setup.md). The billing link below is unchanged in spirit; just
> read "user" as **the `auth.users` id** (GoTrue's user) rather than a `github_id`. `is_entitled()` /
> `entitlement_tier()` and every RLS policy are untouched — only what *fills* `entitlements` changes.

## Tier model

| Tier | Price (test mode) | Who | What it unlocks |
|------|-------------------|-----|-----------------|
| **Free** | $0 | Everyone | Entire local CLI — capture, tiered store, search, Hot-tier Orient load, `celeborn metrics`. Plus free **git-daemon sync** and **BYO-Supabase**. No account, fully offline. |
| **Pro** | **$8 / seat / mo** (annual ≈ $80/yr) | Individuals | Hosted Supabase sync (cross-device, real-time, zero-setup). **Unlimited projects.** **5 GB/mo soft bandwidth cap** (see "Bandwidth cap" below). |
| **Team** | **$12 / seat / mo** (annual ≈ $120/yr) | Teams | Everything in Pro, plus **shared projects**, **org admin**, **shared context**, and **shared agent telepathy** (the multi-agent channel/bus). |
| **Enterprise** | Custom (contact us) | Orgs | Team + SSO/SAML, custom DPA, volume pricing, optional self-host support. Not a Stripe self-serve product; sales-assisted. |

Free vs paid boundary is unchanged from `sync-design.md`: anything that moves `.context/` through the
**Celeborn-hosted** backend is paid. Local + BYO + git-daemon stay free.

### Bandwidth cap (Pro) — **5 GB / month, soft**

**Decision: 5 GB/month, enforced as a soft cap.** Rationale — `.context/` is markdown/JSON; a mature
project's full set is ~100 KB–1 MB, and with incremental (diff-based) sync each push moves only what
changed. Usage is bimodal: the casual majority sync at session boundaries (tens of MB/mo), while the
power-user minority run `--watch` across many large projects all day (low single-digit GB/mo). 5 GB sits
at roughly the **80th percentile** — the casual ~80% never approach it; the heavy ~20% brush it, which is
exactly the cohort Team is for. Generous enough to never punish normal use, tight enough to be a real
upsell signal.

Enforcement = **soft cap + overage email** (no hard block): track bytes synced/month per customer in a
`usage` table; over the cap, email a nudge to upgrade to Team and surface it in `celeborn sync` output —
never refuse a push. (A hard cap or Stripe metered/usage-based overage billing are possible later; both
are worse UX or more work than v1 needs.)

> Prerequisite: the cap assumes **diff-based push** (upload only changed files per cycle). The current
> `_push` re-uploads the full syncable set every push, which would make `--watch` blow any cap on
> redundant bytes — optimize `_push` to skip unchanged files (compare local mtime/hash vs. last-pushed)
> before this cap is meaningful. Tracked as a sync-efficiency follow-up.

## Stripe object design

Create in **test mode** first (keys already in `.env`). One Product per paid tier; two recurring Prices
per product (monthly + annual), `usage_type=licensed`, `billing_scheme=per_unit`, billed by `quantity` =
seat count.

| Product | Price (monthly) | Price (annual) | Env var for the ID |
|---------|-----------------|----------------|--------------------|
| Celeborn Pro | $8 / seat / mo | $80 / seat / yr | `STRIPE_PRICE_PRO_MONTHLY` / `_ANNUAL` |
| Celeborn Team | $12 / seat / mo | $120 / seat / yr | `STRIPE_PRICE_TEAM_MONTHLY` / `_ANNUAL` |

Enterprise = no self-serve price (sales creates a custom invoice/subscription).

### Creating them later (CLI appendix — do NOT run yet)

```bash
set -a; . ./.env; set +a   # load STRIPE_SECRET_KEY

# Pro
stripe products create --api-key "$STRIPE_SECRET_KEY" \
  --name "Celeborn Pro" --description "Hosted sync, unlimited projects" -d "metadata[tier]=pro"
stripe prices create --api-key "$STRIPE_SECRET_KEY" \
  --product <pro_prod_id> --unit-amount 800 --currency usd \
  -d "recurring[interval]=month" -d "recurring[usage_type]=licensed" -d "metadata[tier]=pro"
stripe prices create --api-key "$STRIPE_SECRET_KEY" \
  --product <pro_prod_id> --unit-amount 8000 --currency usd \
  -d "recurring[interval]=year" -d "recurring[usage_type]=licensed" -d "metadata[tier]=pro"
# Team: same pattern, --unit-amount 1200 / 12000, metadata[tier]=team
```

Capture each returned `price_...` id into `.env` (and `.env.example` with placeholders). The webhook and
`auth-exchange` map a subscription's price id → tier via the `metadata[tier]` we set above (no hard-coded
id lists in code).

## The identity ↔ subscription link (the crux)

GitHub identity (who you are) and Stripe customer (who pays) must be tied together. Flow:

1. `celeborn register` / `celeborn login` — **Supabase Auth** (email+password +TOTP, or `--github`
   OAuth). GoTrue creates the `auth.users` row and issues the session JWT directly (regardless of tier;
   a free/logged-in user just gets no paid entitlement). No custom `auth-exchange` mint step.
2. `celeborn upgrade` (new) — calls a new `create-checkout` Edge Function with the user's JWT. The
   function creates a **Stripe Checkout Session** with `client_reference_id = auth.users.id` (and
   `customer_email` prefilled from the GoTrue user), and `subscription_data.metadata.user_id = <id>`.
   Returns the Checkout URL; the CLI opens it in a browser.
3. User pays in Stripe Checkout.
4. **`stripe-webhook`** (replaces `sponsors-webhook`) receives `checkout.session.completed` →
   reads `client_reference_id` → links `stripe_customer_id` to that `users` row, and writes an
   `entitlements` row (tier from the price metadata, `seats` from quantity, `status=active`,
   `current_period_end`).
5. Subsequent `celeborn sync` calls: `auth-exchange` (on JWT refresh) reads the `entitlements` row; RLS
   lets the user read/write rows only while `status=active` and not past `current_period_end` (+ grace).

This keeps the gate **server-enforced** (RLS), exactly as today — we've only swapped what fills the
`entitlements` table.

## Database schema changes

Extend the existing schema (`supabase/migrations/0001_celeborn_sync.sql`) with a follow-up migration —
shipped as **`0004_stripe_billing.sql`** (0002/0003 were already taken by usage_totals and auth_profiles):

- **Identity = `auth.users`** (GoTrue), not a separate `users`+`github_id` table. `stripe_customer_id`
  lives on the `entitlements` row (PK `user_id`), so it persists across status changes. *(The earlier
  `users.github_id` sketch here predates the GoTrue pivot — ignore it.)*
- **`entitlements`** (extend, as built): added `tier`, `seats`, `status`, `stripe_customer_id`,
  `stripe_subscription_id`, `current_period_end`; kept the legacy `active`/`expires_at` backstop so —
  - `user_id` → `users.id`
  - `tier` text check in (`pro`,`team`,`enterprise`)
  - `seats` int default 1
  - `status` text (`active`,`past_due`,`canceled`,`trialing`)
  - `stripe_subscription_id` text
  - `current_period_end` timestamptz
  - keep `ENTITLEMENT_TTL`/`updated_at` backstop semantics
- **`is_entitled()`** → keep the name (RLS policies depend on it) but redefine: true when the caller's
  `entitlements.status='active'` (or `trialing`) and `now() < current_period_end + grace`. Add
  **`entitlement_tier()`** returning the tier so Team-only tables (shared projects, agent bus) can gate
  on tier, not just on "paid".
- RLS on the Team-only tables (shared projects / channel-bus) checks `entitlement_tier() in ('team','enterprise')`.
- (Optional) **`usage`** table for the bandwidth cap: `(user_id, period, bytes_synced)`.

## Edge Function changes

| Function | Status | Change |
|----------|--------|--------|
| `auth-exchange` | **modify** | Stop calling GitHub `isSponsoredBy`. Keep: validate GitHub token → resolve `github_id` → upsert `users` → mint JWT. Entitlement now comes from the `entitlements` table (written by the webhook), not from a live Sponsors API call. Drop `GITHUB_ORG_TOKEN` / `SPONSOR_ORG`. |
| `sponsors-webhook` | **replace → `stripe-webhook`** | Verify `Stripe-Signature` with `STRIPE_WEBHOOK_SECRET` (replaces GitHub HMAC). Handle the events below; upsert/expire `entitlements`. |
| `create-checkout` | **new** | Auth'd by the user JWT. Creates a Stripe Checkout Session (mode=subscription, the chosen price, `quantity`=seats, `client_reference_id`=github_id). Returns `{ url }`. |
| `billing-portal` | **new** | Auth'd by the user JWT. Creates a Stripe Billing Portal session for the linked `stripe_customer_id`. Returns `{ url }` so users self-manage seats/cancel/cards. |

### Stripe webhook events to handle

- `checkout.session.completed` → link customer + create entitlement.
- `customer.subscription.created` / `customer.subscription.updated` → upsert tier/seats/status/period_end
  (handles plan switches, seat changes, renewals, `past_due`).
- `customer.subscription.deleted` → mark `canceled` (RLS then denies after grace).
- `invoice.payment_failed` → mark `past_due` (optional: email).

Deploy with `--no-verify-jwt` like the current webhook (Stripe sends no Supabase auth; the signature
check is the gate). `create-checkout`/`billing-portal` instead require a valid user JWT.

### Webhook hardening — POST-MVP, before live traffic

The MVP webhook is O(1) per event and scales fine on raw load (Stripe is event-driven, our handler does
one indexed PK upsert). The scaling risk is **correctness under Stripe's retries + out-of-order
delivery**, not throughput. Three fixes, deferred until after the MVP ships but **required before live
traffic**:

1. **Drop the synchronous Stripe GET.** On `checkout.session.completed` the handler currently does
   `GET /subscriptions/{id}` to read tier/seats/period. Remove it and rely on the
   `customer.subscription.created` event Stripe fires alongside — it already carries items/price/
   quantity/status/`current_period_end`. Eliminates the extra round-trip (and its rate-limit exposure
   under a launch spike). `checkout.session.completed` then only links `client_reference_id` →
   `stripe_customer_id`.
2. **Idempotency + version-gating (most important).** Stripe delivers events **more than once** and
   **out of order** (a `subscription.updated` can arrive after a `subscription.deleted`, flipping a
   canceled user back to active). Store the Stripe `event.id` and ignore duplicates; gate writes on the
   subscription object's own version/timestamp — only apply if newer than the row's
   `current_period_end`/updated marker. Today it's last-write-by-arrival, which is wrong under reorder.
3. **Observability.** Log `event.id` + `type` + outcome to a `webhook_events` table; alert on sustained
   failures. A systematic bug (bad env, schema drift) currently fails events silently behind Stripe's
   retry schedule until someone notices sync is broken.

(#2 implies the small `webhook_events` table in a follow-up migration; #1 and #2 are a ~30-line change
to `stripe-webhook/index.ts`.)

## CLI changes (`celeborn_sync.py`) — follow-up, tracked separately

These are the code edits deferred under tasks #1–#6; listed here so the backend and client stay in sync:

- `SPONSOR_URL` → `UPGRADE_URL` (a pricing/checkout page); `_upgrade_hint()` copy → subscription wording.
- The three `"hosted sync requires an active sponsorship."` `die()`s → `"hosted sync requires a Celeborn
  Pro subscription."` Read `upgrade_url`/`checkout_url` from the 403 payload (keep `sponsor_url` as a
  back-compat key during cutover).
- New commands: **`celeborn upgrade`** (calls `create-checkout`, opens the URL) and **`celeborn billing`**
  (calls `billing-portal`). The 403 path can point users straight at `celeborn upgrade`.
- `_exchange` / module docstrings: "sponsorship" → "subscription/entitlement".

## Environment variables

Server (Supabase secrets):
```
STRIPE_SECRET_KEY=sk_test_...          # already in .env for local CLI use
STRIPE_WEBHOOK_SECRET=whsec_...        # from `stripe listen` or the dashboard endpoint
STRIPE_PRICE_PRO_MONTHLY=price_...
STRIPE_PRICE_PRO_ANNUAL=price_...
STRIPE_PRICE_TEAM_MONTHLY=price_...
STRIPE_PRICE_TEAM_ANNUAL=price_...
# REMOVE after cutover: GITHUB_ORG_TOKEN, SPONSOR_ORG
```
Client (unchanged): `CELEBORN_SUPABASE_URL`, `CELEBORN_SUPABASE_ANON_KEY`, `CELEBORN_GITHUB_CLIENT_ID`.

## Local testing with the Stripe CLI (already installed, v1.42.1)

```bash
set -a; . ./.env; set +a
# Forward live test-mode events to a locally-served stripe-webhook function:
stripe listen --api-key "$STRIPE_SECRET_KEY" \
  --forward-to localhost:54321/functions/v1/stripe-webhook
# (the printed whsec_... is STRIPE_WEBHOOK_SECRET for local runs)

# Drive events without real checkout:
stripe trigger checkout.session.completed --api-key "$STRIPE_SECRET_KEY"
stripe trigger customer.subscription.deleted --api-key "$STRIPE_SECRET_KEY"
```

## Cutover plan

1. ✅ **DONE (2026-06-08)** — Built test-mode Products/Prices (Celeborn Pro/Team, monthly+annual);
   captured the 4 `price_…` IDs into `.env` and Supabase secrets.
2. ✅ **DONE (2026-06-08)** — `0004_stripe_billing.sql` applied to remote; `stripe-webhook`,
   `create-checkout`, `billing-portal` deployed; `STRIPE_WEBHOOK_SECRET` set; dashboard webhook endpoint
   registered → deployed function. (`auth-exchange` left legacy/untouched — moot under GoTrue identity.)
   **End-to-end verified** against the deployed webhook: `subscription.created` → correct `entitlements`
   row; `subscription.deleted` → `canceled`. (Test user/customer/sub created and fully cleaned up.)
3. ✅ **DONE** — CLI `celeborn upgrade` / `celeborn billing` shipped (commit 85a6ee7).
4. Migrate any existing sponsors: one-time script grants a comp `entitlements` row (e.g. `tier=pro`,
   long `current_period_end`) so current sponsors aren't cut off — or hand them a 100%-off Stripe coupon.
5. Flip docs (README/SKILL/PLAN/sync-design/supabase-setup) from Sponsors → Stripe wording.
6. Remove the Sponsors webhook + `GITHUB_ORG_TOKEN` once no entitlements depend on them.
7. Repeat in **live mode** (`sk_live_`) — separate Products/Prices/webhook secret.

## Open decisions

- **Bandwidth cap number** for Pro (see above).
- **Free trial?** (the AskUserQuestion offered it; current choice is Free+Pro+Team+Enterprise with no
  trial). If wanted later: Stripe Checkout `subscription_data.trial_period_days` + `status='trialing'`
  already handled by `is_entitled()`.
- **Team seat model**: self-serve seat purchase via quantity vs. invite-based. v1: quantity at checkout,
  managed in the Billing Portal.
