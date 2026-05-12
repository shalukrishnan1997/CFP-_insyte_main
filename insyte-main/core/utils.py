"""
Utility functions for core app
"""

import csv
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.http import HttpRequest, QueryDict
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from campaigns.models import DataFileUpload

# Models imported inside functions to avoid circular dependencies

try:
    import openpyxl

    HAS_OPENPYXL = True
except ImportError:
    openpyxl = None  # type: ignore[assignment]
    HAS_OPENPYXL = False


def restore_session_filters(
    request: HttpRequest,
    session_key: str,
    url_name: str,
    filter_keys: tuple[str, ...] | None = None,
    default_params: dict[str, str] | None = None,
) -> Any:
    """Restore saved GET filters from session and redirect, or return None.

    If *filter_keys* are supplied and any are already in ``request.GET``,
    the session is NOT consulted (the user has applied explicit filters).
    If the session has saved filters, redirects to ``url_name`` with them.
    If *default_params* are supplied and nothing else matches, redirects with
    those defaults (useful for pages that want e.g. ``?status=active`` by default).
    Returns ``None`` when no redirect is needed so callers can do::

        if redir := restore_session_filters(request, ...):
            return redir
    """
    if filter_keys and any(k in request.GET for k in filter_keys):
        return None

    if session_key in request.session:
        saved = request.session[session_key]
        qd = QueryDict(mutable=True)
        for k, v in saved.items():
            qd[k] = v
        if qd:
            return redirect(f"{reverse(url_name)}?{qd.urlencode()}")

    if default_params:
        return redirect(f"{reverse(url_name)}?{urlencode(default_params)}")

    return None


def sanitize_error(exc: Exception) -> str:
    """
    Sanitize technical exceptions into user-friendly messages.
    Avoids leaking database details or internal logic.
    """
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.db import IntegrityError
    from rest_framework.exceptions import ValidationError as DRFValidationError

    exc_str = str(exc)

    if isinstance(exc, (DjangoValidationError, DRFValidationError)):
        return exc_str

    if isinstance(exc, IntegrityError):
        if "unique_urn_per_datafile" in exc_str:
            return "A donor with this URN already exists in this campaign data file."
        return (
            "A database integrity error occurred. Please check for duplicate records."
        )

    if "Missing" in exc_str:
        return exc_str

    # Generic catch-all for unexpected technical errors
    logger.error("Internal technical error: %s", exc, exc_info=True)
    return "An unexpected error occurred while processing this record. Please contact support if this persists."


def parse_csv_file(file: UploadedFile) -> list[dict]:
    """
    Parse a pipe-delimited CSV file and return a list of dictionaries.

    Raises:
        ValueError: If the uploaded CSV is not pipe-delimited.
    """
    rows = []
    if hasattr(file, "seek"):
        file.seek(0)
    decoded_file = file.read().decode("utf-8-sig")  # Handle BOM

    csv_reader = csv.DictReader(decoded_file.splitlines(), delimiter="|")
    fieldnames = [field.strip() for field in csv_reader.fieldnames or [] if field]
    if (
        len(fieldnames) == 1
        and "|" not in fieldnames[0]
        and any(separator in fieldnames[0] for separator in [",", ";", "\t"])
    ):
        raise ValueError(
            "Invalid delimiter. Only pipe-delimited CSV files are supported. "
            "Use '|' between columns."
        )

    for row in csv_reader:
        # Clean up whitespace from keys and values
        cleaned_row = {k.strip(): v.strip() if v else "" for k, v in row.items()}
        rows.append(cleaned_row)

    return rows


def parse_excel_file(file: UploadedFile) -> list[dict]:
    """
    Parse an Excel file and return a list of dictionaries
    """
    if not HAS_OPENPYXL:
        raise ImportError(
            "openpyxl is required for Excel file parsing. Install with: uv add openpyxl"
        )

    assert openpyxl is not None  # HAS_OPENPYXL guarantees this
    workbook = openpyxl.load_workbook(file, read_only=True)
    sheet = workbook.active

    if sheet is None:
        raise ValueError("Excel file has no active sheet")

    # Get headers from first row
    headers = []
    for cell in sheet[1]:
        headers.append(cell.value.strip() if cell.value else "")

    # Get data rows
    rows = []
    for row in sheet.iter_rows(min_row=2, values_only=True):
        row_dict = {}
        for idx, value in enumerate(row):
            if idx < len(headers):
                row_dict[headers[idx]] = str(value).strip() if value else ""
        rows.append(row_dict)

    return rows


