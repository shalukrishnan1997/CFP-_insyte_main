"""Integration tests for the ``gdpr_erase_donor`` management command.

Audit 2026-05-02 §7.2: the command must (a) detach ``StripeCustomer`` rows
linked to the donor and (b) record R2 / media prefixes in the audit-log
row so an operator can purge them. (c) The audit row uses an action value
that satisfies the ``AuditLog.ACTION_CHOICES`` contract (``EXPORT``).
"""

from __future__ import annotations

from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from audit.models import AuditLog
from donations.models import Donation
from donors.models import Donor
from payments.models import StripeCustomer
from tests.factories import (
    CampaignFactory,
    ClientFactory,
    DonationBatchFactory,
    DonorFactory,
    StripeCustomerFactory,
    UserFactory,
)


@pytest.mark.django_db()
class TestGdprEraseDonorExtended:
    def _seed(self) -> tuple[Donor, list[StripeCustomer]]:
        client = ClientFactory()
        user = UserFactory()
        campaign = CampaignFactory(client=client, created_by=user)
        donor = DonorFactory(client=client, urn="ERASE-ME-001")
        batch = DonationBatchFactory(campaign=campaign)
        Donation.objects.create(
            campaign=campaign,
            donor=donor,
            batch=batch,
            amount=Decimal("25.00"),
            currency="GBP",
            payment_method="cheque",
            cheque_number="000123",
            card_holder_name="Jane Donor",
            card_last_four="4242",
            card_expiry_date="12/30",
        )
        sc1 = StripeCustomerFactory(
            client=client, donor=donor, email="jane@example.com"
        )
        sc2 = StripeCustomerFactory(
            client=client, donor=donor, email="jane2@example.com"
        )
        return donor, [sc1, sc2]

    def test_stripe_customers_are_deleted(self) -> None:
        donor, _customers = self._seed()
        out = StringIO()

        call_command(
            "gdpr_erase_donor",
            donor.urn,
            "--dry-run",
            stdout=out,
        )
        # Dry-run must not delete.
        assert StripeCustomer.objects.filter(donor=donor).count() == 2

        call_command(
            "gdpr_erase_donor",
            donor.urn,
            "--reason=integration test",
            "--no-confirm",
            stdout=out,
        )

        assert StripeCustomer.objects.filter(donor=donor).count() == 0

    def test_audit_row_uses_valid_action_and_records_purge_targets(self) -> None:
        donor, customers = self._seed()
        out = StringIO()

        call_command(
            "gdpr_erase_donor",
            donor.urn,
            "--reason=test",
            "--no-confirm",
            stdout=out,
        )

        log = AuditLog.objects.get(action="EXPORT", object_id=donor.urn)
        assert log.changes["kind"] == "GDPR_ERASURE"
        assert log.changes["stripe_customers_deleted"] == 2
        assert sorted(log.changes["stripe_customer_ids_for_remote_purge"]) == sorted(
            c.stripe_customer_id for c in customers
        )
        # The r2_targets list shape is recorded even if empty.
        assert "r2_targets_for_operator_purge" in log.changes
