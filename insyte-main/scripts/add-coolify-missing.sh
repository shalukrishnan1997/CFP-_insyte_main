#!/usr/bin/env bash
# Optional: add the 3 categories of variables that the audit found MISSING
# from Coolify but read by application code.
#
# These are NOT auto-applied — fill in real values, then run with the var
# name(s) you want to add as arguments. Examples:
#   ./add-coolify-missing.sh stripe
#   ./add-coolify-missing.sh sentry getaddress
#
# All three are functional gaps:
#   * stripe       — without these, card payment features stay disabled
#   * sentry       — without this, no production error tracking
#   * getaddress   — without this, UK postcode lookup in donor admin is off

set -euo pipefail

APP_UUID="gytvqegmkw4w4b1sp2i79fy0"

# ─── Stripe values ───
STRIPE_PUBLIC_KEY="${STRIPE_PUBLIC_KEY:-pk_live_REPLACE_ME}"
STRIPE_SECRET_KEY="${STRIPE_SECRET_KEY:-sk_live_REPLACE_ME}"
STRIPE_WEBHOOK_SECRET="${STRIPE_WEBHOOK_SECRET:-whsec_REPLACE_ME}"

# ─── Sentry value ───
SENTRY_DSN="${SENTRY_DSN:-https://REPLACE_ME@o0.ingest.sentry.io/0}"

# ─── GetAddress.io value ───
GETADDRESS_API_KEY="${GETADDRESS_API_KEY:-REPLACE_ME}"

if [[ $# -eq 0 ]]; then
  cat <<EOF
Usage: $0 <group> [<group> …]

Groups:
  stripe       — adds STRIPE_PUBLIC_KEY, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
  sentry       — adds SENTRY_DSN
  getaddress   — adds GETADDRESS_API_KEY

Provide real values via env vars before running, e.g.:
  STRIPE_SECRET_KEY=sk_live_xxx STRIPE_PUBLIC_KEY=pk_live_yyy \\
  STRIPE_WEBHOOK_SECRET=whsec_zzz $0 stripe
EOF
  exit 1
fi

create() {
  local key="$1"
  local value="$2"
  if [[ "$value" == *REPLACE_ME* ]]; then
    echo "  ! $key not set — pass it as an env var before running. Skipping." >&2
    return
  fi
  printf "  + %s … " "$key"
  if coolify app env create "$APP_UUID" --key "$key" --value "$value" >/dev/null 2>&1; then
    echo "ok"
  else
    echo "FAILED (it may already exist; use 'coolify app env update' instead)"
  fi
}

for group in "$@"; do
  case "$group" in
    stripe)
      echo "Adding Stripe vars:"
      create STRIPE_PUBLIC_KEY    "$STRIPE_PUBLIC_KEY"
      create STRIPE_SECRET_KEY    "$STRIPE_SECRET_KEY"
      create STRIPE_WEBHOOK_SECRET "$STRIPE_WEBHOOK_SECRET"
      ;;
    sentry)
      echo "Adding Sentry vars:"
      create SENTRY_DSN "$SENTRY_DSN"
      ;;
    getaddress)
      echo "Adding GetAddress vars:"
      create GETADDRESS_API_KEY "$GETADDRESS_API_KEY"
      ;;
    *)
      echo "Unknown group: $group" >&2
      exit 1
      ;;
  esac
done

echo
echo "Done. After adding vars, redeploy in Coolify so containers pick them up."