def process_data_file_upload(upload: DataFileUpload) -> tuple[int, int, list[str]]:
    """
    Process a data file upload and create donor records
    Returns: (successful_count, failed_count, errors)
    """
    from donors.models import DataFileDonor

    upload.status = "processing"
    upload.save()

    error_log = []
    successful_count = 0
    failed_count = 0

    try:
        # Determine file type and parse
        file_name = upload.file.name.lower()

        if file_name.endswith(".csv"):
            rows = parse_csv_file(upload.file)
        else:
            upload.status = "failed"
            upload.error_log = [
                {
                    "error": "Unsupported file format. Please upload a pipe-delimited .csv file."
                }
            ]
            upload.save()
            return (
                0,
                0,
                ["Unsupported file format. Please upload a pipe-delimited .csv file."],
            )

        upload.total_rows = len(rows)
        upload.save()

        # Expected column mappings (case-insensitive)
        column_mapping = {
            "urn": ["urn", "donor_id", "donor_urn", "reference"],
            "title": ["title", "salutation"],
            "first_name": ["first_name", "firstname", "first", "given_name"],
            "last_name": ["last_name", "lastname", "last", "surname", "family_name"],
            "email": ["email", "email_address", "e-mail"],
            "phone": ["phone", "telephone", "phone_number", "mobile", "contact_number"],
            "address_line1": ["address_line1", "address1", "address", "street"],
            "address_line2": ["address_line2", "address2"],
            "city": ["city", "town"],
            "county": ["county", "state", "region"],
            "postcode": ["postcode", "postal_code", "zip", "zipcode"],
            "country": ["country"],
            "package_code": ["package_code", "package", "pkg_code", "pkg"],
        }

        def find_column(row: dict, field_name: str) -> str | None:
            """Find the actual column name in the row"""
            possible_names = column_mapping.get(field_name, [field_name])
            for key in row:
                if key.lower() in [name.lower() for name in possible_names]:
                    return key
            return None

        # Pre-process: Check if required columns exist in the first row
        if rows:
            first_row = rows[0]
            missing_required = []
            for req in ["urn", "first_name", "last_name"]:
                if not find_column(first_row, req):
                    missing_required.append(req.replace("_", " ").title())

            if missing_required:
                error_msg = f"Missing required columns: {', '.join(missing_required)}"
                upload.status = "failed"
                upload.error_log = [{"error": error_msg}]
                upload.save()
                return 0, 0, [error_msg]

        def parse_boolean(value: str) -> bool:
            """Parse boolean values from various formats"""
            if not value:
                return False
            value_lower = value.lower().strip()
            return value_lower in ["true", "yes", "y", "1", "on", "checked"]

        donors_to_create: list[DataFileDonor] = []
        seen_urns: set[str] = set()

        # Validate each row before replacing the existing source rows
        for idx, row in enumerate(rows, start=1):
            row_errors = []
            urn = None
            first_name = ""
            last_name = ""
            try:
                # Extract URN (required)
                urn_col = find_column(row, "urn")
                if not urn_col or not row[urn_col]:
                    row_errors.append("Missing URN")
                else:
                    urn = str(row[urn_col]).strip()

                # Extract Names (required)
                fn_col = find_column(row, "first_name")
                if not fn_col or not row[fn_col]:
                    row_errors.append("Missing First Name")
                else:
                    first_name = str(row[fn_col]).strip()

                ln_col = find_column(row, "last_name")
                if not ln_col or not row[ln_col]:
                    row_errors.append("Missing Last Name")
                else:
                    last_name = str(row[ln_col]).strip()

                if row_errors:
                    failed_count += 1
                    error_log.append(f"Row {idx}: {', '.join(row_errors)}")
                    continue

                normalized_urn = urn.casefold() if urn else ""
                if normalized_urn in seen_urns:
                    failed_count += 1
                    error_log.append(f"Row {idx} ({urn}): Duplicate URN in upload")
                    continue
                seen_urns.add(normalized_urn)

                # Build donor data
                donor_data = {
                    "data_file": upload.data_file,
                    "client": upload.data_file.campaign.client,
                    "urn": urn,
                    "first_name": first_name,
                    "last_name": last_name,
                    "created_by": upload.uploaded_by,
                }

                # Map other optional fields
                fields_to_map = {
                    "title": "title",
                    "email": "email",
                    "phone": "phone",
                    "address_line1": "address_line1",
                    "address_line2": "address_line2",
                    "city": "city",
                    "county": "county",
                    "postcode": "postcode",
                    "country": "country",
                    "package_code": "package_code",
                }

                for field, mapping in fields_to_map.items():
                    col = find_column(row, mapping)
                    if col and row[col]:
                        donor_data[field] = row[col]

                donors_to_create.append(
                    DataFileDonor(
                        **donor_data,
                    )
                )

            except Exception as e:
                failed_count += 1
                sanitized_msg = sanitize_error(e)
                error_log.append(
                    f"Row {idx} ({(urn if urn else 'unknown')!s}): {sanitized_msg}"
                )
                logger.exception("Error processing row %d", idx)

        if error_log:
            upload.successful_imports = 0
            upload.failed_imports = failed_count
            upload.error_log = [
                {"row": i + 1, "error": msg} for i, msg in enumerate(error_log)
            ]
            upload.status = "failed"
            upload.completed_at = timezone.now()
            upload.save()
            return 0, failed_count, error_log

        with transaction.atomic():
            upload.data_file.donors.all().delete()
            DataFileDonor.objects.bulk_create(donors_to_create)
            successful_count = len(donors_to_create)

        # Update upload record
        upload.successful_imports = successful_count
        upload.failed_imports = failed_count
        upload.error_log = [
            {"row": i + 1, "error": msg} for i, msg in enumerate(error_log)
        ]
        upload.status = "completed"
        upload.completed_at = timezone.now()
        upload.save()

        # Update data file donor count
        upload.data_file.total_donors = upload.data_file.donors.count()
        upload.data_file.save(update_fields=["total_donors"])

        # Create system notification
        try:
            from notifications.models import Notification

            if failed_count == 0:
                notif_type = Notification.TYPE_SUCCESS
                notif_title = "✅ Data File Upload Completed"
                notif_message = (
                    f"Successfully imported {successful_count} donors "
                    f"to {upload.data_file.campaign.name}"
                )
            elif successful_count > 0:
                notif_type = Notification.TYPE_WARNING
                notif_title = "⚠ Data File Upload Completed with Errors"
                # Include first few errors in message if possible
                # Ensure we sanitize the error log messages for the notification
                error_summary = ". ".join(
                    [
                        msg
                        if isinstance(msg, str)
                        else msg.get("error", "Unknown error")
                        for msg in error_log[:2]
                    ]
                )
                if len(error_log) > 2:
                    error_summary += "..."
                notif_message = (
                    f"Imported {successful_count} donors, {failed_count} failed "
                    f"for {upload.data_file.campaign.name}. Errors: {error_summary!s}"
                )
            else:
                notif_type = Notification.TYPE_ERROR
                notif_title = "❌ Data File Upload Failed"
                notif_message = (
                    f"Failed to import donors for {upload.data_file.campaign.name}. "
                    f"All {failed_count} rows failed. Error: "
                    f"{error_log[0] if error_log else 'Unknown error'}"
                )

            Notification.objects.create(
                user=upload.uploaded_by,
                title=notif_title,
                message=notif_message,
                notification_type=notif_type,
                related_object_type="DataFileUpload",
                related_object_id=str(upload.id),
                link=f"/admin/campaigns/{upload.data_file.campaign.id}/data-file/",
            )
        except Exception as e:
            # Don't fail the upload if notification creation fails
            logger.exception("Failed to create upload notification: %s", e)

    except Exception as e:
        upload.status = "failed"
        upload.error_log = [{"error": str(e)}]
        upload.save()
        return 0, 0, [str(e)]

    return successful_count, failed_count, error_log


