# INSYTE End-to-End Test Plan

## Purpose

This document specifies an end-to-end (e2e) integration test suite that drives
the full INSYTE donation pipeline against a single canonical input fixture —
`tests/fixtures/000015.pdf`, a 64-page Kodak Capture Pro scan of physical
donation forms (1,022,333 bytes). The suite exercises the same surfaces that
production hits in sequence:

```
scanner webhook  ->  R2 upload  ->  Document AI OCR
              ->  ScanBatch / DonationBatch
              ->  QA review  ->  approval
              ->  payment capture (Stripe + banking)
              ->  letter generation  ->  invoice issue
              ->  AuditLog / ApprovalLog / ExportLog
```

Each scenario is owned by a single test module under `tests/integration/` so
that the 14 work units in the parallel build batch can be developed
independently and then merged onto `main` without conflicts. The suite is
**hermetic** — no real GCP, R2, Stripe, or Resend calls are made; all external
collaborators are stubbed via `unittest.mock` or fakes.

## Source fixture

| Path | Description |
|---|---|
| `tests/fixtures/000015.pdf` | Canonical 64-page Kodak scan; original of the production capture path. The TIFF stream embedded in this PDF stands in for the multi-page image that the scanner workstation would normally POST to `/webhooks/scanner/*`. Size: 1,022,333 bytes. |

The fixture is checked into git (the root `.gitignore` excludes only top-level
`*.pdf`, so files under `tests/fixtures/` are tracked).

## Unit matrix

The 14 units below carve the suite into independently-mergeable modules. Unit 1
(this document plus the fixture) is the prerequisite for every other unit;
units 2–14 may then proceed in parallel.

| # | Title | Test file | One-line description |
|---|---|---|---|
| 1 | Test plan + PDF fixture | `docs/E2E_TEST_PLAN.md`, `tests/fixtures/000015.pdf` | This document and the canonical 64-page scan that every other unit consumes. |
| 2 | Webhook ingest happy path | `tests/integration/test_e2e_webhook_ingest.py` | Posts a valid HMAC-signed multipart upload of `000015.pdf`, asserts a `ScanBatch` with the right page count is created and the OCR task is enqueued. |
| 3 | Webhook security (HMAC replay, unit 21) | `tests/integration/test_e2e_webhook_security.py` | Replays a previously-accepted request, sends bad HMAC, missing client_id, and stale timestamps; expects 401/403 without side-effects. |
| 4 | Banking lifecycle (cheque/cash/CAF/postal) | `tests/integration/test_e2e_banking_lifecycle.py` | Approves a batch containing cheque, cash, CAF voucher, and postal-order donations; asserts a `PayingInSlip` is generated and totals reconcile. |
| 5 | Stripe card success | `tests/integration/test_e2e_payment_card_success.py` | Approves a card-payment batch; the stubbed `StripePaymentService` returns a succeeded `PaymentIntent`; asserts `StripePayment` rows and `Donation.payment_status` flips to PAID. |
| 6 | Stripe decline + 3DS | `tests/integration/test_e2e_payment_card_decline_3ds.py` | Stubs Stripe to return `card_declined` and `requires_action` (3DS); asserts donations stay PENDING with the correct decline_reason and no letter is generated. |
| 7 | Stripe webhooks (dispute/refund) | `tests/integration/test_e2e_payment_card_webhooks.py` | POSTs `charge.dispute.created` and `charge.refunded` events through `/webhooks/stripe/`; verifies `StripeWebhookEvent` is recorded and the donation transitions accordingly. |
| 8 | Direct debit + non-financial | `tests/integration/test_e2e_payment_other.py` | Approves a batch with direct-debit mandates and non-financial entries (volunteer signups, address updates); asserts the right routing and zero-value invoice line behaviour. |
| 9 | Mixed-payment batch routing | `tests/integration/test_e2e_mixed_batch.py` | Single batch contains card + cheque + DD + non-financial; asserts the splitter creates the correct number of `PayingInSlip`/`StripePayment`/letter rows. |
| 10 | OCR low-confidence flagging (unit 23) | `tests/integration/test_e2e_ocr_low_confidence.py` | Stubs Document AI to return entities below the confidence threshold; asserts donations are flagged for QA and not auto-approved. |
| 11 | Donor fuzzy-match candidates (unit 24) | `tests/integration/test_e2e_donor_match_fuzzy.py` | Seeds near-duplicate `SystemDonor` rows; asserts the matcher surfaces candidate suggestions in QA and leaves the canonical donor selection to the operator. |
| 12 | QA concurrent approval (unit 10) | `tests/integration/test_e2e_qa_concurrency.py` | Two staff users hit the approve endpoint simultaneously on the same `ScanBatch`; asserts only one wins and the second receives a clear conflict response. |
| 13 | Redaction R2 failure rollback | `tests/integration/test_e2e_redaction_failure.py` | Forces the R2 client to raise on the redacted-image upload; asserts the batch is rolled back to UNREDACTED and the original is preserved. |
| 14 | Letter + invoice + audit coverage | `tests/integration/test_e2e_letter_invoice_audit.py` | Approves a batch end-to-end; asserts a `LetterBatch` is rendered via `docxtpl`, an `Invoice` with the correct service items is issued, and `AuditLog`/`ApprovalLog`/`ExportLog` rows are populated. |

