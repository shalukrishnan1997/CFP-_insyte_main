"""Test factories for the INSYTE DMS test suite.

Uses factory_boy to create model instances for testing.
All factories are importable from ``tests.factories``.

Usage:
    from tests.factories import UserFactory, CampaignFactory
    user = UserFactory()
    campaign = CampaignFactory(status="active")
"""

import uuid
from decimal import Decimal

import factory
from django.utils import timezone
from factory.declarations import (
    LazyAttribute,
    LazyFunction,
    SelfAttribute,
    Sequence,
    SubFactory,
)
from factory.faker import Faker


class UserFactory(factory.django.DjangoModelFactory):
    """Factory for creating User instances.

    Creates a staff user by default with a unique username and email.
    """

    class Meta:
        model = "core.User"
        django_get_or_create = ("username",)
        skip_postgeneration_save = True

    username = Sequence(lambda n: f"testuser-{n}")
    email = LazyAttribute(lambda o: f"{o.username}@test.insyte.local")
    first_name = Faker("first_name")
    last_name = Faker("last_name")
    is_staff = True
    is_active = True
    password = factory.django.Password("testpass123!")


class ClientFactory(factory.django.DjangoModelFactory):
    """Factory for creating Client instances."""

    class Meta:
        model = "clients.Client"

    name = Sequence(lambda n: f"Test Charity {n}")
    email = LazyAttribute(lambda o: f"contact@{o.name.lower().replace(' ', '')}.org")
    phone = "020 7946 0958"
    is_active = True
    city = "London"
    country = "United Kingdom"


class CampaignFactory(factory.django.DjangoModelFactory):
    """Factory for creating Campaign instances.

    Creates a draft campaign linked to a client by default.
    """

    class Meta:
        model = "campaigns.Campaign"

    client = SubFactory(ClientFactory)
    name = Sequence(lambda n: f"Test Campaign {n}")
    description = "Test campaign for unit tests"
    status = "draft"
    campaign_temperature = "cold"
    appeal_type = "Donation"
    appeal_start = LazyFunction(timezone.now)
    appeal_end = LazyFunction(lambda: timezone.now() + timezone.timedelta(days=30))
    created_by = SubFactory(UserFactory)
    hgv_amount = Decimal("1000.00")
    lgv_amount = Decimal("10.00")


class DonorFactory(factory.django.DjangoModelFactory):
    """Factory for creating Donor instances."""

    class Meta:
        model = "donors.Donor"

    urn = Sequence(lambda n: f"URN{n:06d}")
    title = "Mr"
    first_name = Faker("first_name")
    last_name = Faker("last_name")
    email = LazyAttribute(
        lambda o: f"{o.first_name.lower()}.{o.last_name.lower()}@test.local"
    )
    phone = "07700 900000"
    address_line1 = Faker("street_address")
    city = "London"
    postcode = "SW1A 1AA"
    country = "United Kingdom"
    consent_contact = True
    opt_in_email = True
    created_by = SubFactory(UserFactory)


class SystemDonorFactory(factory.django.DjangoModelFactory):
    """Factory for creating SystemDonor instances."""

    class Meta:
        model = "donors.SystemDonor"

    client = SubFactory(ClientFactory)
    external_urn = Sequence(lambda n: f"SYSURN{n:06d}")
    title = "Mr"
    first_name = Faker("first_name")
    last_name = Faker("last_name")
    email = LazyAttribute(
        lambda o: f"{o.first_name.lower()}.{o.last_name.lower()}@system.test"
    )
    phone = "07700 900000"
    address_line1 = Faker("street_address")
    city = "London"
    postcode = "SW1A 1AA"
    country = "United Kingdom"
    consent_contact = True
    opt_in_email = True
    source_snapshot = LazyFunction(dict)
    created_by = SubFactory(UserFactory)


class DonationBatchFactory(factory.django.DjangoModelFactory):
    """Factory for creating DonationBatch instances."""

    class Meta:
        model = "donations.DonationBatch"

    campaign = SubFactory(CampaignFactory)
    batch_name = Sequence(lambda n: f"BATCH-{n:05d}")
    status = "pending_qa"
    default_payment_method = "card"
    default_currency = "GBP"
    created_by = SubFactory(UserFactory)
    total_donations = 0
    total_amount = Decimal("0.00")


def _build_system_donor_for_donation(resolver: object) -> object:
    """Return a stable system donor for a donation factory instance."""
    from donors.models import SystemDonor

    campaign = resolver.campaign
    donor = getattr(resolver, "donor", None)
    data_file_donor = getattr(resolver, "data_file_donor", None)

    source = donor or data_file_donor
    external_urn = str(getattr(source, "urn", "") or "").strip()
    defaults = {
        "title": getattr(source, "title", "") or "Mr",
        "first_name": getattr(source, "first_name", "") or "John",
        "last_name": getattr(source, "last_name", "") or "Doe",
        "email": getattr(source, "email", "")
        or f"system-donor-{uuid.uuid4()}@test.local",
        "phone": getattr(source, "phone", "") or "07700 900000",
        "address_line1": getattr(source, "address_line1", "") or "10 High Street",
        "city": getattr(source, "city", "") or "London",
        "postcode": getattr(source, "postcode", "") or "SW1A 1AA",
        "country": getattr(source, "country", "") or "United Kingdom",
        "created_by": getattr(source, "created_by", None) or UserFactory(),
    }

    if external_urn:
        system_donor, _ = SystemDonor.objects.get_or_create(
            client=campaign.client,
            external_urn=external_urn,
            defaults=defaults,
        )
        return system_donor

    return SystemDonorFactory(
        client=campaign.client,
        external_urn=f"TEMP-{uuid.uuid4()}",
        **defaults,
    )


