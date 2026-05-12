"""Centralised pagination helpers for INSYTE DMS.

All views should use ``paginate_queryset`` instead of repeating the
``Paginator`` + ``try/except PageNotAnInteger/EmptyPage`` boilerplate.
"""

import contextlib

from django.core.paginator import EmptyPage, Page, PageNotAnInteger, Paginator
from django.db.models import QuerySet
from django.http import HttpRequest


def paginate_queryset(
    queryset: QuerySet | list,
    request: HttpRequest,
    per_page: int = 25,
    *,
    page_param: str = "page",
    per_page_param: str | None = None,
) -> Page:
    """Paginate a queryset or list using request GET parameters.

    Args:
        queryset: Django QuerySet or plain list to paginate.
        request: The current HTTP request (reads ``page`` param from GET).
        per_page: Default items per page.
        page_param: GET parameter name for the page number.
        per_page_param: Optional GET parameter name to allow the client
            to override *per_page*.  If ``None``, the default is used.

    Returns:
        A Django ``Page`` object ready for template rendering.
    """
    if per_page_param:
        with contextlib.suppress(TypeError, ValueError):
            per_page = int(request.GET.get(per_page_param, per_page))

    page_number = request.GET.get(page_param, 1)
    paginator = Paginator(queryset, per_page)

    try:
        page_obj = paginator.page(page_number)
    except PageNotAnInteger:
        page_obj = paginator.page(1)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages)

    return page_obj
