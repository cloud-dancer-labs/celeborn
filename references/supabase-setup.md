# Hosted sync setup (Supabase) — legacy GitHub Sponsors backend

> ⚠️ **Superseded.** This documents the original **GitHub Sponsors**-gated backend. The billing model
> has moved to **Stripe subscriptions** (Free / Pro / Team / Enterprise) — see
> [`freemium-billing.md`](freemium-billing.md) for the current design. The Supabase, GitHub-device-flow,
> RLS, and secret-redaction pieces below still apply; only the **gating** changes: the `sponsors-webhook`
> + `isSponsoredBy` check (steps 3 & 5) are replaced by a Stripe webhook feeding the `entitlements`
> table, and `auth-exchange` reads that table instead of calling the Sponsors API.

How to stand up the **premium** sync backend (Phase 8b). The free local CLI and the
git-daemon (8a) need none of this. This is for whoever **runs** the hosted service — either Celeborn
itself, or a self-hoster bringing their own Supabase ("BYO").

Architecture recap: the CLI authenticates with **GitHub (device flow)**, an Edge Function checks the
user's **subscription entitlement** and mints a short-lived Supabase JWT, and `.context/*.md` rows live in
Postgres behind **row-level security** that requires an active entitlement. The local SQLite index is
never uploaded; server-side search uses a separate Postgres `tsvector`.

## 1. Create the Supabase project + apply the schema

```bash
supabase init                       # if you don't have a supabase/ dir yet
supabase link --project-ref <ref>
supabase db push                    # applies supabase/migrations/0001_celeborn_sync.sql
```

This creates `projects`, `context_files`, `entitlements`, the `is_entitled()` gate, RLS policies, the
FTS index, and adds `context_files` to the `supabase_realtime` publication.

## 2. Create a GitHub OAuth app (for the CLI device flow)

GitHub → Settings → Developer settings → **OAuth Apps** → New. Enable **Device Flow**. Note the
**Client ID** (public). Users authorize with scope `read:user` — the CLI only needs their identity;
the sponsorship check happens server-side.

## 3. Create the org maintainer token (for the sponsorship check)

A PAT on the sponsored org (`cloud-dancer-labs`) with permission to read sponsorship. The Edge Function uses
it to call `organization(login){ isSponsoredBy(accountLogin) }` — a privacy-safe boolean that includes
private sponsors. Store it as `GITHUB_ORG_TOKEN` (never ships to clients).

## 4. Deploy the Edge Functions

```bash
supabase functions deploy auth-exchange
supabase functions deploy sponsors-webhook
```

Set their secrets:

Deploy both with `--no-verify-jwt`: `auth-exchange` is called with the (non-JWT) publishable key
and the webhook is called by GitHub with no Supabase auth, so the default gateway JWT check would
reject both. Each does its own gating (sponsorship check / HMAC signature).

```bash
supabase functions deploy auth-exchange  --no-verify-jwt
supabase functions deploy sponsors-webhook --no-verify-jwt

supabase secrets set \
  GITHUB_ORG_TOKEN=ghp_xxx \
  SPONSOR_ORG=cloud-dancer-labs \
  ENTITLEMENT_TTL_SECONDS=3600 \
  GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32) \
  CELEBORN_JWT_SECRET=<legacy JWT secret from Settings → JWT Keys>
# SUPABASE_* secrets are auto-injected (URL, ANON_KEY, SERVICE_ROLE_KEY, DB_URL, JWKS, etc.) AND the
# SUPABASE_ prefix is RESERVED — you cannot set a secret named SUPABASE_JWT_SECRET. The legacy HS256
# secret is therefore set under CELEBORN_JWT_SECRET (the function reads that name).
```

> `auth-exchange` mints an HS256 JWT signed with `CELEBORN_JWT_SECRET` (the legacy secret), so the
> issued session is accepted by PostgREST/RLS exactly like a normal Supabase login. NOTE: on projects
> migrated to asymmetric JWT signing keys with the legacy secret revoked, this hand-signing breaks —
> mint the session via GoTrue's admin generate-link + verify instead.

## 5. Wire the Sponsors webhook

cloud-dancer-labs org → Sponsors dashboard → Webhooks → add
`https://<ref>.functions.supabase.co/sponsors-webhook` with the `GITHUB_WEBHOOK_SECRET` from step 4.
It grants/revokes entitlements on `created` / `tier_changed` / `cancelled` in near-real-time; the
entitlement TTL is the backstop if a webhook is ever missed.

## 6. Point the client at it

Per-user, via env (or `.celebornrc` `"sync": { "url", "anon_key", "github_client_id" }`):

```bash
export CELEBORN_SUPABASE_URL=https://<ref>.supabase.co
export CELEBORN_SUPABASE_ANON_KEY=<anon-key>      # public; RLS protects the data
export CELEBORN_GITHUB_CLIENT_ID=<oauth-client-id>
```

Then:

```bash
celeborn login     # GitHub device flow → sponsor check → session
celeborn sync      # push/pull .context/*.md (secrets redacted out on the way up)
celeborn sync --watch --interval 5
```

A non-sponsor is refused at `login`/`sync` with an upgrade hint pointing at the Sponsors page. The
free git-daemon sync (8a) and bring-your-own Supabase remain available with no account.

## Security notes

- **Secrets are redacted** out of every uploaded copy (`[REDACTED:<type>]`); the local file is intact.
- **Credentials** (GitHub token + session JWT) are stored at `~/.config/celeborn/credentials.json` with
  `0600` perms — never inside any repo or `.context/`.
- RLS is the real gate: no entitlement → no rows, even with a valid login. The CLI gate is just UX.
