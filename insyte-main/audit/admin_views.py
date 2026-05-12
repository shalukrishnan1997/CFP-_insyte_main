"""Audit log views for tracking system changes with optimized performance."""

from django.core.cache import cache
from django.db.models import Count, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.dateparse import parse_date

from audit.models import AuditLog
from core.models import User
from core.pagination import paginate_queryset
from responsehandling.permissions import has_permission_or_is_staff


@has_permission_or_is_staff("view_auditlog")
def audit_log_history(request: HttpRequest) -> HttpResponse:
    """Display comprehensive audit log history with filters and pagination.

    Shows all CRUD operations across the system with optimized performance (<500ms).
    Includes filters for user, action type, model, date range, and search.
    Displays bulk operations as summarized entries.

    Args:
        request: HTTP request with optional filter parameters.

    Returns:
        Rendered audit log history page with optimized queries.
    """
    # Get filter parameters
    user_filter = request.GET.get("user", "")
    action_filter = request.GET.get("action", "")
    model_filter = request.GET.get("model", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    search_query = request.GET.get("search", "").strip()
    per_page = int(request.GET.get("per_page", 50))

    # Build optimized queryset with select_related and only()
    logs_qs = AuditLog.objects.select_related("user").only(
        "id",
        "user__id",
        "user__username",
        "user__first_name",
        "user__last_name",
        "action",
        "model_name",
        "object_repr",
        "batch_id",
        "batch_size",
        "summary",
        "created_at",
    )

    # Apply filters
    if user_filter:
        logs_qs = logs_qs.filter(user_id=user_filter)

    if action_filter:
        logs_qs = logs_qs.filter(action=action_filter)

    if model_filter:
        logs_qs = logs_qs.filter(model_name=model_filter)

    if date_from:
        try:
            date_from_parsed = parse_date(date_from)
            if date_from_parsed:
                logs_qs = logs_qs.filter(created_at__date__gte=date_from_parsed)
        except ValueError:
            pass

    if date_to:
        try:
            date_to_parsed = parse_date(date_to)
            if date_to_parsed:
                # Include entire day
                logs_qs = logs_qs.filter(created_at__date__lte=date_to_parsed)
        except ValueError:
            pass

    if search_query:
        logs_qs = logs_qs.filter(
            Q(summary__icontains=search_query)
            | Q(object_repr__icontains=search_query)
            | Q(batch_id__icontains=search_query)
            | Q(user__username__icontains=search_query)
            | Q(user__first_name__icontains=search_query)
            | Q(user__last_name__icontains=search_query)
        )

    # Order by most recent first
    logs_qs = logs_qs.order_by("-created_at")

    logs = paginate_queryset(logs_qs, request, per_page=per_page)

    # Get filter options (cached for 5 minutes)
    cache_key = "audit_filter_options"
    filter_options = cache.get(cache_key)

    if filter_options is None:
        # Optimized queries for filter dropdowns
        all_users_qs = (
            User.objects.filter(audit_logs__isnull=False)
            .distinct()
            .only("id", "username", "first_name", "last_name")
            .order_by("username")[:100]  # Limit for performance
        )

        # Convert to list of dicts for caching
        all_models_data = list(
            AuditLog.objects.values("model_name")
            .annotate(count=Count("id"))
            .order_by("model_name")
        )

        all_actions_data = list(
            AuditLog.objects.values("action")
            .annotate(count=Count("id"))
            .order_by("action")
        )

        # Convert users to list of dicts for caching
        all_users_data = [
            {
                "id": user.id,
                "username": user.username,
                "first_name": getattr(user, "first_name", ""),
                "last_name": getattr(user, "last_name", ""),
            }
            for user in all_users_qs
        ]

        filter_options = {
            "users": all_users_data,
            "models": all_models_data,
            "actions": all_actions_data,
        }
        cache.set(cache_key, filter_options, 300)  # Cache for 5 minutes

    all_users = filter_options["users"]
    all_models = filter_options["models"]
    all_actions = filter_options["actions"]

    context = {
        "logs": logs,
        "all_users": all_users,
        "all_models": all_models,
        "all_actions": all_actions,
        "user_filter": user_filter,
        "action_filter": action_filter,
        "model_filter": model_filter,
        "date_from": date_from,
        "date_to": date_to,
        "search_query": search_query,
        "per_page": per_page,
        "active": "audit_history",
        "breadcrumbs": [
            {"name": "Audit History", "url": None},
        ],
    }

    return render(request, "admin/audit_history.html", context)
