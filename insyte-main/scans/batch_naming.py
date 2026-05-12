"""Helpers for deriving canonical physical batch identifiers."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_PDF_PAGE_KEY_SEPARATOR = "::pdf_page::"


@dataclass(frozen=True, slots=True)
class BatchIdentity:
    """Canonical physical batch identity derived from an uploaded file.

    Attributes:
        batch_name: Normalized batch identifier used across the application.
        source_filename: Original uploaded filename including extension.
        source_key: Source R2 object key for the uploaded file.
    """

    batch_name: str
    source_filename: str
    source_key: str


def normalize_batch_name(raw_value: str) -> str:
    """Normalize a physical batch name for storage.

    Args:
        raw_value: Raw filename or batch identifier.

    Returns:
        Normalized batch identifier without a file extension.

    Raises:
        ValueError: If the resulting batch name is blank or too long.
    """

    filename = PurePosixPath(raw_value).name.strip()
    if filename.lower().endswith(".pdf"):
        filename = filename[:-4]
    normalized = re.sub(r"\s+", " ", filename).strip()
    if not normalized:
        raise ValueError("Batch name cannot be blank.")
    if len(normalized) > 255:
        raise ValueError("Batch name cannot exceed 255 characters.")
    return normalized


def derive_batch_identity(
    r2_keys: list[str],
    requested_batch_name: str = "",
) -> BatchIdentity:
    """Derive the canonical batch identity from uploaded R2 keys.

    Args:
        r2_keys: Source R2 keys associated with the physical batch upload.
        requested_batch_name: Optional caller-provided batch name.

    Returns:
        BatchIdentity for the uploaded physical batch.

    Raises:
        ValueError: If the upload does not resolve to exactly one PDF source file
            or if the requested batch name conflicts with the filename-derived
            canonical name.
    """

    if not r2_keys:
        raise ValueError("No R2 keys provided for scan batch.")

    source_keys = {_source_r2_key(r2_key) for r2_key in r2_keys}
    if len(source_keys) != 1:
        raise ValueError(
            "Each physical batch must be uploaded as exactly one PDF file."
        )

    source_key = source_keys.pop()
    source_filename = PurePosixPath(source_key).name
    if not source_filename.lower().endswith(".pdf"):
        raise ValueError("Physical batch uploads must use a PDF filename.")

    derived_name = normalize_batch_name(source_filename)
    if requested_batch_name:
        normalized_requested = normalize_batch_name(requested_batch_name)
        if normalized_requested != derived_name:
            raise ValueError(
                "Provided batch name must match the uploaded PDF filename."
            )

    return BatchIdentity(
        batch_name=derived_name,
        source_filename=source_filename,
        source_key=source_key,
    )


def _source_r2_key(r2_key: str) -> str:
    """Return the original uploaded source key for an R2 object key."""

    if _PDF_PAGE_KEY_SEPARATOR not in r2_key:
        return r2_key

    source_key, _, _page_text = r2_key.rpartition(_PDF_PAGE_KEY_SEPARATOR)
    if source_key:
        return source_key
    return r2_key
