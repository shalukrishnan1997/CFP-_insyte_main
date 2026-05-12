"""
Base settings for responsehandling project.
Contains common settings shared across all environments.
"""

import os
import socket
from pathlib import Path

from dotenv import load_dotenv

# Initialize django-environ
load_dotenv(".env")

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "insecure-dev-key-change-in-production",  # Safe default for local dev only
)

# Application definition
INSTALLED_APPS = [
    # Custom user app MUST be first
    "core",
    # Django core
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "django_celery_beat",
    "django_celery_results",
    "whitenoise.runserver_nostatic",
    "rest_framework",
    "django_filters",
    # Tailwind CSS (standalone CLI, no Node.js)
    "django_tailwind_cli",
    # Two-Factor Authentication
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    # Security
    "axes",
    # Local apps
    "auth_app",
    "custom_admin",
    # Domain apps (carved out from core; models migrate progressively)
    "audit",
    "clients",
    "campaigns",
    "donors",
    "donations",
    "scans",
    "letters",
    "invoices",
    "payments",
    "banking",
    "notifications",
    "client_portal",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.security_middleware.ScanDetectionMiddleware",
    "core.middleware.RequestIDMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",  # 2FA middleware
    "csp.middleware.CSPMiddleware",  # Content Security Policy headers
    "audit.middleware.AuditRequestMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "client_portal.middleware.ClientPortalMiddleware",
    "auth_app.middleware.ForcePasswordChangeMiddleware",
    "axes.middleware.AxesMiddleware",
]

# Authentication backends — AxesStandaloneBackend MUST be first
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

# ─── Django Axes (Login Rate Limiting) ───
AXES_FAILURE_LIMIT = 5  # Lock after 5 failed attempts
AXES_COOLOFF_TIME = 0.25  # 15-minute lockout (in hours)
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]  # Lock per user+IP, not globally
AXES_RESET_ON_SUCCESS = True  # Clear failed attempts on successful login
AXES_ENABLE_ACCESS_FAILURE_LOG = True  # Log failed attempts
AXES_VERBOSE = True  # Log details to logger

ROOT_URLCONF = "responsehandling.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "responsehandling.context_processors.user_permissions",
                "responsehandling.context_processors.currency_constants",
            ],
        },
    },
]

WSGI_APPLICATION = "responsehandling.wsgi.application"

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.getenv("TIME_ZONE", "Europe/London")
USE_I18N = True
USE_TZ = True

# UK Date and Time Formats
DATE_FORMAT = "d/m/Y"
DATETIME_FORMAT = "d/m/Y H:i"
SHORT_DATE_FORMAT = "d/m/Y"
SHORT_DATETIME_FORMAT = "d/m/Y H:i"
DATE_INPUT_FORMATS = [
    "%d/%m/%Y",  # DD/MM/YYYY - UK format (primary)
    "%Y-%m-%d",  # YYYY-MM-DD - ISO format (fallback)
    "%d-%m-%Y",  # DD-MM-YYYY
]
DATETIME_INPUT_FORMATS = [
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
]

# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"
STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STATIC_ROOT = BASE_DIR / "staticfiles"

# Tailwind CSS (standalone CLI via django-tailwind-cli)
TAILWIND_CLI_VERSION = "4.1.17"
TAILWIND_CLI_SRC_CSS = "assets/input.css"
TAILWIND_CLI_DIST_CSS = "css/output.css"

STATICFILES_FINDERS = (
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
)

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Media files (User uploads - scanned forms, etc.)
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth
AUTH_USER_MODEL = "core.User"
LOGIN_URL = "/auth/login/"

# ─── Session Security ───
# SESSION_SAVE_EVERY_REQUEST=False saves Redis writes but stops session-sliding.
# Compensate with a longer SESSION_COOKIE_AGE so active reviewers aren't kicked out.
SESSION_COOKIE_AGE = 14400  # 4 hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
# Writing the session on every request scales poorly under 100 concurrent QA
# reviewers (each navigation hits Redis with a SETEX). Django still saves on
# explicit modification (login / logout / form submits / messages framework /
# CSRF rotation). Login lockout state is owned by django-axes' own DB tables
# (AxesAttempt / AccessLog / AccessAttempt), not the session, so disabling
# per-request writes does not weaken brute-force protection.
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# REST Framework Configuration
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "30/minute",
        "user": "120/minute",
    },
    "EXCEPTION_HANDLER": "responsehandling.exceptions.custom_exception_handler",
}

# GetAddress.io API Configuration (UK Address Lookup)
GETADDRESS_API_KEY = os.getenv("GETADDRESS_API_KEY", "")

