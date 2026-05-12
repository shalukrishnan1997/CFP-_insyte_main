"""Letter batch generation tasks.

Donations with qa_status=approved receive the campaign's active thank-you
template; donations with qa_status=rejected receive the active issue template.
Donations in any other QA state (pending, flagged) are excluded from letter
generation.
"""

import contextlib
import os
import tempfile
from copy import deepcopy
from datetime import date
from datetime import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from celery import shared_task
from celery.result import AsyncResult
from celery.utils.log import get_task_logger
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from docx import Document
from docxtpl import DocxTemplate
from jinja2 import Environment

from core.constants import CURRENCY_CODE, CURRENCY_SYMBOL
from core.storage_helpers import local_storage_path, normalize_media_storage_name

if TYPE_CHECKING:
    from donations.models import Donation

logger = get_task_logger(__name__)

DEFAULT_LETTERS_PER_FILE = 100
MAX_LETTERS_PER_FILE = 500
MAX_ERROR_LOG_ENTRIES = 100


# ---------------------------------------------------------------------------
#  Jinja2 filters available inside docxtpl templates
# ---------------------------------------------------------------------------


def _filter_date_format(value: Any, fmt: str = "%d %B %Y") -> str:
    if value is None:
        return ""
    if isinstance(value, (date, dt)):
        return value.strftime(fmt)
    return str(value)


def _filter_currency(value: Any, symbol: str = "£") -> str:
    if value is None:
        return f"{symbol}0.00"
    try:
        return f"{symbol}{float(value):,.2f}"
    except ValueError, TypeError:
        return str(value)


def _filter_to_int(value: Any) -> int:
    try:
        return int(float(value))
    except ValueError, TypeError:
        return 0


def _filter_to_float(value: Any, decimals: int = 2) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except ValueError, TypeError:
        return "0.00"


def get_jinja_env() -> Environment:
    """Return a Jinja2 environment with the custom docxtpl filters."""
    jinja_env = Environment(autoescape=True)
    jinja_env.filters["date_format"] = _filter_date_format
    jinja_env.filters["currency"] = _filter_currency
    jinja_env.filters["to_int"] = _filter_to_int
    jinja_env.filters["to_float"] = _filter_to_float
    return jinja_env


# ---------------------------------------------------------------------------
#  Template context
# ---------------------------------------------------------------------------


def _s(value: Any) -> str:
    return value if value else ""


def _build_donor_fields(donor: Any) -> dict[str, Any]:
    title = _s(donor.title)
    first = _s(donor.first_name)
    last = _s(donor.last_name)
    return {
        "donor_title": title,
        "donor_first_name": first,
        "donor_last_name": last,
        "donor_full_name": f"{title} {first} {last}".strip(),
        "donor_email": _s(donor.email),
        "donor_phone": _s(donor.phone),
        "donor_address_line1": _s(donor.address_line1),
        "donor_address_line2": _s(donor.address_line2),
        "donor_city": _s(donor.city),
        "donor_county": _s(donor.county),
        "donor_postcode": _s(donor.postcode),
        "donor_country": getattr(donor, "country", None) or "United Kingdom",
    }


def _format_amount(amount: Any) -> str:
    return f"{amount:.2f}" if amount else "0.00"


def _build_donation_fields(donation: Donation) -> dict[str, Any]:
    amt = donation.amount
    payment_method = (
        donation.get_payment_method_display()
        if hasattr(donation, "get_payment_method_display")
        else _s(donation.payment_method)
    )
    date_str = (
        donation.donation_date.strftime("%d %B %Y") if donation.donation_date else ""
    )
    return {
        "amount": _format_amount(amt),
        "amount_formatted": f"{CURRENCY_SYMBOL}{_format_amount(amt)}",
        "currency_symbol": (
            CURRENCY_SYMBOL if donation.currency == CURRENCY_CODE else donation.currency
        ),
        "currency": donation.currency or CURRENCY_CODE,
        "payment_method": payment_method,
        "donation_date": date_str,
        "donation_reference": str(donation.id)[:8].upper(),
        "gift_aid": "Yes" if donation.gift_aid else "No",
        "amount_raw": float(amt) if amt else 0.0,
        "donation_date_obj": donation.donation_date,
        "payment_status": donation.payment_status or "pending",
    }


