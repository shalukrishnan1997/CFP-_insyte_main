"""Regression checks for donor migration end-state semantics."""

import pytest
from django.db import connection

from tests.factories import ClientFactory, DonorFactory


@pytest.mark.django_db()
def test_donor_table_contains_current_columns() -> None:
    """The fully migrated donor table exposes the current donor schema."""
    expected_columns = {
        "id",
        "client_id",
        "urn",
        "gift_aid_declaration",
        "gift_aid_date",
        "contact_status",
        "contact_status_reason",
        "contact_status_changed_at",
        "address_last_verified_at",
    }

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, "donors_donor"
            )
        }

    assert expected_columns.issubset(columns)


@pytest.mark.django_db()
def test_duplicate_urns_are_allowed_across_clients() -> None:
    """Donor URNs remain scoped by client after the full migration chain."""
    first_client = ClientFactory()
    second_client = ClientFactory()

    first = DonorFactory(client=first_client, urn="SHARED-URN-001")
    second = DonorFactory(client=second_client, urn="SHARED-URN-001")

    assert first.urn == second.urn
