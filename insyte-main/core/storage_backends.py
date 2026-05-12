"""Cloudflare R2 storage backend for scanned donation forms.

Usage in settings.py (optional — only for direct Django file uploads):
    DEFAULT_FILE_STORAGE = 'core.storage_backends.ScannedFormStorage'

Most scanned forms are uploaded by the scanner workstation via the sync
script and webhook, not through Django's file upload mechanism. This
backend is provided for:
    - Future admin-side manual uploads
    - Programmatic access to R2 from Django (exists check, URL generation)
"""

import logging
from datetime import datetime
from typing import Any, NamedTuple

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


def get_r2_client():
    """Return a boto3 S3 client configured for Cloudflare R2."""
    try:
        import boto3
    except ImportError as exc:
        raise ImportError(
            "boto3 is required for R2 storage. Install with: uv add boto3"
        ) from exc

    # Cloudflare R2 works best with 'auto' for the S3 client region
    region = getattr(settings, "R2_REGION", "")

    # Use explicit endpoint URL if provided, otherwise construct it
    endpoint_url = getattr(settings, "R2_S3_ENDPOINT_URL", "")
    if not endpoint_url:
        endpoint_prefix = (
            f"{settings.R2_ACCOUNT_ID}.{region}."
            if region and region != "auto"
            else f"{settings.R2_ACCOUNT_ID}."
        )
        endpoint_url = f"https://{endpoint_prefix}r2.cloudflarestorage.com"

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",  # Recommended for Cloudflare R2
    )


def r2_enabled() -> bool:
    """Return True when all required R2 settings are configured."""
    return bool(
        getattr(settings, "R2_BUCKET_NAME", "")
        and getattr(settings, "R2_ACCESS_KEY_ID", "")
        and getattr(settings, "R2_SECRET_ACCESS_KEY", "")
        and getattr(settings, "R2_ACCOUNT_ID", "")
    )


def r2_key_exists(key: str) -> bool:
    """Check whether an object exists in the R2 bucket."""
    if not r2_enabled():
        return False
    try:
        client = get_r2_client()
        client.head_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
        return True
    except Exception:
        return False


# Safety headroom subtracted from the URL's signed lifetime when caching, so
# we never serve a cached URL that's about to expire mid-request. 10 minutes
# leaves room for clock skew between Django/R2 and slow client renders.
_R2_PRESIGNED_CACHE_HEADROOM_SECONDS = 600


def _r2_presigned_cache_ttl(expires_in: int) -> int:
    """Return the cache TTL for a presigned URL with the given signed lifetime.

    Subtracts a fixed headroom so cached URLs are evicted before R2 considers
    them expired. For lifetimes shorter than the headroom we fall back to the
    full ``expires_in`` (the URL is short-lived enough that caching it for its
    whole life is still safe).
    """
    return min(expires_in - _R2_PRESIGNED_CACHE_HEADROOM_SECONDS, expires_in)


def r2_presigned_url(key: str, expiry: int = 3600) -> str | None:
    """Generate a presigned (time-limited) GET URL for a private R2 object.

    The result is cached in Django's default cache so concurrent QA reviewers
    loading the same scan don't each trigger a fresh boto3 ``generate_presigned_url``
    roundtrip. Cached for ``min(expiry - 600, expiry)`` seconds so the URL is
    evicted before R2 considers it expired.

    Args:
        key: R2 object key.
        expiry: URL validity in seconds (default 1 hour).

    Returns:
        Presigned URL string, or None if R2 is not configured.
    """
    if not r2_enabled():
        return None

    cache_key = f"r2_url:{key}"
    cached = cache.get(cache_key)
    if isinstance(cached, str):
        return cached

    try:
        client = get_r2_client()
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.R2_BUCKET_NAME, "Key": key},
            ExpiresIn=expiry,
        )
    except Exception:
        logger.exception("Failed to generate presigned URL for key '%s'", key)
        return None

    if isinstance(url, str):
        cache.set(cache_key, url, _r2_presigned_cache_ttl(expiry))
    return url


