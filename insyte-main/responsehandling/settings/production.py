"""Production settings for responsehandling project."""

import logging
import os
from pathlib import Path
from urllib.parse import quote_plus

from .base import *  # noqa

BASE_DIR = Path(__file__).resolve().parents[2]

_settings_log = logging.getLogger(__name__)

# ─── PCI-oriented defaults (production) ───
# Tighten retention of Stripe JSON and card columns in exports unless env
# explicitly opts in. Empty env → false; set to "true" to allow full payloads.
_store_raw = os.getenv("STORE_STRIPE_RAW_PAYLOADS", "").strip()
STORE_STRIPE_RAW_PAYLOADS = _store_raw.lower() == "true" if _store_raw else False
_card_export = os.getenv("ALLOW_CARD_METADATA_EXPORT", "").strip()
ALLOW_CARD_METADATA_EXPORT = _card_export.lower() == "true" if _card_export else False

# SECURITY: Always disable debug in production — never allow env override
DEBUG = False

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
USE_X_FORWARDED_HOST = True

# ─── Django Axes proxy hop count (audit 2026-05-02 §1.7) ───
# When the app is behind a reverse proxy (Traefik / Coolify), axes must be
# told how many trusted proxies sit in front of it so it parses
# X-Forwarded-For correctly when locking accounts. Default 1 matches our
# single-proxy deployment. The reverse proxy MUST strip any client-supplied
# X-Forwarded-For headers and set its own — otherwise an attacker can
# spoof their source IP to dodge per-IP lockouts.
AXES_PROXY_COUNT = int(os.getenv("AXES_PROXY_COUNT", "1"))
AXES_IPWARE_PROXY_COUNT = AXES_PROXY_COUNT
AXES_META_PRECEDENCE_ORDER = ("HTTP_X_FORWARDED_FOR", "REMOTE_ADDR")

if AXES_PROXY_COUNT == 0:
    _settings_log.warning(
        "AXES_PROXY_COUNT is 0 in production — axes will treat the reverse "
        "proxy's IP as the client IP, causing every user to share a single "
        "lockout bucket. Set AXES_PROXY_COUNT to the number of trusted "
        "proxies in front of the app (typically 1)."
    )

# SECURITY: SECRET_KEY must be set in production — fail loudly if missing
if not os.getenv("SECRET_KEY"):
    raise ValueError(
        "SECRET_KEY environment variable is required in production. "
        "Set it to a strong random value."
    )
SECRET_KEY = os.environ["SECRET_KEY"]

# SECURITY: FIELD_ENCRYPTION_SALT must be set — hardcoded default is insecure
if not os.getenv("FIELD_ENCRYPTION_SALT"):
    raise ValueError(
        "FIELD_ENCRYPTION_SALT environment variable is required in production. "
        "Set it to a unique, random string. WARNING: once set, never change it — "
        "existing encrypted data becomes unreadable."
    )
SALT_KEY = os.environ["FIELD_ENCRYPTION_SALT"]

ALLOWED_HOSTS = [
    h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()
]
if not ALLOWED_HOSTS:
    raise ValueError(
        "ALLOWED_HOSTS environment variable is required in production. "
        "Set it to a comma-separated list of allowed hostnames."
    )


def build_database_options() -> dict[str, str | int]:
    """Build PostgreSQL connection options from environment variables.

    Returns:
        Dictionary suitable for Django's DATABASES["default"]["OPTIONS"].
    """
    options: dict[str, str | int] = {}

    connect_timeout = os.getenv("POSTGRES_CONNECT_TIMEOUT", "10").strip()
    if connect_timeout:
        options["connect_timeout"] = int(connect_timeout)

    sslmode = os.getenv("POSTGRES_SSLMODE", "").strip()
    if sslmode:
        options["sslmode"] = sslmode

    sslrootcert = os.getenv("POSTGRES_SSLROOTCERT", "").strip()
    if sslrootcert:
        options["sslrootcert"] = sslrootcert

    sslcert = os.getenv("POSTGRES_SSLCERT", "").strip()
    if sslcert:
        options["sslcert"] = sslcert

    sslkey = os.getenv("POSTGRES_SSLKEY", "").strip()
    if sslkey:
        options["sslkey"] = sslkey

    return options


