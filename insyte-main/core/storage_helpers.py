"""Helpers for storage-backed media access across local and remote backends."""

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.files.storage import default_storage


def normalize_media_storage_name(path: str) -> str:
    """Return a storage-relative media name for a stored path."""
    raw_path = (path or "").strip()
    if not raw_path:
        return ""

    if os.path.isabs(raw_path):
        media_root = os.path.abspath(str(settings.MEDIA_ROOT))
        absolute_path = os.path.abspath(raw_path)
        try:
            common_root = os.path.commonpath([absolute_path, media_root])
        except ValueError:
            return ""
        if common_root != media_root:
            return ""
        relative_path = os.path.relpath(absolute_path, media_root)
        return relative_path.replace(os.sep, "/")

    normalized = raw_path.replace("\\", "/").lstrip("/")
    parts = [part for part in normalized.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def media_storage_exists(path: str) -> bool:
    """Return whether *path* exists in the configured default storage."""
    storage_name = normalize_media_storage_name(path)
    return bool(storage_name) and default_storage.exists(storage_name)


def open_media_storage_file(path: str, mode: str = "rb") -> Any:
    """Open *path* from the configured default storage backend."""
    storage_name = normalize_media_storage_name(path)
    if not storage_name:
        raise FileNotFoundError(path)
    return default_storage.open(storage_name, mode)


def media_storage_size(path: str) -> int:
    """Return the file size for *path* from the configured default storage."""
    storage_name = normalize_media_storage_name(path)
    if not storage_name:
        return 0
    return int(default_storage.size(storage_name))


def media_storage_modified_time(path: str) -> Any:
    """Return the modified time for *path* from the configured default storage."""
    storage_name = normalize_media_storage_name(path)
    if not storage_name:
        return None
    return default_storage.get_modified_time(storage_name)


@contextlib.contextmanager
def local_storage_path(field_file: Any) -> Iterator[str]:
    """Yield a local filesystem path for a storage-backed file."""
    try:
        yield str(field_file.path)
        return
    except AttributeError, NotImplementedError, ValueError:
        pass

    suffix = Path(getattr(field_file, "name", "")).suffix or ".tmp"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        temp_path = tmp.name
        field_file.open("rb")
        try:
            chunks = getattr(field_file, "chunks", None)
            if callable(chunks):
                for chunk in field_file.chunks():
                    tmp.write(chunk)
            else:
                tmp.write(field_file.read())
        finally:
            field_file.close()

    try:
        yield temp_path
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
