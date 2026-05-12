# INSYTE DMS — Donation Management System

A **Donation Management System (DMS)** for UK charities, built as a back-office BPO platform for service bureaus that process physical donation response forms at scale.

## What It Does

INSYTE handles the complete donation response-handling workflow:

1. **Campaign Management** — Create and manage charity donation campaigns (direct mail appeals, raffles, etc.)
2. **Scan Ingestion** — Upload scanned physical donation forms via scanner workstation integration
3. **Scan-First Intake** — Ingest scanned donation forms, run OCR extraction, and send created batches into QA review
4. **Donor Management** — Dual donor architecture: house file (master) + campaign-specific data files
5. **Letter Generation** — Automated thank-you and issue letter generation from DOCX templates via Celery
6. **Payment Processing** — Stripe integration for card payments, with batch-level processing
7. **Daily Banking** — Paying-in slips grouping physical payments (cheques, cash, CAF vouchers) for bank deposit
8. **Invoicing** — Service-based invoicing with PDF generation to bill charity clients
9. **Client Portal** — Read-only portal for charity clients to view dashboards and reports
10. **Audit Trail** — Signal-based audit logging with field-level change tracking

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django 5.2 LTS, Django REST Framework 3.16, Python 3.14+ |
| Database | PostgreSQL 18 (prod), SQLite (dev/test) |
| Cache/Queue | Redis 8, Celery 5.6, django-celery-beat |
| Frontend | Django Templates, Tailwind CSS 4, Alpine.js 3.15, HTMX 2.0.8 |
| Auth | Session-based + JWT, django-otp (TOTP + Email 2FA), django-axes |
| Payments | Stripe (checkout sessions, payment intents, webhooks) |
| Storage | Cloudflare R2 (S3-compatible), WhiteNoise for static files |
| PDF/Docs | ReportLab, pypdf, python-docx, docxtpl, openpyxl |
| Monitoring | Sentry SDK, structured JSON logging |
| Tooling | uv, ruff, pyright, Playwright (E2E tests) |

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/) (package manager)
- PostgreSQL 18 (production) or SQLite (development)
- Redis 8 (production, for cache/Celery)

## Quick Start

```bash
# Clone the repository
git clone https://github.com/vimalhari/insyte.git
cd insyte

# Install dependencies
uv sync

# Create .env.development (see .env.example)
cp .env.example .env.development

# Run migrations
uv run python manage.py migrate

# Populate default service items (for invoicing)
uv run python manage.py populate_service_items

# Build Tailwind CSS
uv run python manage.py tailwind build

# Create a superuser
uv run python manage.py createsuperuser

# Run the development server
uv run python manage.py runserver
```

## Environment Variables

Create a `.env.development` file (development) or set these in production:

```env
# Required
SECRET_KEY=your-strong-secret-key
DJANGO_SETTINGS_MODULE=responsehandling.settings.development

# Database (production only)
POSTGRES_ENGINE=django.db.backends.postgresql
POSTGRES_NAME=insyte_db
POSTGRES_USER=insyte
POSTGRES_PASSWORD=strong-password
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_SSLMODE=require
POSTGRES_CONNECT_TIMEOUT=10

# Redis (production only)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_PASSWORD=

# Stripe
STRIPE_PUBLIC_KEY=pk_test_...
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# Email (production)
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_HOST_USER=noreply@example.com
EMAIL_HOST_PASSWORD=email-password

# Cloudflare R2 Storage (production)
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=
R2_CUSTOM_DOMAIN=

# Field Encryption
FIELD_ENCRYPTION_SALT=your-unique-salt-never-change

# Monitoring
SENTRY_DSN=https://...@sentry.io/...
```

## Project Structure

```
├── auth_app/           # Authentication, 2FA, login/logout
├── core/               # Core business logic
│   ├── models/         # Django models (16 model files)
│   ├── services/       # Business logic services
│   ├── admin/          # Django admin customisation
│   ├── management/     # Management commands
│   └── migrations/     # Database migrations
├── custom_admin/       # Staff admin panel views, DRF API
│   ├── views/          # Admin views (campaigns, donations, QA, etc.)
│   ├── forms/          # Django forms
│   └── templatetags/   # Custom template tags
├── responsehandling/   # Project config (settings, URLs, permissions)
│   └── settings/       # Environment-specific settings
├── templates/          # Django templates
│   ├── admin/          # Staff admin templates
│   ├── auth/           # Authentication templates
│   ├── client_portal/  # Client portal templates
│   ├── components/     # Reusable UI components
│   ├── email/          # Email templates
│   └── layouts/        # Layout templates
├── static/             # Static assets (CSS, JS, images)
├── tests/              # Test suite (pytest + Playwright)
└── scripts/            # Utility scripts
```

## Running Tests