def _obj_attr(obj: Any, attr: str, default: str = "") -> str:
    return getattr(obj, attr, default) if obj else default


def _build_campaign_fields(donation: Donation) -> dict[str, Any]:
    campaign = donation.campaign
    client = campaign.client if campaign else None
    return {
        "campaign_name": _obj_attr(campaign, "name"),
        "campaign_description": _obj_attr(campaign, "description"),
        "appeal_code": _obj_attr(campaign, "appeal_code"),
        "client_name": _obj_attr(client, "name"),
        "client_email": _obj_attr(client, "email"),
        "client_phone": _obj_attr(client, "phone"),
        "client_address_line1": _obj_attr(client, "address_line1"),
        "client_city": _obj_attr(client, "city"),
        "client_postcode": _obj_attr(client, "postal_code"),
    }


def get_donor_context(donation: Donation) -> dict[str, Any]:
    """Build the full template context for one donation."""
    donor = donation.donor if donation.donor else donation.data_file_donor
    if not donor:
        raise ValueError(f"No donor found for donation {donation.id}")

    now = timezone.now()
    reject_reason_label = (
        donation.get_qa_reject_reason_display() if donation.qa_reject_reason else ""
    )
    return {
        **_build_donor_fields(donor),
        **_build_donation_fields(donation),
        **_build_campaign_fields(donation),
        "date": now.strftime("%d %B %Y"),
        "year": now.strftime("%Y"),
        "month": now.strftime("%B"),
        "day": now.strftime("%d"),
        "reject_reason": reject_reason_label,
        "reject_notes": donation.qa_notes or "",
    }


# ---------------------------------------------------------------------------
#  Document generation
# ---------------------------------------------------------------------------


def _append_rendered_body(master_doc: Any, source_doc: Any) -> None:
    """Append the body XML of a rendered letter into the master document."""
    master_doc.add_page_break()
    master_body = master_doc.element.body
    final_section = master_body.sectPr
    for element in source_doc.element.body:
        if element.tag.endswith("}sectPr"):
            continue
        copied_element = deepcopy(element)
        if final_section is not None:
            final_section.addprevious(copied_element)
        else:
            master_body.append(copied_element)


def generate_merged_document(
    template_path: str,
    donations: list[Donation],
    output_path: str,
) -> tuple[int, int, list[str], list[Any]]:
    """Render each donation against the template and merge into one DOCX.

    Returns:
        Tuple of (generated_count, failed_count, error_messages, successful_ids).
    """
    generated = 0
    failed = 0
    errors: list[str] = []
    successful_ids: list[Any] = []
    master_doc: Any | None = None

    jinja_env = get_jinja_env()

    for idx, donation in enumerate(donations):
        try:
            doc = DocxTemplate(template_path)
            doc.render(get_donor_context(donation), jinja_env=jinja_env)

            if idx == 0:
                doc.save(output_path)
                master_doc = Document(output_path)
            else:
                tmp_fd, tmp_path = tempfile.mkstemp(suffix=".docx")
                try:
                    os.close(tmp_fd)
                    doc.save(tmp_path)
                    temp_doc = Document(tmp_path)
                    if master_doc is None:
                        raise ValueError("Master document was not initialized")
                    _append_rendered_body(master_doc, temp_doc)
                    del temp_doc
                finally:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_path)

            generated += 1
            successful_ids.append(donation.id)
        except Exception as exc:
            failed += 1
            error_msg = f"Donation {donation.id}: {exc!s}"
            errors.append(error_msg)
            logger.warning(error_msg)

    if master_doc is None:
        raise ValueError("No letters were generated for the merged document")

    master_doc.save(output_path)
    return generated, failed, errors, successful_ids


