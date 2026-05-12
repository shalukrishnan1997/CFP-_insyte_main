import contextlib
import logging
import re
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpRequest
from django.urls import reverse

from donations.models import Donation
from donors.models import Donor

logger = logging.getLogger(__name__)

EMAIL_FONT_STACK = (
    "'IBM Plex Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
)


def parse_email_list(email_string: str) -> list[str]:
    """
    Parse a comma-separated string of emails into a validated list.

    Args:
        email_string: Comma-separated string of email addresses

    Returns:
        List of valid email addresses
    """
    if not email_string:
        return []

    email_regex = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    emails = []

    for email in email_string.split(","):
        email = email.strip().lower()
        if email and email_regex.match(email):
            emails.append(email)

    return emails


def send_hgv_notification(donation: Donation, campaign: object) -> None:
    """
    Send HGV (High Gift Value) notification email to campaign managers.

    Checks if donation amount meets or exceeds campaign's HGV threshold
    and sends notification to all emails in campaign_manager_emails.

    Args:
        donation: Donation object that was just saved
        campaign: Campaign object associated with the donation
    """
    # Check if campaign has HGV threshold configured
    if not campaign.hgv_amount or campaign.hgv_amount <= 0:
        logger.debug(
            f"Campaign {campaign.name} has no HGV threshold set, skipping notification"
        )
        return

    # Check if donation amount meets HGV threshold
    donation_amount = Decimal(str(donation.amount))
    hgv_threshold = Decimal(str(campaign.hgv_amount))

    if donation_amount < hgv_threshold:
        logger.debug(
            f"Donation £{donation_amount} below HGV threshold £{hgv_threshold}"
        )
        return

    # Get and validate campaign manager emails
    recipient_emails = parse_email_list(
        getattr(campaign, "campaign_manager_emails", "") or ""
    )

    if not recipient_emails:
        logger.warning(
            f"HGV donation detected (£{donation_amount}) but no campaign_manager_emails "
            f"configured for campaign '{campaign.name}'"
        )
        return

    # Prepare email content
    donor = donation.donor
    donor_name = f"{donor.title or ''} {donor.first_name} {donor.last_name}".strip()

    subject = (
        f"🎉 HGV Alert: High Gift Value Donation - £{donation_amount} ({campaign.name})"
    )

    message = f"""
High Gift Value (HGV) Donation Alert
=====================================

A donation has been received that meets or exceeds your HGV threshold.

Campaign: {campaign.name}
Client: {campaign.client.name if campaign.client else "N/A"}

Donation Details:
-----------------
Amount: £{donation_amount:,.2f}
HGV Threshold: £{hgv_threshold:,.2f}
Payment Method: {donation.payment_method}
Gift Aid: {"Yes" if donation.gift_aid else "No"}
Date: {donation.donation_date or donation.created_at.date()}

Donor Information:
------------------
Name: {donor_name}
URN: {donor.urn}
Email: {donor.email or "Not provided"}
Phone: {donor.phone or "Not provided"}
Address: {donor.address_line1 or ""} {donor.address_line2 or ""}
         {donor.city or ""} {donor.county or ""} {donor.postcode or ""}

---
This is an automated notification from the Response Handling System.
"""

    html_message = f"""
<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: {EMAIL_FONT_STACK}; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #10B981, #059669); color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .content {{ background: #f9fafb; padding: 20px; border: 1px solid #e5e7eb; }}
        .amount {{ font-size: 36px; font-weight: bold; color: #059669; }}
        .section {{ background: white; padding: 15px; margin: 15px 0; border-radius: 8px; border: 1px solid #e5e7eb; }}
        .section h3 {{ margin-top: 0; color: #1f2937; border-bottom: 2px solid #10B981; padding-bottom: 8px; }}
        .label {{ font-weight: bold; color: #6b7280; }}
        .footer {{ text-align: center; padding: 20px; color: #9ca3af; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎉 High Gift Value Donation Alert</h1>
        </div>
        <div class="content">
            <p>A donation has been received that meets or exceeds your HGV threshold.</p>

            <div style="text-align: center; margin: 20px 0;">
                <span class="amount">£{donation_amount:,.2f}</span>
                <p style="margin: 5px 0; color: #6b7280;">HGV Threshold: £{hgv_threshold:,.2f}</p>
            </div>

            <div class="section">
                <h3>📊 Campaign</h3>
                <p><span class="label">Campaign:</span> {campaign.name}</p>
                <p><span class="label">Client:</span> {campaign.client.name if campaign.client else "N/A"}</p>
            </div>

            <div class="section">
                <h3>💳 Donation Details</h3>
                <p><span class="label">Amount:</span> £{donation_amount:,.2f}</p>
                <p><span class="label">Payment Method:</span> {donation.payment_method}</p>
                <p><span class="label">Gift Aid:</span> {"Yes ✓" if donation.gift_aid else "No"}</p>
                <p><span class="label">Date:</span> {donation.donation_date or donation.created_at.date()}</p>
            </div>

            <div class="section">
                <h3>👤 Donor Information</h3>
                <p><span class="label">Name:</span> {donor_name}</p>
                <p><span class="label">URN:</span> {donor.urn}</p>
                <p><span class="label">Email:</span> {donor.email or "Not provided"}</p>
                <p><span class="label">Phone:</span> {donor.phone or "Not provided"}</p>
                <p><span class="label">Address:</span> {donor.address_line1 or ""} {donor.address_line2 or ""}<br>
                {donor.city or ""} {donor.county or ""} {donor.postcode or ""}</p>
            </div>
        </div>
        <div class="footer">
            <p>This is an automated notification from the Response Handling System.</p>
        </div>
    </div>
</body>
</html>
"""

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL
            if hasattr(settings, "DEFAULT_FROM_EMAIL")
            else "noreply@responsehandling.com",
            recipient_list=recipient_emails,
            html_message=html_message,
            fail_silently=False,
        )
        recipients_str = ", ".join(recipient_emails)
        logger.info(
            f"HGV notification sent to {recipients_str} for "
            f"donation £{donation_amount} on campaign '{campaign.name}'"
        )
    except Exception as e:
        recipients_str = ", ".join(recipient_emails)
        logger.error("Failed to send HGV notification to %s: %s", recipients_str, e)


