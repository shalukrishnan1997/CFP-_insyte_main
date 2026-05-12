"""Unified donor import views for house files and campaign data files."""

from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from campaigns.models import Campaign, CampaignDataFile, DataFileUpload
from clients.models import Client
from responsehandling.permissions import is_authenticated_and_is_staff

from .admin_views import replace_house_file_upload


def _selected_client(client_id: str | None) -> Client | None:
    """Return the selected client when the identifier is valid."""
    if not client_id:
        return None
    return Client.objects.filter(pk=client_id).first()


def _selected_campaign(
    campaign_id: str | None, client: Client | None
) -> Campaign | None:
    """Return the selected campaign, scoped to the selected client if present."""
    if not campaign_id:
        return None

    campaigns = Campaign.objects.select_related("client")
    if client is not None:
        campaigns = campaigns.filter(client=client)
    return campaigns.filter(pk=campaign_id).first()


def _build_donor_import_context(
    *,
    import_type: str,
    client: Client | None,
    campaign: Campaign | None,
    house_file_results: dict[str, Any] | None = None,
    house_file_filename: str = "",
) -> dict[str, Any]:
    """Build template context for the donor import page."""
    if campaign is not None and client is None:
        client = campaign.client

    initial_campaigns: list[dict[str, Any]] = []
    if client is not None:
        initial_campaigns = list(
            client.campaigns.order_by("name").values("id", "name", "status")
        )

    data_file = None
    if campaign is not None:
        data_file = CampaignDataFile.objects.filter(campaign=campaign).first()

    recent_data_file_uploads = []
    if campaign is not None and data_file is not None:
        recent_data_file_uploads = list(
            data_file.uploads.select_related("uploaded_by", "data_file__campaign")[:10]
        )
    elif client is not None:
        recent_data_file_uploads = list(
            DataFileUpload.objects.filter(
                data_file__campaign__client=client
            ).select_related("uploaded_by", "data_file__campaign")[:10]
        )

    return {
        "active": "donor_imports",
        "breadcrumbs": [{"name": "Donor Imports", "url": None}],
        "clients": Client.objects.filter(is_active=True).order_by("name"),
        "import_type": import_type,
        "selected_client": client,
        "selected_campaign": campaign,
        "selected_client_id": str(client.id) if client is not None else "",
        "selected_campaign_id": str(campaign.id) if campaign is not None else "",
        "initial_campaigns": initial_campaigns,
        "house_file_donor_count": client.donors.count() if client is not None else 0,
        "pending_house_file_count": (
            client.donors.filter(urn__isnull=True).count() if client is not None else 0
        ),
        "data_file_donor_count": data_file.total_donors if data_file is not None else 0,
        "recent_data_file_uploads": recent_data_file_uploads,
        "house_file_results": house_file_results,
        "house_file_filename": house_file_filename,
    }


@is_authenticated_and_is_staff
def donor_imports(request: HttpRequest) -> HttpResponse:
    """Upload house files or campaign data files from one operational screen."""
    if request.method == "POST":
        import_type = request.POST.get("import_type", "house_file")
        client = _selected_client(request.POST.get("client_id", "").strip())
        campaign = _selected_campaign(
            request.POST.get("campaign_id", "").strip(),
            client,
        )

        if import_type != "house_file":
            query_string = (
                f"?import_type=data_file&client_id={client.id if client else ''}"
            )
            if campaign is not None:
                query_string += f"&campaign_id={campaign.id}"
            return redirect(f"{reverse('custom_admin:donor_imports')}{query_string}")

        if client is None:
            messages.error(
                request, "Please choose a client before uploading a house file."
            )
            return render(
                request,
                "admin/donor_imports.html",
                _build_donor_import_context(
                    import_type=import_type,
                    client=client,
                    campaign=campaign,
                ),
            )

        uploaded = request.FILES.get("house_file")
        if uploaded is None:
            messages.error(request, "Please select a house file to upload.")
            return render(
                request,
                "admin/donor_imports.html",
                _build_donor_import_context(
                    import_type=import_type,
                    client=client,
                    campaign=campaign,
                ),
            )

        try:
            results = replace_house_file_upload(
                uploaded_file=uploaded,
                client=client,
                uploaded_by=request.user,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return render(
                request,
                "admin/donor_imports.html",
                _build_donor_import_context(
                    import_type=import_type,
                    client=client,
                    campaign=campaign,
                ),
            )

        if results["replaced_count"]:
            messages.success(
                request,
                f"Replaced the house file for {client.name} with {results['replaced_count']} donor(s).",
            )

        return render(
            request,
            "admin/donor_imports.html",
            _build_donor_import_context(
                import_type=import_type,
                client=client,
                campaign=campaign,
                house_file_results=results,
                house_file_filename=uploaded.name or "",
            ),
        )

    import_type = request.GET.get("import_type", "house_file")
    client = _selected_client(request.GET.get("client_id", "").strip())
    campaign = _selected_campaign(request.GET.get("campaign_id", "").strip(), client)

    return render(
        request,
        "admin/donor_imports.html",
        _build_donor_import_context(
            import_type=import_type,
            client=client,
            campaign=campaign,
        ),
    )