# ---------------------------------------------------------------------------
#  Queryset + group splitting
# ---------------------------------------------------------------------------


def _exclude_postally_suppressed_donations(
    donations_qs: QuerySet[Donation],
) -> QuerySet[Donation]:
    """Drop donors flagged deceased, gone-away, bad-address, or suppressed."""
    from donors.models import DataFileDonor, Donor

    suppressed_statuses = [
        Donor.CONTACT_STATUS_GONE_AWAY,
        Donor.CONTACT_STATUS_DECEASED,
        Donor.CONTACT_STATUS_BAD_ADDRESS,
        Donor.CONTACT_STATUS_TEMPORARILY_SUPPRESSED,
        DataFileDonor.CONTACT_STATUS_GONE_AWAY,
        DataFileDonor.CONTACT_STATUS_DECEASED,
        DataFileDonor.CONTACT_STATUS_BAD_ADDRESS,
        DataFileDonor.CONTACT_STATUS_TEMPORARILY_SUPPRESSED,
    ]
    return donations_qs.exclude(
        Q(donor__contact_status__in=suppressed_statuses)
        | Q(data_file_donor__contact_status__in=suppressed_statuses)
    )


def build_letter_generation_queryset(
    campaign: Any,
    donation_filter: str,
    regenerate_mode: bool,
    source_donation_batch_id: int | str | None = None,
) -> QuerySet[Donation]:
    """Return the donations eligible for letter generation.

    Only donations that QA has classified as approved or rejected are returned.
    Pending and flagged donations are excluded — they'll be picked up in a
    later batch once QA moves them.
    """
    from donations.models import Donation

    eligible_statuses = [Donation.QA_STATUS_APPROVED, Donation.QA_STATUS_REJECTED]

    donations_qs = Donation.objects.filter(
        campaign=campaign,
        qa_status__in=eligible_statuses,
    )

    # Block letter generation for donations whose underlying payment is no
    # longer valid (banking reversal, refund, finalized dispute loss) or whose
    # letter has already been voided by the cascade. Applied in both
    # regenerate and non-regenerate modes so a regenerate run can't silently
    # re-issue an invalidated letter.
    donations_qs = donations_qs.exclude(
        Q(
            payment_status__in=[
                Donation.PAYMENT_STATUS_REVERSED,
                Donation.PAYMENT_STATUS_REFUNDED,
                Donation.PAYMENT_STATUS_DISPUTE_LOST,
            ]
        )
        | Q(letter_voided_at__isnull=False)
    )

    if not regenerate_mode:
        donations_qs = donations_qs.filter(letter_status="pending")

    if source_donation_batch_id is not None:
        donations_qs = donations_qs.filter(batch_id=source_donation_batch_id)

    donations_qs = _exclude_postally_suppressed_donations(donations_qs)

    if donation_filter == "only_hgv":
        threshold = (
            campaign.hgv_amount
            if getattr(campaign, "hgv_amount", None)
            else Decimal("1000.00")
        )
        donations_qs = donations_qs.filter(amount__gte=threshold)
    elif donation_filter == "exclude_lgv":
        threshold = (
            campaign.lgv_amount
            if getattr(campaign, "lgv_amount", None)
            else Decimal("50.00")
        )
        donations_qs = donations_qs.exclude(amount__lte=threshold)

    return donations_qs.select_related(
        "donor", "data_file_donor", "campaign", "campaign__client"
    ).order_by("created_at")


