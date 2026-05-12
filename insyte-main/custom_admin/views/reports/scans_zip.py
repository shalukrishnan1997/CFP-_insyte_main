"""Per-report 'Download scans (ZIP)' export.

Streams a ZIP archive of the donor scan PDFs that match the same filters
as the existing CSV / Excel report export. Mirrors the filter-parsing
contract of :func:`custom_admin.views.reports.main.report_export` so the
Reports UI can hang the new button off the same querystring.

Safety / scope:
    - Staff-only — wrapped with the ``is_authenticated_and_is_staff``
      decorator so anonymous users hit the login flow and non-staff get
      bounced to the auth page.
    - Only includes scans that are **both** QA-approved
      (``Donation.qa_status == 'approved'``) **and** fully redacted
      (``ScanPlaceholder.redaction_status == 'completed'``). Unredacted
      scans contain PAN/CVV/signatures and must never reach a client
      bundle, especially since Paul's brief on this feature is
      "shared with client".
    - Reports that do not aggregate at donation grain (paying-in-slip,
      ROI, campaign summary) are rejected with 400 — the button is also
      hidden for those report types in the template.

Filename pattern: ``<donation_id>_<urn_or_blank>_scan.pdf``. Each scan is
assembled into a single merged PDF using the same precedence
``ScanPlaceholder`` uses elsewhere: ``page_keys`` if populated, else
``image_path`` as the sole key. Empty filter result falls back to a tiny
ZIP containing a ``README.txt`` so the download contract stays uniform.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from typing import TYPE_CHECKING, Any

from django.http import HttpRequest, HttpResponse

from audit.utils import log_request_action
from donations.models import Donation
from responsehandling.permissions import is_authenticated_and_is_staff
from scans.models import ScanPlaceholder

from .constants import REPORT_TYPES
from .helpers import _get_filtered_donations, _parse_date_param

if TYPE_CHECKING:  # pragma: no cover — typing only
    from django.db.models import QuerySet


logger = logging.getLogger(__name__)


# Reports that don't expose donation-row scans (paying-in slips render at
# slip grain; ROI / campaign summary are aggregates). The "Download scans"
# button is hidden in the template for these types and the view rejects
# them so a hand-crafted querystring can't silently produce an empty ZIP.
SCAN_ZIP_UNSUPPORTED_REPORTS: frozenset[str] = frozenset(
    {"paying_in_slips", "roi", "campaign_summary"}
)

# Hard cap on the number of donor scans included in a single bundle.
# Each entry triggers an R2 download + pypdf merge inside the request
# worker, so an unbounded result set risks a gunicorn timeout or OOM.
# Mirrors the 500-row precedent in ``report_export_pdf``. Operators who
# need a larger bundle should narrow the filters and run multiple jobs.
MAX_SCANS_PER_ZIP = 500


def report_supports_scan_zip(report_type: str) -> bool:
    """Return True when *report_type* exposes per-donation scan downloads.

    Args:
        report_type: One of the keys from :data:`REPORT_TYPES`.

    Returns:
        ``True`` if the report renders donation-row data and the scans-zip
        button should be visible / the endpoint should respond 200.
    """
    return (
        report_type in REPORT_TYPES and report_type not in SCAN_ZIP_UNSUPPORTED_REPORTS
    )


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _zip_entry_name(donation: Donation, placeholder: ScanPlaceholder) -> str:
    """Build the in-archive filename for a donation's scan.

    Donation IDs (UUIDs) and donor URNs are already alphanumeric in normal
    operation; the regex strips anything outside that envelope so a stray
    legacy URN can never inject path separators or control bytes into the
    archive's directory entry.
    """
    donation_id = _FILENAME_SAFE.sub("_", str(donation.id))
    urn = _FILENAME_SAFE.sub("_", placeholder.urn or "").strip("_")
    if urn:
        return f"{donation_id}_{urn}_scan.pdf"
    return f"{donation_id}_scan.pdf"


def _matched_donations_with_scans(
    donations: QuerySet[Donation],
) -> QuerySet[Donation]:
    """Filter *donations* to those eligible for client scan delivery.

    Requires both QA approval (so the donation is part of a finalised
    batch) and redaction completion (so PAN/CVV are guaranteed scrubbed).
    """
    return (
        donations.filter(
            qa_status=Donation.QA_STATUS_APPROVED,
            scan_placeholder__isnull=False,
            scan_placeholder__redaction_status=ScanPlaceholder.REDACTION_COMPLETED,
        )
        .select_related("scan_placeholder")
        .order_by("id")
    )


def _resolve_scan_page_keys(placeholder: ScanPlaceholder) -> list[str]:
    """Return the R2 keys that make up *placeholder*'s donor document.

    Mirrors the precedence used by :func:`scans.api_views.scan_placeholder_pdf`:
    multi-page documents go via ``page_keys``, single-page legacy rows fall
    back to ``image_path``.
    """
    page_keys: list[str] = list(placeholder.page_keys or [])
    if not page_keys and placeholder.image_path:
        page_keys = [placeholder.image_path]
    return page_keys


def _build_pdf_for_keys(
    page_keys: list[str], placeholder: ScanPlaceholder
) -> bytes | None:
    """Return merged PDF bytes for *page_keys* or ``None`` on R2 failure."""
    # Local import avoids a hard top-level dependency on the R2 helpers
    # so the module imports cleanly when R2 is not configured (dev/tests).
    from scans.scan_processing_r2 import build_pdf_bytes_from_r2_keys

    try:
        return build_pdf_bytes_from_r2_keys(page_keys)
    except Exception:
        logger.exception(
            "Failed to build scan PDF for placeholder %s (donation %s)",
            placeholder.id,
            placeholder.donation_id,
        )
        return None


def _empty_readme(report_type: str) -> str:
    """Render the README.txt body explaining why the bundle is empty."""
    return (
        f"No scans matched the filters for the "
        f"'{REPORT_TYPES[report_type]['title']}' report.\n"
        "This usually means none of the donations in the date range have "
        "been QA-approved with a fully redacted scan.\n"
    )


def _truncated_readme(report_type: str, included: int) -> str:
    """Render the README body shown when the bundle hit ``MAX_SCANS_PER_ZIP``."""
    return (
        f"Result truncated — only the first {included} scans for the "
        f"'{REPORT_TYPES[report_type]['title']}' report are included.\n"
        f"The bundle is capped at {MAX_SCANS_PER_ZIP} entries to keep export "
        "downloads inside worker memory and timeout limits. Narrow the date "
        "range or filter by campaign to pull the remaining scans in a "
        "follow-up download.\n"
    )


def _build_zip_bytes(
    donations: QuerySet[Donation],
    report_type: str,
) -> tuple[bytes, int, bool]:
    """Assemble the ZIP body, the number of scans included, and a truncated flag.

    Donations without a usable scan (missing ``page_keys`` / ``image_path``
    or PDF-build failure) are skipped silently. Empty results emit a
    ``README.txt`` so the response is always a valid archive. When the
    matched queryset would exceed :data:`MAX_SCANS_PER_ZIP`, iteration
    stops at the cap and a truncation note is added to the archive so
    operators know to narrow filters.
    """
    matched = _matched_donations_with_scans(donations)
    seen_keys: set[tuple[str, ...]] = set()
    included = 0
    truncated = False

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for donation in matched.iterator(chunk_size=200):
            if included >= MAX_SCANS_PER_ZIP:
                truncated = True
                break

            placeholder = getattr(donation, "scan_placeholder", None)
            if placeholder is None:
                continue

            page_keys = _resolve_scan_page_keys(placeholder)
            if not page_keys:
                continue

            # Legacy rows can reuse the same physical scan — skip duplicates.
            dedupe_key = tuple(page_keys)
            if dedupe_key in seen_keys:
                continue

            pdf_bytes = _build_pdf_for_keys(page_keys, placeholder)
            if pdf_bytes is None:
                continue

            archive.writestr(_zip_entry_name(donation, placeholder), pdf_bytes)
            seen_keys.add(dedupe_key)
            included += 1

        if truncated:
            archive.writestr("README.txt", _truncated_readme(report_type, included))
        elif included == 0:
            archive.writestr("README.txt", _empty_readme(report_type))

    return buffer.getvalue(), included, truncated


@is_authenticated_and_is_staff
def report_scans_zip(request: HttpRequest) -> HttpResponse:
    """Stream a ZIP of donor scans for the filtered donation set.

    Accepts the same querystring shape as
    :func:`custom_admin.views.reports.main.report_export`:
    ``report_type``, ``client``, ``campaign``, ``date_from``, ``date_to``.

    Args:
        request: Authenticated staff HTTP request.

    Returns:
        ``application/zip`` response with ``Content-Disposition: attachment``,
        or HTTP 400 when the report type does not expose donation-row
        scans.
    """
    report_type = request.GET.get("report_type", "donations")
    if not report_supports_scan_zip(report_type):
        return HttpResponse("This report does not support scan downloads.", status=400)

    client_id = request.GET.get("client", "")
    campaign_id = request.GET.get("campaign", "")
    date_from = _parse_date_param(request.GET.get("date_from"), default_offset_days=30)
    date_to = _parse_date_param(request.GET.get("date_to"))

    donations = _get_filtered_donations(date_from, date_to, client_id, campaign_id)
    payload, included, truncated = _build_zip_bytes(donations, report_type)

    config = REPORT_TYPES[report_type]
    filename = f"scans_{report_type}_{date_from}_to_{date_to}.zip"

    response = HttpResponse(payload, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Content-Length"] = str(len(payload))

    _log_scans_zip_export(request, config, included, truncated)
    return response


def _log_scans_zip_export(
    request: HttpRequest,
    config: dict[str, Any],
    included: int,
    truncated: bool,
) -> None:
    """Audit the scans-zip download mirror of ``_log_export``."""
    summary = f"Exported {config['title']} scans bundle ({included} scan files)"
    if truncated:
        summary += f" — truncated at {MAX_SCANS_PER_ZIP} cap"
    log_request_action(
        request,
        action="DOWNLOAD",
        model_name="Report",
        object_repr=f"{config['title']} (Scans ZIP)",
        summary=summary,
    )