def r2_public_url(key: str) -> str:
    """Build a public URL for an R2 object.

    Handles the case where ``R2_CUSTOM_DOMAIN`` is stored with or without
    a scheme prefix (e.g. ``https://...`` vs just the bare hostname).
    Spaces in the key are percent-encoded so the URL is valid.
    """
    from urllib.parse import quote

    encoded_key = quote(key, safe="/")
    domain = getattr(settings, "R2_CUSTOM_DOMAIN", "")
    if domain:
        # Strip any existing scheme so we always produce exactly one https://
        bare_domain = (
            domain.removeprefix("https://").removeprefix("http://").rstrip("/")
        )
        return f"https://{bare_domain}/{encoded_key}"
    return f"https://{settings.R2_BUCKET_NAME}.r2.cloudflarestorage.com/{encoded_key}"


def r2_put_object(
    key: str, body: bytes, content_type: str = "application/octet-stream"
) -> None:
    """Upload *body* to R2 at *key* (staff redaction uploads, etc.)."""
    if not r2_enabled():
        raise RuntimeError("R2 storage is not configured")
    client = get_r2_client()
    client.put_object(
        Bucket=settings.R2_BUCKET_NAME,
        Key=key,
        Body=body,
        ContentType=content_type,
    )


def r2_get_object(key: str) -> bytes:
    """Download *key* from the R2 bucket and return its raw bytes.

    Args:
        key: R2 object key.

    Returns:
        The object's body as a bytes blob.

    Raises:
        RuntimeError: R2 storage is not configured.
        botocore.exceptions.ClientError: Propagated from boto3 for any
            S3-side failure (NoSuchKey, AccessDenied, transient 5xx, ...).
            Callers decide whether to retry or surface a user-facing error.
    """
    if not r2_enabled():
        raise RuntimeError("R2 storage is not configured")
    client = get_r2_client()
    response = client.get_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


def r2_copy_object(source_key: str, destination_key: str) -> None:
    """Server-side copy an object within the R2 bucket.

    Used by the atomic redaction upload pattern: bytes are first PUT to a
    temp key, then a server-side ``copy_object`` swaps them into the final
    ``redacted/`` key. Doing the swap server-side (instead of re-PUTing
    bytes) avoids a second client-side upload that could fail mid-stream
    and leave a partial final blob — exactly the failure mode that
    justified the temp pattern.

    Args:
        source_key: R2 key to copy from.
        destination_key: R2 key to copy to.

    Raises:
        RuntimeError: When R2 is not configured.
        botocore.exceptions.ClientError: Propagated from boto3 for any
            S3-side failure (NoSuchKey on the source, AccessDenied,
            transient 5xx, ...).
    """
    if not r2_enabled():
        raise RuntimeError("R2 storage is not configured")
    client = get_r2_client()
    client.copy_object(
        Bucket=settings.R2_BUCKET_NAME,
        Key=destination_key,
        CopySource={"Bucket": settings.R2_BUCKET_NAME, "Key": source_key},
    )


def r2_delete_object(key: str) -> None:
    """Delete an object from the R2 bucket.

    Used to purge originals after a redaction upload succeeds so unredacted
    PCI/PII content does not linger in object storage.

    Args:
        key: R2 object key to delete.

    Raises:
        RuntimeError: When R2 is not configured.
        botocore.exceptions.ClientError: For non-NoSuchKey errors. Deleting a
            missing key is treated as a no-op so callers can be idempotent.
    """
    if not r2_enabled():
        raise RuntimeError("R2 storage is not configured")

    from botocore.exceptions import ClientError

    client = get_r2_client()
    try:
        client.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "NoSuchKey":
            return
        raise


# Auth/permission codes — retrying these just hammers R2 with broken creds.
# Anything else (5xx, throttling, network) is treated as transient.
_R2_NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "NoSuchBucket",
        "AllAccessDisabled",
    }
)


class R2ConfigurationError(RuntimeError):
    """Raised when an R2 list call fails for non-retryable reasons (auth/config)."""