def _resolve_donor_display_name(donation: Donation) -> str:
    """Return the best human-readable donor name for *donation*.

    Mirrors the priority chain used by ``_build_rejected_records`` in
    ``qa_review.py`` (system_donor → donor → data_file_donor) so the email
    matches what the QA dashboard shows.
    """
    candidates = (
        donation.system_donor,
        donation.donor,
        donation.data_file_donor,
    )
    for source in candidates:
        if source is None:
            continue
        name = f"{source.first_name or ''} {source.last_name or ''}".strip()
        if name:
            return name
    return "Unknown"


def send_rejection_notification(
    donation: Donation, *, request: HttpRequest | None = None
) -> None:
    """Email the configured ops inbox about a QA-rejected donation.

    Skips silently when ``settings.OPERATIONS_REJECT_EMAIL`` is empty. Send
    failures are logged and swallowed — the rejection itself must commit
    even if delivery fails. Comma-separated recipient values are supported
    via :func:`parse_email_list`.

    Args:
        donation: The donation that has just transitioned to ``rejected``.
        request: Optional request used to build an absolute admin URL for
            the donation. Falls back to a relative URL when omitted.
    """
    recipient_emails = parse_email_list(settings.OPERATIONS_REJECT_EMAIL or "")
    if not recipient_emails:
        return

    # Imported lazily: qa_utils imports from core/donations/responsehandling
    # only, so there's no cycle today, but the QA stack lives upstream of
    # this module conceptually — keeping the import local stops a future
    # refactor accidentally creating one.
    from custom_admin.views.qa_utils import get_donation_display_urn

    campaign = donation.campaign
    campaign_name = getattr(campaign, "name", "Unknown campaign")
    client_name = getattr(getattr(campaign, "client", None), "name", "Unknown client")

    donor_name = _resolve_donor_display_name(donation)
    donor_urn = get_donation_display_urn(donation) or "no URN"
    reason_label = dict(Donation.QA_REJECT_REASON_CHOICES).get(
        donation.qa_reject_reason, donation.qa_reject_reason or "Not specified"
    )
    notes = (donation.qa_notes or "").strip() or "No additional notes."

    donation_path = reverse(
        "custom_admin:qa_single_donation_review",
        args=[donation.batch_id, donation.id],
    )
    donation_url = (
        request.build_absolute_uri(donation_path)
        if request is not None
        else donation_path
    )

    placeholder = getattr(donation, "scan_placeholder", None)
    scan_url = getattr(placeholder, "image_url", "") if placeholder is not None else ""

    subject = f"[INSYTE] Donation rejected — {donor_name} ({campaign_name})"
    body_lines = [
        "A donation was rejected during QA review and may need follow-up.",
        "",
        f"Donor: {donor_name} (URN: {donor_urn})",
        f"Campaign: {campaign_name}",
        f"Client: {client_name}",
        f"Amount: {donation.currency} {donation.amount}",
        f"Payment method: {donation.payment_method}",
        f"Reject reason: {reason_label}",
        f"Reviewer notes: {notes}",
        "",
        f"Open donation: {donation_url}",
    ]
    if scan_url:
        body_lines.append(f"Scanned form: {scan_url}")
    body_lines += [
        "",
        "---",
        "This is an automated notification from the INSYTE Response Handling System.",
    ]

    recipients_str = ", ".join(recipient_emails)
    try:
        send_mail(
            subject=subject,
            message="\n".join(body_lines),
            from_email=getattr(
                settings, "DEFAULT_FROM_EMAIL", "noreply@responsehandling.com"
            ),
            recipient_list=recipient_emails,
            fail_silently=False,
        )
    except Exception as exc:
        logger.warning(
            "Failed to send QA rejection notification to %s for donation %s: %s",
            recipients_str,
            donation.pk,
            exc,
        )
        return

    logger.info(
        "QA rejection notification sent to %s for donation %s (%s)",
        recipients_str,
        donation.pk,
        reason_label,
    )