# ─── Cloudflare R2 (S3-compatible) Configuration ───
R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "")
R2_REGION = os.getenv("R2_REGION", "")
R2_CUSTOM_DOMAIN = os.getenv("R2_CUSTOM_DOMAIN", "")  # e.g. cdn.example.com
R2_S3_ENDPOINT_URL = os.getenv("R2_S3_ENDPOINT_URL", "")  # Override for S3 API endpoint

# Webhook secret for scanner upload notifications (HMAC-SHA256)
SCAN_WEBHOOK_SECRET = os.getenv("SCAN_WEBHOOK_SECRET", "")

# ─── R2 Folder-based Scan Intake (rclone path) ───
# Root prefix inside the R2 bucket where rclone mirrors scan folders.
# Expected structure: {SCAN_R2_INTAKE_PREFIX}/{appeal_code}/{payment_method}/
# Example: rclone copy C:\ScanOutput r2:bucket/ScanOutput --min-age 30s
# Staff create folders like:  C:\ScanOutput\SPRING25\cheque\
# Rclone mirrors them to:     ScanOutput/SPRING25/cheque/
SCAN_R2_INTAKE_PREFIX = os.getenv("SCAN_R2_INTAKE_PREFIX", "ScanOutput/")

# Maximum number of scanned forms per QA review batch.
# When a folder contains more images than this, the service splits them
# into multiple ScanBatches so staff can review in manageable chunks.
# Set to 0 to disable splitting (all images in one batch).
SCAN_BATCH_MAX_SIZE = int(os.getenv("SCAN_BATCH_MAX_SIZE", "30"))

# ─── Google Document AI Configuration ───
# Credentials (preferred for deployments):
# - GOOGLE_APPLICATION_CREDENTIALS_JSON_B64 (base64-encoded service account JSON), or
# - GOOGLE_APPLICATION_CREDENTIALS_JSON (raw JSON), or
# - GOOGLE_APPLICATION_CREDENTIALS (file path; local/dev fallback).
# Service account requires roles/documentai.apiUser.
# See: https://cloud.google.com/document-ai/docs/setup
#
# Each charity's Document AI processor ID is stored on the Client model
# (Client.document_ai_processor_id). The project and location are global.
GOOGLE_CLOUD_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "")

# Auto-trigger OCR processing when scanner upload completes via webhook.
# Set to False to require manual triggering from the admin UI.
OCR_AUTO_PROCESS_ON_UPLOAD = (
    os.getenv("OCR_AUTO_PROCESS_ON_UPLOAD", "true").lower() == "true"
)

# When True, the scan-upload webhook rejects payloads with unknown status
# values with HTTP 400. When False (default), unknown status values are
# logged at WARNING and coerced to "scanning" for backwards compatibility
# with scanner workstations that emit empty/typo'd status values.
# Flip to True after confirming all production scanner workstations are
# emitting valid status values (idle/scanning/complete/error).
STRICT_SCANNER_STATUS_VALIDATION = (
    os.getenv("INSYTE_STRICT_SCANNER_STATUS_VALIDATION", "false").lower() == "true"
)

# Maximum number of pages allowed in a single PDF processed by Document AI.
# PDFs that exceed this limit are rejected at upload-completion time (HTTP 413)
# and never enqueued for OCR. Document AI itself caps imageless mode at 30
# pages per document, but charity scanners occasionally produce 100+ page
# accidental dumps that would burn quota with no usable result.
MAX_OCR_PAGES_PER_PDF = int(os.getenv("MAX_OCR_PAGES_PER_PDF", "100"))

# ─── Document AI client / retry tuning ───
# Maximum number of attempts for the wrapped Document AI process_document
# call when transient errors (429/503/network timeouts) are raised.
DOCUMENT_AI_MAX_RETRY_ATTEMPTS = int(os.getenv("DOCUMENT_AI_MAX_RETRY_ATTEMPTS", "5"))
# Lifetime (seconds) of the cached DocumentProcessorServiceClient before it
# is rebuilt; the client is also rebuilt eagerly when the credential JSON
# env var changes (detected via SHA-256 hash).
DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS = int(
    os.getenv("DOCUMENT_AI_CLIENT_CACHE_TTL_SECONDS", "3600")
)

# When True, the scan-upload webhook *requires* a valid X-Scan-Timestamp
# header (or "timestamp" body field) on every request — missing/unparseable
# timestamps return 401. When False (default), timestamps are accepted but
# optional; requests without one skip the replay-protection check (the 24h
# batch-dedup hash still applies to "complete" payloads).
#
# Coordinate with scanner workstation operators before flipping this to True:
# they must upgrade their sync script to sign and send timestamps first.
SCAN_WEBHOOK_TIMESTAMP_REQUIRED = (
    os.getenv("INSYTE_SCAN_WEBHOOK_TIMESTAMP_REQUIRED", "false").lower() == "true"
)