def _build_generation_groups(
    donations_qs: QuerySet[Donation],
    template_path: str,
    failure_template_path: str | None,
) -> list[tuple[list[Any], str, str]]:
    """Split donations by qa_status and pair each group with its template.

    Approved donations go to the thank-you template; rejected donations go to
    the issue template. Rejected donations are only included when a failure
    template is provided.

    Returns:
        List of (id_list, template_path, label) tuples.
    """
    from donations.models import Donation

    approved_ids = list(
        donations_qs.filter(qa_status=Donation.QA_STATUS_APPROVED).values_list(
            "id", flat=True
        )
    )
    rejected_ids = list(
        donations_qs.filter(qa_status=Donation.QA_STATUS_REJECTED).values_list(
            "id", flat=True
        )
    )

    groups: list[tuple[list[Any], str, str]] = []
    if approved_ids:
        groups.append((approved_ids, template_path, "THANKS"))
    if rejected_ids and failure_template_path:
        groups.append((rejected_ids, failure_template_path, "ISSUE"))
    return groups


# ---------------------------------------------------------------------------
#  Chunk processing + progress reporting
# ---------------------------------------------------------------------------


def _process_letter_chunk(
    chunk_ids: list[Any],
    group_template_path: str,
    output_dir: Path,
    batch: Any,
    group_label: str,
    file_number: int,
) -> tuple[int, int, list[str], str | None]:
    """Render one merged DOCX for a chunk of donation IDs and save to storage."""
    from donations.models import Donation

    chunk_donations = list(
        Donation.objects.filter(id__in=chunk_ids)
        .select_related("donor", "data_file_donor", "campaign", "campaign__client")
        .order_by("created_at")
    )

    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = (
        f"letters_batch{batch.batch_number}_{group_label}_file{file_number}"
        f"_{timestamp}.docx"
    )
    storage_name = normalize_media_storage_name(str(output_dir / filename)) or filename

    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            output_path = tmp.name

        try:
            generated, failed, errors, successful_ids = generate_merged_document(
                group_template_path, chunk_donations, output_path
            )

            if generated > 0:
                with open(output_path, "rb") as generated_file:
                    default_storage.save(storage_name, File(generated_file))
        finally:
            with contextlib.suppress(OSError):
                os.unlink(output_path)

        failed_ids = [
            donation_id
            for donation_id in chunk_ids
            if donation_id not in successful_ids
        ]

        with transaction.atomic():
            if successful_ids:
                Donation.objects.filter(id__in=successful_ids).update(
                    letter_status="generated",
                    letter_generated_at=timezone.now(),
                    letter_batch=batch,
                )
            if failed_ids:
                Donation.objects.filter(id__in=failed_ids).update(
                    letter_status="failed",
                    letter_batch=batch,
                )

        return generated, failed, errors, storage_name if generated > 0 else None
    except Exception as exc:
        error_msg = f"Chunk {file_number} ({group_label}) failed: {exc!s}"
        logger.error(
            "Failed to process chunk %d (%s): %s", file_number, group_label, exc
        )
        return 0, len(chunk_donations), [error_msg], None


def _update_batch_progress(
    batch: Any,
    task: Any,
    batch_id: str,
    generated_total: int,
    failed_total: int,
    output_files: list[str],
    processed_so_far: int,
    total_to_process: int,
    group_label: str,
) -> None:
    """Persist counters on the batch and report progress to Celery."""
    progress = (
        int((processed_so_far / total_to_process) * 100)
        if total_to_process > 0
        else 100
    )
    batch.generated_count = generated_total
    batch.failed_count = failed_total
    batch.progress_percent = progress
    batch.file_count = len(output_files)
    batch.output_files = output_files
    batch.save(
        update_fields=[
            "generated_count",
            "failed_count",
            "progress_percent",
            "file_count",
            "output_files",
        ]
    )
    task.update_state(
        state="PROGRESS",
        meta={
            "current": processed_so_far,
            "total": total_to_process,
            "percent": progress,
            "files_generated": len(output_files),
        },
    )
    logger.info(
        "Batch %s: %d/%d (%d%%), %d files (%s)",
        batch_id,
        processed_so_far,
        total_to_process,
        progress,
        len(output_files),
        group_label,
    )


