#!/usr/bin/env bash
# Delete env-var cruft from the Coolify INSYTE application.
#
# Background: an audit on 2026-05-01 found 27 variables set in Coolify that
# the application code never reads (see plan at
# /Users/vimalhari/.claude/plans/there-are-so-many-idempotent-star.md and
# /Users/vimalhari/Developer/github.com/vimalhari/insyte/scripts/coolify-cruft-vars.json
# for the (key, uuid) list).
#
# This script:
#   * Re-fetches the (key → uuid) map at run time so UUIDs are fresh
#   * Prints a confirmation prompt with the full list before doing anything
#   * Calls `coolify app env delete <APP_UUID> <VAR_UUID>` per entry
#
# Run with --dry-run first to inspect.

set -euo pipefail

APP_UUID="gytvqegmkw4w4b1sp2i79fy0"

CRUFT_KEYS=(
  ADMIN_AUDIT_TRAIL
  ADMIN_LOG_RETENTION_DAYS
  ADMIN_PAGINATION_SIZE
  PAGINATION_SIZE
  DEBUG_SQL_QUERIES
  SLOW_QUERY_THRESHOLD
  ENABLE_PERFORMANCE_MONITORING
  DJANGO_REDIS_URL
  DONATION_REPORT_INCLUDE_URN
  DONATION_REPORT_PAGE_SIZE
  FEATURE_ADVANCED_REPORTING
  FEATURE_BATCH_IMPORT
  FEATURE_LETTER_GENERATION
  FEATURE_LOG_HISTORY
  GOOGLE_CLOUD_VISION_API_KEY
  LETTER_GENERATION_BATCH_SIZE
  LETTER_GENERATION_PAGE_BREAK
  MAX_LETTERS_PER_DOCUMENT
  MAX_UPLOAD_SIZE
  LOG_FILE
  MEDIA_ROOT
  MEDIA_URL
  UPLOAD_DIR
  USE_REDIS_CACHE
  CORS_ALLOWED_ORIGINS
  REQUIRE_MANUAL_REDACTION
  DEBUG
)

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

if ! command -v coolify >/dev/null 2>&1; then
  echo "ERROR: coolify CLI not found in PATH" >&2
  exit 1
fi

echo "Fetching current env list from Coolify…"
ENV_JSON="$(coolify app env list "$APP_UUID" --format json)"

# Build a quick key → uuid lookup
declare -a TO_DELETE
for key in "${CRUFT_KEYS[@]}"; do
  uuid="$(jq -r --arg k "$key" '.[] | select(.key == $k) | .uuid' <<<"$ENV_JSON")"
  if [[ -n "$uuid" && "$uuid" != "null" ]]; then
    TO_DELETE+=("$key=$uuid")
  fi
done

if [[ ${#TO_DELETE[@]} -eq 0 ]]; then
  echo "No cruft vars currently present in Coolify. Nothing to do."
  exit 0
fi

echo
echo "About to delete ${#TO_DELETE[@]} environment variables from app $APP_UUID:"
for entry in "${TO_DELETE[@]}"; do
  echo "  - ${entry%%=*}  (uuid: ${entry##*=})"
done
echo

if (( DRY_RUN )); then
  echo "Dry run only. Re-run without --dry-run to actually delete."
  exit 0
fi

read -r -p "Type 'yes' to proceed: " confirm
if [[ "$confirm" != "yes" ]]; then
  echo "Aborted."
  exit 1
fi

for entry in "${TO_DELETE[@]}"; do
  key="${entry%%=*}"
  uuid="${entry##*=}"
  printf "Deleting %-40s … " "$key"
  if coolify app env delete "$APP_UUID" "$uuid" >/dev/null 2>&1; then
    echo "ok"
  else
    echo "FAILED"
  fi
done

echo
echo "Done. Verify with: coolify app env list $APP_UUID --format table"