def parse_donation_entries(request: HttpRequest, campaign: object) -> list[dict]:
    """Parse donation entries from POST data."""
    entries_data = []
    entry_index = 0

    while f"entries[{entry_index}][urn]" in request.POST:
        entry = {
            "urn": request.POST.get(f"entries[{entry_index}][urn]", "").strip(),
            "title": request.POST.get(f"entries[{entry_index}][title]", "").strip(),
            "first_name": request.POST.get(
                f"entries[{entry_index}][first_name]", ""
            ).strip(),
            "last_name": request.POST.get(
                f"entries[{entry_index}][last_name]", ""
            ).strip(),
            "email": request.POST.get(f"entries[{entry_index}][email]", "").strip(),
            "phone": request.POST.get(f"entries[{entry_index}][phone]", "").strip(),
            "address_line1": request.POST.get(
                f"entries[{entry_index}][address_line1]", ""
            ).strip(),
            "address_line2": request.POST.get(
                f"entries[{entry_index}][address_line2]", ""
            ).strip(),
            "city": request.POST.get(f"entries[{entry_index}][city]", "").strip(),
            "county": request.POST.get(f"entries[{entry_index}][county]", "").strip(),
            "postcode": request.POST.get(
                f"entries[{entry_index}][postcode]", ""
            ).strip(),
            "consent_contact": request.POST.get(
                f"entries[{entry_index}][consent_contact]"
            )
            == "1",
            "opt_in_email": request.POST.get(f"entries[{entry_index}][opt_in_email]")
            == "1",
            "opt_in_sms": request.POST.get(f"entries[{entry_index}][opt_in_sms]")
            == "1",
            "opt_in_phone": request.POST.get(f"entries[{entry_index}][opt_in_phone]")
            == "1",
            "opt_in_post": request.POST.get(f"entries[{entry_index}][opt_in_post]")
            == "1",
            "amount": request.POST.get(f"entries[{entry_index}][amount]", "").strip(),
            "currency": request.POST.get(
                f"entries[{entry_index}][currency]", "GBP"
            ).strip(),
            "payment_method": request.POST.get(
                f"entries[{entry_index}][payment_method]", "card"
            ).strip(),
            "donation_date": request.POST.get(
                f"entries[{entry_index}][donation_date]", ""
            ).strip(),
            "cheque_number": request.POST.get(
                f"entries[{entry_index}][cheque_number]", ""
            ).strip(),
            "cheque_date": request.POST.get(
                f"entries[{entry_index}][cheque_date]", ""
            ).strip(),
            "gift_aid": request.POST.get(f"entries[{entry_index}][gift_aid]") == "1",
            "donation_frequency": request.POST.get(
                f"entries[{entry_index}][donation_frequency]", ""
            ).strip(),
            # Extra fields for capture/edit
            "card_holder_name": request.POST.get(
                f"entries[{entry_index}][card_holder_name]", ""
            ).strip(),
            "card_last_four": request.POST.get(
                f"entries[{entry_index}][card_last_four]", ""
            ).strip(),
            "card_expiry_date": request.POST.get(
                f"entries[{entry_index}][card_expiry_date]", ""
            ).strip(),
            "direct_debit_start_date": request.POST.get(
                f"entries[{entry_index}][direct_debit_start_date]", ""
            ).strip(),
            "direct_debit_end_date": request.POST.get(
                f"entries[{entry_index}][direct_debit_end_date]", ""
            ).strip(),
            "caf_voucher_number": request.POST.get(
                f"entries[{entry_index}][caf_voucher_number]", ""
            ).strip(),
            "caf_donor_name": request.POST.get(
                f"entries[{entry_index}][caf_donor_name]", ""
            ).strip(),
            "caf_amount": request.POST.get(
                f"entries[{entry_index}][caf_amount]", ""
            ).strip(),
            "postal_order_number": request.POST.get(
                f"entries[{entry_index}][postal_order_number]", ""
            ).strip(),
            "postal_order_date": request.POST.get(
                f"entries[{entry_index}][postal_order_date]", ""
            ).strip(),
            "postal_issuer": request.POST.get(
                f"entries[{entry_index}][postal_issuer]", ""
            ).strip(),
            "package_codes": request.POST.get(
                f"entries[{entry_index}][package_codes]", ""
            ).strip(),
            # Per-donation donor source and new-donor tracking
            "donor_source": request.POST.get(
                f"entries[{entry_index}][donor_source]", ""
            ).strip(),
            "data_file_donor_id": request.POST.get(
                f"entries[{entry_index}][data_file_donor_id]", ""
            ).strip(),
            "is_new_donor": request.POST.get(
                f"entries[{entry_index}][is_new_donor]", ""
            ).strip()
            == "true",
            "custom_fields": {},
        }

        custom_fields: dict[str, str] = {}

        for field in campaign.fields.all():
            field_key = f"entries[{entry_index}][field_{field.id}]"
            if field_key in request.POST:
                custom_fields[str(field.id)] = request.POST.get(field_key, "").strip()

        entry["custom_fields"] = custom_fields

        if (
            entry["urn"]
            and entry["first_name"]
            and entry["last_name"]
            and entry["amount"]
        ):
            entries_data.append(entry)

        entry_index += 1

    return entries_data


