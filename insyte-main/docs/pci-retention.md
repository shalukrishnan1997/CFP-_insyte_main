# PCI DSS retention & redaction policy

This document describes how INSYTE handles cardholder data captured on scanned donation forms, in support of the bureau's PCI DSS Self-Assessment Questionnaire. It is the authoritative retention policy referenced by `INSYTE_REDACTION_RETENTION_DAYS` and the `apply_stale_deferred_redactions` periodic task.

## Scope

What's stored, where, and why.

| Data | Storage | Retention | Notes |
|---|---|---|---|
| Scanned donation form (TIFF/PDF/PNG) — original | Cloudflare R2 (`originals/` prefix) | Until QA captures a `Donation` from the placeholder, then deleted | Retained only as long as needed to drive the OCR + QA workflow. |
| Scanned donation form — operator-redacted final | Cloudflare R2 (alongside the original key) | Same lifetime as the parent `DonationBatch`; subject to the project-wide donor data retention rules | Operator blackouts cover PAN, expiry, signature, and CVV. After the post-charge pass, no PAN bytes remain readable. |
| `ScanPlaceholder.redaction_coords_cvv` | PostgreSQL JSONField | Same lifetime as the placeholder | Per-page CVV rectangle coordinates. **Never the CVV value itself.** |
| `ScanPlaceholder.redaction_coords_post_charge` | PostgreSQL JSONField | Same lifetime as the placeholder | Per-page PAN/expiry/signature rectangle coordinates. |
| `ScanPlaceholder.cvv_redacted_at`, `redaction_completed_at` | PostgreSQL DateTimeField | Same lifetime as the placeholder | PCI audit timestamps. |
| `Donation.bank_sort_code`, `Donation.bank_account_number` | PostgreSQL, encrypted at rest via `django-fernet-encrypted-fields` | Until donor erase request or donor lifecycle expiry | Direct-debit fields only; no PAN. |
| Stripe PaymentIntent / charge / refund payloads | PostgreSQL `StripePayment.stripe_response` | Same lifetime as the donation | Persisted in summarised form by default (`STORE_STRIPE_RAW_PAYLOADS=false`); never includes PAN or CVV. |
| `AuditLog` rows for unredacted-image views, redaction edits | PostgreSQL | 7 years (regulatory minimum for donation records) | Every successful read of an unredacted scan emits a row with user, IP, placeholder ID, and reason. |

The system **never** stores: the full PAN as text, the CVV / CVC code as text or image (after the auth attempt), or any track / chip data.

## Redaction lifecycle

For every scanned card donation:

1. **Ingest** — scanner workstation pushes the TIFF to R2 (`/webhooks/scanner/*`, HMAC-authenticated). `ScanPlaceholder` row created with `redaction_status = REDACTION_PENDING`. The bytes contain a readable PAN and CVV.
2. **OCR** — Document AI extracts donor / amount / payment method. Card-number / CVV are *not* extracted into `extracted_data` JSON; only the image holds them.
3. **QA review** — operator opens the donation, reads the PAN + CVV from the image, types them into a Stripe Elements iframe (browser-side tokenisation; the server never sees the cleartext PAN). Operator draws blackout rectangles: red for the **CVV box** (one per page), black for everything else (PAN, expiry, signature). Coords saved to `redaction_coords_cvv` and `redaction_coords_post_charge`. Status flips to `REDACTION_CVV_PENDING`. **R2 image is unchanged at this point.**
4. **Approve / authorize** — Stripe `PaymentIntent.create(confirm=True)` runs. **Immediately** after the call returns, regardless of `intent_status`, the `apply_cvv_redaction` Celery task fires. PCI DSS Requirement 3.2 forbids storing sensitive authentication data after authorization, so the CVV box is blacked out within seconds of the auth attempt — successful, declined, requires_action, or otherwise. Status: `REDACTION_DEFERRED`.
5. **Settle** — depending on outcome:
   - `succeeded`: `apply_deferred_redaction` fires from the sync hook; the PAN / expiry / signature rectangles are blacked out. Status: `REDACTION_COMPLETED`.
   - `requires_action` (3DS / SCA): donor authenticates; `payment_intent.succeeded` webhook fires later, triggering the post-charge pass. Until then, the placeholder sits in `REDACTION_DEFERRED` (CVV already gone, PAN still readable for an authentication-link dispatch).
   - `requires_payment_method` (declined): operator may retry with a fresh card on the still-readable PAN. CVV is already gone, forcing a fresh CVV from the donor. If the operator gives up and rejects the donation, `apply_deferred_redaction` fires from the QA reject path.
   - `failed` permanently: same as decline + reject.
