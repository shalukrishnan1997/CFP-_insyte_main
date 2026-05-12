"""Unit tests for ``donations.intake.create_phone_donation``."""

from datetime import date
from decimal import Decimal

import pytest

from audit.models import AuditLog
from donations.intake import (
    PhoneDonationPayload,
    create_phone_donation,
    create_phone_intake_batch,
)
from donations.models import Donation
from donors.updates import DonorVanished
from tests.factories import (
    CampaignFactory,
    DonorFactory,
    SystemDonorFactory,
    UserFactory,
)


def _payload(**overrides) -> PhoneDonationPayload:
    """Return a baseline phone-intake payload for tests."""
    base: PhoneDonationPayload = {
        "amount": Decimal("25.00"),
        "currency": "GBP",
        "payment_method": Donation.PAYMENT_METHOD_CHEQUE,
        "donation_date": date(2026, 5, 2),
        "gift_aid": True,
        "donation_frequency": "",
        "donor_source": "house_file",
        "donor_match_status": "exact",
    }
    base.update(overrides)
    return base


@pytest.mark.django_db()
class TestCreatePhoneDonation:
    """Phone-intake donation creation."""

    def test_cheque_donation_lands_in_pending_qa(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = DonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(cheque_number="123456", cheque_date=date(2026, 4, 30)),
        )

        assert donation.qa_status == Donation.QA_STATUS_PENDING
        assert donation.payment_method == Donation.PAYMENT_METHOD_CHEQUE
        assert donation.amount == Decimal("25.00")
        assert donation.cheque_number == "123456"
        assert donation.cheque_date == date(2026, 4, 30)
        assert donation.donor_id == donor.id
        assert donation.donor_source == "house_file"
        assert donation.gift_aid is True

    def test_field_data_marks_intake_method_phone(self) -> None:
        operator = UserFactory(username="alice")
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(payment_method=Donation.PAYMENT_METHOD_CASH),
        )

        assert donation.field_data["intake_method"] == "phone"
        assert donation.field_data["intake_operator_username"] == "alice"
        assert donation.field_data["intake_operator_id"] == str(operator.id)
        assert donation.field_data["donor_match_status"] == "exact"
        assert "captured_at" in donation.field_data

    def test_system_donor_links_via_system_donor_fk(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
        )

        assert donation.system_donor_id == donor.id
        assert donation.donor_id is None

    def test_zero_amount_is_flagged(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = DonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(amount=Decimal("0.00")),
        )

        assert donation.qa_status == Donation.QA_STATUS_FLAGGED

    def test_pending_review_donor_is_flagged(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, pending_review=True)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
        )

        assert donation.qa_status == Donation.QA_STATUS_FLAGGED

    def test_no_donor_creates_donation_with_null_links(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=None,
            payload=_payload(),
        )

        assert donation.donor_id is None
        assert donation.system_donor_id is None
        assert donation.data_file_donor_id is None

    def test_direct_debit_payload_stores_encrypted_bank_fields(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(
                payment_method=Donation.PAYMENT_METHOD_DIRECT_DEBIT,
                sort_code="608371",
                account_number="12345678",
                direct_debit_start_date=date(2026, 6, 1),
                donation_frequency="monthly",
                card_holder_name="Jane Doe",
                # Phase 3 requires mandate consent for DD intake.
                dd_mandate_consent={"verbatim_read_aloud": True},
            ),
        )

        # Reload to confirm the encrypted round-trip works.
        donation.refresh_from_db()
        assert donation.sort_code == "608371"
        assert donation.account_number == "12345678"
        assert donation.direct_debit_start_date == date(2026, 6, 1)
        assert donation.donation_frequency == "monthly"
        assert donation.card_holder_name == "Jane Doe"
        assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING

    def test_card_payload_stores_card_metadata_only(self) -> None:
        """Phase 1 stores card_last_four / holder name. No charge yet."""
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(
                payment_method=Donation.PAYMENT_METHOD_CARD,
                card_holder_name="Jane Doe",
                card_last_four="4242",
                card_expiry_date="12/2030",
            ),
        )

        assert donation.card_last_four == "4242"
        assert donation.card_holder_name == "Jane Doe"
        assert donation.card_expiry_date == "12/2030"
        assert donation.payment_status == Donation.PAYMENT_STATUS_PENDING

    def test_card_last_four_truncates_long_input(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(
                payment_method=Donation.PAYMENT_METHOD_CARD,
                card_last_four="4242XXXXXX",
            ),
        )
        assert donation.card_last_four == "4242"

    def test_batch_campaign_mismatch_raises(self) -> None:
        operator = UserFactory()
        campaign_a = CampaignFactory()
        campaign_b = CampaignFactory()
        donor = SystemDonorFactory(client=campaign_a.client)
        batch_a = create_phone_intake_batch(operator=operator, campaign=campaign_a)

        with pytest.raises(ValueError):
            create_phone_donation(
                operator=operator,
                campaign=campaign_b,  # mismatched
                batch=batch_a,
                donor=donor,
                payload=_payload(),
            )


