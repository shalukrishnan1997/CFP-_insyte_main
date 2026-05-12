"""Regression tests for QA review N+1 query hotspots.

These tests pin the query budget for two of the worst offenders we see at
scale (100 QA reviewers + 5 scanners):

* ``custom_admin.views.qa_review._render_single_donation_review`` — used
  by both ``qa_batch_review`` and ``qa_single_donation_review``. The
  template iterates donations and reads ``donor`` / ``data_file_donor``
  / ``system_donor`` / ``scan_placeholder`` / ``batch`` / ``campaign``
  for every row.
* ``custom_admin.api_views.LetterBatchViewSet`` — Task Notifications
  polls this endpoint frequently; the inner ``donations`` access used
  to issue a follow-up query per donation.

The thresholds below are intentionally generous (well above the actual
count for the optimised paths) so they only fail when the underlying
``select_related`` / ``prefetch_related`` set is regressed back into an
N+1 shape — not on minor unrelated query churn elsewhere in the view.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from donations.models import Donation, DonationBatch
from letters.models import LetterBatch, LetterTemplate
from tests.factories import (
    CampaignFactory,
    DonationBatchFactory,
    DonationFactory,
    UserFactory,
)

# Query-count ceilings. These are "regression" budgets — they should be
# *several times* lower than the un-optimised query count so a refactor
# that re-introduces the N+1 immediately trips the assertion. With the
# optimisations in place the actual count is comfortably under each.
QA_REVIEW_QUERY_BUDGET = 35
LETTER_BATCH_LIST_QUERY_BUDGET = 15


def _build_qa_batch(num_donations: int) -> DonationBatch:
    """Create a pending-QA batch populated with ``num_donations`` donations.

    Donations use ``cheque`` payment so the QA view skips the Stripe
    publishable-key lookup, keeping the query budget focused on the
    donation list (the hotspot under test).
    """
    batch = DonationBatchFactory(
        status=DonationBatch.STATUS_PENDING_QA,
        default_payment_method="cheque",
    )
    DonationFactory.create_batch(
        num_donations,
        batch=batch,
        campaign=batch.campaign,
        payment_method="cheque",
        qa_status=Donation.QA_STATUS_PENDING,
    )
    return batch


@pytest.mark.django_db()
def test_qa_batch_review_query_count_does_not_grow_with_donations(
    client: Client,
) -> None:
    """50 donations in a batch must not trigger 50 follow-up queries.

    The batch detail view delegates to ``_render_single_donation_review``
    which iterates donations and traverses several FKs per row. With
    proper ``select_related`` the query count is bounded by the helper
    queries (auth / aggregate / nav) and stays well under the budget.
    """
    staff = UserFactory(is_staff=True, is_superuser=True)
    batch = _build_qa_batch(50)
    url = reverse("custom_admin:qa_batch_review", args=[batch.id])

    client.force_login(staff)

    # Warm the response once so any first-request side effects (template
    # cache, content-type lookup) don't pollute the measurement, then
    # measure the steady-state query count.
    client.get(url)
    with CaptureQueriesContext(connection) as ctx:
        response = client.get(url)

    assert response.status_code == 200, response.content[:500]
    assert len(ctx.captured_queries) < QA_REVIEW_QUERY_BUDGET, (
        f"qa_batch_review issued {len(ctx.captured_queries)} queries for "
        f"50 donations (budget {QA_REVIEW_QUERY_BUDGET}). "
        "Did the donation queryset lose its select_related?"
    )


@pytest.mark.django_db()
def test_letter_batch_list_query_count_constant_across_batch_size(
    client: Client,
) -> None:
    """Listing N letter batches stays at a constant number of queries.

    ``LetterBatchViewSet`` previously used ``prefetch_related("donations")``
    without a custom queryset, so any donor access in serialization or
    downstream template rendering would issue a query per donation. With
    ``Prefetch("donations", queryset=Donation.objects.select_related(...))``
    plus ``select_related("campaign__client", "template")`` on the base
    queryset, the count is bounded.
    """
    staff = UserFactory(is_staff=True, is_superuser=True)
    campaign = CampaignFactory()
    template = LetterTemplate.objects.create(
        campaign=campaign,
        created_by=staff,
    )

    # Three letter batches, each with a handful of donations. If the
    # prefetch is regressed, query count grows ~linearly with the total
    # number of donations across batches.
    for batch_number in range(1, 4):
        lb = LetterBatch.objects.create(
            campaign=campaign,
            template=template,
            batch_number=batch_number,
        )
        donations = DonationFactory.create_batch(5, campaign=campaign)
        Donation.objects.filter(pk__in=[d.pk for d in donations]).update(
            letter_batch=lb
        )

    api_client = APIClient()
    api_client.force_authenticate(user=staff)

    # Touch the endpoint once to settle any first-request setup.
    api_client.get("/admin/api/letter-batches/")
    with CaptureQueriesContext(connection) as ctx:
        response = api_client.get("/admin/api/letter-batches/")

    assert response.status_code == 200, getattr(response, "content", b"")[:500]
    payload: list[dict[str, Any]] = response.json()
    assert len(payload) == 3
    assert len(ctx.captured_queries) < LETTER_BATCH_LIST_QUERY_BUDGET, (
        f"LetterBatch list issued {len(ctx.captured_queries)} queries for "
        f"3 batches (budget {LETTER_BATCH_LIST_QUERY_BUDGET}). "
        "Did the prefetch_related lose its custom Donation queryset?"
    )
