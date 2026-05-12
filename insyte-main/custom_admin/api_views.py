"""API Views for Django templates - Minimal REST API endpoints.

This module provides only the API endpoints actually used by Django templates:
    - CampaignDataFileViewSet: Campaign-specific donor list management
    - DataFileUploadViewSet: File upload and progress tracking
    - LetterBatchViewSet: Letter batch status notifications
    - donor_search: Donor autocomplete search (function-based)
    - campaigns_by_client_api: Campaign filtering by client (function-based)
    - address_lookup_proxy: Server-side proxy for UK address API (function-based)

All other endpoints have been removed as they were not used by templates.
"""

import logging
import mimetypes
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests
from django.conf import settings
from django.db.models import Prefetch, Q
from django.http import FileResponse, HttpRequest, JsonResponse
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.request import Request
from rest_framework.response import Response

from campaigns.models import Campaign, CampaignDataFile, DataFileUpload
from clients.models import Client
from core.services.phone import looks_like_phone_query, normalize_phone
from donations.models import Donation
from donors.models import DataFileDonor, Donor, SystemDonor
from letters.models import LetterBatch
from responsehandling.permissions import (
    HasSystemAccessPermission,
    is_authenticated_and_is_staff,
)

from .serializers import (
    CampaignDataFileSerializer,
    DataFileDonorSerializer,
    DataFileUploadSerializer,
)

logger = logging.getLogger(__name__)


def _start_data_file_upload_processing(upload: DataFileUpload) -> str:
    """Start background processing or fall back to synchronous import.

    Args:
        upload: Uploaded data file awaiting processing.

    Returns:
        A string describing whether processing was queued or run synchronously.
    """
    from core.tasks import process_data_file_upload_task
    from core.utils import process_data_file_upload

    try:
        task = process_data_file_upload_task.delay(str(upload.id))
    except Exception:
        logger.exception("Failed to queue data file upload processing")
        process_data_file_upload(upload)
        return "synchronous"

    upload.error_log = [{"task_id": task.id, "status": "queued"}]
    upload.save(update_fields=["error_log"])
    return "queued"


# ═══════════════════════════════════════════════════════════════════
# Address Lookup Proxy (keeps API key server-side)
# ═══════════════════════════════════════════════════════════════════


def _lookup_getaddress(postcode: str, api_key: str) -> JsonResponse | None:
    """Try UK address lookup via GetAddress.io.

    Args:
        postcode: Cleaned postcode (no spaces).
        api_key: GetAddress.io API key.

    Returns:
        JsonResponse on success, or None on failure.
    """
    try:
        resp = requests.get(
            f"https://api.getaddress.io/find/{postcode}",
            params={"api-key": api_key, "expand": "true"},
            timeout=5,
        )
        if resp.ok:
            data = resp.json()
            addresses = [
                {
                    "line_1": addr.get("line_1", ""),
                    "line_2": addr.get("line_2", ""),
                    "line_3": addr.get("line_3", ""),
                    "locality": addr.get("locality", ""),
                    "town_or_city": addr.get("town_or_city", ""),
                    "county": addr.get("county", ""),
                    "postcode": data.get("postcode", postcode),
                }
                for addr in data.get("addresses", [])
            ]
            return JsonResponse(
                {"success": True, "provider": "getaddress", "addresses": addresses}
            )
    except requests.RequestException:
        logger.warning("GetAddress.io lookup failed, falling back to postcodes.io")
    return None


def _lookup_postcodes_io(postcode: str, original: str) -> JsonResponse | None:
    """Try UK address lookup via free postcodes.io API.

    Args:
        postcode: Cleaned postcode (no spaces).
        original: Original postcode string for fallback display.

    Returns:
        JsonResponse on success, or None on failure.
    """
    try:
        resp = requests.get(
            f"https://api.postcodes.io/postcodes/{postcode}",
            timeout=5,
        )
        if resp.ok:
            result = resp.json().get("result", {})
            return JsonResponse(
                {
                    "success": True,
                    "provider": "postcodes.io",
                    "addresses": [
                        {
                            "line_1": "",
                            "line_2": "",
                            "line_3": "",
                            "locality": result.get("parish", ""),
                            "town_or_city": result.get("admin_ward", ""),
                            "county": result.get("admin_county", "")
                            or result.get("admin_district", ""),
                            "postcode": result.get("postcode", original),
                        }
                    ],
                }
            )
    except requests.RequestException:
        pass
    return None