# ======================================================
# Database (PostgreSQL)
# ======================================================

DATABASES = {
    "default": {
        "ENGINE": os.getenv("POSTGRES_ENGINE", "django.db.backends.postgresql"),
        "NAME": os.getenv("POSTGRES_NAME", ""),
        "USER": os.getenv("POSTGRES_USER", ""),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "HOST": os.getenv("POSTGRES_HOST", ""),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 600,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": build_database_options(),
    }
}

SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "true").lower() == "true"
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
CSRF_COOKIE_SECURE = os.getenv("CSRF_COOKIE_SECURE", "true").lower() == "true"

SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Audit 2026-05-02 §10.2: a stray ``SECURE_HSTS_SECONDS=0`` in the env (or a
# dev .env shipped to prod by mistake) silently disables HSTS without
# tripping ``manage.py check --deploy`` outside of CI. Surface it loudly.
if SECURE_HSTS_SECONDS == 0:
    _settings_log.warning(
        "SECURE_HSTS_SECONDS is 0 in production — HSTS is disabled. "
        "This is only safe behind a TLS-terminating proxy that owns HSTS "
        "itself; verify with the operator before relying on it."
    )

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

# ─── Optional integrations (log once at import; avoid RuntimeWarning noise) ───

# ─── SECURITY: SCAN_WEBHOOK_SECRET must be set in production ───
# An empty SCAN_WEBHOOK_SECRET makes _verify_scan_signature fail-closed —
# the scanner pipeline returns 403 on every request — but until 2026-05-02
# this only logged at INFO, so a misconfigured deploy looked healthy while
# silently rejecting all scanner traffic. Mirror the SECRET_KEY /
# FIELD_ENCRYPTION_SALT pattern above and refuse to start instead.
if not os.getenv("SCAN_WEBHOOK_SECRET"):
    raise ValueError(
        "SCAN_WEBHOOK_SECRET environment variable is required in production. "
        "Set it to the same shared secret configured on every scanner "
        "workstation; without it, the scan-upload webhook rejects all "
        "incoming HMAC-signed requests with HTTP 403."
    )

if not os.getenv("GOOGLE_CLOUD_PROJECT_ID"):
    _settings_log.info(
        "GOOGLE_CLOUD_PROJECT_ID is not set; Document AI OCR will fail if invoked."
    )

# ─── Webhook legacy-signature invariant (audit 2026-05-02 §1.8) ───
# ``SCAN_WEBHOOK_ALLOW_LEGACY_SIG=true`` accepts body-only HMAC signatures
# as a fallback. Without ``SCAN_WEBHOOK_TIMESTAMP_REQUIRED=true`` that
# fallback is replayable indefinitely (no timestamp binds the signature).
# The two flags must agree before legacy mode is safe — refuse to start
# in the unsafe combination.
_legacy_sig = (
    os.getenv("INSYTE_SCAN_WEBHOOK_ALLOW_LEGACY_SIG", "false").lower() == "true"
)
_ts_required = (
    os.getenv("INSYTE_SCAN_WEBHOOK_TIMESTAMP_REQUIRED", "false").lower() == "true"
)
if _legacy_sig and not _ts_required:
    raise ValueError(
        "INSYTE_SCAN_WEBHOOK_ALLOW_LEGACY_SIG=true requires "
        "INSYTE_SCAN_WEBHOOK_TIMESTAMP_REQUIRED=true to close the legacy-replay "
        "window. Either upgrade scanner workstations to send modern "
        "timestamp-prefixed signatures and set the timestamp flag, or disable "
        "the legacy fallback."
    )

REDIS_HOST = os.getenv("REDIS_HOST", "insyte-redis")
REDIS_PORT = os.getenv("REDIS_PORT", "6379")
REDIS_PASSWORD_RAW = os.getenv("REDIS_PASSWORD", "").strip()
REDIS_PASSWORD = quote_plus(REDIS_PASSWORD_RAW) if REDIS_PASSWORD_RAW else ""

REDIS_CACHE_DB = os.getenv("REDIS_CACHE_DB", "1")
REDIS_SESSION_DB = os.getenv("REDIS_SESSION_DB", "2")
REDIS_CELERY_RESULT_DB = os.getenv("REDIS_CELERY_RESULT_DB", "3")
REDIS_CELERY_BROKER_DB = os.getenv("REDIS_CELERY_BROKER_DB", "0")


def build_redis_url(db: str) -> str:
    if REDIS_PASSWORD:
        return f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{db}"
    return f"redis://{REDIS_HOST}:{REDIS_PORT}/{db}"


REDIS_CACHE_URL = os.getenv("DJANGO_REDIS_CACHE_URL") or build_redis_url(REDIS_CACHE_DB)
REDIS_SESSION_URL = os.getenv("DJANGO_REDIS_SESSION_URL") or build_redis_url(
    REDIS_SESSION_DB
)

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL") or build_redis_url(
    REDIS_CELERY_BROKER_DB
)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND") or build_redis_url(
    REDIS_CELERY_RESULT_DB
)

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
        },
        "KEY_PREFIX": "dms",
        "TIMEOUT": 3600,
    },
    "sessions": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_SESSION_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
        },
        "KEY_PREFIX": "session",
        "TIMEOUT": 86400,
    },
}

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "sessions"