class DonationFactory(factory.django.DjangoModelFactory):
    """Factory for creating Donation instances.

    Creates a donation linked to a campaign, batch, and donor.
    """

    class Meta:
        model = "donations.Donation"

    campaign = SubFactory(CampaignFactory)
    batch = SubFactory(
        DonationBatchFactory,
        campaign=SelfAttribute("..campaign"),
    )
    donor_source = "house_file"
    donor = SubFactory(DonorFactory)
    system_donor = LazyAttribute(_build_system_donor_for_donation)
    amount = Decimal("25.00")
    currency = "GBP"
    payment_method = "card"
    donation_date = LazyFunction(timezone.now)
    gift_aid = False
    qa_status = "pending"
    filled_by = SubFactory(UserFactory)


class PayingInSlipFactory(factory.django.DjangoModelFactory):
    """Factory for creating PayingInSlip instances."""

    class Meta:
        model = "banking.PayingInSlip"

    slip_number = Sequence(lambda n: f"PIS-{n:06d}")
    client = SubFactory(ClientFactory)
    payment_type = "mixed"
    banking_date = LazyFunction(timezone.now().date)
    total_amount = Decimal("0.00")
    total_items = 0
    status = "draft"
    notes = ""
    banked_at = None
    banked_by = None
    bank_processed_date = None
    processed_amount = None
    completion_status = ""
    processing_issues = LazyFunction(list)
    custom_issue = ""
    processed_by = None
    processed_at = None
    created_by = SubFactory(UserFactory)


class ScanBatchFactory(factory.django.DjangoModelFactory):
    """Factory for creating ScanBatch instances."""

    class Meta:
        model = "scans.ScanBatch"

    campaign = SubFactory(CampaignFactory)
    batch_name = Sequence(lambda n: f"SCAN-BATCH-{n:05d}")
    payment_method = "cheque"
    scan_form_type = "simplex_with_payment"
    status = "pending"
    created_by = SubFactory(UserFactory)


class ScanPlaceholderFactory(factory.django.DjangoModelFactory):
    """Factory for creating ScanPlaceholder instances.

    Represents a scanned form linked to a ScanBatch and optionally a Donation.
    ``image_url`` defaults to a realistic R2 public URL pattern.
    """

    class Meta:
        model = "scans.ScanPlaceholder"

    batch = SubFactory(ScanBatchFactory)
    image_url = Sequence(
        lambda n: f"https://cdn.example.com/ScanOutput/appeal/cheque/URN{n:06d}.jpg"
    )
    image_path = Sequence(lambda n: f"ScanOutput/appeal/cheque/URN{n:06d}.jpg")
    urn = Sequence(lambda n: f"URN{n:06d}")
    ocr_status = "matched"
    is_captured = True


class StripeCustomerFactory(factory.django.DjangoModelFactory):
    """Factory for creating StripeCustomer instances."""

    class Meta:
        model = "payments.StripeCustomer"

    stripe_customer_id = Sequence(lambda n: f"cus_test_{n:08d}")
    client = SubFactory(ClientFactory)
    donor = None
    email = LazyAttribute(
        lambda o: o.client.email if o.client else (o.donor.email if o.donor else "")
    )
    name = LazyAttribute(
        lambda o: (
            o.client.name
            if o.client
            else (
                f"{o.donor.first_name} {o.donor.last_name}".strip() if o.donor else ""
            )
        )
    )
    phone = ""
    metadata = LazyFunction(dict)


class StripePaymentMethodFactory(factory.django.DjangoModelFactory):
    """Factory for creating StripePaymentMethod instances."""

    class Meta:
        model = "payments.StripePaymentMethod"

    stripe_payment_method_id = Sequence(lambda n: f"pm_test_{n:08d}")
    stripe_customer = SubFactory(StripeCustomerFactory)
    type = "card"
    card_brand = "visa"
    card_last4 = "4242"
    card_exp_month = 12
    card_exp_year = 2030
    is_default = True


class StripePaymentFactory(factory.django.DjangoModelFactory):
    """Factory for creating StripePayment instances."""

    class Meta:
        model = "payments.StripePayment"

    stripe_payment_intent_id = Sequence(lambda n: f"pi_test_{n:08d}")
    stripe_charge_id = ""
    stripe_checkout_session_id = ""
    stripe_customer = SubFactory(StripeCustomerFactory)
    stripe_payment_method = SubFactory(StripePaymentMethodFactory)
    invoice = None
    donation = None
    amount = Decimal("100.00")
    currency = "GBP"
    status = "pending"
    amount_refunded = Decimal("0.00")
    refund_reason = ""
    retry_count = 0
    max_retries = 3
    next_retry_at = None
    last_retry_at = None
    stripe_response = LazyFunction(dict)
    error_message = ""
    error_code = ""
    error_type = ""
    description = "Test Stripe payment"
    metadata = LazyFunction(dict)
    processed_by = SubFactory(UserFactory)


class PaymentGatewayConfigFactory(factory.django.DjangoModelFactory):
    """Factory for creating PaymentGatewayConfig instances."""

    class Meta:
        model = "payments.PaymentGatewayConfig"

    client = SubFactory(ClientFactory)
    provider = "stripe"
    is_active = True
    publishable_key_encrypted = "pk_test_123"
    secret_key_encrypted = "sk_test_123"
    webhook_secret_encrypted = "whsec_test_123"