def _normalize_date(date_str: str) -> str | None:
    """Convert DD/MM/YYYY or ISO date string to ISO format.

    Returns None if parsing fails.
    """
    if not date_str:
        return None
    try:
        if "/" in date_str:
            day, month, year = date_str.split("/")
            return f"{year}-{month}-{day}"
        return date_str
    except Exception:
        return None


def _set_date_field(donation: Donation, attr: str, raw: str) -> None:
    """Set a date field on the donation if the raw value parses."""
    parsed = _normalize_date(raw)
    if parsed:
        setattr(donation, attr, parsed)


_DONOR_STR_FIELDS = (
    "title",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "city",
    "county",
    "postcode",
)
_DONOR_BOOL_FIELDS = (
    "consent_contact",
    "opt_in_email",
    "opt_in_sms",
    "opt_in_phone",
    "opt_in_post",
)


def _build_donor_defaults(entry_data: dict) -> dict[str, object]:
    """Build the defaults dict for Donor.objects.update_or_create."""
    defaults: dict[str, object] = {
        "first_name": entry_data["first_name"],
        "last_name": entry_data["last_name"],
    }
    for field in _DONOR_STR_FIELDS:
        defaults[field] = entry_data.get(field) or ""
    for field in _DONOR_BOOL_FIELDS:
        defaults[field] = entry_data.get(field, False)
    return defaults


