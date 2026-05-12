"""Tests for enforcing current donation date in edit flows."""

from datetime import datetime
from unittest.mock import patch

import pytest
from django.test import RequestFactory
from django.utils import timezone

from custom_admin.views import donation_quick_edit
from custom_admin.views.qa_utils import update_donation_from_post
from donations.admin_views import _save_donation_from_post
from tests.factories import DonationFactory, UserFactory


@pytest.mark.django_db
def test_quick_edit_overrides_submitted_date_with_today() -> None:
    donation = DonationFactory(donation_date=datetime(2024, 1, 1).date())
    user = UserFactory(is_staff=True, is_superuser=True)
    fixed_now = timezone.make_aware(datetime(2026, 3, 27, 12, 0, 0))

    request = RequestFactory().post(
        "/",
        data={
            "amount": "33.00",
            "currency": "GBP",
            "payment_method": "card",
            "donation_date": "2020-01-01",
            "gift_aid": "on",
        },
        HTTP_REFERER="/admin/",
    )
    request.user = user

    with (
        patch(
            "donations.admin_views_quick_edit.timezone.now",
            return_value=fixed_now,
        ),
        patch("donations.admin_views_quick_edit.messages.success"),
    ):
        response = donation_quick_edit(request, donation.id)

    assert response.status_code == 302
    donation.refresh_from_db()
    assert donation.donation_date == fixed_now.date()


@pytest.mark.django_db
def test_donation_edit_save_overrides_submitted_date_with_today() -> None:
    donation = DonationFactory(donation_date=datetime(2024, 1, 1).date())
    fixed_now = timezone.make_aware(datetime(2026, 3, 27, 12, 0, 0))

    request = RequestFactory().post(
        "/",
        data={
            "payment_method": "card",
            "gift_aid": "",
            "donation_frequency": "one_off",
            "cheque_no": "",
            "donation_date": "2020-01-01",
            "cheque_date": "",
        },
    )

    with patch("donations.admin_views.timezone.now", return_value=fixed_now):
        errors = _save_donation_from_post(request, donation, [])

    assert errors == []
    donation.refresh_from_db()
    assert donation.donation_date == fixed_now.date()


@pytest.mark.django_db
def test_qa_update_persists_submitted_donation_date() -> None:
    donation = DonationFactory(donation_date=datetime(2024, 1, 1).date())

    update_donation_from_post(
        donation,
        {
            "amount": "25.00",
            "currency": donation.currency,
            "payment_method": donation.payment_method,
            "donation_date": "2020-01-01",
        },
    )

    assert donation.donation_date == datetime(2020, 1, 1).date()
