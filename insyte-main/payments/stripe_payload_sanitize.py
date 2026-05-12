"""Allowlisted summaries of Stripe API objects for safer persistence (PCI scope)."""

from __future__ import annotations

from typing import Any


def stripe_object_to_dict(obj: Any) -> dict[str, Any]:
    """Coerce a Stripe object or mapping to a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        raw = to_dict()
        return raw if isinstance(raw, dict) else {}
    try:
        return dict(obj)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return {}


def summarize_payment_intent_dict(obj: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a minimal payment-intent-shaped summary."""
    if not obj:
        return None
    out: dict[str, Any] = {
        "id": obj.get("id"),
        "object": obj.get("object"),
        "status": obj.get("status"),
        "amount": obj.get("amount"),
        "amount_received": obj.get("amount_received"),
        "currency": obj.get("currency"),
        "created": obj.get("created"),
        "description": obj.get("description"),
        "metadata": obj.get("metadata"),
        "latest_charge": obj.get("latest_charge"),
        "cancellation_reason": obj.get("cancellation_reason"),
    }
    err = obj.get("last_payment_error")
    if isinstance(err, dict):
        out["last_payment_error"] = {
            "code": err.get("code"),
            "decline_code": err.get("decline_code"),
            "message": err.get("message"),
            "type": err.get("type"),
        }
    return {k: v for k, v in out.items() if v is not None}


def summarize_payment_intent(obj: Any) -> dict[str, Any]:
    """Summarize a PaymentIntent-like Stripe object."""
    d = stripe_object_to_dict(obj)
    summary = summarize_payment_intent_dict(d)
    return summary if summary is not None else {}


def summarize_webhook_event_dict(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a minimal Stripe webhook event envelope.

    Keeps ``data.object`` so async handlers (e.g. Celery) that read
    ``payload["data"]["object"]`` continue to work with a summarized object.
    """
    data = event_dict.get("data") or {}
    inner = data.get("object")
    inner_summary: dict[str, Any] | None = None
    if isinstance(inner, dict):
        inner_summary = summarize_payment_intent_dict(inner)
    out: dict[str, Any] = {
        "id": event_dict.get("id"),
        "type": event_dict.get("type"),
        "api_version": event_dict.get("api_version"),
        "created": event_dict.get("created"),
        "livemode": event_dict.get("livemode"),
    }
    if inner_summary:
        out["data"] = {"object": inner_summary}
    return {k: v for k, v in out.items() if v is not None}


def maybe_persist_stripe_response(raw: Any, *, store_full: bool) -> dict[str, Any]:
    """Return dict to store as ``stripe_response`` or webhook payload fragment."""
    if store_full:
        d = stripe_object_to_dict(raw)
        return d
    return summarize_payment_intent(raw)


def maybe_persist_webhook_payload(
    event_dict: dict[str, Any], *, store_full: bool
) -> dict[str, Any]:
    """Return dict to store as ``StripeWebhookEvent.payload``."""
    if store_full:
        return dict(event_dict)
    return summarize_webhook_event_dict(event_dict)
