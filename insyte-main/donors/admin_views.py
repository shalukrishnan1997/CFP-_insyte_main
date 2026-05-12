"""Donor management helpers.

This module contains the reusable house-file reconciliation helpers used by
client setup.
"""

import csv
import io
import logging
from typing import Any

from django.db import transaction

from clients.models import Client
from donors.models import Donor

logger = logging.getLogger(__name__)


def _parse_upload(uploaded_file: Any) -> list[dict[str, str]]:
    """Parse a pipe-delimited CSV upload into a list of row dicts.

    Args:
        uploaded_file: Django InMemoryUploadedFile or TemporaryUploadedFile.

    Returns:
        List of normalised row dicts with lowercase keys.

    Raises:
        ValueError: If the file format is unsupported, the delimiter is
            invalid, or required columns are missing.
    """
    name = uploaded_file.name.lower()

    if name.endswith(".csv"):
        rows = _parse_csv(uploaded_file)
    else:
        raise ValueError(
            "Unsupported file format. Please upload a pipe-delimited .csv file."
        )

    required = {"urn", "first_name", "last_name", "postcode"}
    if rows and not required.issubset(rows[0].keys()):
        missing = required - set(rows[0].keys())
        raise ValueError(
            f"Missing required columns: {', '.join(sorted(missing))}. "
            f"Found: {', '.join(sorted(rows[0].keys()))}"
        )

    return rows


def _parse_csv(uploaded_file: Any) -> list[dict[str, str]]:
    """Parse raw pipe-delimited CSV bytes into normalised row dicts.

    Args:
        uploaded_file: File-like object with CSV content.

    Returns:
        List of row dicts with lowercase stripped keys.

    Raises:
        ValueError: If the CSV is not pipe-delimited.
    """
    content = uploaded_file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content), delimiter="|")
    fieldnames = [field.strip().lower() for field in reader.fieldnames or [] if field]
    if (
        len(fieldnames) == 1
        and "|" not in fieldnames[0]
        and any(separator in fieldnames[0] for separator in [",", ";", "\t"])
    ):
        raise ValueError(
            "Invalid delimiter. Only pipe-delimited CSV files are supported. "
            "Use '|' between columns."
        )

    return [
        {k.strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader
    ]


def _clean_house_file_row(
    row: dict[str, str], row_index: int
) -> tuple[Donor | None, str | None]:
    """Validate and convert one house-file row into a Donor source record."""
    urn = row.get("urn", "").strip()
    first_name = row.get("first_name", "").strip()
    last_name = row.get("last_name", "").strip()
    postcode = row.get("postcode", "").strip()

    missing_fields = [
        field_name
        for field_name, value in (
            ("URN", urn),
            ("First Name", first_name),
            ("Last Name", last_name),
            ("Postcode", postcode),
        )
        if not value
    ]
    if missing_fields:
        return None, f"Row {row_index}: Missing {', '.join(missing_fields)}"

    donor = Donor(
        urn=urn,
        title=row.get("title", "").strip(),
        first_name=first_name,
        last_name=last_name,
        email=row.get("email", "").strip(),
        phone=row.get("phone", "").strip(),
        address_line1=row.get("address_line1", "").strip(),
        address_line2=row.get("address_line2", "").strip(),
        city=row.get("city", "").strip(),
        county=row.get("county", "").strip(),
        postcode=postcode,
        country=row.get("country", "").strip() or "United Kingdom",
        verification_status=Donor.VERIFICATION_VERIFIED,
    )
    return donor, None


def replace_house_file_rows(
    *,
    rows: list[dict[str, str]],
    client: Client,
    uploaded_by: Any | None = None,
) -> dict[str, Any]:
    """Replace a client's imported house-file donors with the provided rows."""
    donors_to_create: list[Donor] = []
    row_errors: list[dict[str, str]] = []
    seen_urns: set[str] = set()

    for row_index, row in enumerate(rows, start=1):
        donor, error = _clean_house_file_row(row, row_index)
        if error is not None:
            row_errors.append({"row": str(row_index), "error": error})
            continue
        assert donor is not None
        normalized_urn = donor.urn.strip().casefold() if donor.urn else ""
        if normalized_urn in seen_urns:
            row_errors.append(
                {
                    "row": str(row_index),
                    "error": f"Row {row_index}: Duplicate URN '{donor.urn}' in upload.",
                }
            )
            continue
        seen_urns.add(normalized_urn)
        donor.client = client
        donor.created_by = uploaded_by
        donors_to_create.append(donor)

    if row_errors:
        raise ValueError(row_errors[0]["error"])

    with transaction.atomic():
        deleted_count, _ = Donor.objects.filter(client=client).delete()
        Donor.objects.bulk_create(donors_to_create)

    logger.info(
        "Replaced house file donors for client %s: deleted=%d created=%d",
        client.pk,
        deleted_count,
        len(donors_to_create),
    )
    return {
        "total_rows": len(rows),
        "created_count": len(donors_to_create),
        "deleted_count": deleted_count,
        "replaced_count": len(donors_to_create),
        "row_errors": row_errors,
    }


def replace_house_file_upload(
    *,
    uploaded_file: Any,
    client: Client,
    uploaded_by: Any | None = None,
) -> dict[str, Any]:
    """Parse and fully replace an uploaded house file for a specific client."""
    rows = _parse_upload(uploaded_file)
    return replace_house_file_rows(rows=rows, client=client, uploaded_by=uploaded_by)


def _normalise_postcode(postcode: str) -> str:
    """Normalise a UK postcode by uppercasing and removing spaces.

    Args:
        postcode: Raw postcode string.

    Returns:
        Uppercase postcode with all whitespace removed.
    """
    return postcode.upper().replace(" ", "")