def _resolve_data_file_donor(entry_data: dict, campaign: object) -> object | None:
    """Resolve a DataFileDonor record for data-file campaigns."""
    try:
        from campaigns.models import CampaignDataFile
        from donors.models import DataFileDonor

        campaign_client = getattr(campaign, "client", None)
        df_donor_id = entry_data.get("data_file_donor_id")
        if df_donor_id:
            return DataFileDonor.objects.filter(
                id=df_donor_id,
                client=campaign_client,
            ).first()
        data_file = CampaignDataFile.objects.get(campaign=campaign)
        return DataFileDonor.objects.filter(
            data_file=data_file,
            client=campaign_client,
            urn=entry_data["urn"],
        ).first()
    except Exception:
        return None


def _set_card_fields(donation: Donation, entry_data: dict) -> None:
    """Set card / direct-debit card fields on the donation."""
    donation.card_holder_name = entry_data.get("card_holder_name") or ""
    raw_card = (entry_data.get("card_last_four") or "").replace(" ", "")
    donation.card_last_four = raw_card[-4:] if raw_card else ""
    donation.card_expiry_date = entry_data.get("card_expiry_date") or ""


def _set_direct_debit_dates(donation: Donation, entry_data: dict) -> None:
    """Set direct-debit start/end dates on the donation."""
    _set_date_field(
        donation,
        "direct_debit_start_date",
        entry_data.get("direct_debit_start_date", ""),
    )
    _set_date_field(
        donation,
        "direct_debit_end_date",
        entry_data.get("direct_debit_end_date", ""),
    )


def _set_cheque_fields(donation: Donation, entry_data: dict) -> None:
    """Set cheque fields on the donation."""
    donation.cheque_number = entry_data.get("cheque_number") or ""
    _set_date_field(donation, "cheque_date", entry_data.get("cheque_date", ""))


def _set_caf_fields(donation: Donation, entry_data: dict) -> None:
    """Set CAF voucher fields on the donation."""
    donation.caf_voucher_number = entry_data.get("caf_voucher_number") or ""
    donation.caf_donor_name = entry_data.get("caf_donor_name") or ""
    if entry_data.get("caf_amount"):
        with contextlib.suppress(ValueError, TypeError):
            donation.caf_amount = float(entry_data["caf_amount"])


def _set_postal_fields(donation: Donation, entry_data: dict) -> None:
    """Set postal order fields on the donation."""
    donation.postal_order_number = entry_data.get("postal_order_number") or ""
    donation.postal_issuer = entry_data.get("postal_issuer") or ""
    _set_date_field(
        donation,
        "postal_order_date",
        entry_data.get("postal_order_date", ""),
    )


def _set_non_financial_fields(donation: Donation, entry_data: dict) -> None:
    """Set non-financial donation fields."""
    donation.non_financial_reason = entry_data.get("non_financial_reason") or ""
    donation.non_financial_notes = entry_data.get("non_financial_notes") or ""