@is_authenticated_and_is_staff
@require_http_methods(["GET"])
def address_lookup_proxy(request: HttpRequest) -> JsonResponse:
    """Server-side proxy for GetAddress.io UK address lookup.

    Keeps the API key server-side instead of exposing it to the browser.
    Falls back to postcodes.io (free, no key needed) if GetAddress is unavailable.

    Query params:
        postcode: UK postcode to look up (required)

    Returns:
        JSON with addresses array or error.
    """
    postcode = getattr(request, "GET", {}).get("postcode", "").strip()  # type: ignore[union-attr]
    if not postcode or len(postcode) < 3:
        return JsonResponse(
            {"success": False, "error": "Postcode is required (min 3 chars)"},
            status=400,
        )

    clean_postcode = postcode.replace(" ", "")
    api_key = getattr(settings, "GETADDRESS_API_KEY", "")

    if api_key:
        result = _lookup_getaddress(clean_postcode, api_key)
        if result is not None:
            return result

    result = _lookup_postcodes_io(clean_postcode, postcode)
    if result is not None:
        return result

    return JsonResponse(
        {"success": False, "error": "Address lookup unavailable"},
        status=503,
    )


def _iso_or_empty(value: date | datetime | None) -> str:
    """Return ``value.isoformat()`` for date/datetime objects, ``""`` for None."""
    if value is None:
        return ""
    return value.isoformat()


def _serialize_house_donor(donor: Donor) -> dict:
    """Serialize a Donor (house file) instance for the search API.

    Args:
        donor: Donor model instance.

    Returns:
        dict suitable for JSON response.
    """
    return {
        "id": donor.urn,
        "donor_pk": str(donor.id),
        "urn": donor.urn,
        "title": donor.title or "",
        "first_name": donor.first_name,
        "last_name": donor.last_name,
        "full_name": donor.full_name,
        "email": donor.email,
        "phone": donor.phone,
        "address_line1": donor.address_line1,
        "address_line2": donor.address_line2,
        "city": donor.city,
        "county": donor.county,
        "postcode": donor.postcode,
        "country": donor.country,
        "date_of_birth": _iso_or_empty(donor.date_of_birth),
        "age": donor.age,
        "gift_aid_declaration": donor.gift_aid_declaration,
        "gift_aid_date": _iso_or_empty(donor.gift_aid_date),
        "contact_status": donor.contact_status,
        "contact_status_reason": donor.contact_status_reason,
        "contact_status_changed_at": _iso_or_empty(donor.contact_status_changed_at),
        "address_last_verified_at": _iso_or_empty(donor.address_last_verified_at),
        "consent_contact": donor.consent_contact,
        "no_thank_you": False,
        "opt_in_email": donor.opt_in_email,
        "opt_in_sms": donor.opt_in_sms,
        "opt_in_phone": donor.opt_in_phone,
        "opt_in_post": donor.opt_in_post,
        "verification_status": donor.verification_status,
        "source": "house_file",
    }


def _str_or_empty(value: str | None) -> str:
    """Return the value as string, or empty string if falsy.

    Args:
        value: String value or None.

    Returns:
        Non-None string.
    """
    return value or ""


def _serialize_data_file_donor(donor: DataFileDonor) -> dict:
    """Serialize a DataFileDonor instance for the search API.

    Args:
        donor: DataFileDonor model instance.

    Returns:
        dict suitable for JSON response.
    """
    return {
        "id": str(donor.id),
        "data_file_donor_id": str(donor.id),
        "urn": _str_or_empty(donor.urn),
        "title": _str_or_empty(donor.title),
        "first_name": _str_or_empty(donor.first_name),
        "last_name": _str_or_empty(donor.last_name),
        "full_name": donor.full_name,
        "email": _str_or_empty(donor.email),
        "phone": _str_or_empty(donor.phone),
        "address_line1": _str_or_empty(donor.address_line1),
        "address_line2": _str_or_empty(donor.address_line2),
        "city": _str_or_empty(donor.city),
        "county": _str_or_empty(donor.county),
        "postcode": _str_or_empty(donor.postcode),
        "country": _str_or_empty(donor.country),
        "date_of_birth": _iso_or_empty(donor.date_of_birth),
        "age": donor.age,
        "gift_aid_declaration": donor.gift_aid_declaration,
        "gift_aid_date": _iso_or_empty(donor.gift_aid_date),
        "contact_status": donor.contact_status,
        "contact_status_reason": donor.contact_status_reason,
        "contact_status_changed_at": _iso_or_empty(donor.contact_status_changed_at),
        "address_last_verified_at": _iso_or_empty(donor.address_last_verified_at),
        "consent_contact": donor.consent_contact,
        "no_thank_you": donor.no_thank_you,
        "opt_in_email": donor.opt_in_email,
        "opt_in_sms": donor.opt_in_sms,
        "opt_in_phone": donor.opt_in_phone,
        "opt_in_post": donor.opt_in_post,
        "source": "data_file",
    }


