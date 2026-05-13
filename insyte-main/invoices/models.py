"""Invoice models for the donation management system."""

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from django.db import models

if TYPE_CHECKING:
    from django.db.models.manager import RelatedManager


class ServiceCategory(models.Model):
    """Service category for invoice line items.

    Hierarchical service organization (e.g., Set Ups, Letter Set Up, Account Management).
    Each category contains multiple service items with descriptions and pricing.

    Attributes:
        id: UUID primary key.
        name: Category name (e.g., "Set Ups", "Direct Mail Pack").
        description: Optional category description.
        is_active: Whether this category is active.
        order: Display order (lower numbers first).
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
        service_items: Reverse relation to ServiceItem model.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(
        max_length=255,
        unique=True,
        help_text="Service category name (e.g., Set Ups, Letter Set Up)",
    )
    description = models.TextField(
        blank=True, help_text="Optional description of the service category"
    )
    is_active = models.BooleanField(
        default=True, help_text="Whether this category is active"
    )
    order = models.PositiveIntegerField(
        default=0, help_text="Display order (lower numbers appear first)"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        pass

    class Meta:
        verbose_name = "Service Category"
        verbose_name_plural = "Service Categories"
        ordering = ["order", "name"]

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Category name.
        """
        return self.name


class ServiceItem(models.Model):
    """Individual service line item with pricing.

    Represents a specific service within a category with unit price and pricing unit.
    Used for building invoice line items with consistent pricing.

    Attributes:
        id: UUID primary key.
        category: Foreign key to ServiceCategory.
        description: Service description (e.g., "Payment Gateway integration").
        unit_price: Price per unit in GBP (£).
        pricing_unit: Pricing comment (e.g., "each", "per hour", "per month").
        is_active: Whether this service is active.
        is_default: Whether this appears in invoice by default.
        order: Display order within category.
        notes: Additional notes (e.g., "ie: SagePay, Barclay Card, WorldPay").
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(
        ServiceCategory, on_delete=models.CASCADE, related_name="service_items"
    )
    description = models.CharField(
        max_length=500,
        help_text="Service description (e.g., Payment Gateway integration)",
    )
    unit_price = models.DecimalField(
        max_digits=10, decimal_places=2, help_text="Unit price in GBP (£)"
    )
    pricing_unit = models.CharField(
        max_length=100,
        help_text="Pricing comment (e.g., each, per hour, per month, per GB per month)",
    )
    is_active = models.BooleanField(
        default=True, help_text="Whether this service is active"
    )
    is_default = models.BooleanField(
        default=False, help_text="Whether this should appear in invoice by default"
    )
    order = models.PositiveIntegerField(
        default=0, help_text="Display order within category"
    )
    notes = models.TextField(
        blank=True,
        help_text="Additional notes or details (e.g., ie: SagePay, Barclay Card, WorldPay)",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Service Item"
        verbose_name_plural = "Service Items"
        ordering = ["category", "order", "description"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "description"],
                name="unique_service_item_category_description",
            ),
        ]

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Category name and service description.
        """
        return f"{self.category.name} - {self.description}"


# Reverse relation for ServiceCategory — annotated here because ServiceItem
# must be defined before this annotation can be resolved by the type checker.
if TYPE_CHECKING:
    ServiceCategory.service_items: RelatedManager[ServiceItem]  # type: ignore[misc]  # noqa: B032