def log_audit(
    user: User,
    action: str,
    model_name: str,
    object_id: str | None = None,
    object_repr: str = "",
    changes: dict[str, Any] | None = None,
    request: HttpRequest | None = None,
    batch_id: str | None = None,
    batch_size: int | None = None,
    summary: str = "",
) -> None:
    """Log an audit entry for tracking CRUD operations.

    Args:
        user: User who performed the action.
        action: Type of operation (CREATE, UPDATE, DELETE, etc.).
        model_name: Name of the model affected.
        object_id: ID of the affected object.
        object_repr: String representation of the object.
        changes: Dictionary of before/after values.
        request: HTTP request object for metadata.
        batch_id: Batch identifier for bulk operations.
        batch_size: Number of records affected in bulk.
        summary: Human-readable summary of the action.
    """
    from audit.models import AuditLog

    ip_address = None
    user_agent = ""

    if request:
        # Get IP address
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip_address = x_forwarded_for.split(",")[0].strip()
        else:
            ip_address = request.META.get("REMOTE_ADDR")

        # Get user agent
        user_agent = request.META.get("HTTP_USER_AGENT", "")[:500]

    normalized_object_id = str(object_id) if object_id else ""
    normalized_batch_id = batch_id or ""

    AuditLog.objects.create(
        user=user,
        action=action,
        model_name=model_name,
        object_id=normalized_object_id,
        object_repr=object_repr[:500] if object_repr else "",
        changes=changes or {},
        ip_address=ip_address,
        user_agent=user_agent,
        batch_id=normalized_batch_id,
        batch_size=batch_size,
        summary=summary[:1000] if summary else "",
    )


def log_bulk_operation(
    user: User,
    action: str,
    model_name: str,
    batch_id: str,
    count: int,
    summary: str = "",
    request: HttpRequest | None = None,
) -> None:
    """Log a bulk operation for efficient audit tracking.

    Args:
        user: User who performed the action.
        action: Type of bulk operation.
        model_name: Name of the model affected.
        batch_id: Unique batch identifier.
        count: Number of records affected.
        summary: Human-readable summary.
        request: HTTP request object for metadata.
    """
    log_audit(
        user=user,
        action=action,
        model_name=model_name,
        batch_id=batch_id,
        batch_size=count,
        summary=summary or f"Bulk {action.lower()} of {count} {model_name} records",
        request=request,
    )
