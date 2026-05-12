"""Development settings for responsehandling project."""

import os
from pathlib import Path

from dotenv import load_dotenv

from .base import *  # noqa: F403

BASE_DIR = Path(__file__).resolve().parents[2]

# Load environment variables from .env file
load_dotenv(os.path.join(".env.development"))

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ["*"]

# Database - SQLite for development
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "timeout": 20,  # Increase timeout to handle bulk processing locks
        },
    }
}

# Cache - Local Memory for Development
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "default-cache",
        "OPTIONS": {
            "MAX_ENTRIES": 10000,
        },
        "KEY_PREFIX": "dms",
        "TIMEOUT": 3600,
    },
    "sessions": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "session-cache",
        "KEY_PREFIX": "session",
        "TIMEOUT": 86400,
    },
    "queries": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "query-cache",
        "KEY_PREFIX": "query",
        "TIMEOUT": 7200,
    },
}

# Session - Database for Development
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# Celery - SQLite broker for development (no Redis required)
CELERY_BROKER_URL = os.getenv(
    "CELERY_BROKER_URL", f"sqla+sqlite:///{BASE_DIR / 'db.sqlite3'}"
)
CELERY_RESULT_BACKEND = "django-db"
CELERY_CACHE_BACKEND = "django-cache"
CELERY_BROKER_POOL_LIMIT = 0
CELERY_BROKER_HEARTBEAT = 0
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# SQLite-backed local brokers do not reliably support Celery inspect.
HEALTH_CHECK_CELERY_MODE = "broker"

# Email - Console backend for development
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Media files serving in development
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Logging - Verbose console logging for development
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",  # Set to DEBUG to see SQL queries
            "propagate": False,
        },
    },
}
