#!/usr/bin/env bash
# Create the Celeborn Stripe Products + recurring Prices (Pro/Team, monthly + annual) in TEST mode.
# Idempotent-ish: it looks up existing products by metadata[tier] before creating, and skips a price
# whose (product, interval, amount) already exists. Prints the STRIPE_PRICE_* lines to paste into .env.
#
# Prereq: Stripe CLI installed, and .env holding STRIPE_SECRET_KEY (sk_test_...). Run from repo root:
#     bash scripts/stripe_bootstrap.sh
#
# Safe to run against TEST mode repeatedly. For LIVE, swap to sk_live_ and re-run (separate objects).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$ROOT/.env" ] || { echo "no $ROOT/.env (copy .env.example and set STRIPE_SECRET_KEY)"; exit 1; }
set -a; . "$ROOT/.env"; set +a
: "${STRIPE_SECRET_KEY:?set STRIPE_SECRET_KEY in .env}"
case "$STRIPE_SECRET_KEY" in
  sk_test_*) MODE="TEST" ;;
  sk_live_*) MODE="LIVE" ;;
  *) echo "STRIPE_SECRET_KEY doesn't look like an sk_ key"; exit 1 ;;
esac
echo "Stripe bootstrap — $MODE mode"
K=(--api-key "$STRIPE_SECRET_KEY")

# jq is handy but not required; fall back to grep/sed if absent.
have_jq() { command -v jq >/dev/null; }
field() { # field <json> <key>   (top-level "key": "value")
  if have_jq; then jq -r ".$2 // empty" <<<"$1"; else
    grep -o "\"$2\": *\"[^\"]*\"" <<<"$1" | head -1 | sed -E 's/.*: *"([^"]*)"/\1/'; fi
}

# find_product <tier>  -> prints product id or empty
find_product() {
  local tier="$1" out
  out=$(stripe products list "${K[@]}" --limit 100 2>/dev/null || echo '{}')
  if have_jq; then
    jq -r --arg t "$tier" '.data[] | select(.metadata.tier==$t) | .id' <<<"$out" | head -1
  else
    # crude: not reliable without jq; just return empty so we create (dups are harmless in test)
    echo ""
  fi
}

ensure_product() { # ensure_product <tier> <name> <desc> -> prints product id
  local tier="$1" name="$2" desc="$3" pid
  pid=$(find_product "$tier")
  if [ -n "$pid" ]; then echo "$pid"; return; fi
  local out
  out=$(stripe products create "${K[@]}" --name "$name" --description "$desc" -d "metadata[tier]=$tier")
  field "$out" id
}

ensure_price() { # ensure_price <product> <tier> <interval> <amount_cents> -> prints price id
  local prod="$1" tier="$2" interval="$3" amount="$4" out
  if have_jq; then
    local existing
    existing=$(stripe prices list "${K[@]}" --product "$prod" --limit 100 2>/dev/null \
      | jq -r --arg i "$interval" --argjson a "$amount" \
          '.data[] | select(.recurring.interval==$i and .unit_amount==$a) | .id' | head -1)
    if [ -n "$existing" ]; then echo "$existing"; return; fi
  fi
  out=$(stripe prices create "${K[@]}" --product "$prod" --unit-amount "$amount" --currency usd \
        -d "recurring[interval]=$interval" -d "recurring[usage_type]=licensed" -d "metadata[tier]=$tier")
  field "$out" id
}

PRO=$(ensure_product pro  "Celeborn Pro"  "Hosted sync, unlimited projects")
TEAM=$(ensure_product team "Celeborn Team" "Pro + shared projects, org admin, shared context & agent bus")
echo "  product pro  = $PRO"
echo "  product team = $TEAM"

PRO_M=$(ensure_price "$PRO"  pro  month 800)
PRO_A=$(ensure_price "$PRO"  pro  year  8000)
TEAM_M=$(ensure_price "$TEAM" team month 1200)
TEAM_A=$(ensure_price "$TEAM" team year  12000)

cat <<EOF

# ---- paste into .env (and .env.example placeholders stay as-is) ----
STRIPE_PRICE_PRO_MONTHLY=$PRO_M
STRIPE_PRICE_PRO_ANNUAL=$PRO_A
STRIPE_PRICE_TEAM_MONTHLY=$TEAM_M
STRIPE_PRICE_TEAM_ANNUAL=$TEAM_A
EOF