# ─── Manual scan redaction & Stripe payload retention (PCI scope) ───
# Per-payment-method redaction policy lives in the ``RedactionSettings``
# singleton (``scans/models.py``); edit it from
# ``/django-admin/scans/redactionsettings/``.
# When True, staff must hold core.view_unredacted_scan to see originals
# before redaction is complete.
STRICT_UNREDACTED_SCAN_VIEW = (
    os.getenv("STRICT_UNREDACTED_SCAN_VIEW", "false").lower() == "true"
)
# Per-page R2 source size cap for the server-side redaction baking path.
# Sources larger than this are rejected before pypdfium2 rasterization,
# where memory usage multiplies ~10x and could OOM a worker.
REDACTION_MAX_SOURCE_BYTES = int(
    os.getenv("REDACTION_MAX_SOURCE_BYTES", str(50 * 1024 * 1024))
)
# How long a deferred redaction may sit in CVV_PENDING / DEFERRED before
# the periodic ``apply_stale_deferred_redactions`` task force-applies it.
# 7 days covers the maximum SCA / 3DS authentication window (~5 days)
# plus an operator buffer. Documented retention policy must match this
# value (see docs/pci-retention.md).
INSYTE_REDACTION_RETENTION_DAYS = int(os.getenv("INSYTE_REDACTION_RETENTION_DAYS", "7"))
# Audit 2026-05-02 §7.3: production overrides this to False (see
# responsehandling/settings/production.py:18). The base default stays True
# because the integration test suite relies on the full payload — the
# webhook payload summarizer drops Stripe Charge-shape fields (e.g.
# ``amount_refunded`` on ``charge.refunded`` events) that the test
# assertions read back. Flipping the base default broke
# tests/integration/test_e2e_payment_card_webhooks.py.
STORE_STRIPE_RAW_PAYLOADS = (
    os.getenv("STORE_STRIPE_RAW_PAYLOADS", "true").lower() == "true"
)
# When False, credit card report exports omit holder / last4 / expiry columns.
ALLOW_CARD_METADATA_EXPORT = (
    os.getenv("ALLOW_CARD_METADATA_EXPORT", "true").lower() == "true"
)

# ─── Field Encryption (django-fernet-encrypted-fields) ───
# Salt for deriving encryption keys from SECRET_KEY.
# CRITICAL: Once set, never change — existing encrypted data becomes unreadable.
#
# Audit 2026-05-02 §1.9: production refuses to start without
# FIELD_ENCRYPTION_SALT (see settings/production.py:38), but dev would
# previously fall back to a hard-coded constant. That is now derived
# per-host so a leaked dev DB doesn't auto-decrypt with the same salt
# everyone else's dev DB uses. The string makes it obvious in logs that
# this value should never reach production.
_default_salt = f"insyte-dev-salt-{socket.gethostname()}-DO-NOT-USE-IN-PROD"  # nosec B105
SALT_KEY = os.getenv("FIELD_ENCRYPTION_SALT", _default_salt)

# ─── Prometheus /metrics ───
# Internal IPs allowed to scrape the /metrics endpoint. Defaults to
# localhost only; set INSYTE_METRICS_ALLOWED_IPS in deployment to a
# comma-separated list (e.g. "10.0.0.4,10.0.0.5") for the monitoring host.
METRICS_ALLOWED_IPS: list[str] = [
    ip.strip()
    for ip in os.getenv("INSYTE_METRICS_ALLOWED_IPS", "127.0.0.1,::1").split(",")
    if ip.strip()
]

# Trusted reverse proxies whose ``X-Forwarded-For`` header may be honoured
# when resolving the metrics scraper's client IP. If ``REMOTE_ADDR`` is not
# in this list the header is ignored, preventing a public attacker from
# spoofing an allowlisted IP. Defaults to localhost only; set
# INSYTE_METRICS_TRUSTED_PROXIES to a comma-separated list of upstream
# proxy IPs (e.g. "10.0.0.2") in deployment.
METRICS_TRUSTED_PROXIES: list[str] = [
    ip.strip()
    for ip in os.getenv("INSYTE_METRICS_TRUSTED_PROXIES", "127.0.0.1,::1").split(",")
    if ip.strip()
]

# ─── Cache ───
# Each environment (development.py, production.py, test.py) defines
# its own CACHES setting. Do not define a default here.

