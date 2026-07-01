# Supabase Auth setup — the free-version identity backend

> **Current identity design.** Celeborn identity is now **Supabase Auth (GoTrue)**: email+password,
> **TOTP MFA (Google Authenticator)**, and **GitHub OAuth** — all native. This supersedes the
> GitHub-device-flow + custom-minted-JWT path in [`supabase-setup.md`](supabase-setup.md) (legacy).
> GoTrue owns `auth.users` and issues the JWTs, so `auth.uid()`, `is_entitled()`, and every RLS policy
> from `0001_celeborn_sync.sql` keep working **unchanged**. Billing (Stripe) is layered on top in
> [`freemium-billing.md`](freemium-billing.md) — this doc is identity only.
>
> **The free account is optional.** The local CLI works fully offline with no account. Registering is
> opt-in: it creates an `auth.users` row + a free `profiles` row (migration `0003`) with **no
> entitlement**, so hosted-sync tables stay denied by RLS until the user upgrades.

These steps are performed **once by whoever runs the hosted service** (the dashboard config can't be
done from the CLI). Local testing uses `supabase start`.

## 1. Apply the schema

```bash
supabase link --project-ref <ref>
supabase db push    # applies 0001_celeborn_sync, 0002_usage_totals, 0003_auth_profiles
```

`0003` adds `public.profiles` (free-tier identity) and an `on auth.users insert` trigger that
auto-creates a profile (seeding `username` from signup `user_metadata` if supplied).

## 2. Email provider + confirmation

Dashboard → **Authentication → Providers → Email**: enable it. Dashboard → **Authentication → Sign In /
Up**: require **Confirm email** (so a real, owned address is proven before login). Set the
**confirmation email template**'s link to the hosted landing page (see §5). New signups land in
`auth.users` with `email_confirmed_at = null` until they click the link.

## 3. MFA → TOTP (Google Authenticator)

Dashboard → **Authentication → Multi-Factor** (or Project Settings → Auth): enable **TOTP**. This makes
the GoTrue `factors` endpoints live:

- `POST /auth/v1/factors` (type `totp`) → returns an `otpauth://` URI + secret. The CLI renders it as an
  ASCII QR + the secret to type into Google Authenticator / Authy.
- `POST /auth/v1/factors/{id}/challenge` then `.../verify` (6-digit code) → activates the factor.

Once a user has a verified TOTP factor, password login returns an **AAL1** session that must be raised to
**AAL2** by verifying a TOTP challenge before RLS-protected data is reachable. (Enforce AAL2 for synced
tables via an RLS predicate on `auth.jwt()->>'aal'` if you want MFA to be mandatory for paid sync;
optional for v1.)

## 4. GitHub OAuth provider

Dashboard → **Authentication → Providers → GitHub**: enable, paste the GitHub OAuth app's **Client ID +
Secret**. In the GitHub OAuth app, set the **Authorization callback URL** to:

```
https://<ref>.supabase.co/auth/v1/callback
```

> This is the **web OAuth** app (server-side, secret held by GoTrue) — different from the old *device
> flow* app. The CLI drives it with **PKCE** through a loopback redirect (§5); no client secret ships.

## 5. Site URL + redirect allowlist (for the CLI loopback)

Dashboard → **Authentication → URL Configuration**:

- **Site URL:** the hosted landing/pricing page (also the email-confirmation destination).
- **Redirect URLs (allowlist):** add the CLI's loopback callbacks so PKCE/email links can return to a
  locally-served port:
  ```
  http://127.0.0.1/*
  http://localhost/*
  ```
  The CLI opens a one-shot loopback server on an ephemeral port, sends `redirect_to` =
  `http://127.0.0.1:<port>/callback`, and exchanges the returned `code` via
  `POST /auth/v1/token?grant_type=pkce`.

## 6. Point the client at it

Per-user env (or `.celebornrc` `"sync": { "url", "anon_key" }`):

```bash
export CELEBORN_SUPABASE_URL=https://<ref>.supabase.co
export CELEBORN_SUPABASE_ANON_KEY=<publishable anon key>   # public; RLS protects the data
```

`CELEBORN_GITHUB_CLIENT_ID` is **no longer needed** — GitHub auth is now a GoTrue provider (secret lives
server-side), not a client-driven device flow.

```bash
celeborn register          # email + password + username → confirm email
celeborn login             # password, then TOTP 6-digit challenge → session
celeborn login --github    # browser PKCE via loopback
celeborn whoami            # email, username, MFA status, tier (free until upgraded)
```

## 7. Retiring the custom auth-exchange

The legacy `auth-exchange` Edge Function (GitHub token → `isSponsoring` → hand-signed HS256 JWT) is **no
longer on the identity path** — GoTrue mints standard sessions and `celeborn sync` sends that bearer
token straight to PostgREST. Keep `auth-exchange` only if you later want a thin server-side hook (e.g.
refresh an entitlement TTL on login); otherwise it can be removed once nothing calls it.

## 8. Local testing

```bash
supabase start                       # local stack incl. GoTrue on :54321
# Email confirmations + magic links are captured by Inbucket at http://localhost:54324
export CELEBORN_SUPABASE_URL=http://127.0.0.1:54321
export CELEBORN_SUPABASE_ANON_KEY=<local anon key from `supabase status`>
celeborn register && celeborn login
```

For TOTP locally, enable MFA in `supabase/config.toml` (`[auth.mfa] ... [auth.mfa.totp] enroll_enabled =
true; verify_enabled = true`) before `supabase start`.

## Security notes (unchanged)

- **RLS is the real gate.** A free/logged-in user with no active `entitlements` row gets **zero** synced
  rows even with a valid session. The CLI gate is UX only.
- **Session tokens** (access + refresh) are stored at `~/.config/celeborn/credentials.json`, `0600` —
  never inside any repo or `.context/`.
- **Secrets are redacted** out of every uploaded `.context/` copy (`[REDACTED:<type>]`); the local file
  is left intact.
- Prefer **email confirmation on** and **TOTP** for any account that will hold paid hosted data.