```bash
# Run all tests
uv run pytest tests/ -v

# Run with coverage
uv run pytest tests/ --cov=core --cov=auth_app --cov-report=html

# Run E2E tests (requires running server)
uv run pytest tests/e2e/ -v
```

## Linting & Type Checking

```bash
# Format code
uv run ruff format .

# Lint
uv run ruff check . --fix

# Type checking (strict mode)
uv run pyright
```

## Docker Deployment

```bash
# Dev / staging: Postgres + Redis in Docker (e.g. Coolify on Hetzner)
docker compose up --build -d

# Production: managed PostgreSQL only (e.g. Coolify on DigitalOcean)
docker compose -f docker-compose.prod.yml up --build -d
```

The Docker setup includes:
- **Traefik** reverse proxy with Let's Encrypt TLS
- **Coolify** deployment platform integration
- Automatic migrations on startup
- Liveness probe at `/health/live/` (DB only; used by Docker/Coolify health checks)
- Full readiness at `/health/` (DB, cache, Celery, etc.)

### Coolify: which compose file?

| Environment | Compose file | Database |
|-------------|--------------|----------|
| **Dev** (e.g. Hetzner) | `docker-compose.yml` | **Bundled** `insyte-db` (Postgres 18 in the stack) |
| **Prod** (e.g. DigitalOcean) | `docker-compose.prod.yml` | **Managed** Postgres — set `POSTGRES_HOST` to your cluster hostname |

**Deployment URLs**

| Environment | Public URL | Coolify `APP_DOMAIN` |
|-------------|------------|------------------------|
| Dev (Hetzner) | https://dev.insyte.uk | `dev.insyte.uk` |
| Prod (DigitalOcean) | https://insyte.uk | `insyte.uk` |

Set Django **`ALLOWED_HOSTS`** to the same hostname (comma-separated if you add more), and **`CSRF_TRUSTED_ORIGINS`** to the HTTPS origin, e.g. dev: `https://dev.insyte.uk`, prod: `https://insyte.uk`.

If Coolify (or another proxy) uses a custom health path, set it to **`/health/live/`** so Celery/Redis blips do not mark the web container unhealthy and produce a blank **"no available server"** page from the load balancer.

Coolify writes service variables to a project `.env` file. Both compose files use **`env_file: .env`** for Django secrets (`SECRET_KEY`, `FIELD_ENCRYPTION_SALT`, Stripe, R2, etc.). You do **not** need a `.env.production` file on the server.

### Dev stack (`docker-compose.yml`) — required Coolify variables

- **Database (for `insyte-db` + Django):** `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_NAME`. Optionally `POSTGRES_HOST` (defaults to `insyte-db` if omitted).
- **Redis:** `REDIS_PASSWORD` (non-empty).
- **Routing:** `APP_DOMAIN=dev.insyte.uk` (Traefik `Host()` rule).
- **Django production:** `SECRET_KEY`, `FIELD_ENCRYPTION_SALT`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, plus integrations you use (Stripe, R2, email, etc.). See `.env.example`.

Leave **`POSTGRES_SSLMODE`** unset (or empty) for the containerised Postgres service.

Automated DB dumps: the `insyte-db-backup` sidecar runs an initial `pg_dump` on container start, then re-dumps every `BACKUP_INTERVAL_SECONDS` (default `86400` / daily) and prunes anything older than `BACKUP_RETENTION_DAYS` (default `7`). Dumps land in the `db_backups` volume (separate from `pgdata`). To force a backup, restart the sidecar: `docker compose restart insyte-db-backup`. To inspect dumps from the Coolify host: `docker run --rm -v <project>_db_backups:/b alpine ls -lh /b`.

### Prod stack (`docker-compose.prod.yml`) — required Coolify variables

- **Managed Postgres:** `POSTGRES_HOST`, `POSTGRES_PORT` (usually `5432`), `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_NAME`, and **`POSTGRES_SSLMODE=require`** for DigitalOcean Managed PostgreSQL (default in this compose file).
- **Redis:** still runs in-stack unless you change the file — set `REDIS_PASSWORD`.
- **Routing:** `APP_DOMAIN=insyte.uk` (Traefik `Host()` rule).
- **Same Django / integration variables** as dev (via Coolify → `.env`).

