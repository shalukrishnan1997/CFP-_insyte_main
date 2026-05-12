"""CSV export helpers for client portal reports."""

from dataclasses import dataclass
from typing import Any

from django.http import HttpResponse

from client_portal.helpers.common import get_client_campaign
from clients.models import Client
from core.constants import CURRENCY_CODE
from donations.models import Donation

from .report_context import PortalReportFilters


@dataclass(frozen=True)
class DonationContactDetails:
    """Donor and address fields written into portal CSV exports."""

    donor_name: str
    donor_urn: str
    address_line1: str
    address_line2: str
    city: str
    county: str
    postcode: str
    country: str
    email: str
    phone: str


PORTAL_DONATION_EXPORT_HEADERS = [
    "Donation Date",
    "Campaign",
    "Donor Name",
    "Donor URN",
    "Address Line 1",
    "Address Line 2",
    "City",
    "County/State",
    "Postcode",
    "Country",
    "Email",
    "Phone",
    "Amount",
    "Currency",
    "Payment Method",
    "Frequency",
    "Gift Aid",
]


def build_export_filename(client: Client, filters: PortalReportFilters) -> str:
    """Return the CSV filename for a portal donation export."""
    filename_parts = [client.name.replace(" ", "_"), "donations_export"]
    campaign = get_client_campaign(client, filters.campaign_id)
    if campaign is not None:
        filename_parts.append(campaign.name.replace(" ", "_"))
    filename_parts.append(f"{filters.date_from}_to_{filters.date_to}")
    return "_".join(filename_parts) + ".csv"


def get_portal_export_donations(
    client: Client,
    filters: PortalReportFilters,
) -> Any:
    """Return the filtered donations queryset used by the CSV export."""
    donations = (
        Donation.objects.select_related("campaign", "donor", "data_file_donor")
        .filter(
            campaign__client=client,
            donation_date__gte=filters.date_from,
            donation_date__lte=filters.date_to,
        )
        .only(
            "donation_date",
            "amount",
            "currency",
            "payment_method",
            "donation_frequency",
            "gift_aid",
            "campaign__name",
            "donor__first_name",
            "donor__last_name",
            "donor__urn",
            "donor__address_line1",
            "donor__address_line2",
            "donor__city",
            "donor__county",
            "donor__postcode",
            "donor__country",
            "donor__email",
            "donor__phone",
            "data_file_donor__first_name",
            "data_file_donor__last_name",
            "data_file_donor__urn",
            "data_file_donor__address_line1",
            "data_file_donor__address_line2",
            "data_file_donor__city",
            "data_file_donor__county",
            "data_file_donor__postcode",
            "data_file_donor__country",
            "data_file_donor__email",
            "data_file_donor__phone",
        )
    )

    if filters.campaign_id:
        donations = donations.filter(campaign_id=filters.campaign_id)

    return donations.order_by("-donation_date")


def build_donation_contact_details(donation: Donation) -> DonationContactDetails:
    """Return donor/contact fields from either donor source."""
    if donation.donor:
        return DonationContactDetails(
            donor_name=f"{donation.donor.first_name} {donation.donor.last_name}".strip(),
            donor_urn=donation.donor.urn or "",
            address_line1=donation.donor.address_line1,
            address_line2=donation.donor.address_line2,
            city=donation.donor.city,
            county=donation.donor.county,
            postcode=donation.donor.postcode,
            country=getattr(donation.donor, "country", "") or "",
            email=donation.donor.email,
            phone=donation.donor.phone,
        )

    if donation.data_file_donor:
        data_file_donor = donation.data_file_donor
        return DonationContactDetails(
            donor_name=f"{data_file_donor.first_name} {data_file_donor.last_name}".strip(),
            donor_urn=data_file_donor.urn or "",
            address_line1=data_file_donor.address_line1,
            address_line2=data_file_donor.address_line2,
            city=data_file_donor.city,
            county=data_file_donor.county,
            postcode=data_file_donor.postcode,
            country=getattr(data_file_donor, "country", "") or "",
            email=data_file_donor.email,
            phone=data_file_donor.phone,
        )

    return DonationContactDetails(
        donor_name="",
        donor_urn="",
        address_line1="",
        address_line2="",
        city="",
        county="",
        postcode="",
        country="",
        email="",
        phone="",
    )


def build_donation_export_row(donation: Donation) -> list[str]:
    """Serialize one donation into the portal CSV export row format."""
    contact = build_donation_contact_details(donation)
    donation_date = (
        donation.donation_date.strftime("%d/%m/%Y") if donation.donation_date else ""
    )
    return [
        donation_date,
        donation.campaign.name if donation.campaign else "",
        contact.donor_name,
        contact.donor_urn,
        contact.address_line1,
        contact.address_line2,
        contact.city,
        contact.county,
        contact.postcode,
        contact.country,
        contact.email,
        contact.phone,
        f"{donation.amount:.2f}",
        donation.currency or CURRENCY_CODE,
        donation.payment_method or "",
        donation.donation_frequency or "",
        "Yes" if donation.gift_aid else "No",
    ]


def build_csv_download_response(filename: str) -> HttpResponse:
    """Return a CSV attachment response with the supplied filename."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _sanitize_csv_value(value: Any) -> str:
    """Escape one CSV cell and guard against spreadsheet formula injection."""
    from custom_admin.views.reports.main import _sanitize_cell

    sanitized = _sanitize_cell(value)
    escaped = sanitized.replace('"', '""')
    if any(char in escaped for char in [",", '"', "\n", "\r"]):
        return f'"{escaped}"'
    return escaped


def append_csv_rows(response: HttpResponse, rows: list[list[Any]]) -> HttpResponse:
    """Serialize a set of CSV rows into the given response."""
    for row in rows:
        response.write(",".join(_sanitize_csv_value(value) for value in row) + "\r\n")
    return response


def build_unmatched_donors_export_response(
    client: Client,
    filters: PortalReportFilters,
) -> HttpResponse:
    """Build the CSV download response for unmatched donor exports."""
    from custom_admin.views.reports.constants import REPORT_TYPES
    from custom_admin.views.reports.generators import _generate_unmatched_donors_report

    report_data = _generate_unmatched_donors_report(
        date_from=filters.date_from,
        date_to=filters.date_to,
        client_id=str(client.id),
    )
    report_config = REPORT_TYPES["unmatched_donors"]
    filename = (
        f"{client.name.replace(' ', '_')}_unmatched_donors"
        f"_{filters.date_from}_to_{filters.date_to}.csv"
    )
    response = build_csv_download_response(filename)
    headers = report_config["headers"][1:]
    return append_csv_rows(response, [headers, *report_data])


def build_donation_export_response(
    client: Client,
    filters: PortalReportFilters,
) -> HttpResponse:
    """Build the CSV download response for the standard donation export."""
    response = build_csv_download_response(build_export_filename(client, filters))
    rows = [
        PORTAL_DONATION_EXPORT_HEADERS,
        *[
            build_donation_export_row(donation)
            for donation in get_portal_export_donations(client, filters)
        ],
    ]
    return append_csv_rows(response, rows)
