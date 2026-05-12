#!/bin/sh
set -e

echo "▶︎ entrypoint: starting container"

# Create writable dirs only (NO chmod on volumes)
mkdir -p /app/static || true

# ===========================
# REQUIRED ENV (FAIL FAST)
# ===========================

: "${POSTGRES_HOST:?POSTGRES_HOST is not set}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:?POSTGRES_USER is not set}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is not set}"
: "${POSTGRES_NAME:?POSTGRES_NAME is not set}"
: "${POSTGRES_TIMEOUT:=60}"

export PGPASSWORD="$POSTGRES_PASSWORD"

echo "Waiting for Postgres at ${POSTGRES_HOST}:${POSTGRES_PORT}..."

# ===========================
# Wait for Postgres
# ===========================

for i in $(seq 1 "$POSTGRES_TIMEOUT"); do
  if pg_isready \
      -h "$POSTGRES_HOST" \
      -p "$POSTGRES_PORT" \
      -U "$POSTGRES_USER" \
      -d "$POSTGRES_NAME" \
      >/dev/null 2>&1; then
    echo "✅ Postgres is available"
    break
  fi

  if [ "$i" -eq "$POSTGRES_TIMEOUT" ]; then
    echo "❌ Timeout waiting for Postgres"
    exit 1
  fi

  sleep 1
done

unset PGPASSWORD

echo "▶︎ Exec: $*"
exec "$@"
