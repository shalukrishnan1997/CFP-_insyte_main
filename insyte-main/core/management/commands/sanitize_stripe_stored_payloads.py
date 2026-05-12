"""Replace stored Stripe JSON with PCI-oriented summaries (one-off / periodic).

Usage:
    uv run python manage.py sanitize_stripe_stored_payloads [--dry-run] [--limit N]
    uv run python manage.py sanitize_stripe_stored_payloads --webhooks-only
    uv run python manage.py sanitize_stripe_stored_payloads --payments-only

Summaries match :mod:`payments.stripe_payload_sanitize` so runtime behaviour
stays aligned with ``STORE_STRIPE_RAW_PAYLOADS=false``.
"""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Rewrite ``StripeWebhookEvent.payload`` and ``StripePayment.stripe_response``."""

    help = "Summarize stored Stripe webhook payloads and payment intent JSON fields"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show counts only; do not write to the database",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Maximum rows to scan per model (webhooks and payments each)",
        )
        parser.add_argument(
            "--webhooks-only",
            action="store_true",
            help="Only process StripeWebhookEvent rows",
        )
        parser.add_argument(
            "--payments-only",
            action="store_true",
            help="Only process StripePayment rows",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Log each updated row (can be very noisy)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        from payments.models import StripePayment, StripeWebhookEvent
        from payments.stripe_payload_sanitize import (
            summarize_payment_intent,
            summarize_webhook_event_dict,
        )

        dry_run: bool = options["dry_run"]
        limit: int | None = options["limit"]
        webhooks_only: bool = options["webhooks_only"]
        payments_only: bool = options["payments_only"]
        verbose: bool = options["verbose"]

        wh_updated = 0
        wh_skipped = 0
        wh_unchanged = 0

        if not payments_only:
            wh_qs = StripeWebhookEvent.objects.all().order_by("created_at")
            if limit is not None:
                wh_qs = wh_qs[:limit]
            for ev in wh_qs.iterator(chunk_size=200):
                raw = ev.payload
                if not isinstance(raw, dict):
                    wh_skipped += 1
                    continue
                new_payload = summarize_webhook_event_dict(raw)
                if new_payload == raw:
                    wh_unchanged += 1
                    continue
                wh_updated += 1
                if verbose:
                    old_sz = len(json.dumps(raw, default=str))
                    new_sz = len(json.dumps(new_payload, default=str))
                    self.stdout.write(
                        f"  webhook {ev.stripe_event_id}: {old_sz} -> {new_sz} bytes"
                    )
                if not dry_run:
                    StripeWebhookEvent.objects.filter(pk=ev.pk).update(
                        payload=new_payload
                    )

        pay_updated = 0
        pay_skipped = 0
        pay_unchanged = 0

        if not webhooks_only:
            pay_qs = StripePayment.objects.all().order_by("created_at")
            if limit is not None:
                pay_qs = pay_qs[:limit]
            for pay in pay_qs.iterator(chunk_size=200):
                raw = pay.stripe_response
                if not isinstance(raw, dict) or not raw:
                    pay_skipped += 1
                    continue
                new_resp = summarize_payment_intent(raw)
                if new_resp == raw:
                    pay_unchanged += 1
                    continue
                pay_updated += 1
                if verbose:
                    old_sz = len(json.dumps(raw, default=str))
                    new_sz = len(json.dumps(new_resp, default=str))
                    self.stdout.write(
                        f"  payment {pay.stripe_payment_intent_id}: "
                        f"{old_sz} -> {new_sz} bytes"
                    )
                if not dry_run:
                    StripePayment.objects.filter(pk=pay.pk).update(
                        stripe_response=new_resp
                    )

        mode = "dry-run" if dry_run else "applied"
        self.stdout.write(
            self.style.SUCCESS(
                f"Stripe payload sanitization ({mode}): "
                f"webhooks updated={wh_updated} unchanged={wh_unchanged} "
                f"skipped_non_dict={wh_skipped}; "
                f"payments updated={pay_updated} unchanged={pay_unchanged} "
                f"skipped_empty={pay_skipped}"
            )
        )