## Shared setup pattern

All e2e tests follow the same conventions:

### Factories

`tests/factories.py` provides `factory_boy` factories for every domain model
(User, Client, Campaign, SystemDonor, ScanBatch, DonationBatch, Donation,
LetterTemplate, etc.). E2e tests should compose these factories rather than
calling `Model.objects.create` directly so that every unit emits the same
default field set.

### Authentication

```python
from django.test import Client

client = Client()
client.force_login(staff_user)
```

`force_login` bypasses the 2FA enforcement in `ClientPortalMiddleware` for
authenticated requests (the middleware only redirects unverified users to the
2FA setup page on GET; a `force_login` plus a verified `EmailDevice` factory
suffices). For DRF endpoints, attach the user via `APIClient.force_authenticate`.

### External-service stubs

| Collaborator | Stub strategy |
|---|---|
| Google Document AI | `unittest.mock.patch` `scans.document_ai.process_document` to return a deterministic `documentai.Document` proto with the entities each scenario needs. |
| Cloudflare R2 | Override `DEFAULT_FILE_STORAGE` to `django.core.files.storage.InMemoryStorage` (Django 5.2). Tests that need to assert R2-specific failure paths (unit 13) `patch` `core.storage_backends.R2Storage._save` to raise `botocore.exceptions.ClientError`. |
| Stripe | `patch` `stripe.PaymentIntent.create` / `stripe.PaymentIntent.confirm` and friends. The `stripe_e2e` pytest marker is reserved for the *separate* live-API smoke test (`pytest -m stripe_e2e`); the e2e suite must remain hermetic. |
| Resend (email) | `responsehandling.settings.test` already pins `EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"`; assert against `django.core.mail.outbox`. |

### HMAC-signed webhook helper

The scanner webhook is HMAC-authenticated via `SCAN_WEBHOOK_SECRET` and scoped
by `client_id` (`core/webhooks.py`). E2e tests call a shared helper that builds
a correctly-signed multipart request:

```python
def post_scanner_upload(client, *, pdf_path, client_id, secret):
    body = pdf_path.read_bytes()
    timestamp = str(int(time.time()))
    sig = hmac.new(
        secret.encode(), f"{timestamp}.{body!r}".encode(), hashlib.sha256
    ).hexdigest()
    return client.post(
        "/webhooks/scan-upload/",
        data={"file": SimpleUploadedFile("000015.pdf", body)},
        HTTP_X_INSYTE_CLIENT_ID=client_id,
        HTTP_X_INSYTE_TIMESTAMP=timestamp,
        HTTP_X_INSYTE_SIGNATURE=sig,
    )
```

(Each unit may inline its own variant; the goal is a single canonical signing
recipe that mirrors the scanner workstation's behaviour.)

### Celery

`responsehandling.settings.test` sets `CELERY_TASK_ALWAYS_EAGER = True`, so
`@shared_task` calls execute synchronously inside the request. Tests do not
need to spin up a worker.

## Running the suite

```bash
# Run only the e2e modules (skip --cov-fail-under to keep iteration fast)
uv run pytest tests/integration/ -v --no-cov

# A single scenario
uv run pytest tests/integration/test_e2e_webhook_ingest.py -v --no-cov

# The full project suite (CI-equivalent — coverage gate enforced)
uv run pytest tests/ -v
```

CI continues to run only `tests/unit` by default; the e2e suite is opt-in via
the path filter above. To wire it into CI, add a job that runs
`uv run pytest tests/integration/ -v --no-cov` after the unit job.

## Out of scope

The following are deliberately not covered by this e2e suite:

- **Playwright / browser visual smoke tests.** A small set lives under
  `tests/e2e/`; those exercise the rendered HTML of the operator console and
  client portal but do not drive the donation pipeline. They remain a separate
  workstream.
- **Real GCP Document AI calls.** The `process_document` collaborator is
  always stubbed. A separate manual smoke test in
  `scripts/` (not part of CI) covers the live integration.
- **Real Stripe API calls.** The `stripe_e2e` marker reserves a small,
  separately-gated suite (`uv run pytest -m stripe_e2e`) that hits the Stripe
  test API; it requires `INSYTE_STRIPE_E2E_SECRET_KEY` and is not part of the
  e2e suite specified here.
- **Real R2 / Resend / SMTP.** Storage is in-memory; email goes to
  `mail.outbox`. Production credentials are never present in the test runner.
- **Performance / load testing.** The e2e suite asserts correctness and audit
  trail, not throughput. Load and soak testing live elsewhere.