Keep Redis in this compose file unless you also point `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `DJANGO_REDIS_CACHE_URL`, and `DJANGO_REDIS_SESSION_URL` at a managed Redis instance.

### General Coolify notes

- Set variables in the Coolify UI; avoid committing real `.env` files.
- **Build-time vs runtime:** In Coolify, set **Runtime only** (disable “available at build time”) for `DJANGO_SETTINGS_MODULE`, `SECRET_KEY`, database passwords, and other secrets. This image only needs env when containers **run** (compose runs Tailwind/migrate/Gunicorn then). Passing dozens of build-args can trigger Django build warnings and, if a value contains shell-special characters, can break `docker compose build`.
- **Tailwind in Docker:** The Dockerfile downloads the standalone Tailwind CLI into `.django_tailwind_cli/` (version `ARG` must stay aligned with `TAILWIND_CLI_VERSION` in `responsehandling/settings/base.py`). If you bump Tailwind in settings, bump the Dockerfile `ARG` too.
- Prefer `GOOGLE_APPLICATION_CREDENTIALS_JSON` or `GOOGLE_APPLICATION_CREDENTIALS_JSON_B64` over mounting credential files into the container.
- For **local** `docker compose` (not Coolify), create a `.env` from `.env.example` in the project root so `env_file: .env` resolves.

### Gunicorn tuning (web container)

The `web` container's `gunicorn` invocation is parameterised via the following env vars (all optional — defaults sized for ~100 concurrent QA reviewers + 5 scanner workstations on a 4-CPU production box):

| Variable | Default | Notes |
|----------|---------|-------|
| `GUNICORN_WORKERS` | `9` | `(2 * CPU) + 1` for a 4-CPU host. Bump for larger boxes; lower for memory-constrained ones. |
| `GUNICORN_THREADS` | `10` | Threads per `gthread` worker — keeps the I/O-bound profile (DB / R2 / Document AI / Stripe). |
| `GUNICORN_MAX_REQUESTS` | `1000` | Recycle each worker after this many requests to limit gradual memory growth. `0` disables. |
| `GUNICORN_MAX_REQUESTS_JITTER` | `100` | Random jitter added to `--max-requests` so workers do not all restart at once. |

`--timeout 300` is hardcoded to cover slow upload paths and synchronous OCR / Document AI calls; bring that down if a smarter upstream proxy owns long-poll routes.

## Key Management Commands

```bash
# Populate default invoice service items
uv run python manage.py populate_service_items

# Generate test data
uv run python manage.py generate_test_data

# GDPR: Anonymise donor data (right to erasure)
uv run python manage.py gdpr_erase_donor <URN> --dry-run
uv run python manage.py gdpr_erase_donor <URN> --reason "Subject access request"
```

## API Endpoints

REST API available at `/admin/api/` (JWT + Session authentication):
- Campaign CRUD
- Donation management
- Batch operations
- Invoice management

## Scanner Webhook Integration

Scanner endpoints are HMAC-authenticated and now require explicit client scope.

### Required Environment Variable

```env
SCAN_WEBHOOK_SECRET=your-shared-hmac-secret
```

### Endpoint: Scanner Campaign List

- URL: `GET /webhooks/scanner/campaigns/?client_id=<client_uuid>`
- Required query param: `client_id`
- Signature input: raw query string bytes (for example `client_id=...`)
- Header: `X-Signature: <hex_hmac_sha256>`
- Response: only active campaigns for the specified active client

Example signature (Python):

```python
import hashlib
import hmac

secret = "your-shared-hmac-secret"
query_string = "client_id=4e5b7e2d-4d35-4d4c-90c8-a1d16d7d1f4a"

signature = hmac.new(
		secret.encode(),
		query_string.encode(),
		hashlib.sha256,
).hexdigest()
```

### Endpoint: Scan Upload Progress

- URL: `POST /webhooks/scan-upload/`
- Required JSON fields:
	- `campaign_id`
	- `client_id`
	- `status`
- Signature input: raw JSON request body bytes
- Header: `X-Signature: <hex_hmac_sha256>`
- Validation rules:
	- campaign must belong to `client_id`
	- campaign must be active
	- client must be active

Minimal payload example:

```json
{
	"campaign_id": "54f5ee9e-77d8-4b3f-a445-17980abde2ab",
	"client_id": "4e5b7e2d-4d35-4d4c-90c8-a1d16d7d1f4a",
	"total_uploaded": 12,
	"total_expected": 100,
	"latest_urn": "IMG_0012.tiff",
	"status": "scanning"
}
```

Example signature (Python):

```python
import hashlib
import hmac
import json

secret = "your-shared-hmac-secret"
payload = {
		"campaign_id": "54f5ee9e-77d8-4b3f-a445-17980abde2ab",
		"client_id": "4e5b7e2d-4d35-4d4c-90c8-a1d16d7d1f4a",
		"status": "scanning",
}
body = json.dumps(payload).encode()

signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
```

## Security Features

- **Two-Factor Authentication** — TOTP (authenticator app) + Email OTP + backup codes
- **Login Rate Limiting** — django-axes (5 attempts, 15-minute lockout)
- **Field Encryption** — Sensitive bank data encrypted at rest (Fernet)
- **GDPR Compliance** — Donor data erasure command, GDPR consent tracking
- **CSP Headers** — Content Security Policy via django-csp
- **Audit Logging** — Signal-based, per-field change tracking
- **Session Security** — 1-hour timeout, HTTP-only cookies
- **PII Stripping** — Sentry events scrubbed of sensitive data

## License

Proprietary — All rights reserved.