# Celery Configuration Base
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60  # 25 minutes
CELERY_RESULT_EXPIRES = 86400  # 24 hours
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_IGNORE_RESULT = False
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 100
CELERY_TASK_SEND_SENT_EVENT = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_CONNECTION_MAX_RETRIES = 10

# ─── Celery Beat Schedule ───
# Periodic tasks for production (celery-beat picks these up automatically).
# In production the DatabaseScheduler is used (set in production.py),
# so admins can also add/edit schedules via the Django admin.
CELERY_BEAT_SCHEDULE = {
    "cleanup-old-uploads": {
        "task": "core.cleanup_old_uploads",
        "schedule": 86400.0,  # every 24 hours (seconds)
    },
    "retry-failed-payments": {
        "task": "core.retry_failed_payments",
        "schedule": 3600.0,  # every 1 hour
    },
    "cleanup-stale-scan-progress": {
        "task": "scans.cleanup_stale_scan_progress",
        "schedule": 3600.0,  # every 1 hour
    },
    "auto-retry-failed-scan-batches": {
        "task": "scans.auto_retry_failed_scan_batches",
        "schedule": 1800.0,  # every 30 minutes
    },
    "reset-stuck-scan-batches": {
        # Sweep stuck batches every 5 minutes. The task itself enforces a
        # 15-minute "stuck" threshold so that a healthy chord (10-min
        # deadline + per-scan 3-min hard limit) is always converged before
        # we reset it.
        "task": "scans.reset_stuck_scan_batches",
        "schedule": 300.0,  # every 5 minutes
    },
    "watch-r2-scan-folders": {
        "task": "scans.watch_r2_scan_folders",
        "schedule": 300.0,  # every 5 minutes
    },
    "cleanup-r2-orphans": {
        "task": "scans.cleanup_r2_orphans",
        "schedule": 21600.0,  # every 6 hours
    },
    "apply-stale-deferred-redactions": {
        # PCI retention TTL: force-apply deferred redactions that have sat
        # in CVV_PENDING / DEFERRED past INSYTE_REDACTION_RETENTION_DAYS.
        "task": "scans.apply_stale_deferred_redactions",
        "schedule": 3600.0,  # every 1 hour
    },
    "auto-export-pending-donors": {
        "task": "core.auto_export_pending_donors_task",
        "schedule": 604800.0,  # weekly (7 x 86400 seconds)
    },
    "release-stale-batch-locks": {
        "task": "donations.release_stale_batch_locks",
        "schedule": 900.0,  # every 15 minutes — TTL is 30 min (donations.models)
    },
    "sweep-expiring-card-auths": {
        # Phone-intake (MOTO) cards are authorised at intake but settlement
        # waits for QA approval. Stripe auths expire after ~7 days; warn QA
        # when an auth is within 24h of expiry so they can either approve
        # (capture) or call the donor back to collect a fresh charge.
        "task": "payments.sweep_expiring_card_auths",
        "schedule": 3600.0,  # every 1 hour
    },
}

# Email Configuration Base
EMAIL_TIMEOUT = 10
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@example.com")
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
RESEND_API_KEY = os.getenv("RESEND_API_KEY")

# Operations inbox for QA rejections — when a donation is rejected via the
# QA UI, a single email is dispatched here so an operations person can
# decide whether to issue a follow-up letter (Basecamp todo #14, Paul
# Nichols 2026-02-27). Comma-separated values are accepted. Leave unset to
# disable; the helper short-circuits when this is empty.
OPERATIONS_REJECT_EMAIL = os.getenv("OPERATIONS_REJECT_EMAIL", "")

# ─── Content Security Policy (django-csp) ───
# Defines which resources the browser is allowed to load
CSP_DEFAULT_SRC = ("'self'",)
CSP_SCRIPT_SRC = (
    "'self'",
    "'unsafe-inline'",  # Required for Alpine.js x-data inline expressions
    "https://js.stripe.com",
    "https://unpkg.com",  # HTMX CDN fallback
    "https://cdn.jsdelivr.net",  # ApexCharts, Flatpickr
)
CSP_STYLE_SRC = (
    "'self'",
    "'unsafe-inline'",  # Required for Tailwind utility classes
)
CSP_IMG_SRC = (
    "'self'",
    "data:",
    "https:",
)
CSP_FONT_SRC = (
    "'self'",
    "data:",
)
CSP_CONNECT_SRC = (
    "'self'",
    "https://api.stripe.com",
    "https://api.getaddress.io",  # UK address lookup
)
CSP_FRAME_SRC = (
    "'self'",
    "https://js.stripe.com",
    "https://hooks.stripe.com",
)
CSP_BASE_URI = ("'self'",)
CSP_FORM_ACTION = ("'self'",)
