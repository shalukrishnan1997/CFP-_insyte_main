from django.urls import include, path

from scans import api_views as scan_api_views

from . import api_views, views

app_name = "custom_admin"

urlpatterns = [
    # Dashboard
    path("", views.admin_dashboard, name="admin_dashboard"),
    # Campaigns list / management
    path("campaigns/", views.admin_campaigns, name="admin_campaigns"),
    path("campaigns/create/", views.campaign_create, name="campaign_create"),
    path(
        "campaigns/import-sample/",
        views.download_campaign_import_sample,
        name="campaign_import_sample",
    ),
    path(
        "campaigns/<uuid:campaign_id>/edit/", views.campaign_edit, name="campaign_edit"
    ),
    # Donations (batch list / detail — scan-only workflow)
    path(
        "donations/<uuid:client_id>/<uuid:campaign_id>/",
        views.donation_batch_list,
        name="donation_batch_list",
    ),
    path(
        "donations/<uuid:client_id>/<uuid:campaign_id>/batch/<int:batch_id>/",
        views.donation_batch_detail,
        name="donation_batch_detail",
    ),
    path(
        "donations/<uuid:client_id>/<uuid:campaign_id>/batch/<int:batch_id>/delete/",
        views.donation_batch_delete,
        name="donation_batch_delete",
    ),
    # Phone donation intake (operator console + JSON endpoints)
    path(
        "phone-intake/",
        views.phone_intake_console,
        name="phone_intake_console",
    ),
    path(
        "phone-intake/help/",
        views.phone_intake_help,
        name="phone_intake_help",
    ),
    path(
        "phone-intake/donor/create/",
        views.phone_intake_create_system_donor,
        name="phone_intake_create_system_donor",
    ),
    path(
        "phone-intake/donation/create/",
        views.phone_intake_create_donation,
        name="phone_intake_create_donation",
    ),
    path(
        "phone-intake/charge/",
        views.phone_intake_charge,
        name="phone_intake_charge",
    ),
    path(
        "phone-intake/refund/",
        views.phone_intake_refund,
        name="phone_intake_refund",
    ),
    # API endpoints - specific endpoints must come before include
    path("api/donors/search/", api_views.donor_search, name="donor_search"),
    path(
        "api/address-lookup/",
        api_views.address_lookup_proxy,
        name="address_lookup_proxy",
    ),
    path(
        "api/scanned-form/lookup/",
        api_views.scanned_form_lookup,
        name="scanned_form_lookup",
    ),
    path(
        "scan-processing/view/",
        scan_api_views.scan_placeholder_view,
        name="scan_placeholder_view",
    ),
    # Scan Processing API endpoints
    path(
        "api/scan-processing/create/",
        scan_api_views.scan_batch_create,
        name="scan_batch_create",
    ),
    path(
        "api/scan-processing/process/",
        scan_api_views.scan_batch_process,
        name="scan_batch_process",
    ),
    path(
        "api/scan-processing/status/",
        scan_api_views.scan_batch_status,
        name="scan_batch_status",
    ),
    path(
        "api/scan-processing/list/",
        scan_api_views.scan_batch_list,
        name="scan_batch_list_api",
    ),
    path(
        "api/scan-processing/placeholders/",
        scan_api_views.scan_placeholder_list,
        name="scan_placeholder_list",
    ),
    path(
        "api/scan-processing/retry/",
        scan_api_views.scan_retry_failed,
        name="scan_retry_failed",
    ),
    path(
        "api/scan-processing/retry-single/",
        scan_api_views.scan_retry_single,
        name="scan_retry_single",
    ),
    path(
        "api/scan-processing/placeholder-pdf/",
        scan_api_views.scan_placeholder_pdf,
        name="scan_placeholder_pdf",
    ),
    path(
        "api/scan-processing/image/",
        scan_api_views.scan_image_serve,
        name="scan_image_serve",
    ),
    # Scan Processing views
    path(
        "scan-processing/",
        views.scan_processing_dashboard,
        name="scan_processing_dashboard",
    ),
    path(
        "scan-processing/new-batch/",
        views.scan_new_batch_view,
        name="scan_new_batch",
    ),
    path(
        "scan-processing/<uuid:scan_batch_id>/redacted-upload/",
        views.scan_redacted_pages_upload,
        name="scan_redacted_pages_upload",
    ),
    path(
        "scan-processing/<uuid:scan_batch_id>/",
        views.scan_batch_detail_view,
        name="scan_batch_detail_view",
    ),
    # REST API for bulk upload and donor management
    path("api/", include("custom_admin.api_urls")),
    # QA Review routes
    path("qa/", views.qa_dashboard, name="qa_dashboard"),
    path("qa/batch/<int:batch_id>/", views.qa_batch_review, name="qa_batch_review"),
    path(
        "qa/batch/<int:batch_id>/donation/<uuid:donation_id>/",
        views.qa_single_donation_review,
        name="qa_single_donation_review",
    ),
    path(
        "qa/batch/<int:batch_id>/donation/<uuid:donation_id>/save-redaction/",
        views.qa_save_scan_redaction,
        name="qa_save_scan_redaction",
    ),
    path(
        "qa/batch/<int:batch_id>/donation/<uuid:donation_id>/resolve-pending-donor/",
        views.qa_resolve_pending_donor,
        name="qa_resolve_pending_donor",
    ),
    path(
        "qa/batch/<int:batch_id>/donation/<uuid:donation_id>/send-auth-link/",
        views.qa_send_authentication_link,
        name="qa_send_authentication_link",
    ),
    path(
        "qa/batch/<int:batch_id>/update-status/",
        views.qa_update_batch_status,
        name="qa_update_batch_status",
    ),
    path(
        "qa/batch/<int:batch_id>/approve-batch/",
        views.qa_approve_batch,
        name="qa_approve_batch",
    ),
    path(
        "qa/batch/<int:batch_id>/reject-batch/",
        views.qa_reject_batch,
        name="qa_reject_batch",
    ),
    path(
        "qa/batch/<int:batch_id>/resubmit/",
        views.qa_batch_resubmit,
        name="qa_batch_resubmit",
    ),
    path("qa/api/stats/", views.qa_batch_stats_api, name="qa_batch_stats_api"),
    path(
        "qa/api/donations/<uuid:donation_id>/assign-donor/",
        views.htmx_assign_donor_to_donation,
        name="htmx_assign_donor_to_donation",
    ),
    path(
        "qa/batch/<int:batch_id>/gift-aid-report/",
        views.download_gift_aid_report,
        name="download_gift_aid_report",
    ),
    path(
        "qa/batch/<int:batch_id>/gift-aid-mark-submitted/",
        views.mark_gift_aid_submitted,
        name="mark_gift_aid_submitted",
    ),
    # Notification routes
    path("notifications/", views.notification_list, name="notification_list"),
    path(
        "notifications/<uuid:notification_id>/mark-read/",
        views.notification_mark_read,
        name="notification_mark_read",
    ),
    path(
        "notifications/mark-all-read/",
        views.notification_mark_all_read,
        name="notification_mark_all_read",
    ),
    path(
        "notifications/api/count/",
        views.notification_count_api,
        name="notification_count_api",
    ),
    path(
        "notifications/api/stream/",
        views.notification_stream_api,
        name="notification_stream_api",
    ),
    # Individual donation edit endpoints
    path(
        "donations/<uuid:donation_id>/edit/", views.donation_edit, name="donation_edit"
    ),
    path(
        "donations/<uuid:donation_id>/quick-edit/",
        views.donation_quick_edit,
        name="donation_quick_edit",
    ),
    # User Management, Reports, Templates, Clients, Settings
    path("users/", views.admin_user_management, name="admin_user_management"),
    path("reports/", views.admin_reports, name="admin_reports"),
    path("reports/export/", views.report_export, name="report_export"),
    path("reports/export/pdf/", views.report_export_pdf, name="report_export_pdf"),
    path(
        "reports/scans-zip/",
        views.report_scans_zip,
        name="report_scans_zip",
    ),
    path(
        "reports/htmx/filters/<str:report_type>/",
        views.admin_reports_filter_partial,
        name="admin_reports_filter_partial",
    ),
    path(
        "reports/htmx/results/",
        views.admin_reports_results_partial,
        name="admin_reports_results_partial",
    ),
    path("clients/", views.client_setup, name="client_setup"),
    path("clients/create/", views.client_create, name="client_create"),
    path("donor-imports/", views.donor_imports, name="donor_imports"),
    path("clients/<uuid:client_id>/edit/", views.client_edit, name="client_edit"),
    path(
        "clients/<uuid:client_id>/payment-config/",
        views.client_payment_config,
        name="client_payment_config",
    ),
    path("portal-users/create/", views.portal_user_create, name="portal_user_create"),
    path("settings/", views.admin_settings, name="admin_settings"),
    # Service settings for invoice line items
    path("settings/services/", views.service_settings, name="admin_service_settings"),
    path(
        "settings/services/item/update/",
        views.service_item_update,
        name="service_item_update",
    ),
    path(
        "settings/services/category/update/",
        views.service_category_update,
        name="service_category_update",
    ),
    path(
        "settings/services/item/create/",
        views.service_item_create,
        name="service_item_create",
    ),
    path(
        "settings/services/item/<uuid:item_id>/delete/",
        views.service_item_delete,
        name="service_item_delete",
    ),
    path(
        "settings/services/category/create/",
        views.service_category_create,
        name="service_category_create",
    ),
    path("api/service-items/", views.service_items_api, name="service_items_api"),
    path(
        "api/campaigns/",
        api_views.campaigns_by_client_api,
        name="campaigns_by_client_api",
    ),
    path(
        "api/campaigns/<uuid:campaign_id>/package-codes/",
        api_views.package_codes_api,
        name="package_codes_api",
    ),
    path(
        "letter-setup/",
        views.letter_print_console,
        name="letter_print_console",
    ),
    path(
        "letter-setup/campaign/<uuid:campaign_id>/",
        views.letter_setup_campaign,
        name="letter_setup_campaign",
    ),
    path(
        "letter-setup/campaign/<uuid:campaign_id>/add-template/",
        views.add_letter_template,
        name="add_letter_template",
    ),
    path(
        "letter-setup/reference-guide/",
        views.serve_reference_guide,
        name="letter_reference_guide",
    ),
    path(
        "letter-setup/campaign/<uuid:campaign_id>/preview-template/<str:template_type>/",
        views.preview_active_template,
        name="preview_active_template",
    ),
    path(
        "letter-setup/campaign/<uuid:campaign_id>/generate-letter/",
        views.generate_letter,
        name="generate_letter",
    ),
    path(
        "letter-setup/campaign/<uuid:campaign_id>/generated-letters/",
        views.view_generated_letters,
        name="view_generated_letters",
    ),
    path(
        "letter-setup/campaign/<uuid:campaign_id>/download-letter/<path:filename>/",
        views.download_letter,
        name="download_letter",
    ),
    path(
        "letter-setup/task-status/<str:task_id>/",
        views.letter_task_status,
        name="letter_task_status",
    ),
    # Batch letter management (new)
    path(
        "letter-setup/campaign/<uuid:campaign_id>/batches/",
        views.letter_batches,
        name="letter_batches",
    ),
    path(
        "letter-setup/batch/<uuid:batch_id>/",
        views.letter_batch_detail,
        name="letter_batch_detail",
    ),
    path(
        "letter-setup/batch/<uuid:batch_id>/status/",
        views.letter_batch_status_api,
        name="letter_batch_status_api",
    ),
    path(
        "letter-setup/batch/<uuid:batch_id>/cancel/",
        views.cancel_batch,
        name="cancel_batch",
    ),
    path(
        "letter-setup/batch/<uuid:batch_id>/download/<int:file_index>/",
        views.download_batch_file,
        name="download_batch_file",
    ),
    path(
        "letter-setup/campaign/<uuid:campaign_id>/reset-failed/",
        views.reset_failed_letters,
        name="reset_failed_letters",
    ),
    # Supporters (SystemDonor lookup)
    path("supporters/", views.supporter_list, name="supporter_list"),
    path(
        "supporters/<uuid:pk>/",
        views.supporter_detail,
        name="supporter_detail",
    ),
    path(
        "supporters/<uuid:pk>/htmx/donations/",
        views.htmx_supporter_donations,
        name="htmx_supporter_donations",
    ),
    # Audit History
    path("audit-history/", views.audit_log_history, name="audit_log_history"),
    # Invoices
    path("invoices/", views.invoice_list, name="invoice_list"),
    path(
        "invoices/create-with-services/",
        views.invoice_create_with_services,
        name="invoice_create",
    ),
    path(
        "invoices/api/metrics/", views.invoice_metrics_api, name="invoice_metrics_api"
    ),
    path("invoices/<uuid:invoice_id>/", views.invoice_detail, name="invoice_detail"),
    path(
        "invoices/<uuid:invoice_id>/mark-paid/",
        views.invoice_mark_paid,
        name="invoice_mark_paid",
    ),
    path(
        "invoices/<uuid:invoice_id>/change-status/",
        views.invoice_change_status,
        name="invoice_change_status",
    ),
    path(
        "invoices/<uuid:invoice_id>/pdf/",
        views.invoice_generate_pdf,
        name="invoice_generate_pdf",
    ),
    path(
        "invoices/<uuid:invoice_id>/preview/",
        views.invoice_preview_pdf,
        name="invoice_preview_pdf",
    ),
    path(
        "invoices/<uuid:invoice_id>/send-email/",
        views.invoice_send_email_view,
        name="invoice_send_email",
    ),
    path(
        "invoices/<uuid:invoice_id>/edit/",
        views.invoice_edit,
        name="invoice_edit",
    ),
    path(
        "invoices/<uuid:invoice_id>/update-line-items/",
        views.invoice_update_line_items,
        name="invoice_update_line_items",
    ),
    # Daily Banking
    path(
        "daily-banking/",
        views.daily_banking_dashboard,
        name="daily_banking_dashboard",
    ),
    path(
        "daily-banking/batch/<int:batch_id>/",
        views.daily_banking_batch_detail,
        name="daily_banking_batch_detail",
    ),
    path(
        "daily-banking/create-slip/",
        views.create_paying_in_slip,
        name="create_paying_in_slip",
    ),
    path(
        "daily-banking/htmx/batches/",
        views.htmx_banking_batch_queue,
        name="htmx_banking_batch_queue",
    ),
    path(
        "daily-banking/slips/",
        views.slip_list,
        name="slip_list",
    ),
    path(
        "daily-banking/slip/<int:slip_id>/",
        views.slip_detail,
        name="slip_detail",
    ),
    path(
        "daily-banking/slip/<int:slip_id>/edit/",
        views.slip_edit,
        name="slip_edit",
    ),
    path(
        "daily-banking/slip/<int:slip_id>/update-status/",
        views.slip_update_status,
        name="slip_update_status",
    ),
    path(
        "daily-banking/slip/<int:slip_id>/record-processing/",
        views.slip_record_processing,
        name="slip_record_processing",
    ),
    path(
        "daily-banking/slip/<int:slip_id>/remove-donation/",
        views.slip_remove_donation,
        name="slip_remove_donation",
    ),
    path(
        "daily-banking/slip/<int:slip_id>/add-donations/",
        views.slip_add_donations,
        name="slip_add_donations",
    ),
    path(
        "daily-banking/slip/<int:slip_id>/delete/",
        views.slip_delete,
        name="slip_delete",
    ),
    path(
        "daily-banking/slip/<int:slip_id>/print/",
        views.slip_print,
        name="slip_print",
    ),
]
