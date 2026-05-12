"""Management command to anonymise donor data for GDPR right-to-erasure requests.

Usage:
    uv run python manage.py gdpr_erase_donor <URN> [--dry-run] [--reason "..."]

This command:
    1. Anonymises all PII fields on the Donor record.
    2. Anonymises linked DataFileDonor records.
    3. Clears sensitive payment fields on associated donations.
    4. Detaches/deletes any ``StripeCustomer`` rows linked to the donor so
       the local Stripe customer cache no longer carries PII (the remote
       Stripe customer must be deleted via the Stripe dashboard / API by
       an operator separately — that side-effect is out of scope for a
       sandbox-safe management command).
    5. Records every R2 prefix attached to the donor's donations / scans /
       letters in the audit-log row so an operator can purge the media.
    6. Writes an ``EXPORT``-action AuditLog entry summarising the erasure.
    7. Retains donation amounts, campaign links, and financial aggregates
       for legal/tax reporting obligations (UK Gift Aid requires 6-year
       retention).
"""

import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    """Anonymise a donor's PII in compliance with GDPR Article 17."""

    help = (
        "Anonymise donor PII for GDPR right-to-erasure (retains financial aggregates)"
    )

    def add_arguments(self, parser: object) -> None:
        """Define command arguments.

        Args:
            parser: Argument parser instance.
        """
        parser.add_argument("urn", type=str, help="Donor URN to anonymise")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without saving",
        )
        parser.add_argument(
            "--reason",
            type=str,
            default="GDPR Article 17 erasure request",
            help="Reason for erasure (stored in audit log)",
        )
        parser.add_argument(
            "--no-confirm",
            action="store_true",
            help=(
                "Skip the interactive 'ERASE' confirmation prompt. "
                "Intended for scripted / CI use; an interactive operator "
                "should answer the prompt instead."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the GDPR erasure.

        Args:
            *args: Positional arguments (unused).
            **options: Parsed command options.
        """
        from audit.models import AuditLog
        from donations.models import Donation
        from donors.models import DataFileDonor, Donor
        from payments.models import StripeCustomer

        urn = options["urn"]
        dry_run = options["dry_run"]
        reason = options["reason"]
        no_confirm = options["no_confirm"]

        try:
            donor = Donor.objects.get(urn=urn)
        except Donor.DoesNotExist:
            raise CommandError(f"Donor with URN '{urn}' not found.") from None

        self.stdout.write(f"\nDonor: {donor.full_name} ({urn})")
        self.stdout.write(f"Email: {donor.email}")
        self.stdout.write(f"Donations: {donor.donations.count()}")
        self.stdout.write(
            f"StripeCustomer rows: {StripeCustomer.objects.filter(donor=donor).count()}"
        )

        # Find linked data file donors
        data_file_donors = DataFileDonor.objects.filter(
            email=donor.email
        ) | DataFileDonor.objects.filter(
            first_name=donor.first_name,
            last_name=donor.last_name,
            postcode=donor.postcode,
        )
        self.stdout.write(f"Linked DataFileDonor records: {data_file_donors.count()}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n[DRY RUN] No changes saved."))
            self._preview_changes(donor, data_file_donors)
            return

        if not no_confirm:
            confirm = input(
                f"\nThis will permanently anonymise all PII for donor '{urn}'. "
                "Type 'ERASE' to confirm: "
            )
            if confirm != "ERASE":
                self.stdout.write(self.style.ERROR("Aborted."))
                return

        anon_id = uuid.uuid4().hex[:8]

        with transaction.atomic():
            # 1. Anonymise Donor record
            donor.title = ""
            donor.first_name = "ERASED"
            donor.last_name = f"DONOR-{anon_id}"
            donor.email = ""
            donor.phone = ""
            donor.address_line1 = ""
            donor.address_line2 = ""
            donor.city = ""
            donor.county = ""
            donor.postcode = ""
            donor.country = ""
            donor.date_of_birth = None
            donor.age = None
            donor.consent_contact = False
            donor.opt_in_email = False
            donor.opt_in_sms = False
            donor.opt_in_phone = False
            donor.opt_in_post = False
            donor.save()

            # 2. Anonymise linked DataFileDonor records
            for dfd in data_file_donors:
                dfd.title = ""
                dfd.first_name = "ERASED"
                dfd.last_name = f"DONOR-{anon_id}"
                dfd.email = ""
                dfd.phone = ""
                dfd.address_line1 = ""
                dfd.address_line2 = ""
                dfd.city = ""
                dfd.county = ""
                dfd.postcode = ""
                dfd.save()

            # 3. Clear sensitive payment fields on donations
            donations = Donation.objects.filter(donor=donor)
            for donation in donations:
                donation.card_holder_name = ""
                donation.card_last_four = ""
                donation.card_expiry_date = ""
                donation.cheque_number = ""
                donation.save(
                    update_fields=[
                        "card_holder_name",
                        "card_last_four",
                        "card_expiry_date",
                        "cheque_number",
                    ]
                )

            # Also clear donations linked via DataFileDonor
            dfd_donations = Donation.objects.filter(
                data_file_donor__in=data_file_donors
            )
            for donation in dfd_donations:
                donation.card_holder_name = ""
                donation.card_last_four = ""
                donation.card_expiry_date = ""
                donation.cheque_number = ""
                donation.save(
                    update_fields=[
                        "card_holder_name",
                        "card_last_four",
                        "card_expiry_date",
                        "cheque_number",
                    ]
                )

            # 4. Detach StripeCustomer rows (audit 2026-05-02 §7.2). The local
            # row holds an email + name copy of the donor, so we delete it
            # entirely — the Stripe-side customer must be removed via the
            # dashboard or a separate API call by an operator.
            stripe_customers = StripeCustomer.objects.filter(donor=donor)
            stripe_customer_ids = list(
                stripe_customers.values_list("stripe_customer_id", flat=True)
            )
            stripe_customer_count = stripe_customers.count()
            stripe_customers.delete()

            # 5. Collect R2 / media prefixes for operator-confirmed cleanup.
            # We do not delete R2 objects inline — that requires network IO
            # and credentials we don't want to take a runtime dependency on
            # from a management command. The audit row records exactly what
            # needs to be purged so an operator (or a follow-up sweep task)
            # can do it.
            r2_targets: list[dict[str, str]] = []
            for donation in donations:
                scan_batch = getattr(donation, "scan_batch", None)
                if scan_batch is not None and getattr(scan_batch, "r2_prefix", None):
                    r2_targets.append(
                        {
                            "kind": "scan_batch",
                            "donation_id": str(donation.pk),
                            "r2_prefix": scan_batch.r2_prefix,
                        }
                    )
                letter_pdf = getattr(donation, "letter_pdf", None)
                if letter_pdf:
                    r2_targets.append(
                        {
                            "kind": "letter_pdf",
                            "donation_id": str(donation.pk),
                            "r2_key": str(letter_pdf),
                        }
                    )

            # 6. Audit log entry (audit 2026-05-02 §7.2). ``EXPORT`` is the
            # closest fit in the existing ACTION_CHOICES — GDPR erasure is
            # an externally-visible operator action that crosses tenant /
            # data-protection boundaries, which is what EXPORT was added for.
            AuditLog.objects.create(
                action="EXPORT",
                model_name="Donor",
                object_id=urn,
                object_repr=f"ERASED DONOR-{anon_id}",
                summary=f"GDPR erasure: {reason}"[:1000],
                changes={
                    "kind": "GDPR_ERASURE",
                    "reason": reason,
                    "anon_id": anon_id,
                    "erased_fields": [
                        "first_name",
                        "last_name",
                        "email",
                        "phone",
                        "address",
                        "date_of_birth",
                        "payment_details",
                    ],
                    "donations_cleared": donations.count() + dfd_donations.count(),
                    "data_file_donors_cleared": data_file_donors.count(),
                    "stripe_customers_deleted": stripe_customer_count,
                    "stripe_customer_ids_for_remote_purge": stripe_customer_ids,
                    "r2_targets_for_operator_purge": r2_targets,
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSuccessfully erased PII for donor '{urn}' "
                f"(anonymised as DONOR-{anon_id}). "
                f"Donations: {donations.count()}, "
                f"DataFileDonors: {data_file_donors.count()}"
            )
        )

    def _preview_changes(self, donor: object, data_file_donors: object) -> None:
        """Preview what fields would be anonymised.

        Args:
            donor: Donor instance to preview.
            data_file_donors: QuerySet of linked DataFileDonor records.
        """
        self.stdout.write("\nFields to be anonymised on Donor:")
        fields = [
            "title",
            "first_name",
            "last_name",
            "email",
            "phone",
            "address_line1",
            "address_line2",
            "city",
            "county",
            "postcode",
            "country",
            "date_of_birth",
            "age",
        ]
        for field in fields:
            value = getattr(donor, field, "")
            if value:
                self.stdout.write(f"  {field}: {value} → [ERASED]")

        self.stdout.write("\nDonation payment fields to be cleared:")
        donation_count = donor.donations.count()
        self.stdout.write(
            f"  {donation_count} donation(s) will have payment details cleared"
        )

        self.stdout.write(
            f"\n{data_file_donors.count()} DataFileDonor record(s) will be anonymised"
        )
