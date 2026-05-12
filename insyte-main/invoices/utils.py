"""Helper function to calculate all invoice line item totals."""

from decimal import Decimal

from invoices.models import Invoice, InvoiceSettings


def _as_decimal(value: object) -> Decimal:
    """Normalize numeric values to Decimal.

    Args:
        value: Numeric value from settings/model fields.

    Returns:
        Decimal representation of the input value.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def calculate_all_line_totals(
    invoice: Invoice, settings: InvoiceSettings
) -> dict[str, Decimal]:
    """Calculate line totals for all invoice metrics.

    Args:
        invoice: Invoice instance
        settings: InvoiceSettings instance

    Returns:
        Dictionary of all line item totals
    """
    line_totals = {}

    # Core Donation Metrics
    if hasattr(settings, "price_per_donation"):
        line_totals["donation_line_total"] = (
            invoice.total_donations_captured * settings.price_per_donation
        )
    if hasattr(settings, "price_per_gift_aid"):
        line_totals["gift_aid_line_total"] = (
            invoice.gift_aid_captured * settings.price_per_gift_aid
        )

    # Campaign & Letter Metrics
    if hasattr(settings, "price_per_campaign"):
        line_totals["campaign_line_total"] = (
            invoice.campaigns_created * settings.price_per_campaign
        )
    if hasattr(settings, "price_per_campaign_field"):
        line_totals["campaign_field_line_total"] = (
            invoice.campaign_fields_configured * settings.price_per_campaign_field
        )
    if hasattr(settings, "price_per_letter"):
        line_totals["letter_line_total"] = (
            invoice.letters_generated * settings.price_per_letter
        )
    if hasattr(settings, "price_per_letter_batch"):
        line_totals["letter_batch_line_total"] = (
            invoice.letter_batches_created * settings.price_per_letter_batch
        )
    if hasattr(settings, "price_per_template"):
        line_totals["letter_template_line_total"] = (
            invoice.letter_templates_used * settings.price_per_template
        )

    # Donor Management Metrics
    if hasattr(settings, "price_per_donor_added"):
        line_totals["donor_added_line_total"] = (
            invoice.donors_added * settings.price_per_donor_added
        )
    if hasattr(settings, "price_per_donor_updated"):
        line_totals["donor_updated_line_total"] = (
            invoice.donors_updated * settings.price_per_donor_updated
        )
    if hasattr(settings, "price_per_donor_upload"):
        line_totals["donor_upload_line_total"] = (
            invoice.donor_uploads_processed * settings.price_per_donor_upload
        )
    if hasattr(settings, "price_per_donor_response"):
        line_totals["donor_response_line_total"] = (
            invoice.donor_responses_received * settings.price_per_donor_response
        )
    if hasattr(settings, "price_per_hgv"):
        line_totals["hgv_line_total"] = invoice.hgv_identified * settings.price_per_hgv
    if hasattr(settings, "price_per_lgv"):
        line_totals["lgv_line_total"] = invoice.lgv_identified * settings.price_per_lgv

    # Data Processing Metrics
    if hasattr(settings, "price_per_data_file"):
        line_totals["data_file_line_total"] = (
            invoice.data_files_processed * settings.price_per_data_file
        )
    if hasattr(settings, "price_per_data_file_upload"):
        line_totals["data_file_upload_line_total"] = (
            invoice.data_file_uploads * settings.price_per_data_file_upload
        )
    if hasattr(settings, "price_per_batch"):
        line_totals["batch_line_total"] = (
            invoice.batch_operations_completed * settings.price_per_batch
        )
    if hasattr(settings, "price_per_segment"):
        line_totals["segment_line_total"] = (
            invoice.segments_created * settings.price_per_segment
        )

    # Communication Metrics
    if hasattr(settings, "price_per_email"):
        line_totals["email_line_total"] = (
            invoice.email_notifications_sent * settings.price_per_email
        )
    if hasattr(settings, "price_per_sms"):
        line_totals["sms_line_total"] = (
            invoice.sms_notifications_sent * settings.price_per_sms
        )
    if hasattr(settings, "price_per_notification"):
        line_totals["notification_line_total"] = (
            invoice.donor_notifications_sent * settings.price_per_notification
        )

    # Export & Report Metrics
    if hasattr(settings, "price_per_report"):
        line_totals["report_line_total"] = (
            invoice.reports_generated * settings.price_per_report
        )
    if hasattr(settings, "price_per_pdf_export"):
        line_totals["pdf_export_line_total"] = (
            invoice.pdf_exports * settings.price_per_pdf_export
        )
    if hasattr(settings, "price_per_csv_export"):
        line_totals["csv_export_line_total"] = (
            invoice.csv_exports * settings.price_per_csv_export
        )
    if hasattr(settings, "price_per_excel_export"):
        line_totals["excel_export_line_total"] = (
            invoice.excel_exports * settings.price_per_excel_export
        )
    if hasattr(settings, "price_per_export"):
        line_totals["export_line_total"] = (
            invoice.export_operations * settings.price_per_export
        )

    # Workflow & Approval Metrics
    if hasattr(settings, "price_per_approval"):
        line_totals["approval_line_total"] = (
            invoice.approval_workflows_processed * settings.price_per_approval
        )
    if hasattr(settings, "price_per_audit_log"):
        line_totals["audit_log_line_total"] = (
            invoice.audit_logs_generated * settings.price_per_audit_log
        )

    # System & Administration Metrics
    if hasattr(settings, "price_per_template"):
        line_totals["template_line_total"] = (
            invoice.templates_created * settings.price_per_template
        )
    if hasattr(settings, "price_per_user"):
        line_totals["user_line_total"] = invoice.users_managed * settings.price_per_user
    if hasattr(settings, "price_per_portal_session"):
        line_totals["portal_session_line_total"] = (
            invoice.client_portal_sessions * settings.price_per_portal_session
        )
    if hasattr(settings, "price_per_package_code"):
        line_totals["package_code_line_total"] = (
            invoice.package_codes_used * settings.price_per_package_code
        )

    # API & Integration Metrics
    if hasattr(settings, "price_per_api_call"):
        line_totals["api_line_total"] = (
            invoice.api_calls_made * settings.price_per_api_call
        )
    if hasattr(settings, "price_per_address_lookup"):
        line_totals["address_lookup_line_total"] = (
            invoice.address_lookups * settings.price_per_address_lookup
        )

    # Storage & Resource Metrics
    if hasattr(settings, "price_per_mb_storage"):
        line_totals["storage_line_total"] = _as_decimal(
            invoice.storage_used_mb
        ) * _as_decimal(settings.price_per_mb_storage)
    if hasattr(settings, "price_per_db_query"):
        line_totals["db_query_line_total"] = _as_decimal(
            invoice.database_queries
        ) * _as_decimal(settings.price_per_db_query)

    return line_totals


def get_invoice_line_items(
    invoice: Invoice, settings: InvoiceSettings
) -> list[tuple[str, int, Decimal, Decimal]]:
    """Get list of line items for invoice with non-zero quantities.

    Args:
        invoice: Invoice instance
        settings: InvoiceSettings instance

    Returns:
        List of tuples (description, quantity, unit_price, total)
    """
    line_items = []

    # Core Donation Metrics
    if invoice.total_donations_captured > 0:
        line_items.append(
            (
                "Donations Captured",
                invoice.total_donations_captured,
                settings.price_per_donation,
                invoice.total_donations_captured * settings.price_per_donation,
            )
        )

    if invoice.gift_aid_captured > 0:
        line_items.append(
            (
                "Gift Aid Declarations",
                invoice.gift_aid_captured,
                settings.price_per_gift_aid,
                invoice.gift_aid_captured * settings.price_per_gift_aid,
            )
        )

    # Campaign & Letter Metrics
    if invoice.campaigns_created > 0:
        line_items.append(
            (
                "Campaigns Created",
                invoice.campaigns_created,
                settings.price_per_campaign,
                invoice.campaigns_created * settings.price_per_campaign,
            )
        )

    if invoice.campaign_fields_configured > 0 and hasattr(
        settings, "price_per_campaign_field"
    ):
        line_items.append(
            (
                "Campaign Fields Configured",
                invoice.campaign_fields_configured,
                settings.price_per_campaign_field,
                invoice.campaign_fields_configured * settings.price_per_campaign_field,
            )
        )

    if invoice.letters_generated > 0:
        line_items.append(
            (
                "Thank You Letters Generated",
                invoice.letters_generated,
                settings.price_per_letter,
                invoice.letters_generated * settings.price_per_letter,
            )
        )

    if invoice.letter_batches_created > 0 and hasattr(
        settings, "price_per_letter_batch"
    ):
        line_items.append(
            (
                "Letter Batches Created",
                invoice.letter_batches_created,
                settings.price_per_letter_batch,
                invoice.letter_batches_created * settings.price_per_letter_batch,
            )
        )

    if invoice.letter_templates_used > 0:
        line_items.append(
            (
                "Letter Templates Used",
                invoice.letter_templates_used,
                settings.price_per_template,
                invoice.letter_templates_used * settings.price_per_template,
            )
        )

    # Donor Management Metrics
    if invoice.donors_added > 0:
        line_items.append(
            (
                "New Donors Added",
                invoice.donors_added,
                settings.price_per_donor_added,
                invoice.donors_added * settings.price_per_donor_added,
            )
        )

    if invoice.donors_updated > 0:
        line_items.append(
            (
                "Donor Records Updated",
                invoice.donors_updated,
                settings.price_per_donor_updated,
                invoice.donors_updated * settings.price_per_donor_updated,
            )
        )

    if invoice.donor_uploads_processed > 0 and hasattr(
        settings, "price_per_donor_upload"
    ):
        line_items.append(
            (
                "Donor CSV Uploads",
                invoice.donor_uploads_processed,
                settings.price_per_donor_upload,
                invoice.donor_uploads_processed * settings.price_per_donor_upload,
            )
        )

    if invoice.hgv_identified > 0:
        line_items.append(
            (
                "High Gift Value (HGV) Donors",
                invoice.hgv_identified,
                settings.price_per_hgv,
                invoice.hgv_identified * settings.price_per_hgv,
            )
        )

    if invoice.lgv_identified > 0:
        line_items.append(
            (
                "Low Gift Value (LGV) Donors",
                invoice.lgv_identified,
                settings.price_per_lgv,
                invoice.lgv_identified * settings.price_per_lgv,
            )
        )

    # Data Processing Metrics
    if invoice.data_files_processed > 0:
        line_items.append(
            (
                "Data Files Processed",
                invoice.data_files_processed,
                settings.price_per_data_file,
                invoice.data_files_processed * settings.price_per_data_file,
            )
        )

    if invoice.data_file_uploads > 0 and hasattr(
        settings, "price_per_data_file_upload"
    ):
        line_items.append(
            (
                "File Upload Operations",
                invoice.data_file_uploads,
                settings.price_per_data_file_upload,
                invoice.data_file_uploads * settings.price_per_data_file_upload,
            )
        )

    if invoice.batch_operations_completed > 0:
        line_items.append(
            (
                "Batch Operations",
                invoice.batch_operations_completed,
                settings.price_per_batch,
                invoice.batch_operations_completed * settings.price_per_batch,
            )
        )

    if invoice.segments_created > 0 and hasattr(settings, "price_per_segment"):
        line_items.append(
            (
                "Donor Segments Created",
                invoice.segments_created,
                settings.price_per_segment,
                invoice.segments_created * settings.price_per_segment,
            )
        )

    # Communication Metrics
    if invoice.email_notifications_sent > 0 and hasattr(settings, "price_per_email"):
        line_items.append(
            (
                "Email Notifications",
                invoice.email_notifications_sent,
                settings.price_per_email,
                invoice.email_notifications_sent * settings.price_per_email,
            )
        )

    if invoice.sms_notifications_sent > 0 and hasattr(settings, "price_per_sms"):
        line_items.append(
            (
                "SMS Notifications",
                invoice.sms_notifications_sent,
                settings.price_per_sms,
                invoice.sms_notifications_sent * settings.price_per_sms,
            )
        )

    # Export & Report Metrics
    if invoice.reports_generated > 0:
        line_items.append(
            (
                "Reports Generated",
                invoice.reports_generated,
                settings.price_per_report,
                invoice.reports_generated * settings.price_per_report,
            )
        )

    if invoice.pdf_exports > 0 and hasattr(settings, "price_per_pdf_export"):
        line_items.append(
            (
                "PDF Exports",
                invoice.pdf_exports,
                settings.price_per_pdf_export,
                invoice.pdf_exports * settings.price_per_pdf_export,
            )
        )

    if invoice.csv_exports > 0 and hasattr(settings, "price_per_csv_export"):
        line_items.append(
            (
                "CSV Exports",
                invoice.csv_exports,
                settings.price_per_csv_export,
                invoice.csv_exports * settings.price_per_csv_export,
            )
        )

    if invoice.excel_exports > 0 and hasattr(settings, "price_per_excel_export"):
        line_items.append(
            (
                "Excel Exports",
                invoice.excel_exports,
                settings.price_per_excel_export,
                invoice.excel_exports * settings.price_per_excel_export,
            )
        )

    # Workflow & Approval Metrics
    if invoice.approval_workflows_processed > 0 and hasattr(
        settings, "price_per_approval"
    ):
        line_items.append(
            (
                "Approval Workflows",
                invoice.approval_workflows_processed,
                settings.price_per_approval,
                invoice.approval_workflows_processed * settings.price_per_approval,
            )
        )

    # System & Administration Metrics
    if invoice.templates_created > 0:
        line_items.append(
            (
                "Templates Created",
                invoice.templates_created,
                settings.price_per_template,
                invoice.templates_created * settings.price_per_template,
            )
        )

    if invoice.users_managed > 0:
        line_items.append(
            (
                "User Accounts Managed",
                invoice.users_managed,
                settings.price_per_user,
                invoice.users_managed * settings.price_per_user,
            )
        )

    if invoice.client_portal_sessions > 0 and hasattr(
        settings, "price_per_portal_session"
    ):
        line_items.append(
            (
                "Client Portal Sessions",
                invoice.client_portal_sessions,
                settings.price_per_portal_session,
                invoice.client_portal_sessions * settings.price_per_portal_session,
            )
        )

    # API & Integration Metrics
    if invoice.api_calls_made > 0:
        line_items.append(
            (
                "API Calls",
                invoice.api_calls_made,
                settings.price_per_api_call,
                invoice.api_calls_made * settings.price_per_api_call,
            )
        )

    if invoice.address_lookups > 0 and hasattr(settings, "price_per_address_lookup"):
        line_items.append(
            (
                "Address Validations",
                invoice.address_lookups,
                settings.price_per_address_lookup,
                invoice.address_lookups * settings.price_per_address_lookup,
            )
        )

    # Storage & Resource Metrics
    if invoice.storage_used_mb > 0:
        line_items.append(
            (
                "Storage Used (MB)",
                float(invoice.storage_used_mb),
                settings.price_per_mb_storage,
                invoice.storage_used_mb * settings.price_per_mb_storage,
            )
        )

    if invoice.database_queries > 0 and hasattr(settings, "price_per_db_query"):
        line_items.append(
            (
                "Database Queries",
                invoice.database_queries,
                settings.price_per_db_query,
                invoice.database_queries * settings.price_per_db_query,
            )
        )

    return line_items
