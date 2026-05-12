#!/usr/bin/env python3
"""Transform .env.audit-tmp (raw KEY=<json-encoded-value>) into a categorized .env.

Reads the staged file produced by `coolify app env list -s --format json`
filtered through jq, and writes a tidy .env with section headers and inline
comments. Values are decoded from JSON (so escaped \\n are restored to real
newlines for multi-line values like the GCP credential JSON).

The script never prints values to stdout. It only writes them to the
destination .env file. Reports counts and missing keys.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / ".env.audit-tmp"
DST = ROOT / ".env"

CATEGORIES: list[tuple[str, list[str]]] = [
    (
        "Django core",
        [
            "DJANGO_SETTINGS_MODULE",
            "SECRET_KEY",
            "FIELD_ENCRYPTION_SALT",
            "ALLOWED_HOSTS",
            "TIME_ZONE",
            "ENVIRONMENT",
            "LOG_LEVEL",
        ],
    ),
    (
        "Database (PostgreSQL)",
        [
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "POSTGRES_NAME",
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_SSLMODE",
            "POSTGRES_ENGINE",
        ],
    ),
    (
        "Redis / Celery",
        [
            "REDIS_PASSWORD",
            "REDIS_HOST",
            "REDIS_PORT",
            "REDIS_CACHE_DB",
            "REDIS_SESSION_DB",
            "REDIS_CELERY_BROKER_DB",
            "REDIS_CELERY_RESULT_DB",
            "DJANGO_REDIS_CACHE_URL",
            "DJANGO_REDIS_SESSION_URL",
            "CELERY_BROKER_URL",
            "CELERY_RESULT_BACKEND",
        ],
    ),
    (
        "Cloudflare R2 storage",
        [
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET_NAME",
            "R2_REGION",
            "R2_CUSTOM_DOMAIN",
            "R2_S3_ENDPOINT_URL",
        ],
    ),
    (
        "Document AI / scanner webhook",
        [
            "GOOGLE_CLOUD_PROJECT_ID",
            "GOOGLE_APPLICATION_CREDENTIALS_JSON_B64",
            "SCAN_WEBHOOK_SECRET",
        ],
    ),
    (
        "Email (Resend primary, SMTP fallback)",
        [
            "EMAIL_BACKEND",
            "RESEND_API_KEY",
            "DEFAULT_FROM_EMAIL",
            "SERVER_EMAIL",
            "EMAIL_HOST",
            "EMAIL_PORT",
            "EMAIL_USE_TLS",
            "EMAIL_HOST_USER",
            "EMAIL_HOST_PASSWORD",
        ],
    ),
    (
        "Security / TLS",
        [
            "CSRF_TRUSTED_ORIGINS",
            "CSRF_COOKIE_SECURE",
            "SESSION_COOKIE_SECURE",
            "SECURE_SSL_REDIRECT",
            "SECURE_HSTS_SECONDS",
        ],
    ),
    (
        "Compose / Coolify orchestration",
        ["APP_DOMAIN", "BACKUP_INTERVAL_SECONDS", "BACKUP_RETENTION_DAYS"],
    ),
]


def quote_for_env(value: str) -> str:
    """Wrap value in double-quotes if it contains whitespace, special chars, or a newline."""
    if value == "":
        return ""
    needs_quoting = any(
        c in value for c in (" ", "\t", "\n", "\r", "#", "$", "'", '"', "\\")
    )
    if not needs_quoting:
        return value
    # Escape backslashes and double-quotes for double-quoted env values
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def main() -> int:
    if not SRC.exists():
        print(
            f"ERROR: {SRC} not found. Run the coolify list+jq pipeline first.",
            file=sys.stderr,
        )
        return 1

    pairs: dict[str, str] = {}
    with SRC.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n")
            if not line or "=" not in line:
                continue
            key, _, json_value = line.partition("=")
            try:
                pairs[key] = json.loads(json_value)
            except json.JSONDecodeError:
                # Fallback: treat as literal
                pairs[key] = json_value

    seen = set()
    out_lines: list[str] = [
        "# ================================================================",
        "# INSYTE DMS — .env (auto-generated from Coolify on audit)",
        "#",
        "# This file is consumed by both `docker compose up` locally AND",
        "# Coolify (via env_file: .env). It is gitignored.",
        "#",
        "# Source of truth: this file. Mirror changes back to Coolify with",
        "#   coolify app env update <app-uuid> <var-uuid> --value '...'",
        "# ================================================================",
        "",
    ]

    for header, keys in CATEGORIES:
        out_lines.append(f"# ─── {header} ───")
        for key in keys:
            seen.add(key)
            if key not in pairs:
                out_lines.append(f"# {key}=  # NOT SET in Coolify")
                continue
            value = pairs[key]
            out_lines.append(f"{key}={quote_for_env(value)}")
        out_lines.append("")

    # Anything in pairs not categorized?
    leftover = sorted(set(pairs.keys()) - seen)
    if leftover:
        out_lines.append("# ─── Uncategorized (review) ───")
        for key in leftover:
            out_lines.append(f"{key}={quote_for_env(pairs[key])}")
        out_lines.append("")

    out_lines.extend(
        [
            "# ─── Optional: not set in Coolify (uncomment + fill if needed) ───",
            "# SENTRY_DSN=https://...@sentry.io/...",
            "# GETADDRESS_API_KEY=",
            "",
            "# Optional hardening (defaults are safe):",
            "# STORE_STRIPE_RAW_PAYLOADS=false",
            "# ALLOW_CARD_METADATA_EXPORT=false",
            "# STRICT_UNREDACTED_SCAN_VIEW=false",
            "# INSYTE_SCAN_WEBHOOK_TIMESTAMP_REQUIRED=true",
            "# INSYTE_STRICT_SCANNER_STATUS_VALIDATION=true",
            "# INSYTE_METRICS_ALLOWED_IPS=127.0.0.1,::1",
            "# INSYTE_METRICS_TRUSTED_PROXIES=127.0.0.1,::1",
            "",
        ]
    )

    DST.write_text("\n".join(out_lines), encoding="utf-8")

    # Report (no values shown)
    print(
        f"Wrote {DST.relative_to(ROOT)} with {len(pairs)} variables across {len(CATEGORIES)} categories"
    )
    missing = [k for k in (k for _, ks in CATEGORIES for k in ks) if k not in pairs]
    if missing:
        print(f"NOT-SET in Coolify (kept as commented stubs): {', '.join(missing)}")
    if leftover:
        print(f"Extra keys present in audit file: {', '.join(leftover)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