def _donor_search_query(query: str) -> Q:
    """Build a Q filter for donor search by URN, name, email, or postcode.

    Used against ``Donor`` (house file) and ``DataFileDonor`` (campaign data
    file). Phone search against these models is intentionally not included
    here — the ``phone`` columns store raw user-supplied formatting (spaces,
    hyphens, ``+44``) that an ``icontains`` filter cannot reliably match
    against an operator-typed digit-only query. Phone search is routed
    through ``SystemDonor.normalized_phone`` instead — see
    :func:`_system_donor_search_query`.

    Args:
        query: Search string.

    Returns:
        Django Q object.
    """
    return (
        Q(urn__icontains=query)
        | Q(first_name__icontains=query)
        | Q(last_name__icontains=query)
        | Q(email__icontains=query)
        | Q(postcode__icontains=query)
    )


def _system_donor_search_query(query: str) -> Q:
    """Build a Q filter for SystemDonor search.

    Branches on the query shape: when the query looks like a phone number
    (≥6 digits after :func:`normalize_phone`), filters on the indexed
    ``normalized_phone`` column. Otherwise filters on
    ``external_urn``, ``first_name``, ``last_name``, ``email`` and
    ``postcode`` — all of which are indexed on ``SystemDonor``.

    Args:
        query: Search string.

    Returns:
        Django Q object scoped to SystemDonor columns.
    """
    if looks_like_phone_query(query):
        return Q(normalized_phone__icontains=normalize_phone(query))
    return (
        Q(external_urn__icontains=query)
        | Q(first_name__icontains=query)
        | Q(last_name__icontains=query)
        | Q(email__icontains=query)
        | Q(postcode__icontains=query)
    )


def _serialize_system_donor(donor: SystemDonor) -> dict:
    """Serialize a SystemDonor instance for the search API.

    Mirrors the shape of :func:`_serialize_house_donor` so the operator UI
    can render any tier with the same template. Adds ``system_donor_id``,
    ``pending_review`` and ``external_urn`` so the caller can distinguish
    SystemDonor results, surface the orange "pending QA" badge, and
    write back the linkage to a new Donation.

    Args:
        donor: SystemDonor model instance.

    Returns:
        dict suitable for JSON response.
    """
    return {
        "id": str(donor.id),
        "system_donor_id": str(donor.id),
        "external_urn": donor.external_urn,
        "urn": donor.external_urn,
        "title": donor.title,
        "first_name": donor.first_name,
        "last_name": donor.last_name,
        "full_name": donor.full_name,
        "email": donor.email,
        "phone": donor.phone,
        "address_line1": donor.address_line1,
        "address_line2": donor.address_line2,
        "city": donor.city,
        "county": donor.county,
        "postcode": donor.postcode,
        "country": donor.country,
        "date_of_birth": _iso_or_empty(donor.date_of_birth),
        "age": donor.age,
        "gift_aid_declaration": donor.gift_aid_declaration,
        "gift_aid_date": _iso_or_empty(donor.gift_aid_date),
        "contact_status": donor.contact_status,
        "contact_status_reason": donor.contact_status_reason,
        "contact_status_changed_at": _iso_or_empty(donor.contact_status_changed_at),
        "address_last_verified_at": _iso_or_empty(donor.address_last_verified_at),
        "consent_contact": donor.consent_contact,
        "no_thank_you": False,
        "opt_in_email": donor.opt_in_email,
        "opt_in_sms": donor.opt_in_sms,
        "opt_in_phone": donor.opt_in_phone,
        "opt_in_post": donor.opt_in_post,
        "pending_review": donor.pending_review,
        "source": "system_donor",
    }


def _resolve_donor_source(source: str, campaign_id: str | None) -> str:
    """Resolve the donor source, falling back to campaign setting.

    Args:
        source: Explicit source ('house_file' or 'data_file'), may be empty.
        campaign_id: Campaign UUID, or None.

    Returns:
        Resolved source string.
    """
    if source:
        return source
    if campaign_id:
        try:
            campaign = Campaign.objects.only("donor_source").get(pk=campaign_id)
            return campaign.donor_source
        except Campaign.DoesNotExist:
            pass
    return "house_file"


def _resolve_house_file_client_id(
    campaign_id: str | None, client_id: str | None
) -> str | None:
    """Resolve which client should scope house-file donor search.

    Args:
        campaign_id: Campaign identifier, if present.
        client_id: Explicit client identifier from request.

    Returns:
        Client identifier string when resolvable.
    """
    if client_id:
        return client_id
    if campaign_id:
        campaign = (
            Campaign.objects.select_related("client")
            .only("client__id")
            .filter(pk=campaign_id)
            .first()
        )
        if campaign is not None and campaign.client_id is not None:
            return str(campaign.client_id)
    return None