@pytest.mark.django_db()
class TestCreatePhoneDonationDonorUpdates:
    """Donor edits applied during financial phone donations.

    PR #154 surfaced the donor-edit panel for every payment method.
    ``create_phone_donation`` accepts an optional ``donor_updates``
    payload and applies it inside the same atomic block as the donation
    row.
    """

    def test_donor_updates_applied_with_donation(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, first_name="Original")
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
            donor_updates={"donor_first_name": "Updated"},
        )

        donor.refresh_from_db()
        assert donor.first_name == "Updated"
        assert donation.system_donor_id == donor.id

    def test_no_donor_updates_leaves_donor_unchanged(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, first_name="Original")
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
            donor_updates=None,
        )

        donor.refresh_from_db()
        assert donor.first_name == "Original"

    def test_empty_donor_updates_leaves_donor_unchanged(self) -> None:
        """Empty dict is treated like None — no lock, no mutation."""
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, first_name="Original")
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
            donor_updates={},
        )

        donor.refresh_from_db()
        assert donor.first_name == "Original"

    def test_full_payload_matching_current_values_no_db_write(self) -> None:
        """Operator opens the panel but doesn't change anything: every
        editable donor_* key carries the donor's current value (mirrors
        what the frontend submits when nothing was edited). The mutation
        step diffs values per-field, so no save fires and no audit-log
        entry is recorded."""
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)
        donor_audit_before = AuditLog.objects.filter(
            model_name__in=["Donor", "SystemDonor", "DataFileDonor"],
            object_id=str(donor.pk),
        ).count()

        # Build the same payload the frontend would send for an unedited
        # submission: every PHONE_INTAKE_EDITABLE_FIELDS key, hydrated
        # from the donor's current state.
        full_payload: dict[str, object] = {
            "donor_title": donor.title or "",
            "donor_first_name": donor.first_name or "",
            "donor_last_name": donor.last_name or "",
            "donor_urn": donor.external_urn or "",
            "donor_email": donor.email or "",
            "donor_phone": donor.phone or "",
            "donor_address_line1": donor.address_line1 or "",
            "donor_address_line2": donor.address_line2 or "",
            "donor_city": donor.city or "",
            "donor_county": donor.county or "",
            "donor_postcode": donor.postcode or "",
            "donor_country": donor.country or "",
            "donor_consent_contact": donor.consent_contact,
            "donor_opt_in_email": donor.opt_in_email,
            "donor_opt_in_sms": donor.opt_in_sms,
            "donor_opt_in_phone": donor.opt_in_phone,
            "donor_opt_in_post": donor.opt_in_post,
            "donor_gift_aid_declaration": donor.gift_aid_declaration,
        }
        if donor.gift_aid_date is not None:
            full_payload["donor_gift_aid_date"] = donor.gift_aid_date.isoformat()
        if donor.date_of_birth is not None:
            full_payload["donor_date_of_birth"] = donor.date_of_birth.isoformat()
        if donor.age is not None:
            full_payload["donor_age"] = donor.age

        create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
            donor_updates=full_payload,
        )

        donor_audit_after = AuditLog.objects.filter(
            model_name__in=["Donor", "SystemDonor", "DataFileDonor"],
            object_id=str(donor.pk),
        ).count()
        assert donor_audit_after == donor_audit_before

    def test_donor_vanished_when_row_deleted_before_submit(self) -> None:
        """Race: donor exists at lookup but is deleted before the
        row-locked re-fetch fires inside the transaction."""
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)
        type(donor).objects.filter(pk=donor.pk).delete()

        with pytest.raises(DonorVanished):
            create_phone_donation(
                operator=operator,
                campaign=campaign,
                batch=batch,
                donor=donor,
                payload=_payload(),
                donor_updates={"donor_first_name": "Doesn't matter"},
            )

    def test_donor_updates_skipped_when_donor_is_none(self) -> None:
        """No donor linked → donor_updates is silently skipped (no crash)."""
        operator = UserFactory()
        campaign = CampaignFactory()
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)

        donation = create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=None,
            payload=_payload(),
            donor_updates={"donor_first_name": "Ignored"},
        )

        assert donation.system_donor_id is None
        assert donation.donor_id is None

    def test_audit_log_records_donor_change(self) -> None:
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client, first_name="Original")
        batch = create_phone_intake_batch(operator=operator, campaign=campaign)
        before = AuditLog.objects.filter(
            model_name__in=["Donor", "SystemDonor", "DataFileDonor"],
            object_id=str(donor.pk),
        ).count()

        create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
            donor_updates={"donor_first_name": "Audited"},
        )

        after = AuditLog.objects.filter(
            model_name__in=["Donor", "SystemDonor", "DataFileDonor"],
            object_id=str(donor.pk),
        ).count()
        assert after > before

    def test_unchanged_contact_status_does_not_restamp_changed_at(self) -> None:
        """Regression: every full-payload submission would previously
        re-stamp ``contact_status_changed_at`` to ``now()`` even when the
        status didn't change, breaking the field's semantic meaning and
        flooding the audit log with phantom contact_status entries.

        Reproduces the scenario: donor's status is the default ``normal``;
        operator submits a card/cheque/cash donation; the frontend's full
        hydrated ``donorEdits`` payload includes ``donor_contact_status =
        'normal'``. The backend now skips the timestamp stamp when the
        status and reason both match the donor's current values.
        """
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        # Pre-condition: factory donors land with the default contact_status.
        assert donor.contact_status, "factory should set a contact_status"
        previous_changed_at = donor.contact_status_changed_at

        batch = create_phone_intake_batch(operator=operator, campaign=campaign)
        create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
            donor_updates={
                "donor_contact_status": donor.contact_status,
                "donor_contact_status_reason": donor.contact_status_reason or "",
            },
        )

        donor.refresh_from_db()
        assert donor.contact_status_changed_at == previous_changed_at, (
            "contact_status_changed_at must not be re-stamped when nothing changed"
        )

    def test_changed_contact_status_does_stamp_changed_at(self) -> None:
        """Sanity check: the equality guard in the regression above must
        not block legitimate status changes."""
        operator = UserFactory()
        campaign = CampaignFactory()
        donor = SystemDonorFactory(client=campaign.client)
        previous_changed_at = donor.contact_status_changed_at
        new_status = "deceased"
        assert donor.contact_status != new_status, (
            "test depends on factory not setting contact_status='deceased'"
        )

        batch = create_phone_intake_batch(operator=operator, campaign=campaign)
        create_phone_donation(
            operator=operator,
            campaign=campaign,
            batch=batch,
            donor=donor,
            payload=_payload(),
            donor_updates={
                "donor_contact_status": new_status,
                "donor_contact_status_reason": "Family informed us",
            },
        )

        donor.refresh_from_db()
        assert donor.contact_status == new_status
        assert donor.contact_status_reason == "Family informed us"
        assert donor.contact_status_changed_at != previous_changed_at
        assert donor.contact_status_changed_at is not None

    def test_empty_external_urn_does_not_trigger_phantom_save(self) -> None:
        """Regression: a SystemDonor with ``external_urn=""`` (empty
        string, the CharField default for donors created without a URN)
        used to flag ``external_urn`` as updated whenever the operator
        submitted an empty URN — because the URN normalisation collapsed
        ``""`` to ``None`` only on one side of the comparison, so
        ``None != ""`` was always True.

        After the fix both sides normalise the same way, so a no-op
        empty-vs-empty submission is correctly skipped.

        Tested at the ``apply_donor_field_updates`` boundary so the
        partial-payload behaviour of other field updaters (which is
        unrelated to the URN block) doesn't add noise.
        """
        from donors.updates import (
            PHONE_INTAKE_EDITABLE_FIELDS,
            apply_donor_field_updates,
        )

        donor = SystemDonorFactory(external_urn="")
        updated = apply_donor_field_updates(
            donor,
            {"donor_urn": ""},
            editable_fields=PHONE_INTAKE_EDITABLE_FIELDS & {"urn"},
        )
        assert updated == [], (
            "Empty URN against an empty stored URN must not flag external_urn "
            f"as updated; got {updated!r}"
        )

    def test_empty_external_urn_against_set_urn_still_clears(self) -> None:
        """Sanity: clearing a real URN (set → empty) must still apply."""
        from donors.updates import (
            PHONE_INTAKE_EDITABLE_FIELDS,
            apply_donor_field_updates,
        )

        donor = SystemDonorFactory(external_urn="SYS123")
        updated = apply_donor_field_updates(
            donor,
            {"donor_urn": ""},
            editable_fields=PHONE_INTAKE_EDITABLE_FIELDS & {"urn"},
        )
        donor.refresh_from_db()
        assert donor.external_urn == ""
        assert "external_urn" in updated