_PAYMENT_METHOD_SETTERS: dict[str, list] = {
    "card": [_set_card_fields],
    "direct_debit": [_set_card_fields, _set_direct_debit_dates],
    "cheque": [_set_cheque_fields],
    "caf": [_set_caf_fields],
    "postal_order": [_set_postal_fields],
    "non_financial": [_set_non_financial_fields],
}


def _apply_payment_method_fields(donation: Donation, entry_data: dict) -> None:
    """Dispatch payment-method-specific field setters."""
    setters = _PAYMENT_METHOD_SETTERS.get(donation.payment_method, [])
    for setter in setters:
        setter(donation, entry_data)


def _resolve_donor_for_entry(entry_data: dict, campaign: object) -> tuple:
    """Resolve or create the Donor and optional DataFileDonor for an entry.

    Returns:
        (donor, data_file_donor, donor_source) tuple.
    """
    donor_source = entry_data.get("donor_source") or getattr(
        campaign, "donor_source", "house_file"
    )
    is_new_donor = entry_data.get("is_new_donor", False)

    donor_defaults = _build_donor_defaults(entry_data)
    campaign_client = getattr(campaign, "client", None)
    donor_defaults["client"] = campaign_client

    if is_new_donor:
        campaign_source = getattr(campaign, "donor_source", "house_file")
        status = (
            Donor.VERIFICATION_UNVERIFIED
            if campaign_source == "data_file"
            else Donor.VERIFICATION_PENDING_EXPORT
        )
        donor_defaults["verification_status"] = status

    donor, _created = Donor.objects.update_or_create(
        urn=entry_data["urn"],
        client=campaign_client,
        defaults=donor_defaults,
    )

    data_file_donor = (
        _resolve_data_file_donor(entry_data, campaign)
        if donor_source == "data_file"
        else None
    )
    return donor, data_file_donor, donor_source


def _link_donation_package_codes(donation: Donation, codes_str: str) -> None:
    """Parse and link package code IDs to a donation."""
    if not codes_str:
        return
    code_ids = [int(pc_id.strip()) for pc_id in codes_str.split(",") if pc_id.strip()]
    if code_ids:
        donation.package_codes.set(code_ids)


def save_donation_entry(
    entry_data: dict,
    campaign: object,
    user: object,
    batch: object | None = None,
) -> Donation:
    """Save a single donation entry.

    Args:
        entry_data: Dictionary of form field values for the donation.
        campaign: The Campaign instance this donation belongs to.
        user: The User who is entering the donation.
        batch: Optional DonationBatch instance.

    Returns:
        The saved Donation instance.
    """
    donor, data_file_donor, donor_source = _resolve_donor_for_entry(
        entry_data,
        campaign,
    )

    donation = Donation(
        campaign=campaign,
        batch=batch,
        donor=donor,
        donor_source=donor_source,
        data_file_donor=data_file_donor,
        amount=Decimal(str(entry_data["amount"]))
        if entry_data["amount"]
        else Decimal("0"),
        currency=entry_data.get("currency", "GBP"),
        payment_method=entry_data.get("payment_method", "card"),
        gift_aid=entry_data.get("gift_aid", False),
        donation_frequency=entry_data.get("donation_frequency") or "",
        filled_by=user,
    )

    if batch:
        donation.currency = batch.default_currency
        donation.payment_method = batch.default_payment_method

    _set_date_field(donation, "donation_date", entry_data.get("donation_date", ""))
    _apply_payment_method_fields(donation, entry_data)

    if entry_data.get("custom_fields"):
        donation.field_data = entry_data["custom_fields"]

    donation.save()

    try:
        send_hgv_notification(donation, campaign)
    except Exception as e:
        logger.error("Error sending HGV notification: %s", e)

    _link_donation_package_codes(donation, entry_data.get("package_codes", ""))

    return donation
