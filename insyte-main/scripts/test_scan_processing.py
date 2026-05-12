"""End-to-end test: scan processing for ABC1 donation PDFs.

Usage — scan from R2 prefix:
    python scripts/test_scan_processing.py [--payment-method postal_order]
                                           [--prefix ScanOutput/ABC1/]
                                           [--limit 9]

Usage — scan from local directory (PDFs are uploaded to R2 then processed):
    python scripts/test_scan_processing.py --local-dir .
                                           --payment-method postal_order

Tests the full pipeline:
  (Local PDF | R2 PDF) → Document AI OCR → field extraction → donor matching
"""

import argparse
import glob
import os
import sys

# ── Bootstrap Django ──────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "responsehandling.settings.development")

import django

django.setup()

# ── Now safe to import app code ───────────────────────────────────────────────
from django.conf import settings  # noqa: E402

from core.storage_backends import get_r2_client, r2_enabled  # noqa: E402
from scans.models import ScanPlaceholder  # noqa: E402
from scans.scan_processing import ScanProcessingService  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
SEPARATOR = "─" * 80
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"


def _colour(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}"


def _upload_local_pdfs_to_r2(local_dir: str, r2_prefix: str) -> list[str]:
    """Upload PDFs from a local directory to R2 under *r2_prefix*.

    Skips files already present in R2 (head-object check).

    Args:
        local_dir: Path to local directory containing PDF files.
        r2_prefix: R2 key prefix to upload under (e.g. ``"tests/abc1_local/"``).

    Returns:
        List of R2 keys for uploaded/existing files.
    """
    from django.conf import settings

    pdf_paths = sorted(glob.glob(os.path.join(local_dir, "abc_1_donation_*.pdf")))
    if not pdf_paths:
        # Fall back to any PDF in the dir
        pdf_paths = sorted(glob.glob(os.path.join(local_dir, "*.pdf")))
    if not pdf_paths:
        print(_colour(f"No PDF files found in '{local_dir}'", RED))
        sys.exit(1)

    client = get_r2_client()  # already imported at module level
    bucket = settings.R2_BUCKET_NAME
    r2_keys: list[str] = []

    print(f"\nUploading {len(pdf_paths)} local PDF(s) to R2 prefix '{r2_prefix}' …")
    for path in pdf_paths:
        filename = os.path.basename(path)
        key = f"{r2_prefix.rstrip('/')}/{filename}"
        # Check if already uploaded
        try:
            client.head_object(Bucket=bucket, Key=key)
            print(f"  ↩  {filename} already in R2, skipping upload")
        except Exception:
            with open(path, "rb") as fh:
                data = fh.read()
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType="application/pdf",
            )
            print(f"  ↑  {filename} ({len(data) // 1024} KB) → {key}")
        r2_keys.append(key)

    return r2_keys


def _list_r2_keys(prefix: str, limit: int) -> list[str]:
    """Return R2 object keys under *prefix* (PDF files only)."""
    if not r2_enabled():
        print(_colour("✗ R2 is not configured — check settings!", RED))
        sys.exit(1)

    client = get_r2_client()
    bucket = settings.R2_BUCKET_NAME
    paginator = client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.lower().endswith(".pdf"):
                keys.append(key)
                if len(keys) >= limit:
                    return keys
    return keys


def _find_campaign(campaign_code: str = "APPEALABC") -> object:
    """Return the ABC1 campaign."""
    from campaigns.models import Campaign

    try:
        return Campaign.objects.select_related("client").get(appeal_code=campaign_code)
    except Campaign.DoesNotExist:
        raise SystemExit(f"Campaign with code '{campaign_code}' not found.") from None