def _complete_batch(
    batch: Any,
    generated_total: int,
    failed_total: int,
    output_files: list[str],
    all_errors: list[str],
) -> None:
    """Mark a batch completed and store final counters/errors."""
    batch.status = "completed"
    batch.completed_at = timezone.now()
    batch.progress_percent = 100
    batch.generated_count = generated_total
    batch.failed_count = failed_total
    batch.file_count = len(output_files)
    batch.output_files = output_files
    batch.error_log = all_errors[:MAX_ERROR_LOG_ENTRIES]
    batch.save()


def _finish_empty_batch(batch: Any) -> None:
    """Short-circuit completion for batches with nothing to process."""
    batch.status = "completed"
    batch.completed_at = timezone.now()
    batch.progress_percent = 100
    batch.generated_count = 0
    batch.failed_count = 0
    batch.file_count = 0
    batch.output_files = []
    batch.save()


def _run_letter_generation(
    batch: Any,
    task: Any,
    batch_id: str,
    donations_qs: QuerySet[Donation],
) -> dict[str, Any]:
    """Execute the letter generation loop for a prepared queryset."""
    total_donations = donations_qs.count()
    if total_donations == 0:
        batch.total_letters = 0
        _finish_empty_batch(batch)
        return {
            "success": True,
            "message": "No donations match the selected filters",
            "total": 0,
        }

    output_dir = Path("generated_letters") / str(batch.campaign.id)
    letters_per_file = min(batch.letters_per_file, MAX_LETTERS_PER_FILE)

    with (
        local_storage_path(batch.template.file) as template_path,
        contextlib.ExitStack() as stack,
    ):
        failure_template_path = (
            stack.enter_context(local_storage_path(batch.failure_template.file))
            if batch.failure_template
            else None
        )

        generation_groups = _build_generation_groups(
            donations_qs, template_path, failure_template_path
        )
        total_to_process = sum(len(ids) for ids, _, _ in generation_groups)

        batch.total_letters = total_to_process
        batch.save(update_fields=["total_letters"])

        if total_to_process == 0:
            _finish_empty_batch(batch)
            return {
                "success": True,
                "batch_id": str(batch_id),
                "total_donations": total_donations,
                "generated_count": 0,
                "failed_count": 0,
                "file_count": 0,
                "output_files": [],
                "errors": [],
                "message": "No eligible donations for the configured templates",
            }

        generated_total = 0
        failed_total = 0
        all_errors: list[str] = []
        output_files: list[str] = []
        file_number = 1
        processed_so_far = 0

        for group_ids, group_template_path, group_label in generation_groups:
            for chunk_start in range(0, len(group_ids), letters_per_file):
                chunk_ids = group_ids[chunk_start : chunk_start + letters_per_file]
                gen, fail, errs, out_path = _process_letter_chunk(
                    chunk_ids,
                    group_template_path,
                    output_dir,
                    batch,
                    group_label,
                    file_number,
                )
                generated_total += gen
                failed_total += fail
                all_errors.extend(errs)
                if out_path:
                    output_files.append(out_path)
                    file_number += 1

                processed_so_far += len(chunk_ids)
                _update_batch_progress(
                    batch,
                    task,
                    batch_id,
                    generated_total,
                    failed_total,
                    output_files,
                    processed_so_far,
                    total_to_process,
                    group_label,
                )

        _complete_batch(batch, generated_total, failed_total, output_files, all_errors)
        logger.info(
            "Completed batch %s: %d generated, %d failed, %d files",
            batch_id,
            generated_total,
            failed_total,
            len(output_files),
        )
        return {
            "success": True,
            "batch_id": str(batch_id),
            "total_donations": total_donations,
            "generated_count": generated_total,
            "failed_count": failed_total,
            "file_count": len(output_files),
            "output_files": output_files,
            "errors": all_errors[:10],
        }


