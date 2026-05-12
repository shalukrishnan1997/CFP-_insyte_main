"""HTMX partial endpoints for the daily banking dashboard."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from responsehandling.permissions import has_permission_or_is_staff

from .view_helpers import build_banking_batch_queue_context


@has_permission_or_is_staff("view_donation")
@require_GET
def htmx_banking_batch_queue(request: HttpRequest) -> HttpResponse:
    """Return the paginated batch queue partial for the daily banking dashboard."""
    return render(
        request,
        "admin/daily_banking/partials/_batch_queue.html",
        build_banking_batch_queue_context(request),
    )