6. **Retention TTL backstop** — the periodic `apply_stale_deferred_redactions` task runs hourly. Any placeholder that has sat in `REDACTION_CVV_PENDING` or `REDACTION_DEFERRED` longer than `INSYTE_REDACTION_RETENTION_DAYS` (default **7 days**) has the post-charge pass force-applied. This catches abandoned SCA challenges, operator no-shows, and any other path that didn't converge.

## Access controls

Unredacted scans (status ≠ `REDACTION_COMPLETED`) are gated by:

- Authentication required: every endpoint runs through Django auth + 2FA.
- `view_unredacted_scan` permission: required for any GET that streams unredacted bytes when `STRICT_UNREDACTED_SCAN_VIEW=true` in production.
- Audit log write: every successful unredacted GET writes an `AuditLog` row through `audit.utils.log_request_action` with the user, IP (via `AuditRequestMiddleware`), placeholder ID, and reason. See `scans/api_views.py::scan_image_serve` and `::scan_placeholder_pdf`.
- Cache control: every response containing unredacted bytes carries `Cache-Control: no-store` so reverse proxies / browsers don't retain the bytes.
- Permissions don't override the redaction status: even a superuser cannot make `apply_cvv_redaction` skip its work; the row lock + status gate is enforced server-side regardless of caller.

## Storage encryption

Cloudflare R2 encrypts all objects at rest with AES-256 by default; encryption cannot be disabled. The production bucket `insyte` (location `WEUR`, jurisdiction `default`) inherits this guarantee. No per-bucket configuration step is required, but the platform-level encryption claim should be re-confirmed against [Cloudflare's documented data-protection posture](https://developers.cloudflare.com/r2/reference/data-security/) on every quarterly compliance review.

PostgreSQL volume encryption is provided by the underlying host (Coolify-managed disk encryption or cloud provider equivalent).

## Destruction

Image bytes are deleted from R2 by the redaction pipeline:

- After the CVV pass, the original (pre-CVV-blackout) blobs are removed by `_finalize_placeholder_redaction` (atomic R2 swap).
- After the post-charge pass, the CVV-only-redacted intermediate blobs are removed.
- Final state: only the fully-redacted blob remains under the placeholder's `page_keys`.

`cleanup_r2_orphans_task` runs every 6 hours and sweeps any blob in `redacted-tmp/`, `redacted/`, or `originals/` older than 24 hours that no `ScanPlaceholder` row references.

GDPR / donor-erase requests use `python manage.py gdpr_erase_donor <URN>`, which drops the donor record and any linked donations along with their R2 page bytes.

## Operational checks

- **Hourly**: `apply_stale_deferred_redactions` Celery beat task (enforces TTL).
- **Every 6 hours**: `cleanup_r2_orphans` (deletes unreferenced blobs).
- **Quarterly**: confirm R2 SSE is enabled; confirm `INSYTE_REDACTION_RETENTION_DAYS` matches this document; sample-audit `AuditLog` rows for unredacted views.
- **Annual**: PCI Self-Assessment Questionnaire renewal; review this document.

## Related code

- `scans/scan_redaction.py` — `save_deferred_redaction_coords`, `apply_cvv_redaction`, `apply_deferred_redaction`
- `scans/tasks.py` — `apply_cvv_redaction_task`, `apply_deferred_redaction_task`, `apply_stale_deferred_redactions_task`, `cleanup_r2_orphans_task`
- `payments/batch_payment.py` — `_enqueue_cvv_redaction`, `_enqueue_deferred_redaction`
- `core/tasks.py` — `process_stripe_webhook` (CVV + post-charge enqueue on `payment_intent.*` events)
- `custom_admin/views/qa_review.py` — `qa_save_scan_redaction` (CVV + post-charge coord intake)
- `templates/components/scanned_form_viewer.html` — operator UI for marking the CVV box
- `responsehandling/settings/base.py` — `INSYTE_REDACTION_RETENTION_DAYS`, `STRICT_UNREDACTED_SCAN_VIEW`, `REDACTION_MAX_SOURCE_BYTES`, `CELERY_BEAT_SCHEDULE`
