import io
import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.db.utils import OperationalError
from django.http import FileResponse, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from campaigns.cache_keys import CAMPAIGN_METRICS_CACHE_KEY
from campaigns.models import Campaign, CampaignField, PackageCode
from clients.models import Client
from core.constants import CURRENCY_CODE, CURRENCY_SYMBOL
from core.date_utils import parse_date
from core.pagination import paginate_queryset
from core.utils import restore_session_filters
from responsehandling.permissions import (
    has_permission_or_is_staff,
    is_authenticated_and_is_staff,
)

# Status constants — use model-defined constants directly
STATUS_DRAFT = Campaign.STATUS_DRAFT
STATUS_LIVE = Campaign.STATUS_LIVE
STATUS_CLOSED = Campaign.STATUS_CLOSED
# These statuses exist in the UI workflow but aren't yet defined on the model
STATUS_PENDING_APPROVAL = "pending"
STATUS_APPROVED = "approved"
SAMPLE_IMPORT_FILENAME = "pipe_delimited_import_sample.csv"
SAMPLE_IMPORT_CONTENT = "\n".join(
    [
        "urn|title|first_name|last_name|email|phone|address_line1|address_line2|city|county|postcode|country|package_code",
        "URN001|Mr|John|Smith|john.smith@example.com|07123456789|1 High Street||London|Greater London|SW1A1AA|GB|PKG001",
        "",
    ]
)


# ---------------------------------------------------------------------------
# View helpers (inlined from campaign_view_helpers)
# ---------------------------------------------------------------------------


def _campaign_status_timeline() -> list[str]:
    return [
        Campaign.STATUS_DRAFT,
        STATUS_PENDING_APPROVAL,
        STATUS_APPROVED,
        Campaign.STATUS_LIVE,
        Campaign.STATUS_CLOSED,
    ]


def normalize_campaign_post_data(request: HttpRequest) -> dict[str, str]:
    post_data = {key: request.POST.get(key, "").strip() for key in request.POST}
    post_data.setdefault("appeal_start", "")
    post_data.setdefault("appeal_end", "")
    return post_data


def _campaign_form_base_context() -> dict[str, Any]:
    """Build common campaign create/edit context values."""
    return {
        "active": "campaigns",
        "appeal_types": ["Donation", "Raffle"],
        "clients": Client.objects.filter(is_active=True).order_by("name"),
        "donor_source_choices": Campaign.DONOR_SOURCE_CHOICES,
        "campaign_temperature_choices": Campaign.CAMPAIGN_TEMPERATURE_CHOICES,
        "scan_purpose_choices": Campaign.SCAN_PURPOSE_CHOICES,
        "custom_field_type_choices": [
            {"value": value, "label": label}
            for value, label in CampaignField.FIELD_TYPE_CHOICES
        ],
    }


def _create_campaign_breadcrumbs() -> list[dict[str, str | None]]:
    """Build breadcrumbs for campaign creation."""
    return [
        {"name": "Campaigns", "url": reverse("custom_admin:admin_campaigns")},
        {"name": "Create Campaign", "url": None},
    ]


def _edit_campaign_breadcrumbs(campaign: Campaign) -> list[dict[str, str | None]]:
    """Build breadcrumbs for campaign editing."""
    return [
        {"name": "Campaigns", "url": reverse("custom_admin:admin_campaigns")},
        {
            "name": campaign.name,
            "url": reverse(
                "custom_admin:campaign_edit",
                kwargs={"campaign_id": campaign.id},
            ),
        },
        {"name": "Edit Campaign", "url": None},
    ]


