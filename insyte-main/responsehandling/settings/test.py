"""
Test settings for responsehandling project.
Optimized for fast test execution.
"""

from .base import *  # noqa: F403

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False
TESTING = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]

# Database - In-memory SQLite for tests
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            "timeout": 20,
        },
    }
}

# Password Hashing - Use fast hasher for tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Cache - LocMem cache for tests (supports set/get unlike DummyCache)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-default",
    },
    "sessions": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-sessions",
    },
    "queries": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-queries",
    },
}

# Static files — use simple backend in tests (no manifest required)
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Session - In-memory for tests
SESSION_ENGINE = "django.contrib.sessions.backends.cache"

# Celery - Always eager for tests (synchronous execution)
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

# Email - Locmem backend for tests
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


# Disable migrations for faster tests (optional)
class DisableMigrations:
    def __contains__(self, item: str) -> bool:
        return True

    def __getitem__(self, item: str) -> None:
        return None


# Uncomment to disable migrations in tests
# MIGRATION_MODULES = DisableMigrations()

# Logging - Minimal logging for tests
LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "ERROR",
    },
}

# Disable django-axes in tests (interferes with force_login / session persistence)
AXES_ENABLED = False

INSYTE_AGENT_DEBUG = False

# Disable role-routing/2FA-enforcement middleware in tests so force_login can
# exercise view logic directly without setup redirects.
MIDDLEWARE = [
    middleware
    for middleware in MIDDLEWARE  # type: ignore[misc]  # noqa: F405
    if middleware != "client_portal.middleware.ClientPortalMiddleware"
]