def _print_scan_report(placeholder: ScanPlaceholder, index: int) -> None:
    """Print a detailed field-by-field extraction report for one scan."""
    extracted: dict = placeholder.extracted_data or {}

    status_colour = {
        ScanPlaceholder.OCR_STATUS_MATCHED: GREEN,
        ScanPlaceholder.OCR_STATUS_UNMATCHED: YELLOW,
        ScanPlaceholder.OCR_STATUS_FAILED: RED,
        ScanPlaceholder.OCR_STATUS_PROCESSING: CYAN,
    }.get(placeholder.ocr_status, RESET)

    donor = placeholder.matched_donor
    data_file_donor = placeholder.matched_data_file_donor

    print(f"\n{SEPARATOR}")
    print(
        _colour(f"  Scan #{index}  [{placeholder.urn or 'URN NOT DETECTED'}]", BOLD)
        + f"  status={_colour(placeholder.ocr_status.upper(), status_colour)}"
        + f"  qr={_colour('YES', GREEN) if placeholder.qr_decoded else _colour('NO', YELLOW)}"
    )
    print(f"  file: {placeholder.image_path}")
    print(f"  OCR confidence: {placeholder.ocr_confidence:.0%}")
    if placeholder.processing_error:
        print(_colour(f"  ⚠  {placeholder.processing_error}", YELLOW))
    print()

    # ── Identity fields ───────────────────────────────────────────────────────
    _field("donor_name", extracted, "Donor Name")
    _field("title", extracted, "Title")
    _field("address_line1", extracted, "Address Line 1")
    _field("address_line2", extracted, "Address Line 2")
    _field("city", extracted, "City")
    _field("postcode", extracted, "Postcode")
    _field("phone", extracted, "Phone")
    _field("email", extracted, "Email")

    # ── Donation fields ───────────────────────────────────────────────────────
    print()
    _field("amount", extracted, "Amount (£)")
    _field("gift_aid", extracted, "Gift Aid")
    _field("donation_date", extracted, "Donation Date")
    _field("payment_method", extracted, "Payment Method")

    # ── Payment-specific fields ────────────────────────────────────────────────
    pm = str(extracted.get("payment_method", "")).lower()
    if pm in ("cheque", "check"):
        _field("cheque_number", extracted, "Cheque Number")
        _field("cheque_date", extracted, "Cheque Date")
    elif pm == "direct_debit":
        _field("account_name", extracted, "Account Name")
        _field("account_number", extracted, "Account Number")
        _field("sort_code", extracted, "Sort Code")
        _field("dd_reference", extracted, "DD Reference")
    elif pm in ("card", "credit_card", "debit_card"):
        _field("card_holder_name", extracted, "Card Holder")
        _field("card_last_four", extracted, "Card Last 4")
        _field("card_expiry_date", extracted, "Card Expiry")
    elif pm == "caf":
        _field("caf_voucher_number", extracted, "CAF Voucher #")
        _field("caf_amount", extracted, "CAF Amount")
    elif pm in ("postal_order",):
        _field("postal_order_number", extracted, "PO Number")

    # ── Consent flags ─────────────────────────────────────────────────────────
    print()
    _field("contact_consent", extracted, "Contact Consent")
    _field("email_consent", extracted, "Email Consent")
    _field("sms_consent", extracted, "SMS Consent")
    _field("phone_consent", extracted, "Phone Consent")
    _field("post_consent", extracted, "Post Consent")

    # ── QR / identifiers ──────────────────────────────────────────────────────
    print()
    _field("urn", extracted, "URN")
    _field("package_code", extracted, "Package Code")
    _field("appeal_code", extracted, "Appeal Code")

    # ── Matched donor ─────────────────────────────────────────────────────────
    print()
    if donor:
        print(
            f"  {'Matched Donor':<22} {_colour(donor.full_name or '(no name)', GREEN)}  [{donor.pk}]"
        )
        print(f"  {'  → postcode':<22} {donor.postcode or '—'}")
        print(f"  {'  → URN':<22} {donor.urn or '—'}")
    elif data_file_donor:
        print(
            f"  {'Data-File Donor':<22} {_colour(data_file_donor.name or '(no name)', CYAN)}"
        )
    else:
        print(f"  {'Matched Donor':<22} {_colour('none — new donor created', YELLOW)}")

    # ── Raw extracted fields not shown above ──────────────────────────────────
    _SHOWN = {
        "donor_name",
        "title",
        "address_line1",
        "address_line2",
        "city",
        "postcode",
        "phone",
        "email",
        "amount",
        "gift_aid",
        "donation_date",
        "payment_method",
        "cheque_number",
        "cheque_date",
        "account_name",
        "account_number",
        "sort_code",
        "dd_reference",
        "card_holder_name",
        "card_last_four",
        "card_expiry_date",
        "caf_voucher_number",
        "caf_amount",
        "postal_order_number",
        "contact_consent",
        "email_consent",
        "sms_consent",
        "phone_consent",
        "post_consent",
        "urn",
        "package_code",
        "appeal_code",
        "confidence",
        # confidence keys inside extracted
        "donor_name_confidence",
        "amount_confidence",
        "urn_confidence",
        "postcode_confidence",
        "gift_aid_confidence",
    }
    extras = {
        k: v
        for k, v in extracted.items()
        if k not in _SHOWN and v not in (None, "", {})
    }
    if extras:
        print()
        print("  Additional extracted fields:")
        for k, v in sorted(extras.items()):
            print(f"    {k:<30} {v!r}")