def _search_system_donors(
    query: str, *, client_id: str | None = None, limit: int = 10
) -> list[dict]:
    """Search SystemDonor (the system-of-record table).

    First tier of the donor search — runs against indexed columns including
    the phone-normalised ``normalized_phone`` field. Most callers (e.g. the
    phone intake console) hit this tier exclusively because anyone the
    operator could plausibly recognise has already been promoted to
    SystemDonor by the scan pipeline.

    Args:
        query: Search string.
        client_id: Optional client UUID to scope the search.
        limit: Maximum results.

    Returns:
        List of serialized SystemDonor dicts.
    """
    donors = SystemDonor.objects.filter(_system_donor_search_query(query))
    if client_id:
        donors = donors.filter(client_id=client_id)
    return [_serialize_system_donor(d) for d in donors[:limit]]


def _search_house_file(
    query: str, *, client_id: str | None = None, limit: int = 10
) -> list[dict]:
    """Search house file donors.

    Args:
        query: Search string.
        limit: Maximum results.

    Returns:
        List of serialized donor dicts.
    """
    donors = Donor.objects.filter(_donor_search_query(query))
    if client_id:
        donors = donors.filter(client_id=client_id)
    donors = donors[:limit]
    return [_serialize_house_donor(d) for d in donors]


def _search_data_file_donors(query: str, campaign_id: str, limit: int) -> list[dict]:
    """Search data file donors for a specific campaign.

    Args:
        query: Search string.
        campaign_id: Campaign UUID.
        limit: Maximum results.

    Returns:
        List of serialized DataFileDonor dicts.
    """
    try:
        data_file = CampaignDataFile.objects.get(campaign_id=campaign_id)
        df_donors = DataFileDonor.objects.filter(
            data_file=data_file,
        ).filter(_donor_search_query(query))[:limit]
        return [_serialize_data_file_donor(d) for d in df_donors]
    except CampaignDataFile.DoesNotExist:
        return []


def _dedupe_donors_by_urn(donors: list[dict]) -> list[dict]:
    """Drop duplicate donor entries that share the same non-empty URN.

    SystemDonor's ``external_urn`` mirrors the latest ``Donor.urn`` for the
    same human, so a search that traverses both tables can return the same
    person twice. Keep the first occurrence (SystemDonor wins because the
    SystemDonor tier runs first).

    Args:
        donors: Ordered list of serialised donor dicts.

    Returns:
        Deduplicated list, preserving order.
    """
    seen: set[str] = set()
    unique: list[dict] = []
    for donor in donors:
        urn = (donor.get("urn") or "").strip()
        if urn and urn in seen:
            continue
        if urn:
            seen.add(urn)
        unique.append(donor)
    return unique


# Donor search API for autocomplete
@is_authenticated_and_is_staff
@require_http_methods(["GET"])
def donor_search(request: HttpRequest) -> JsonResponse:
    """Search donors for autocomplete.

    Searches across:

    * ``SystemDonor`` first (indexed on phone, postcode, name, URN, email)
    * ``Donor`` (house file) second
    * ``DataFileDonor`` (campaign data file) when ``source=data_file`` and
      a campaign is supplied

    Phone-shaped queries (≥6 digits) hit the indexed
    ``SystemDonor.normalized_phone`` column. Other queries fall back to
    URN / name / email / postcode ``__icontains`` filters.

    Returns up to 10 deduplicated donors.
    """
    query = request.GET.get("q", "").strip()
    campaign_id = request.GET.get("campaign_id", None)
    client_id = request.GET.get("client_id", "").strip() or None
    source = _resolve_donor_source(request.GET.get("source", ""), campaign_id)

    if len(query) < 2:
        return JsonResponse({"donors": []})

    resolved_client_id = _resolve_house_file_client_id(campaign_id, client_id)

    # Data-file campaigns prefer data-file matches and only fall back to the
    # house file when the data-file search returns nothing — that
    # "exclusive when populated" rule is what stops a duplicate name in the
    # house file leaking into a data-file workflow. SystemDonor (a phone-intake
    # tier) is intentionally bypassed here for the same reason.
    if source == "data_file" and campaign_id:
        data_file_donors = _search_data_file_donors(query, campaign_id, limit=10)
        if data_file_donors:
            return JsonResponse({"donors": data_file_donors[:10]})
        house_donors = _search_house_file(
            query,
            client_id=resolved_client_id,
            limit=10,
        )
        return JsonResponse({"donors": house_donors[:10]})

    # House-file flow (default + phone-intake): SystemDonor first, then Donor.
    donor_list: list[dict] = _search_system_donors(
        query,
        client_id=resolved_client_id,
        limit=10,
    )
    if len(donor_list) < 10:
        house_donors = _search_house_file(
            query,
            client_id=resolved_client_id,
            limit=10 - len(donor_list),
        )
        donor_list = _dedupe_donors_by_urn(donor_list + house_donors)

    return JsonResponse({"donors": donor_list[:10]})