# ---------------------------------------------------------------------------
#  Celery tasks
# ---------------------------------------------------------------------------


@shared_task(
    bind=True,
    name="letters.generate_letter_batch_task",
    max_retries=3,
    soft_time_limit=7200,
    time_limit=7500,
    track_started=True,
)
def generate_letter_batch_task(
    self: Any,
    batch_id: str,
    source_donation_batch_id: int | str | None = None,
) -> dict[str, Any]:
    """Generate letters for a LetterBatch with progress tracking."""
    from letters.models import LetterBatch

    try:
        batch = LetterBatch.objects.select_related(
            "campaign", "template", "failure_template", "created_by"
        ).get(id=batch_id)
    except LetterBatch.DoesNotExist:
        logger.error("LetterBatch %s not found", batch_id)
        return {"success": False, "error": "Batch not found"}

    if batch.status == "processing":
        logger.warning("Batch %s is already being processed", batch_id)
        return {"success": False, "error": "Batch is already processing"}

    batch.status = "processing"
    batch.started_at = timezone.now()
    batch.celery_task_id = self.request.id or ""
    batch.save(update_fields=["status", "started_at", "celery_task_id"])

    try:
        donations_qs = build_letter_generation_queryset(
            campaign=batch.campaign,
            donation_filter=batch.donation_filter,
            regenerate_mode=batch.regenerate_mode,
            source_donation_batch_id=source_donation_batch_id,
        )
        return _run_letter_generation(batch, self, batch_id, donations_qs)
    except Exception as exc:
        logger.error("Error in letter batch task %s: %s", batch_id, exc, exc_info=True)
        batch.status = "failed"
        batch.completed_at = timezone.now()
        batch.error_log = [str(exc)]
        batch.save(update_fields=["status", "completed_at", "error_log"])

        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
        return {"success": False, "error": str(exc)}


@shared_task(name="letters.cancel_letter_batch")
def cancel_letter_batch(batch_id: str) -> dict[str, Any]:
    """Revoke a running letter batch task and mark the batch cancelled."""
    from letters.models import LetterBatch

    try:
        batch = LetterBatch.objects.get(id=batch_id)
    except LetterBatch.DoesNotExist:
        return {"success": False, "error": "Batch not found"}

    if batch.status in ("completed", "cancelled"):
        return {"success": False, "error": f"Batch already {batch.status}"}

    if batch.celery_task_id:
        AsyncResult(batch.celery_task_id).revoke(terminate=True)

    batch.status = "cancelled"
    batch.completed_at = timezone.now()
    batch.save(update_fields=["status", "completed_at"])
    return {"success": True, "message": "Batch cancelled"}


@shared_task(name="letters.reset_failed_donations")
def reset_failed_donations(campaign_id: str) -> dict[str, Any]:
    """Reset failed donations so they can be retried in a new batch.

    Skips donations whose underlying payment is no longer valid (reversed /
    refunded / dispute_lost) or whose letter has been voided by the
    banking-reversal cascade — those are no longer legitimate retry
    candidates. ``payment_status="pending"`` (e.g. card awaiting capture)
    rows are still reset, since their letters can legitimately be retried
    once payment lands.
    """
    from donations.models import Donation

    count = (
        Donation.objects.filter(campaign_id=campaign_id, letter_status="failed")
        .exclude(
            Q(
                payment_status__in=[
                    Donation.PAYMENT_STATUS_REVERSED,
                    Donation.PAYMENT_STATUS_REFUNDED,
                    Donation.PAYMENT_STATUS_DISPUTE_LOST,
                ]
            )
            | Q(letter_voided_at__isnull=False)
        )
        .update(
            letter_status="pending",
            letter_generated_at=None,
            letter_batch=None,
        )
    )
    return {"success": True, "reset_count": count}