CELERY_CACHE_BACKEND = "django-cache"
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers.DatabaseScheduler"


EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "core.mail_backends.ResendBackend")

# Resend API Key is already in base.py, but we ensure it's here for clarity if needed
RESEND_API_KEY = os.getenv("RESEND_API_KEY")

# Traditional SMTP settings (kept for fallback or other use cases if needed)
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")


SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

    from responsehandling.sentry_scrubber import SENSITIVE_KEYS, strip_pii

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        traces_sample_rate=0.1,
        send_default_pii=False,  # GDPR: never auto-send PII
        before_send=strip_pii,  # pyright: ignore[reportArgumentType]
        # Defense in depth: Sentry's own scrubber walks the payload using a
        # deny-list and redacts matching keys, in addition to our before_send
        # hook. Keep DEFAULT_DENYLIST plus our domain-specific keys so the
        # built-in patterns (Authorization, password, etc.) stay in effect.
        event_scrubber=EventScrubber(
            denylist=list(DEFAULT_DENYLIST) + sorted(SENSITIVE_KEYS),
        ),
        environment=os.getenv("ENVIRONMENT", "production"),
    )


LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        # Pulls the active request_id off core.middleware's contextvar and
        # attaches it to every record so the JSON formatter can emit it.
        "request_id": {
            "()": "core.middleware.RequestIDLogFilter",
        },
    },
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.json.JsonFormatter",
            # request_id is supplied by the request_id filter below; the
            # JSON formatter renders any record attribute mentioned here.
            "format": ("%(asctime)s %(levelname)s %(name)s %(request_id)s %(message)s"),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["request_id"],
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "django.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "json",
            "filters": ["request_id"],
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": os.getenv("LOG_LEVEL", "INFO"),
    },
}

# Tailwind CSS — binary is pre-installed in the Docker image (see Dockerfile).
# Set TAILWIND_CLI_AUTOMATIC_DOWNLOAD=true only if you need a deploy-time download instead.
_tw_auto = os.getenv("TAILWIND_CLI_AUTOMATIC_DOWNLOAD", "").strip().lower()
TAILWIND_CLI_AUTOMATIC_DOWNLOAD = _tw_auto in ("true", "1", "yes")