class CampaignDataFileViewSet(viewsets.ModelViewSet):
    """ViewSet for Campaign Data File management."""

    queryset = CampaignDataFile.objects.all()
    serializer_class = CampaignDataFileSerializer
    permission_classes = [HasSystemAccessPermission]
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["campaign"]
    search_fields = ["campaign__name"]
    ordering_fields = ["created_at", "total_donors"]

    def perform_create(self, serializer: CampaignDataFileSerializer) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        # Validate campaign is not closed
        campaign_id = self.request.data.get("campaign")
        if campaign_id:
            try:
                campaign = Campaign.objects.get(id=campaign_id)
                if campaign.status == "closed":
                    raise ValidationError(
                        {"error": "Cannot upload donors to a closed campaign"}
                    )
            except Campaign.DoesNotExist:
                raise ValidationError({"error": "Campaign not found"}) from None

        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["get"])
    def donors(self, request: Request, pk: str | None = None) -> Response:
        """Get all donors in this data file"""
        data_file = self.get_object()
        donors = DataFileDonor.objects.filter(data_file=data_file)

        # Pagination
        page = self.paginate_queryset(donors)
        if page is not None:
            serializer = DataFileDonorSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = DataFileDonorSerializer(donors, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=["delete"])
    def clear_donors(self, request: Request, pk: str | None = None) -> Response:
        """Queue deletion of all donors from this data file."""
        data_file = self.get_object()
        count = data_file.donors.count()

        if count == 0:
            return Response(
                {
                    "message": "No donors to delete",
                    "deleted_count": 0,
                    "method": "noop",
                }
            )

        from core.tasks import bulk_delete_data_file_donors_task

        try:
            task = bulk_delete_data_file_donors_task.delay(str(data_file.campaign_id))
            return Response(
                {
                    "message": f"Deletion of {count} donors started in background",
                    "task_id": task.id,
                    "estimated_count": count,
                    "method": "asynchronous",
                }
            )
        except Exception as exc:
            logger.exception(
                "Failed to queue donor deletion for campaign %s", data_file.campaign_id
            )
            return Response(
                {
                    "error": "Background deletion queue unavailable",
                    "detail": str(exc),
                    "deleted_count": 0,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )


class DataFileUploadViewSet(viewsets.ModelViewSet):
    """ViewSet for Data File Upload management."""

    queryset = DataFileUpload.objects.all()
    serializer_class = DataFileUploadSerializer
    permission_classes = [HasSystemAccessPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ["data_file", "status"]
    ordering_fields = ["created_at", "completed_at"]

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Ensure the campaign data-file container exists before validation."""
        data_file_id = request.data.get("data_file")
        if data_file_id:
            try:
                campaign = Campaign.objects.get(pk=data_file_id)
            except Campaign.DoesNotExist:
                raise ValidationError({"error": "Campaign not found"}) from None

            CampaignDataFile.objects.get_or_create(
                campaign=campaign,
                defaults={"created_by": request.user},
            )

        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer: DataFileUploadSerializer) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        # Validate campaign is not closed before saving
        data_file_id = self.request.data.get("data_file")
        if data_file_id:
            try:
                # campaign is the primary key for CampaignDataFile
                data_file = CampaignDataFile.objects.select_related("campaign").get(
                    campaign=data_file_id
                )
                if data_file.campaign.status == "closed":
                    raise ValidationError(
                        {"error": "Cannot upload donors to a closed campaign"}
                    )
            except CampaignDataFile.DoesNotExist:
                raise ValidationError({"error": "Data file not found"}) from None

        upload = serializer.save(uploaded_by=self.request.user)

        _start_data_file_upload_processing(upload)

    @action(detail=True, methods=["post"])
    def process(self, request: Request, pk: str | None = None) -> Response:
        """Manually trigger processing of an upload"""
        upload = self.get_object()

        if upload.status == "processing":
            return Response(
                {"error": "Upload is already being processed"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        processing_mode = _start_data_file_upload_processing(upload)
        upload.refresh_from_db()

        if processing_mode == "queued":
            task_id = None
            if upload.error_log and isinstance(upload.error_log[0], dict):
                task_id = upload.error_log[0].get("task_id")
            return Response(
                {
                    "message": "Processing started in background",
                    "task_id": task_id,
                    "upload_id": str(upload.id),
                    "status": "queued",
                }
            )

        return Response(
            {
                "message": "Processed synchronously",
                "upload_id": str(upload.id),
                "status": upload.status,
                "total_rows": upload.total_rows,
                "successful_imports": upload.successful_imports,
                "failed_imports": upload.failed_imports,
                "error_log": upload.error_log,
                "completed_at": upload.completed_at,
            }
        )

    @action(detail=True, methods=["get"])
    def download(self, request: Request, pk: str | None = None) -> FileResponse:
        """Stream the uploaded data file back to the caller as an attachment."""
        upload = self.get_object()
        if not upload.file:
            raise NotFound("No file attached to this upload")
        filename = Path(upload.file.name).name or "data_file.csv"
        content_type, _ = mimetypes.guess_type(filename)
        return FileResponse(
            upload.file.open("rb"),
            as_attachment=True,
            filename=filename,
            content_type=content_type or "application/octet-stream",
        )

    @action(detail=True, methods=["get"])
    def task_status(self, request: Request, pk: str | None = None) -> Response:
        """Get the current state of a data file upload.

        This serves as a polling endpoint for the frontend.
        """
        upload = self.get_object()

        return Response(
            {
                "id": str(upload.id),
                "status": upload.status,
                "total_rows": upload.total_rows,
                "successful_imports": upload.successful_imports,
                "failed_imports": upload.failed_imports,
                "error_log": upload.error_log,
                "completed_at": upload.completed_at,
            }
        )


class LetterBatchViewSet(viewsets.ReadOnlyModelViewSet):
    """API endpoint for letter batch status tracking.

    Provides endpoints for monitoring letter generation task progress
    and retrieving batch details. Used by Task Notifications system.
    """

    # ``letter_batch_detail.html`` reads ``donation.donor`` /
    # ``donation.data_file_donor`` for every donation and the serializer
    # reads ``campaign.name`` / ``template.name`` per batch — keep those
    # joins on the base queryset so listing N batches stays O(1) queries.
    # Pinned by tests/unit/test_qa_review_perf.py.
    queryset = LetterBatch.objects.select_related(
        "campaign",
        "campaign__client",
        "template",
        "created_by",
    ).prefetch_related(
        Prefetch(
            "donations",
            queryset=Donation.objects.select_related("donor", "data_file_donor"),
        )
    )
    permission_classes = [HasSystemAccessPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["campaign", "status"]

    def get_serializer_data(self, obj: LetterBatch) -> dict:
        """Serialize LetterBatch with key metrics for notifications."""
        return {
            "id": str(obj.id),
            "batch_number": obj.batch_number,
            "campaign_id": str(obj.campaign.id),
            "campaign_name": obj.campaign.name,
            "template_name": obj.template.name if obj.template else "N/A",
            "status": obj.status,
            "progress_percent": obj.progress_percent,
            "total_letters": obj.total_letters,
            "generated_count": obj.generated_count,
            "failed_count": obj.failed_count,
            "file_count": obj.file_count,
            "letters_per_file": obj.letters_per_file,
            "created_at": obj.created_at.isoformat(),
            "started_at": obj.started_at.isoformat() if obj.started_at else None,
            "completed_at": obj.completed_at.isoformat() if obj.completed_at else None,
        }

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """List letter batches with simple metrics."""
        queryset = self.filter_queryset(self.get_queryset())
        serialized = [self.get_serializer_data(obj) for obj in queryset]
        return Response(serialized)

    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Get detailed batch info including task status."""
        obj = self.get_object()
        data = self.get_serializer_data(obj)

        # Get Celery task status if running
        if obj.celery_task_id and obj.status == "processing":
            from celery.result import AsyncResult

            task_result = AsyncResult(obj.celery_task_id)
            data["celery_status"] = task_result.state
            data["celery_info"] = task_result.info if task_result.info else None

        return Response(data)

    @action(detail=True, methods=["get"], url_path="task-status")
    def task_status(self, request: Request, pk: str | None = None) -> Response:
        """Get current batch task status for notifications.

        Returns simplified metrics suitable for Task Notifications:
        - progress_percent: 0-100 completion percentage
        - status: pending/processing/completed/failed/cancelled
        - generated_count: letters successfully generated
        - failed_count: letters failed to generate
        - total_letters: total letters in batch
        """
        batch = self.get_object()

        # Get Celery task info if processing
        celery_info = None
        if batch.celery_task_id and batch.status in ["processing", "pending"]:
            from celery.result import AsyncResult

            task_result = AsyncResult(batch.celery_task_id)
            celery_info = {
                "state": task_result.state,
                "info": task_result.info,
            }

        return Response(
            {
                "id": str(batch.id),
                "batch_number": batch.batch_number,
                "status": batch.status,
                "progress_percent": batch.progress_percent,
                "generated_count": batch.generated_count,
                "failed_count": batch.failed_count,
                "total_letters": batch.total_letters,
                "file_count": batch.file_count,
                "celery_info": celery_info,
                "message": f"{batch.generated_count}/{batch.total_letters} letters generated",
            }
        )

    @action(
        detail=False,
        methods=["get"],
        url_path="by-celery-task/(?P<celery_task_id>[^/.]+)",
    )
    def by_celery_task(self, request: Request, celery_task_id: str) -> Response:
        """Find a letter batch by its Celery task ID.

        This is useful for task notifications when only the task_id is known
        (before the batch creation task completes).

        Args:
            celery_task_id: The Celery task ID from generate_letter_batch_task.

        Returns:
            Batch status if found, or 404 if not found.
        """
        try:
            batch = LetterBatch.objects.get(celery_task_id=celery_task_id)
            return Response(self.get_serializer_data(batch))
        except LetterBatch.DoesNotExist:
            # Batch might not exist yet, check Celery task result
            from celery.result import AsyncResult

            task_result = AsyncResult(celery_task_id)

            if task_result.state == "SUCCESS" and task_result.result:
                result = task_result.result
                batch_id = result.get("batch_id")
                if batch_id:
                    try:
                        batch = LetterBatch.objects.get(id=batch_id)
                        return Response(self.get_serializer_data(batch))
                    except LetterBatch.DoesNotExist:
                        pass

            return Response(
                {
                    "error": "Batch not found",
                    "celery_task_id": celery_task_id,
                    "celery_state": task_result.state,
                },
                status=404,
            )


@is_authenticated_and_is_staff
@require_http_methods(["GET"])
def campaigns_by_client_api(request: HttpRequest) -> JsonResponse:
    """API endpoint to get campaigns for a specific client.

    Query params:
        client_id: UUID of the client.
        status: Optional. When provided, restrict the result to campaigns
            with this status (e.g. ``active``). Must match one of
            ``Campaign.STATUS_CHOICES``; unknown values return 400.
            Omitted/empty returns all statuses (default).

    Returns:
        JSON response with campaigns list.
    """
    client_id = request.GET.get("client_id")

    if not client_id:
        return JsonResponse({"error": "client_id is required"}, status=400)

    status_filter = request.GET.get("status", "").strip().lower()
    if status_filter:
        valid_statuses = {choice[0] for choice in Campaign.STATUS_CHOICES}
        if status_filter not in valid_statuses:
            return JsonResponse({"error": "Invalid status"}, status=400)

    try:
        client_obj = Client.objects.get(id=client_id)
    except Client.DoesNotExist:
        return JsonResponse({"error": "Client not found"}, status=404)

    qs = client_obj.campaigns.order_by("name")
    if status_filter:
        qs = qs.filter(status=status_filter)
    campaigns = qs.values("id", "name", "description", "status")

    return JsonResponse({"campaigns": list(campaigns), "status": "success"})


@is_authenticated_and_is_staff
@require_http_methods(["GET"])
def package_codes_api(request: HttpRequest, campaign_id: str) -> JsonResponse:
    """API endpoint to get package codes for a specific campaign.

    Args:
        campaign_id: UUID of the campaign."""
    try:
        campaign = Campaign.objects.get(id=campaign_id)
    except Campaign.DoesNotExist:
        return JsonResponse({"error": "Campaign not found"}, status=404)

    package_codes = campaign.package_codes.order_by("code").values(
        "id", "code", "description", "is_active"
    )

    return JsonResponse({"package_codes": list(package_codes), "status": "success"})


_SCAN_EXTENSIONS = ("jpg", "jpeg", "png", "pdf", "tiff", "bmp", "webp")


def _find_local_scan_image(media_root: Path, base_path: Path, urn: str) -> str | None:
    """Search local filesystem for a scanned form image.

    Args:
        media_root: Absolute path to MEDIA_ROOT.
        base_path: Relative path prefix (client/appeal/pkg).
        urn: Donor URN to find.

    Returns:
        Media URL string if found, or None.
    """
    for ext in _SCAN_EXTENSIONS:
        candidate = base_path / f"{urn}.{ext}"
        if (media_root / candidate).exists():
            media_url = getattr(settings, "MEDIA_URL", "") or ""
            prefix = media_url.rstrip("/")
            normalized = str(candidate).replace("\\", "/")
            return f"{prefix}/{normalized}"
    return None


def _find_r2_scan_image(
    client_slug: str, appeal_slug: str, pkg_slug: str, urn: str
) -> str | None:
    """Search R2 storage for a scanned form image.

    Args:
        client_slug: Slugified client name.
        appeal_slug: Slugified appeal code.
        pkg_slug: Slugified package code.
        urn: Donor URN to find.

    Returns:
        Public URL string if found, or None.
    """
    try:
        from core.storage_backends import (
            r2_enabled,
            r2_list_prefix,
        )

        if r2_enabled():
            prefix = f"{client_slug}/{appeal_slug}/{pkg_slug}/{urn}"
            keys = r2_list_prefix(prefix, max_keys=1)
            if keys:
                from core.storage_backends import r2_presigned_url

                return r2_presigned_url(keys[0])
    except Exception as e:
        logger.error("R2 lookup error: %s", e)
    return None


def _resolve_scan_slugs(campaign: Campaign, package_code: str) -> tuple[str, str, str]:
    """Resolve slugified path components for a scanned form lookup.

    Args:
        campaign: Campaign model instance with client prefetched.
        package_code: Package code string.

    Returns:
        Tuple of (client_slug, appeal_slug, pkg_slug).
    """
    client_name = campaign.client.name if campaign.client else "client"
    appeal_code = getattr(campaign, "appeal_code", None) or campaign.name

    return (
        slugify(client_name) or "client",
        slugify(appeal_code) or "appeal",
        slugify(package_code) or "package",
    )


@is_authenticated_and_is_staff
@require_http_methods(["GET"])
def scanned_form_lookup(request: HttpRequest) -> JsonResponse:
    """
    Lookup scanned form URL for a given URN, campaign, and package code.
    Used by batch entry for predictive image display.

    When ``batch_id`` is supplied, the donation lookup is constrained to that
    batch — prevents cross-batch contamination when the same URN appears in
    more than one batch in the same campaign (e.g. card + cheque batches for
    the same donor).
    """
    campaign_id = request.GET.get("campaign_id")
    urn = request.GET.get("urn")
    package_code = request.GET.get("package_code", "")
    batch_id = request.GET.get("batch_id", "")
    allow_pending_redaction = request.GET.get("allow_pending_redaction") == "1"

    if not campaign_id or not urn:
        return JsonResponse({"error": "campaign_id and urn are required"}, status=400)

    try:
        campaign = Campaign.objects.select_related("client").get(id=campaign_id)
    except (Campaign.DoesNotExist, ValueError):  # fmt: skip
        return JsonResponse({"error": "Campaign not found"}, status=404)

    client_slug, appeal_slug, pkg_slug = _resolve_scan_slugs(campaign, package_code)
    base_path = Path(client_slug) / appeal_slug / pkg_slug
    image_url = None
    page_urls: list[str] = []

    try:
        from scans.donation_scan import DonationScanService

        donation_qs = (
            Donation.objects.select_related(
                "scan_placeholder",
                "donor",
                "data_file_donor",
            )
            .filter(campaign=campaign)
            .filter(
                Q(donor__urn=urn)
                | Q(data_file_donor__urn=urn)
                | Q(scan_placeholder__urn=urn)
            )
        )
        if batch_id:
            donation_qs = donation_qs.filter(scan_placeholder__batch_id=batch_id)
        donation = donation_qs.order_by("-created_at").first()
        if donation is not None:
            page_urls = DonationScanService.get_scanned_form_page_urls(
                donation,
                require_existing=True,
                user=request.user if request.user.is_authenticated else None,
                allow_pending_redaction=allow_pending_redaction,
            )
            if page_urls:
                image_url = page_urls[0]
    except Exception:
        logger.exception("Failed to resolve placeholder-backed scanned form pages")

    # Check local filesystem
    if image_url is None:
        media_root_value = getattr(settings, "MEDIA_ROOT", None)
        if media_root_value:
            media_root = Path(media_root_value)
            if media_root.exists():
                image_url = _find_local_scan_image(media_root, base_path, urn)

    # Fall back to R2
    if image_url is None:
        image_url = _find_r2_scan_image(client_slug, appeal_slug, pkg_slug, urn)
        if image_url:
            page_urls = [image_url]

    if image_url and not page_urls:
        page_urls = [image_url]

    predicted = (
        f"{getattr(settings, 'MEDIA_URL', '').rstrip('/')}"
        f"/{client_slug}/{appeal_slug}/{pkg_slug}/{urn}.jpg"
    )

    return JsonResponse(
        {
            "success": True,
            "has_image": image_url is not None,
            "image_url": image_url,
            "page_urls": page_urls,
            "predicted_url": predicted,
        }
    )