class InvoiceSettings(models.Model):
    """Default pricing settings for invoice generation.

    Singleton model that stores default prices for all service metrics
    used in invoice calculations. Only one instance should exist.

    Attributes:
        price_per_donation: Price per donation captured.
        price_per_gift_aid: Price per gift aid declaration.
        price_per_letter: Price per letter generated.
        price_per_campaign: Price per campaign created.
        price_per_hgv: Price per HGV identified.
        price_per_lgv: Price per LGV identified.
        price_per_donor_response: Price per donor response.
        price_per_letter_batch: Price per letter batch created.
        price_per_campaign_field: Price per campaign field configured.
        price_per_donor_upload: Price per donor CSV upload.
        price_per_segment: Price per donor segment created.
        price_per_email: Price per email notification.
        price_per_sms: Price per SMS notification.
        price_per_pdf_export: Price per PDF export.
        price_per_csv_export: Price per CSV export.
        price_per_excel_export: Price per Excel export.
        price_per_export: Price per export operation.
        price_per_approval: Price per approval workflow.
        price_per_audit_log: Price per audit log entry.
        price_per_portal_session: Price per portal session.
        price_per_package_code: Price per package code.
        price_per_address_lookup: Price per address lookup.
        price_per_data_file_upload: Price per data file upload.
        price_per_db_query: Price per database query.
        price_per_notification: Price per notification sent.
        price_per_data_file: Price per data file processed.
        price_per_batch: Price per batch operation.
        price_per_report: Price per report generated.
        price_per_template: Price per template created.
        price_per_user: Price per user managed.
        price_per_donor_added: Price per donor added.
        price_per_donor_updated: Price per donor updated.
        price_per_api_call: Price per API call.
        price_per_mb_storage: Price per MB storage.
        default_tax_rate: Default VAT/tax rate percentage.
        default_due_days: Default payment due days.
        company_name: Company name for invoices.
        company_address: Company address for invoices.
        company_phone: Company phone number.
        company_email: Company email address.
        company_vat_number: Company VAT registration number.
        company_logo_url: URL to company logo.
        updated_at: Timestamp when last updated.
        updated_by: User who last updated settings.
    """

    # Pricing per metric
    price_per_donation = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.05,
        help_text="Price per donation captured (£)",
    )
    price_per_gift_aid = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.10,
        help_text="Price per gift aid declaration (£)",
    )
    price_per_letter = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.25,
        help_text="Price per letter generated (£)",
    )
    price_per_campaign = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=5.00,
        help_text="Price per campaign created (£)",
    )
    price_per_hgv = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.50,
        help_text="Price per HGV identified (£)",
    )
    price_per_lgv = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.10,
        help_text="Price per LGV identified (£)",
    )
    price_per_donor_response = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.15,
        help_text="Price per donor response received (£)",
    )
    price_per_letter_batch = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=2.00,
        help_text="Price per letter batch created (£)",
    )
    price_per_campaign_field = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.50,
        help_text="Price per campaign field configured (£)",
    )
    price_per_donor_upload = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=1.50,
        help_text="Price per donor CSV upload (£)",
    )
    price_per_segment = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=3.00,
        help_text="Price per donor segment created (£)",
    )
    price_per_email = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.08,
        help_text="Price per email notification (£)",
    )
    price_per_sms = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.20,
        help_text="Price per SMS notification (£)",
    )
    price_per_pdf_export = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.30,
        help_text="Price per PDF export (£)",
    )
    price_per_csv_export = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.20,
        help_text="Price per CSV export (£)",
    )
    price_per_excel_export = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.25,
        help_text="Price per Excel export (£)",
    )
    price_per_export = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.15,
        help_text="Price per export operation (£)",
    )
    price_per_approval = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=1.00,
        help_text="Price per approval workflow (£)",
    )
    price_per_audit_log = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.05,
        help_text="Price per audit log entry (£)",
    )
    price_per_portal_session = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.10,
        help_text="Price per portal session (£)",
    )
    price_per_package_code = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.30,
        help_text="Price per package code (£)",
    )
    price_per_address_lookup = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.02,
        help_text="Price per address lookup (£)",
    )
    price_per_data_file_upload = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=2.50,
        help_text="Price per data file upload (£)",
    )
    price_per_db_query = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.001,
        help_text="Price per database query (£)",
    )
    price_per_notification = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.02,
        help_text="Price per notification sent (£)",
    )
    price_per_data_file = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=2.00,
        help_text="Price per data file processed (£)",
    )
    price_per_batch = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.50,
        help_text="Price per batch operation (£)",
    )
    price_per_report = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=1.00,
        help_text="Price per report generated (£)",
    )
    price_per_template = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=2.50,
        help_text="Price per template created (£)",
    )
    price_per_user = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=5.00,
        help_text="Price per user managed (£)",
    )
    price_per_donor_added = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.05,
        help_text="Price per donor added (£)",
    )
    price_per_donor_updated = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.02,
        help_text="Price per donor updated (£)",
    )
    price_per_api_call = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.001,
        help_text="Price per API call (£)",
    )
    price_per_mb_storage = models.DecimalField(
        max_digits=8,
        decimal_places=4,
        default=0.01,
        help_text="Price per MB storage used (£)",
    )

    # Default settings
    default_tax_rate = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=20.00,
        help_text="Default VAT/tax rate percentage",
    )
    default_due_days = models.PositiveIntegerField(
        default=30, help_text="Default payment due days from issue date"
    )

    # Company details for invoices
    company_name = models.CharField(
        max_length=255, default="INSYTE", help_text="Company name for invoices"
    )
    company_address = models.TextField(
        blank=True, default="", help_text="Company address for invoices"
    )
    company_phone = models.CharField(
        max_length=50, blank=True, default="", help_text="Company phone number"
    )
    company_email = models.EmailField(
        blank=True, default="", help_text="Company email address"
    )
    company_vat_number = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text="Company VAT registration number",
    )
    company_logo_url = models.URLField(
        blank=True, default="", help_text="URL to company logo for invoices"
    )

    # Metadata
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoice_settings_updates",
    )

    class Meta:
        verbose_name = "Invoice Settings"
        verbose_name_plural = "Invoice Settings"

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Static label.
        """
        return "Invoice Settings"

    def save(self, *args: object, **kwargs: object) -> None:
        """Ensure only one instance exists (singleton pattern)."""
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_settings(cls) -> InvoiceSettings:
        """Get or create the singleton settings instance.

        Returns:
            InvoiceSettings: The singleton settings instance.
        """
        settings, _ = cls.objects.get_or_create(pk=1)
        return settings


class Invoice(models.Model):
    """Professional invoice for client services and billing.

    Tracks comprehensive metrics for billing periods including donations,
    gift aid, letters, campaigns, and all service activities.

    Attributes:
        id: UUID primary key.
        invoice_number: Unique auto-generated invoice number.
        client: Foreign key to Client.
        campaign: Optional foreign key to Campaign.
        donations: Many-to-many to Donation.
        billing_period_start: Start of billing period.
        billing_period_end: End of billing period.
        issue_date: Invoice issue date.
        due_date: Payment due date.
        status: Invoice status.
        total_donations_captured: Total donations recorded.
        total_donation_amount: Total donation amount.
        gift_aid_captured: Gift aid declarations.
        gift_aid_amount: Gift aid amount.
        campaigns_created: New campaigns created.
        campaigns_updated: Campaign updates.
        campaign_fields_configured: Campaign fields configured.
        letters_generated: Thank you letters generated.
        letter_batches_created: Letter batch operations.
        letter_templates_used: Letter templates utilized.
        donors_added: New donors added.
        donors_updated: Donor records updated.
        donor_uploads_processed: Donor CSV uploads.
        donor_responses_received: Donor responses tracked.
        hgv_identified: High Gift Value donors.
        lgv_identified: Low Gift Value donors.
        data_files_processed: Data files imported.
        data_file_uploads: File upload operations.
        batch_operations_completed: Batch processing jobs.
        segments_created: Donor segments created.
        email_notifications_sent: Email notifications.
        sms_notifications_sent: SMS notifications.
        donor_notifications_sent: General notifications.
        reports_generated: Reports generated.
        pdf_exports: PDF exports.
        csv_exports: CSV exports.
        excel_exports: Excel exports.
        export_operations: Total export operations.
        approval_workflows_processed: Approval workflows.
        audit_logs_generated: Audit trail entries.
        templates_created: Templates configured.
        users_managed: User accounts managed.
        client_portal_sessions: Client portal logins.
        package_codes_used: Package codes utilized.
        api_calls_made: API calls executed.
        address_lookups: Address validation lookups.
        storage_used_mb: Storage used (MB).
        database_queries: Database queries executed.
        service_fee: Service fee amount.
        processing_fee: Processing fee amount.
        setup_fee: Setup fee amount.
        additional_charges: Additional charges amount.
        discount_amount: Discount amount.
        subtotal: Subtotal amount.
        tax_rate: Tax rate percentage.
        tax_amount: Tax amount.
        total_amount: Total invoice amount.
        amount_paid: Amount paid.
        balance_due: Balance due.
        payment_method: Payment method used.
        payment_reference: Payment reference.
        payment_date: Payment date.
        notes: Additional notes.
        terms_and_conditions: Terms and conditions.
        created_by: User who created the invoice.
        created_at: Timestamp when created.
        updated_at: Timestamp when last updated.
    """

    STATUS_DRAFT = "draft"
    STATUS_ISSUED = "issued"
    STATUS_PAID = "paid"
    STATUS_OVERDUE = "overdue"
    STATUS_CANCELLED = "cancelled"

    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_ISSUED, "Issued"),
        (STATUS_PAID, "Paid"),
        (STATUS_OVERDUE, "Overdue"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    PAYMENT_METHOD_CHOICES = [
        ("credit_card", "Credit Card"),
        ("debit_card", "Debit Card"),
        ("cheque", "Cheque"),
        ("cash", "Cash"),
        ("other", "Other"),
    ]

    # Basic Information
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice_number = models.CharField(max_length=50, unique=True, db_index=True)
    client = models.ForeignKey(
        "clients.Client", on_delete=models.PROTECT, related_name="invoices"
    )
    campaign = models.ForeignKey(
        "campaigns.Campaign",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
        help_text="Optional: Generate invoice for specific campaign only",
    )

    # Link to specific donations included in this invoice
    donations = models.ManyToManyField(
        "donations.Donation",
        blank=True,
        related_name="invoices",
        help_text="Specific donations included in this invoice for detailed line items",
    )

    # Billing Period
    billing_period_start = models.DateField()
    billing_period_end = models.DateField()
    issue_date = models.DateField(auto_now_add=True)
    due_date = models.DateField()
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True
    )

    # Service Metrics (35+ comprehensive metrics)
    # Core Donation Metrics
    total_donations_captured = models.PositiveIntegerField(
        default=0, help_text="Total donations recorded"
    )
    total_donation_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Total donation amount (£)",
    )
    gift_aid_captured = models.PositiveIntegerField(
        default=0, help_text="Gift aid declarations"
    )
    gift_aid_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text="Gift aid amount (£)"
    )

    # Campaign & Letter Metrics
    campaigns_created = models.PositiveIntegerField(
        default=0, help_text="New campaigns created"
    )
    campaigns_updated = models.PositiveIntegerField(
        default=0, help_text="Campaign updates"
    )
    campaign_fields_configured = models.PositiveIntegerField(
        default=0, help_text="Campaign fields configured"
    )
    letters_generated = models.PositiveIntegerField(
        default=0, help_text="Thank you letters generated"
    )
    letter_batches_created = models.PositiveIntegerField(
        default=0, help_text="Letter batch operations"
    )
    letter_templates_used = models.PositiveIntegerField(
        default=0, help_text="Letter templates utilized"
    )

    # Donor Management Metrics
    donors_added = models.PositiveIntegerField(default=0, help_text="New donors added")
    donors_updated = models.PositiveIntegerField(
        default=0, help_text="Donor records updated"
    )
    donor_uploads_processed = models.PositiveIntegerField(
        default=0, help_text="Donor CSV uploads"
    )
    donor_responses_received = models.PositiveIntegerField(
        default=0, help_text="Donor responses tracked"
    )
    hgv_identified = models.PositiveIntegerField(
        default=0, help_text="High Gift Value donors"
    )
    lgv_identified = models.PositiveIntegerField(
        default=0, help_text="Low Gift Value donors"
    )

    # Data Processing Metrics
    data_files_processed = models.PositiveIntegerField(
        default=0, help_text="Data files imported"
    )
    data_file_uploads = models.PositiveIntegerField(
        default=0, help_text="File upload operations"
    )
    batch_operations_completed = models.PositiveIntegerField(
        default=0, help_text="Batch processing jobs"
    )
    segments_created = models.PositiveIntegerField(
        default=0, help_text="Donor segments created"
    )

    # Communication Metrics
    email_notifications_sent = models.PositiveIntegerField(
        default=0, help_text="Email notifications"
    )
    sms_notifications_sent = models.PositiveIntegerField(
        default=0, help_text="SMS notifications"
    )
    donor_notifications_sent = models.PositiveIntegerField(
        default=0, help_text="General notifications"
    )

    # Export & Report Metrics
    reports_generated = models.PositiveIntegerField(
        default=0, help_text="Reports generated"
    )
    pdf_exports = models.PositiveIntegerField(default=0, help_text="PDF exports")
    csv_exports = models.PositiveIntegerField(default=0, help_text="CSV exports")
    excel_exports = models.PositiveIntegerField(default=0, help_text="Excel exports")
    export_operations = models.PositiveIntegerField(
        default=0, help_text="Total export operations"
    )

    # Workflow & Approval Metrics
    approval_workflows_processed = models.PositiveIntegerField(
        default=0, help_text="Approval workflows"
    )
    audit_logs_generated = models.PositiveIntegerField(
        default=0, help_text="Audit trail entries"
    )

    # System & Administration Metrics
    templates_created = models.PositiveIntegerField(
        default=0, help_text="Templates configured"
    )
    users_managed = models.PositiveIntegerField(
        default=0, help_text="User accounts managed"
    )
    client_portal_sessions = models.PositiveIntegerField(
        default=0, help_text="Client portal logins"
    )
    package_codes_used = models.PositiveIntegerField(
        default=0, help_text="Package codes utilized"
    )

    # API & Integration Metrics
    api_calls_made = models.PositiveIntegerField(
        default=0, help_text="API calls executed"
    )
    address_lookups = models.PositiveIntegerField(
        default=0, help_text="Address validation lookups"
    )

    # Storage & Resource Metrics
    storage_used_mb = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Storage used (MB)",
    )
    database_queries = models.PositiveIntegerField(
        default=0, help_text="Database queries executed"
    )

    # Financial Details
    service_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    processing_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    setup_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    additional_charges = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    discount_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    subtotal = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00")
    )
    tax_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    total_amount = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    amount_paid = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )
    balance_due = models.DecimalField(
        max_digits=10, decimal_places=2, default=Decimal("0.00")
    )

    # Payment Information
    payment_method = models.CharField(
        max_length=50, choices=PAYMENT_METHOD_CHOICES, blank=True
    )
    payment_reference = models.CharField(max_length=255, blank=True)
    payment_date = models.DateField(null=True, blank=True)

    # Additional Information
    notes = models.TextField(blank=True)
    terms_and_conditions = models.TextField(
        blank=True,
        default="Payment is due within 30 days of invoice date. Late payments may incur additional charges.",
    )

    # Metadata
    created_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_invoices",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issue_date", "-invoice_number"]
        verbose_name = "Invoice"
        verbose_name_plural = "Invoices"
        indexes = [
            models.Index(fields=["client", "-issue_date"]),
            models.Index(fields=["status", "-issue_date"]),
        ]

    def __str__(self) -> str:
        """Return string representation.

        Returns:
            str: Invoice number and client name.
        """
        return f"Invoice {self.invoice_number} - {self.client.name}"

    def save(self, *args: object, **kwargs: object) -> None:
        """Override save to prepare invoice fields via InvoiceService.

        The entire prepare step (including invoice-number allocation with
        database locks) runs in ``transaction.atomic()`` so numbering and the
        ``INSERT``/``UPDATE`` share one transaction --- required for PostgreSQL
        ``pg_advisory_xact_lock`` and ``SELECT … FOR UPDATE`` to protect
        against duplicate ``invoice_number`` values under concurrency.
        """
        from django.db import transaction

        from invoices.services import InvoiceService

        with transaction.atomic():
            InvoiceService.prepare_for_save(self)
            super().save(*args, **kwargs)