def _field(key: str, extracted: dict, label: str) -> None:
    val = extracted.get(key)
    conf_key = f"{key}_confidence"
    conf = extracted.get(conf_key)
    if val in (None, "", {}, []):
        print(f"  {label:<22} {_colour('(empty)', YELLOW)}")
    else:
        conf_str = f"  [{conf:.0%}]" if isinstance(conf, (float, int)) else ""
        val_str = str(val)[:80]
        print(f"  {label:<22} {_colour(val_str, GREEN)}{conf_str}")


def run(
    r2_prefix: str,
    payment_method: str,
    limit: int,
    campaign_code: str,
    local_dir: str = "",
    form_type_override: str = "",
) -> None:
    """Run the end-to-end scan test."""
    print(f"\n{SEPARATOR}")
    print(_colour("  INSYTE DMS — Donation Form Scan Test", BOLD))
    if local_dir:
        print(f"  Local dir : {os.path.abspath(local_dir)}")
    else:
        print(f"  R2 prefix : {r2_prefix}")
    print(f"  Payment   : {payment_method}")
    print(f"  Limit     : {limit} files")
    print(f"  Campaign  : {campaign_code}")
    print(SEPARATOR)

    # 1. Obtain R2 keys — either from a local upload or by listing R2
    if local_dir:
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
        upload_prefix = f"tests/abc1_local/{ts}"
        r2_keys = _upload_local_pdfs_to_r2(local_dir, upload_prefix)[:limit]
        r2_prefix = upload_prefix  # used only for display further down
    else:
        print(f"\nListing R2 objects under '{r2_prefix}' …")
        r2_keys = _list_r2_keys(r2_prefix, limit)

    if not r2_keys:
        print(_colour("No PDFs found", RED))
        return
    print(f"Found {len(r2_keys)} PDF(s):")
    for k in r2_keys:
        print(f"  • {k}")

    # 2. Find campaign
    campaign = _find_campaign(campaign_code)
    from scans.models import ScanBatch

    print(f"\nCampaign    : {campaign.name} [{campaign.id}]")
    print(f"Client      : {campaign.client.name}")
    print(
        f"Doc AI proc : {campaign.client.document_ai_processor_id or _colour('NOT CONFIGURED', RED)}"
    )
    allowed_form_types = ScanBatch.allowed_scan_form_type_choices_for_payment_method(
        payment_method
    )
    default_form_type = allowed_form_types[0][0] if allowed_form_types else "simplex"
    selected_form_type = form_type_override or default_form_type

    if form_type_override and form_type_override != default_form_type:
        print(
            _colour(
                f"Form Layout : overriding '{default_form_type}' → '{form_type_override}' "
                f"(batch-only, DB unchanged)",
                YELLOW,
            )
        )
    ppd = ScanBatch.scan_form_type_pages_per_donor(selected_form_type)
    ppd_label = f"{ppd} pages/donor" if ppd else "— (Legacy Mixed Mail)"
    print(f"Form Layout : {selected_form_type} · {ppd_label}")
    print(f"Scan Purpose: {campaign.get_scan_purpose_display()}")

    # 3. Create scan batch
    if ppd and ppd > 1:
        expected_donors = len(r2_keys) // ppd
        remainder = len(r2_keys) % ppd
        print(f"\nCreating scan batch: {len(r2_keys)} files ÷ {ppd} pages/donor …")
        if remainder:
            print(
                _colour(
                    f"  ⚠  {remainder} orphan page(s) will form an incomplete final donor group",
                    YELLOW,
                )
            )
        else:
            print(f"  → {expected_donors} donor document(s) ({ppd} pages each)")
    else:
        print(f"\nCreating scan batch with {len(r2_keys)} scans (1 file = 1 donor) …")
    scan_batch = ScanProcessingService.create_scan_batch_from_r2(
        campaign_id=str(campaign.id),
        r2_keys=r2_keys,
        payment_method=payment_method,
        scan_form_type=selected_form_type,
        batch_name=f"TEST-BATCH-{len(r2_keys)}",
    )
    print(f"  Scan batch created: {scan_batch.id}  name={scan_batch.batch_name}")
    print(f"  Placeholder records: {scan_batch.total_scans}")

    # 4. Process each scan
    placeholders = list(
        ScanPlaceholder.objects.filter(batch=scan_batch).order_by("created_at")
    )
    print(
        f"\nProcessing {len(placeholders)} donor document(s) through Document AI OCR ..."
    )
    print("(this may take 10-30 seconds per document)\n")

    results_summary: list[dict] = []
    for i, ph in enumerate(placeholders, start=1):
        page_label = (
            f"{ph.image_path.rsplit('/', 1)[-1]} + {len(ph.page_keys) - 1} more page(s)"
            if ph.page_keys and len(ph.page_keys) > 1
            else ph.image_path.rsplit("/", 1)[-1]
        )
        print(
            f"  [{i}/{len(placeholders)}] Processing {page_label} …", end="", flush=True
        )
        ScanProcessingService.process_single_scan(str(ph.id))
        ph.refresh_from_db()
        status = ph.ocr_status
        symbol = (
            "✓"
            if status == ScanPlaceholder.OCR_STATUS_MATCHED
            else ("~" if status == ScanPlaceholder.OCR_STATUS_UNMATCHED else "✗")
        )
        extracted = ph.extracted_data or {}
        print(
            f"  {symbol} {status.upper()}  URN={ph.urn or '?'}  amount=£{extracted.get('amount', '?')}  gift_aid={extracted.get('gift_aid', '?')}"
        )
        results_summary.append(
            {
                "index": i,
                "file": ph.image_path.rsplit("/", 1)[-1],
                "urn": ph.urn or "",
                "status": status,
                "amount": extracted.get("amount", ""),
                "gift_aid": extracted.get("gift_aid", ""),
                "donor_name": extracted.get("donor_name", ""),
                "postcode": extracted.get("postcode", ""),
                "payment_method": extracted.get("payment_method", ""),
                "qr": ph.qr_decoded,
            }
        )

    # 5. Detailed report for each scan
    print(f"\n{SEPARATOR}")
    print(_colour("  DETAILED EXTRACTION REPORT", BOLD))

    placeholders = list(
        ScanPlaceholder.objects.select_related(
            "matched_donor", "matched_data_file_donor"
        )
        .filter(batch=scan_batch)
        .order_by("created_at")
    )
    for i, ph in enumerate(placeholders, start=1):
        _print_scan_report(ph, i)

    # 6. Summary table
    print(f"\n{SEPARATOR}")
    print(_colour("  SUMMARY TABLE", BOLD))
    print(
        f"\n  {'#':<3} {'File':<25} {'URN':<12} {'Status':<12} {'£':<8} {'GA':<5} {'Donor':<25} {'Postcode':<10}"
    )
    print(
        f"  {'─' * 3} {'─' * 25} {'─' * 12} {'─' * 12} {'─' * 8} {'─' * 5} {'─' * 25} {'─' * 10}"
    )
    matched = unmatched = failed = 0
    for r in results_summary:
        st = r["status"]
        if st == ScanPlaceholder.OCR_STATUS_MATCHED:
            sc = GREEN
            matched += 1
        elif st == ScanPlaceholder.OCR_STATUS_UNMATCHED:
            sc = YELLOW
            unmatched += 1
        else:
            sc = RED
            failed += 1
        print(
            f"  {r['index']:<3} {r['file']:<25} {r['urn'] or '—':<12} "
            f"{_colour(st[:11], sc):<22} {str(r['amount'])[:7]:<8} {str(r['gift_aid'])[:5]:<5} "
            f"{str(r['donor_name'])[:24]:<25} {r['postcode']:<10}"
        )

    print(
        f"\n  Total: {len(results_summary)}  "
        f"{_colour(f'Matched: {matched}', GREEN)}  "
        f"{_colour(f'Unmatched: {unmatched}', YELLOW)}  "
        f"{_colour(f'Failed: {failed}', RED)}"
    )

    # 7. Check for empty fields
    empty_fields: dict[str, int] = {}
    total_fields = {
        "donor_name",
        "postcode",
        "amount",
        "gift_aid",
        "payment_method",
    }
    placeholders_fresh = ScanPlaceholder.objects.filter(batch=scan_batch)
    for ph in placeholders_fresh:
        ed = ph.extracted_data or {}
        for f in total_fields:
            if not ed.get(f):
                empty_fields[f] = empty_fields.get(f, 0) + 1

    if empty_fields:
        print(f"\n  {_colour('Fields with missing values:', YELLOW)}")
        for f, count in sorted(empty_fields.items(), key=lambda x: -x[1]):
            pct = count / len(results_summary) * 100
            print(
                f"    {f:<25}  missing in {count}/{len(results_summary)} scans ({pct:.0f}%)"
            )
    else:
        print(f"\n  {_colour('✓ All key fields populated in every scan', GREEN)}")

    print(f"\n{SEPARATOR}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test donation form scan processing")
    parser.add_argument(
        "--prefix",
        default="ScanOutput/ABC1/",
        help="R2 key prefix to list PDFs from (default: ScanOutput/ABC1/)",
    )
    parser.add_argument(
        "--payment-method",
        default="postal_order",
        help="Payment method for the scan batch (default: postal_order)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=9,
        help="Max number of PDFs to process (default: 9)",
    )
    parser.add_argument(
        "--campaign-code",
        default="APPEALABC",
        help="Campaign appeal code (default: APPEALABC)",
    )
    parser.add_argument(
        "--local-dir",
        default="",
        metavar="DIR",
        help="Local directory of PDFs to upload to R2 then test (overrides --prefix)",
    )
    parser.add_argument(
        "--form-type",
        default="",
        choices=[
            "",
            "simplex",
            "duplex",
            "simplex_with_payment",
            "duplex_with_payment",
        ],
        help=(
            "Override the batch scan_form_type for this test run. "
            "Useful for testing page grouping without editing the intake UI. "
            "E.g. --form-type duplex_with_payment groups files 4-at-a-time."
        ),
    )

    args = parser.parse_args()
    run(
        r2_prefix=args.prefix,
        payment_method=args.payment_method,
        limit=args.limit,
        campaign_code=args.campaign_code,
        local_dir=args.local_dir,
        form_type_override=getattr(args, "form_type", ""),
    )