def r2_list_prefix(prefix: str, max_keys: int = 20) -> list[str]:
    """List object keys matching a prefix in R2 (fully paginated).

    Uses a paginator so batches larger than 1 000 files are never silently
    truncated.  ``max_keys`` sets an upper bound on the total number of keys
    returned across all pages.

    Raises:
        R2ConfigurationError: Non-retryable failures (403 AccessDenied,
            InvalidAccessKeyId, NoSuchBucket, etc.) — the bucket is
            misconfigured and retrying will not help.
        botocore.exceptions.ClientError: Transient errors (5xx, throttling)
            propagate so callers can let Celery retry with backoff.  Other
            unexpected exceptions (socket/DNS) propagate too — a successful
            empty list must mean the bucket really had no matching keys.
    """
    if not r2_enabled():
        return []

    from botocore.exceptions import ClientError

    try:
        client = get_r2_client()
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=settings.R2_BUCKET_NAME,
            Prefix=prefix,
            PaginationConfig={
                "MaxItems": max_keys,
                "PageSize": min(max_keys, 1000),
            },
        )
        keys: list[str] = []
        for page in pages:
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return keys
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in _R2_NON_RETRYABLE_ERROR_CODES:
            logger.error(
                "R2 list_prefix non-retryable failure for '%s': %s",
                prefix,
                error_code,
            )
            raise R2ConfigurationError(
                f"R2 list failed (non-retryable): {error_code}"
            ) from exc
        logger.warning(
            "R2 list_prefix transient failure for '%s': %s — propagating for retry",
            prefix,
            error_code or exc,
        )
        raise


class R2ObjectInfo(NamedTuple):
    """Lightweight bag of fields returned by paginated list_objects_v2 entries."""

    key: str
    size: int
    last_modified: datetime


def r2_list_prefix_with_metadata(
    prefix: str, max_keys: int = 1000
) -> list[R2ObjectInfo]:
    """List object key + size + last-modified tuples in R2 for *prefix*.

    The orphan-cleanup task needs both age (to enforce a 24h grace period
    so we never delete a temp blob mid-upload) and size (to log how many
    bytes were freed). ``r2_list_prefix`` returns only keys, so this is a
    parallel helper rather than a replacement.

    Raises the same exceptions as :func:`r2_list_prefix`.
    """
    if not r2_enabled():
        return []

    from botocore.exceptions import ClientError

    try:
        client = get_r2_client()
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(
            Bucket=settings.R2_BUCKET_NAME,
            Prefix=prefix,
            PaginationConfig={
                "MaxItems": max_keys,
                "PageSize": min(max_keys, 1000),
            },
        )
        results: list[R2ObjectInfo] = []
        for page in pages:
            for obj in page.get("Contents", []):
                results.append(
                    R2ObjectInfo(
                        key=obj["Key"],
                        size=int(obj.get("Size", 0) or 0),
                        last_modified=obj["LastModified"],
                    )
                )
        return results
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in _R2_NON_RETRYABLE_ERROR_CODES:
            logger.error(
                "R2 list_prefix_with_metadata non-retryable failure for '%s': %s",
                prefix,
                error_code,
            )
            raise R2ConfigurationError(
                f"R2 list failed (non-retryable): {error_code}"
            ) from exc
        logger.warning(
            "R2 list_prefix_with_metadata transient failure for '%s': %s — "
            "propagating for retry",
            prefix,
            error_code or exc,
        )
        raise


try:
    from storages.backends.s3boto3 import S3Boto3Storage

    class ScannedFormStorage(S3Boto3Storage):
        """Django storage backend pointing at the R2 scanned-forms bucket.

        Requires django-storages[s3] and boto3.
        Only instantiate this when R2 is actually configured.
        """

        def __init__(self, **kwargs: Any) -> None:
            region = getattr(settings, "R2_REGION", "")
            endpoint_url = getattr(settings, "R2_S3_ENDPOINT_URL", "")
            if not endpoint_url:
                endpoint_prefix = (
                    f"{settings.R2_ACCOUNT_ID}.{region}."
                    if region and region != "auto"
                    else f"{settings.R2_ACCOUNT_ID}."
                )
                endpoint_url = f"https://{endpoint_prefix}r2.cloudflarestorage.com"

            kwargs.setdefault("endpoint_url", endpoint_url)
            kwargs.setdefault("access_key", settings.R2_ACCESS_KEY_ID)
            kwargs.setdefault("secret_key", settings.R2_SECRET_ACCESS_KEY)
            kwargs.setdefault("bucket_name", settings.R2_BUCKET_NAME)
            kwargs.setdefault("region_name", "auto")
            kwargs.setdefault("default_acl", "public-read")
            kwargs.setdefault("querystring_auth", False)
            custom_domain = getattr(settings, "R2_CUSTOM_DOMAIN", "")
            if custom_domain:
                kwargs.setdefault("custom_domain", custom_domain)
            super().__init__(**kwargs)

except ImportError:
    # django-storages not installed — ScannedFormStorage unavailable
    pass
