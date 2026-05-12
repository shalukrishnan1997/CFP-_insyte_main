# ============================================================
# Stage 1: Builder — install deps (no Django runtime config needed)
# ============================================================
FROM python:3.14-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Build-time system dependencies (not carried to final image)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    pkg-config \
    libpq-dev \
    libffi-dev \
    libssl-dev \
    libxml2-dev \
    libxslt1-dev \
    zlib1g-dev \
    libjpeg62-turbo-dev \
    libzbar-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install Python deps in the builder (no DJANGO_SETTINGS_MODULE / SECRET_KEY here —
# Coolify should mark those Runtime-only so they are not passed as --build-arg.)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-cache

# Copy app code into builder
COPY . .
# Tailwind CSS build is handled by insyte-tailwind compose service at runtime


# ============================================================
# Stage 2: Runtime — lean production image
# ============================================================
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime-only system libraries (no compilers)
# `procps` ships pgrep, used by the celery-worker and celery-beat compose
# healthchecks. Without it, pgrep returns "command not found" and the
# containers are permanently marked unhealthy.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    libffi8 \
    libxml2 \
    libxslt1.1 \
    zlib1g \
    libjpeg62-turbo \
    libzbar0 \
    fontconfig \
    shared-mime-info \
    fonts-dejavu-core \
    ca-certificates \
    postgresql-client \
    procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -r deploy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Ownership set during copy — avoids slow chown -R on large .venv (Coolify timeouts)
COPY --from=builder --chown=deploy:deploy /app /app

# django-tailwind-cli expects a binary here when TAILWIND_CLI_AUTOMATIC_DOWNLOAD is off.
# Keep TAILWIND_CLI_VERSION in sync with responsehandling/settings/base.py
ARG TAILWIND_CLI_VERSION=4.1.17
USER root
RUN set -eux; \
    darch="$(dpkg --print-architecture)"; \
    case "$darch" in \
        amd64) TW_ARCH=x64 ;; \
        arm64) TW_ARCH=arm64 ;; \
        *) echo "unsupported dpkg arch: $darch"; exit 1 ;; \
    esac; \
    mkdir -p /app/.django_tailwind_cli; \
    curl -fsSL "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_CLI_VERSION}/tailwindcss-linux-${TW_ARCH}" \
        -o "/app/.django_tailwind_cli/tailwindcss-linux-${TW_ARCH}-${TAILWIND_CLI_VERSION}"; \
    chmod 755 "/app/.django_tailwind_cli/tailwindcss-linux-${TW_ARCH}-${TAILWIND_CLI_VERSION}"; \
    chown deploy:deploy "/app/.django_tailwind_cli/tailwindcss-linux-${TW_ARCH}-${TAILWIND_CLI_VERSION}"

RUN mkdir -p /app/static /app/staticfiles /app/media /app/logs \
    && chown -R deploy:deploy /app \
    && chmod 0755 /app/static /app/staticfiles /app/media /app/logs

USER deploy

EXPOSE 8000

# Defaults only; compose / Coolify inject real values at runtime
ENV DJANGO_SETTINGS_MODULE=responsehandling.settings.production

# IMPORTANT: entrypoint must always run (web, celery, beat)
ENTRYPOINT ["bash", "/app/entrypoint.prod.sh"]

# default command (overridden by docker-compose for celery / beat)
#
# Gunicorn tuning rationale (target: 100 concurrent QA reviewers + 5 scanners):
#   * --workers default 9 follows the (2 * CPU) + 1 rule for a 4-CPU prod box
#     (Coolify DigitalOcean droplet sizing). Override with GUNICORN_WORKERS.
#   * --worker-class gthread + --threads 10 keeps the I/O-bound profile (DB,
#     R2, Document AI, Stripe) — threads share memory so the per-worker
#     footprint stays bounded. Combined: 9 * 10 = 90 concurrent in-flight
#     requests before queueing.
#   * --max-requests / --max-requests-jitter recycle each worker after
#     ~1000 (+/- jitter) requests to limit gradual memory growth from
#     long-running pipeline imports (docxtpl, OCR clients).
#   * --timeout default 120s (env-tunable). Was 300s, but if a single
#     request actually wedges a worker thread for 5 minutes it can starve
#     the /health/live/ probe long enough for Docker to mark the container
#     unhealthy and Traefik to drop it from the upstream pool, surfacing
#     to users as "no available server". Keep slow synchronous paths
#     (large TIFF batches, sync OCR) under 120s, OR move them to Celery,
#     OR set GUNICORN_TIMEOUT higher in the deploy env when you know
#     you're hitting it.
#   * --graceful-timeout (env-tunable, default 30s) gives a worker
#     killed by --timeout a window to finish any cleanup before SIGKILL.
CMD ["sh", "-c", "uv run gunicorn responsehandling.wsgi:application --bind 0.0.0.0:8000 --forwarded-allow-ips=* --workers ${GUNICORN_WORKERS:-9} --worker-class gthread --threads ${GUNICORN_THREADS:-10} --timeout ${GUNICORN_TIMEOUT:-120} --graceful-timeout ${GUNICORN_GRACEFUL_TIMEOUT:-30} --max-requests ${GUNICORN_MAX_REQUESTS:-1000} --max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER:-100}"]