def build_campaign_form_context(
    *,
    campaign: Campaign | None = None,
    form_data: dict[str, str] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = _campaign_form_base_context()
    if campaign is None:
        context.update(
            {
                "form_data": form_data or {},
                "breadcrumbs": _create_campaign_breadcrumbs(),
            }
        )
        return context
    context.update(_existing_campaign_form_context(campaign))
    return context


def _serialize_custom_fields(fields: list[CampaignField]) -> list[dict[str, Any]]:
    """Serialize editable custom fields for template JSON bootstrap."""
    return [
        {
            "id": str(field.id),
            "label": field.label,
            "field_type": field.field_type,
            "required": field.required,
            "options": field.options,
        }
        for field in fields
        if not field.is_default_field
    ]


def _existing_campaign_form_context(campaign: Campaign) -> dict[str, Any]:
    """Build campaign-specific edit context values."""
    fields = list(
        campaign.fields.all().order_by("order") if hasattr(campaign, "fields") else []
    )
    campaign.package_codes_json = [
        {"code": pc.code} for pc in campaign.package_codes.all()
    ]
    return {
        "campaign": campaign,
        "fields": fields,
        "custom_fields_json": _serialize_custom_fields(fields),
        "status_choices": getattr(Campaign, "STATUS_CHOICES", []),
        "breadcrumbs": _edit_campaign_breadcrumbs(campaign),
    }


def build_campaign_list_context(
    campaigns_page: Any,
    metrics: dict[str, int],
    *,
    status_filter: str | None,
    search_query: str,
    sort_by: str,
    sort_order: str,
) -> dict[str, Any]:
    return {
        "campaigns": campaigns_page,
        "metrics": metrics,
        "status_timeline": _campaign_status_timeline(),
        "active": "campaigns",
        "currency_symbol": CURRENCY_SYMBOL,
        "currency_code": CURRENCY_CODE,
        "breadcrumbs": [{"name": "Campaigns", "url": None}],
        "paginator": campaigns_page.paginator,
        "page_obj": campaigns_page,
        "status_filter": status_filter,
        "search_query": search_query,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }


_CAMPAIGNS_SESSION_KEY = "campaigns_filters"
_CAMPAIGNS_PERSISTENT_PARAMS = ("status", "search", "sort", "order")
_ALLOWED_SORT_FIELDS = frozenset(
    {
        "name",
        "created_at",
        "start_date",
        "client__name",
        "status",
        "appeal_code",
    }
)
_ALLOWED_STATUS_VALUES = frozenset({"draft", "active", "closed"})


def _set_if_present(obj: Any, post: Any, post_key: str, attr: str) -> None:
    """Set an attribute on obj only if the POST key has a value."""
    val = post.get(post_key)
    if val:
        setattr(obj, attr, val)


def _set_date_if_present(obj: Any, post: Any, key: str) -> None:
    """Parse and set a date attribute on obj only if the POST key has a value."""
    raw = post.get(key)
    if raw:
        setattr(obj, key, parse_date(raw))


def _handle_campaign_edit(request: HttpRequest) -> HttpResponse:
    """Handle the campaign 'edit' POST action."""
    cid = request.POST.get("campaign_id")
    if not cid:
        messages.error(request, "Missing campaign id.")
        return redirect("custom_admin:admin_campaigns")
    c = get_object_or_404(Campaign, pk=cid)
    c.name = (request.POST.get("name") or c.name).strip()
    c.description = (request.POST.get("description") or c.description).strip()
    c.target_amount = request.POST.get("target_amount") or c.target_amount
    c.status = request.POST.get("status") or c.status

    # Apply optional fields only if provided
    _set_if_present(c, request.POST, "client_id", "client_id")
    _set_date_if_present(c, request.POST, "start_date")
    _set_date_if_present(c, request.POST, "end_date")

    c.save()
    messages.success(request, f"Campaign '{c.name}' updated.")
    return redirect("custom_admin:admin_campaigns")


def _handle_campaign_delete(request: HttpRequest) -> HttpResponse:
    """Handle the campaign 'delete' POST action."""
    cid = request.POST.get("campaign_id")
    if not cid:
        messages.error(request, "Missing campaign id.")
        return redirect("custom_admin:admin_campaigns")
    c = get_object_or_404(Campaign, pk=cid)
    c.delete()
    messages.success(request, "Campaign deleted.")
    return redirect("custom_admin:admin_campaigns")


def _handle_campaign_status_change(request: HttpRequest) -> HttpResponse:
    """Handle the campaign 'change_status' POST action."""
    cid = request.POST.get("campaign_id")
    status = request.POST.get("status")
    if not cid or not status:
        messages.error(request, "Missing data for status change.")
        return redirect("custom_admin:admin_campaigns")
    c = get_object_or_404(Campaign, pk=cid)

    # Issue 10: Block closure while scans are active or donation batches are pending QA.
    if status == "closed":
        from donations.models import DonationBatch
        from scans.models import ScanBatch

        if ScanBatch.objects.filter(
            campaign=c,
            status__in=[ScanBatch.STATUS_PENDING, ScanBatch.STATUS_PROCESSING],
        ).exists():
            messages.error(
                request,
                "Cannot close campaign: there are scan batches still being processed. "
                "Wait for all scans to complete before closing.",
            )
            return redirect("custom_admin:admin_campaigns")

        if DonationBatch.objects.filter(
            campaign=c,
            status__in=[
                DonationBatch.STATUS_PENDING_QA,
                DonationBatch.STATUS_IN_REVIEW,
            ],
        ).exists():
            messages.error(
                request,
                "Cannot close campaign: there are donation batches awaiting QA approval. "
                "Approve or reject all pending batches before closing.",
            )
            return redirect("custom_admin:admin_campaigns")

    c.status = status
    c.save()
    messages.success(request, f"Campaign status updated to {status}.")
    return redirect("custom_admin:admin_campaigns")


def _dispatch_campaign_post(request: HttpRequest) -> HttpResponse:
    """Dispatch a campaign POST action and return a redirect."""
    action = request.POST.get("action")
    if action == "create":
        return redirect("custom_admin:campaign_create")
    handler = {
        "edit": _handle_campaign_edit,
        "delete": _handle_campaign_delete,
        "change_status": _handle_campaign_status_change,
    }.get(action or "")
    if handler:
        return handler(request)
    messages.error(request, "Unknown action.")
    return redirect("custom_admin:admin_campaigns")


def _save_campaign_filters(request: HttpRequest) -> None:
    """Persist campaign filter params to session."""
    filters = {
        p: request.GET[p] for p in _CAMPAIGNS_PERSISTENT_PARAMS if request.GET.get(p)
    }
    request.session[_CAMPAIGNS_SESSION_KEY] = filters


_CAMPAIGN_SEARCH_FIELDS = (
    "name__icontains",
    "description__icontains",
    "appeal_code__icontains",
    "client__name__icontains",
)


def _apply_campaign_filters(
    qs: Any,
    status_filter: str | None,
    search_query: str,
) -> Any:
    """Apply status and search filters to a campaigns queryset."""
    active_filter = "" if status_filter == "all" else status_filter
    if active_filter and active_filter in _ALLOWED_STATUS_VALUES:
        qs = qs.filter(status=active_filter)
    if search_query:
        q = Q()
        for field in _CAMPAIGN_SEARCH_FIELDS:
            q |= Q(**{field: search_query})
        qs = qs.filter(q)
    return qs


def _build_campaigns_queryset(
    status_filter: str | None,
    search_query: str,
    sort_by: str,
    sort_order: str,
) -> Any:
    """Build the filtered, sorted campaigns queryset."""
    if sort_by not in _ALLOWED_SORT_FIELDS:
        sort_by = "created_at"
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    try:
        qs = (
            Campaign.objects.select_related("client", "created_by")
            .prefetch_related("package_codes")
            .only(
                "id",
                "name",
                "description",
                "status",
                "created_at",
                "start_date",
                "end_date",
                "target_amount",
                "appeal_code",
                "client__id",
                "client__name",
                "created_by__id",
                "created_by__first_name",
                "created_by__last_name",
            )
        )
        qs = _apply_campaign_filters(qs, status_filter, search_query)
        order_prefix = "" if sort_order == "asc" else "-"
        return qs.order_by(f"{order_prefix}{sort_by}")
    except OperationalError:
        messages.error(  # type: ignore[call-arg]
            None,  # pyright: ignore[reportArgumentType]
            "Database schema out of sync. Run 'python manage.py migrate'.",
        )
        return Campaign.objects.none()


def _get_campaign_metrics(request: HttpRequest) -> dict[str, int]:
    """Get cached campaign metrics (live/draft/total counts).

    Cache is invalidated by ``campaigns.signals._invalidate_campaign_metrics``
    on every ``Campaign`` save / delete, so a new draft campaign is reflected
    in the dashboard badge immediately. The key is intentionally per-installation
    (no user scoping) — these aggregate counts are identical for every viewer.
    """
    metrics = cache.get(CAMPAIGN_METRICS_CACHE_KEY)
    if metrics is not None:
        return metrics
    try:
        live_count = Campaign.objects.filter(status__in=[STATUS_LIVE, "active"]).count()
        draft_count = Campaign.objects.filter(status=STATUS_DRAFT).count()
        total_campaigns = Campaign.objects.count()
    except OperationalError:
        live_count = draft_count = total_campaigns = 0
    metrics = {
        "live_count": live_count,
        "draft_count": draft_count,
        "total_campaigns": total_campaigns,
    }
    cache.set(CAMPAIGN_METRICS_CACHE_KEY, metrics, 300)
    return metrics


@has_permission_or_is_staff("view_campaign")
def admin_campaigns(request: HttpRequest) -> HttpResponse:
    """List and manage campaigns with pagination and cached metrics."""
    if request.method == "POST":
        try:
            return _dispatch_campaign_post(request)
        except Exception as exc:
            messages.error(request, f"Action failed: {exc}")
            return redirect("custom_admin:admin_campaigns")

    # Handle clear
    if request.GET.get("clear"):
        request.session.pop(_CAMPAIGNS_SESSION_KEY, None)
        return redirect("custom_admin:admin_campaigns")

    # Restore filters or redirect to defaults
    redir = restore_session_filters(
        request,
        _CAMPAIGNS_SESSION_KEY,
        "custom_admin:admin_campaigns",
        filter_keys=_CAMPAIGNS_PERSISTENT_PARAMS,
        default_params={"status": "active"},
    )
    if redir:
        return redir

    # Save current filters
    _save_campaign_filters(request)

    search_query = request.GET.get("search", "").strip()
    sort_by = request.GET.get("sort", "created_at")
    sort_order = request.GET.get("order", "desc")
    status_filter = request.GET.get("status")

    campaigns_qs = _build_campaigns_queryset(
        status_filter,
        search_query,
        sort_by,
        sort_order,
    )

    campaigns_page = paginate_queryset(campaigns_qs, request, per_page=20)

    context = build_campaign_list_context(
        campaigns_page,
        _get_campaign_metrics(request),
        status_filter=status_filter,
        search_query=search_query,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    response = render(request, "admin/campaigns.html", context)
    response["Cache-Control"] = "max-age=60, private"
    return response


_REQUIRED_CREATE_FIELDS = {
    "title": "Title is required.",
    "appeal_code": "Appeal Code is required.",
    "client_id": "Client is required.",
    "campaign_temperature": "Campaign type is required.",
}


def _normalize_appeal_code(value: str) -> str:
    """Normalize an appeal code: strip surrounding whitespace and uppercase."""
    return (value or "").strip().upper()


def _validate_campaign_form(
    post_data: dict[str, str],
    exclude_id: Any = None,
) -> list[str]:
    """Validate campaign form and return list of error strings.

    Args:
        post_data: Dictionary of POST values to validate.
        exclude_id: Optional campaign PK to exclude from the per-client
            uniqueness checks (used on edit so the current campaign's own
            name/appeal code are not treated as duplicates).
    """
    errors: list[str] = []
    for field, msg in _REQUIRED_CREATE_FIELDS.items():
        if not post_data.get(field):
            errors.append(msg)
    if not post_data.get("appeal_start"):
        errors.append("Appeal Start Date is required.")
    if not post_data.get("appeal_end"):
        errors.append("Appeal End Date is required.")

    title = post_data.get("title", "")
    client_id = post_data.get("client_id", "")
    qs = Campaign.objects.filter(name=title, client_id=client_id)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    if not errors and qs.exists():
        errors.append(
            f"A campaign with name '{title}' already exists for this client. "
            "Please use a different name."
        )

    appeal_code = _normalize_appeal_code(post_data.get("appeal_code", ""))
    if appeal_code and client_id:
        code_qs = Campaign.objects.filter(
            appeal_code__iexact=appeal_code, client_id=client_id
        )
        if exclude_id:
            code_qs = code_qs.exclude(pk=exclude_id)
        if code_qs.exists():
            errors.append(
                f"A campaign with appeal code '{appeal_code}' already exists "
                "for this client. Please use a different code."
            )
    return errors


def _build_campaign_kwargs(
    post_data: dict[str, str],
    user: Any,
) -> dict[str, Any]:
    """Build kwargs dict for Campaign.objects.create."""
    kwargs: dict[str, Any] = {
        "name": post_data["title"],
        "description": post_data.get("description", ""),
        "client_id": post_data.get("client_id") or None,
        "created_by": user,
        "status": Campaign.STATUS_DRAFT,
        "hgv_amount": post_data.get("hgv_amount", "0"),
        "lgv_amount": post_data.get("lgv_amount", "0"),
        "campaign_manager_emails": post_data.get("campaign_manager_emails", ""),
        "donor_source": post_data.get("donor_source", Campaign.DONOR_SOURCE_HOUSE_FILE),
        "campaign_temperature": post_data.get(
            "campaign_temperature", Campaign.CAMPAIGN_TEMPERATURE_COLD
        ),
        "scan_purpose": post_data.get("scan_purpose", Campaign.SCAN_PURPOSE_DONATION),
    }
    field_names = {f.name for f in Campaign._meta.get_fields()}
    optional_fields: dict[str, Any] = {
        "appeal_code": _normalize_appeal_code(post_data.get("appeal_code", "")),
        "appeal_type": post_data.get("appeal_type", "Donation"),
        "appeal_start": parse_date(post_data.get("appeal_start")),
        "appeal_end": parse_date(post_data.get("appeal_end")),
    }
    for fname, val in optional_fields.items():
        if fname in field_names and val:
            kwargs[fname] = val
    return kwargs


def _validate_campaign_instance(campaign: Campaign) -> None:
    """Run model-level validation for custom admin campaign workflows."""
    campaign.full_clean()


def _link_package_codes(campaign: Campaign, codes_str: str) -> None:
    """Parse comma-separated package codes and link to campaign."""
    if not codes_str:
        return
    for code_str in (c.strip().upper() for c in codes_str.split(",") if c.strip()):
        pc, _ = PackageCode.objects.get_or_create(
            code=code_str, defaults={"is_active": True}
        )
        campaign.package_codes.add(pc)


_DEFAULT_CAMPAIGN_FIELDS = [
    {"label": "Donor Name", "field_type": "text", "required": True, "order": 1},
    {"label": "Email", "field_type": "email", "required": False, "order": 2},
    {"label": "Phone", "field_type": "phone", "required": False, "order": 3},
    {"label": "Amount", "field_type": "number", "required": True, "order": 4},
    {"label": "Donation Date", "field_type": "date", "required": True, "order": 5},
    {
        "label": "Payment Method",
        "field_type": "dropdown",
        "required": True,
        "order": 6,
        "options": ["Cash", "Cheque", "Online Transfer", "Card", "Other"],
    },
]


def _create_default_fields(campaign: Campaign) -> None:
    """Create default CampaignField entries for a new campaign."""
    for fd in _DEFAULT_CAMPAIGN_FIELDS:
        CampaignField.objects.create(campaign=campaign, is_default_field=True, **fd)


_CHOICE_FIELD_TYPES = frozenset({"dropdown", "radio", "checkbox"})
_VALID_CUSTOM_FIELD_TYPES = frozenset(
    field_type for field_type, _ in CampaignField.FIELD_TYPE_CHOICES
)


def _parse_field_options(raw_options: Any) -> list[str]:
    """Normalize field options to a list of strings."""
    if isinstance(raw_options, str):
        return [o.strip() for o in raw_options.split(",") if o.strip()]
    return raw_options or []


def _create_single_extra_field(
    campaign: Campaign,
    field_def: dict[str, Any],
    order: int,
) -> str | None:
    """Create a single extra CampaignField.

    Returns an error message string if validation fails, or None on success.
    """
    label = str(field_def.get("label", "")).strip()
    if not label:
        return None
    field_type = field_def.get("field_type", "text")
    required = bool(field_def.get("required", False))
    options = _parse_field_options(field_def.get("options"))

    if field_type in _CHOICE_FIELD_TYPES and not options:
        return (
            f"Field '{label}' is of type '{field_type}' and requires "
            "at least one option."
        )

    CampaignField.objects.create(
        campaign=campaign,
        label=label,
        field_type=field_type,
        required=required,
        options=options,
        order=order,
    )
    return None


def _create_extra_fields(
    request: HttpRequest,
    campaign: Campaign,
) -> bool:
    """Parse and create extra custom fields from JSON POST data.

    Returns True if a validation error caused the campaign to be deleted.
    """
    extra_json = (request.POST.get("extra_fields_json") or "").strip()
    if not extra_json:
        return False
    try:
        extra_fields = json.loads(extra_json)
        cur_max = (
            CampaignField.objects.filter(campaign=campaign).aggregate(Max("order"))[
                "order__max"
            ]
            or 0
        )
        for i, f in enumerate(extra_fields, start=1):
            err = _create_single_extra_field(campaign, f, cur_max + i)
            if err:
                messages.error(request, err)
                campaign.delete()
                return True
    except Exception as e:
        messages.warning(request, f"Some custom fields were not created: {e}")
    return False


def _deserialize_custom_field_payload(
    extra_json: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """Deserialize and validate submitted custom-field payload."""
    if not extra_json.strip():
        return [], None

    try:
        raw_fields = json.loads(extra_json)
    except json.JSONDecodeError:
        return [], "Custom field settings could not be parsed."

    if not isinstance(raw_fields, list):
        return [], "Custom field settings payload is invalid."

    normalized_fields: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            return [], "Custom field settings payload is invalid."

        label = str(raw_field.get("label", "")).strip()
        if not label:
            continue

        label_key = label.casefold()
        if label_key in seen_labels:
            return [], f"Custom field '{label}' is duplicated."
        seen_labels.add(label_key)

        field_type = str(
            raw_field.get("field_type") or CampaignField.FIELD_TEXT
        ).strip()
        if field_type not in _VALID_CUSTOM_FIELD_TYPES:
            return [], f"Custom field '{label}' uses an unsupported field type."

        options = _parse_field_options(raw_field.get("options"))
        if field_type in _CHOICE_FIELD_TYPES and not options:
            return (
                [],
                f"Field '{label}' is of type '{field_type}' and requires at least one option.",
            )

        normalized_fields.append(
            {
                "id": str(raw_field.get("id", "")).strip(),
                "label": label,
                "field_type": field_type,
                "required": bool(raw_field.get("required", False)),
                "options": options,
            }
        )

    return normalized_fields, None


def _sync_campaign_extra_fields(
    campaign: Campaign,
    custom_fields: list[dict[str, Any]],
) -> None:
    """Replace editable custom fields while preserving default campaign fields."""
    existing_fields = {
        str(field.id): field
        for field in campaign.fields.filter(is_default_field=False).order_by("order")
    }
    default_label_keys = {
        label.casefold()
        for label in campaign.fields.filter(is_default_field=True).values_list(
            "label", flat=True
        )
    }
    submitted_label_keys = {field["label"].casefold() for field in custom_fields}
    conflicting_default_labels = sorted(
        default_label_keys.intersection(submitted_label_keys)
    )
    if conflicting_default_labels:
        raise ValueError("Custom field labels cannot reuse default field names.")

    default_order_max = (
        campaign.fields.filter(is_default_field=True).aggregate(Max("order"))[
            "order__max"
        ]
        or 0
    )
    retained_field_ids: set[str] = set()

    for offset, field_data in enumerate(custom_fields, start=1):
        field_id = field_data["id"]
        payload = {
            "label": field_data["label"],
            "field_type": field_data["field_type"],
            "required": field_data["required"],
            "options": field_data["options"],
            "order": default_order_max + offset,
        }

        if field_id and field_id in existing_fields:
            field = existing_fields[field_id]
            field.label = payload["label"]
            field.field_type = payload["field_type"]
            field.required = payload["required"]
            field.options = payload["options"]
            field.order = payload["order"]
            field.save(
                update_fields=["label", "field_type", "required", "options", "order"]
            )
            retained_field_ids.add(field_id)
            continue

        created_field = CampaignField.objects.create(
            campaign=campaign,
            is_default_field=False,
            **payload,
        )
        retained_field_ids.add(str(created_field.id))

    for field_id, field in existing_fields.items():
        if field_id not in retained_field_ids:
            field.delete()


@has_permission_or_is_staff("add_campaign")
def campaign_create(request: HttpRequest) -> HttpResponse:
    """Create new campaign (donation form)."""
    if request.method == "POST":
        post = normalize_campaign_post_data(request)

        validation_errors = _validate_campaign_form(post)

        if validation_errors:
            for err in validation_errors:
                messages.error(request, err)
            return render(
                request,
                "admin/campaign_create.html",
                build_campaign_form_context(form_data=post),
            )

        kwargs = _build_campaign_kwargs(post, request.user)
        campaign = Campaign(**kwargs)
        _validate_campaign_instance(campaign)
        campaign.save()

        _link_package_codes(campaign, post.get("package_codes", ""))
        _create_default_fields(campaign)

        if _create_extra_fields(request, campaign):
            return redirect("custom_admin:campaign_create")

        messages.success(
            request, f"Campaign '{campaign.name}' created with default fields!"
        )
        return redirect(f"{reverse('custom_admin:admin_campaigns')}?status=all")

    return render(
        request,
        "admin/campaign_create.html",
        build_campaign_form_context(),
    )


def _apply_edit_updates(campaign: Campaign, post: Any) -> None:
    """Apply POST data updates to an existing campaign instance."""
    campaign.name = (post.get("title") or campaign.name).strip()
    campaign.description = post.get("description", "").strip()
    campaign.status = post.get("status") or campaign.status
    campaign.hgv_amount = post.get("hgv_amount", "0")
    campaign.lgv_amount = post.get("lgv_amount", "0")
    campaign.campaign_manager_emails = post.get("campaign_manager_emails", "").strip()
    campaign.donor_source = post.get("donor_source", campaign.donor_source).strip()
    campaign.campaign_temperature = (
        post.get("campaign_temperature", campaign.campaign_temperature).strip()
        or campaign.campaign_temperature
    )
    campaign.scan_purpose = (
        post.get("scan_purpose", campaign.scan_purpose).strip() or campaign.scan_purpose
    )

    _set_if_present(campaign, post, "client_id", "client_id")

    # Set appeal fields only if they exist in the model schema
    appeal_fields: dict[str, Any] = {
        "appeal_code": _normalize_appeal_code(post.get("appeal_code", "")),
        "appeal_type": post.get("appeal_type", "Donation").strip() or "Donation",
        "appeal_start": parse_date(post.get("appeal_start")),
        "appeal_end": parse_date(post.get("appeal_end")),
    }
    model_fields = {f.name for f in Campaign._meta.get_fields()}
    for fname, val in appeal_fields.items():
        if fname in model_fields:
            setattr(campaign, fname, val)

    _validate_campaign_instance(campaign)
    campaign.save()


@is_authenticated_and_is_staff
def download_campaign_import_sample(request: HttpRequest) -> FileResponse:
    """Download the sample pipe-delimited campaign import CSV."""
    sample_path = Path(settings.BASE_DIR) / "static" / "files" / SAMPLE_IMPORT_FILENAME
    sample_handle: io.BytesIO | None = None
    if sample_path.is_file():
        file_obj = sample_path.open("rb")
    else:
        sample_handle = io.BytesIO(SAMPLE_IMPORT_CONTENT.encode("utf-8"))
        file_obj = sample_handle

    response = FileResponse(
        file_obj,
        as_attachment=True,
        filename=SAMPLE_IMPORT_FILENAME,
        content_type="application/octet-stream",
    )
    response["X-Content-Type-Options"] = "nosniff"
    if sample_handle is not None:
        response["Content-Length"] = str(sample_handle.getbuffer().nbytes)
    return response


@has_permission_or_is_staff("change_campaign")
def campaign_edit(request: HttpRequest, campaign_id: int) -> HttpResponse:
    """Edit campaign metadata and appeal information."""
    campaign = get_object_or_404(Campaign, pk=campaign_id)

    if request.method != "POST":
        return render(
            request,
            "admin/campaign_edit.html",
            build_campaign_form_context(campaign=campaign),
        )

    post_data = {
        "title": request.POST.get("title", "").strip(),
        "appeal_code": _normalize_appeal_code(request.POST.get("appeal_code", "")),
        "client_id": request.POST.get("client_id", "").strip(),
        "package_codes": request.POST.get("package_codes", "").strip(),
        "campaign_temperature": request.POST.get("campaign_temperature", "").strip(),
        "appeal_start": request.POST.get("appeal_start", ""),
        "appeal_end": request.POST.get("appeal_end", ""),
    }
    errors = _validate_campaign_form(post_data, exclude_id=campaign_id)
    if errors:
        for err in errors:
            messages.error(request, err)
        return redirect("custom_admin:campaign_edit", campaign_id=campaign_id)

    custom_fields, custom_field_error = _deserialize_custom_field_payload(
        request.POST.get("extra_fields_json", "")
    )
    if custom_field_error:
        messages.error(request, custom_field_error)
        return redirect("custom_admin:campaign_edit", campaign_id=campaign_id)

    try:
        with transaction.atomic():
            _apply_edit_updates(campaign, request.POST)
            campaign.package_codes.clear()
            _link_package_codes(campaign, post_data["package_codes"])
            _sync_campaign_extra_fields(campaign, custom_fields)
    except (IntegrityError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("custom_admin:campaign_edit", campaign_id=campaign_id)

    messages.success(request, f"Campaign '{campaign.name}' updated.")
    return redirect("custom_admin:admin_campaigns")
