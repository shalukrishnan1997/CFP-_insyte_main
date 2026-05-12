"""Automatic audit logging using Django signals.

This module sets up signal handlers to automatically capture CRUD operations
on specified models without manual logging calls.

Per-model field exclusions
--------------------------
Audited models can opt fields out of the snapshot/diff stored in
``AuditLog.changes`` by declaring an ``audit_exclude_fields`` attribute on the
model class — a tuple of ``attname`` strings. ``audit_pre_save`` skips reading
those fields when capturing the pre-save snapshot, and ``get_instance_changes``
ignores any pre-existing snapshot entries with those names. The result is that
neither the field name nor either value (old/new) ever lands in the audit log.

This exists because audit captures every concrete field via
``sender._meta.fields`` and stringifies values into ``AuditLog.changes``. For
encrypted PII columns (e.g. ``Donation.sort_code`` / ``Donation.account_number``)
that round-trip via ``django-fernet-encrypted-fields``, the ORM-level value
seen by the signal is the *plaintext* — writing it to ``AuditLog.changes`` would
defeat the at-rest encryption. Use ``audit_exclude_fields`` for any field whose
storage representation differs from its in-memory representation, or any field
that must never appear in audit metadata.

Example::

    class Donation(models.Model):
        audit_exclude_fields = ("sort_code", "account_number")
        ...
"""

import contextlib
import contextvars
import logging
from typing import Any

import sentry_sdk
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Model
from django.db.models.signals import (
    m2m_changed,
    post_delete,
    post_save,
    pre_delete,
    pre_save,
)
from django.http import HttpRequest

logger = logging.getLogger(__name__)

# Context variable for request context (async/ASGI-safe, replaces threading.local)
_current_request_var: contextvars.ContextVar = contextvars.ContextVar(
    "current_request", default=None
)

User = get_user_model()


def get_current_request() -> HttpRequest | None:
    """Get current request from context variable."""
    return _current_request_var.get()  # type: ignore[return-value]


def set_current_request(request: HttpRequest | None) -> None:
    """Set current request in context variable."""
    _current_request_var.set(request)


def clear_current_request() -> None:
    """Clear current request from context variable."""
    _current_request_var.set(None)


def get_client_ip(request: HttpRequest | None) -> str | None:
    """Extract client IP from request."""
    if not request:
        return None

    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip


# Models to audit - centralized set used by both should_audit_model and signal registration
#
# StripeWebhookEvent is intentionally NOT audited: every event already has its
# own immutable row keyed by Stripe's idempotency id, and re-auditing every
# event would roughly double the audit-log write volume during normal payment
# traffic without adding investigative value.
AUDITED_MODELS = {
    "Campaign",
    "Client",
    "ClientPortalUser",
    "CampaignDataFile",
    "DataFileDonor",
    "Donation",
    "DonationBatch",
    "Donor",
    "DonorUpload",
    "Invoice",
    "LetterBatch",
    "LetterTemplate",
    "PayingInSlip",
    "PaymentGatewayConfig",
    "ScanBatch",
    "StripeCustomer",
    "StripePayment",
    "StripePaymentMethod",
    "SystemDonor",
    "User",
}

# Financial / state-changing models whose audit history must never be silently
# lost. If ``AuditLog.objects.create`` fails for one of these, ``audit_post_save``
# re-raises so the parent transaction rolls back — better to fail-safe than to
# accept a write whose audit trail vanished. For non-critical models we still
# log + report to Sentry, but the parent save is allowed to succeed.
#
# ``PaymentGatewayConfig`` is critical because its rows hold each client's
# Stripe webhook signing secret and API keys; an unrecorded change there would
# let an attacker substitute a secret they control without leaving a trail.
CRITICAL_AUDITED_MODELS = {
    "Donation",
    "DonationBatch",
    "Invoice",
    "PayingInSlip",
    "PaymentGatewayConfig",
    "StripePayment",
}


def should_audit_model(model_name: str) -> bool:
    """Check if model should be audited.

    Args:
        model_name: Name of the model class.

    Returns:
        True if model should be audited.
    """
    # Never audit AuditLog itself
    if model_name == "AuditLog":
        return False

    return model_name in AUDITED_MODELS


def get_instance_changes(
    instance: Model, original_data: dict[str, Any] | None
) -> dict[str, Any]:
    """Get changed fields between original and current instance.

    Honours the model's optional ``audit_exclude_fields`` tuple — listed
    fields are skipped entirely so neither the field name nor either value
    appears in the returned diff. See module docstring for the rationale.

    Args:
        instance: Current instance.
        original_data: Original field values before update.

    Returns:
        Dictionary of changed fields with before/after values.
    """
    if not original_data:
        return {}

    excluded = set(getattr(type(instance), "audit_exclude_fields", ()))
    changes = {}
    for field_name, old_value in original_data.items():
        if field_name in excluded:
            continue
        try:
            new_value = getattr(instance, field_name)
            if old_value != new_value:
                changes[field_name] = {
                    "old": str(old_value) if old_value is not None else None,
                    "new": str(new_value) if new_value is not None else None,
                }
        except (AttributeError, ObjectDoesNotExist):  # fmt: skip
            pass

    return changes


def audit_pre_save(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Capture original field values before save for change tracking.

    Stores the original data on the instance as ``_audit_original_data``
    so that ``audit_post_save`` can compute field-level diffs.

    Args:
        sender: Model class that is about to be saved.
        instance: The model instance about to be saved.
        **kwargs: Additional signal arguments.
    """
    model_name = sender.__name__

    if not should_audit_model(model_name):
        return

    # Only capture for existing instances (updates), not new ones
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            excluded = set(getattr(sender, "audit_exclude_fields", ()))
            original_data: dict[str, Any] = {}
            for field in sender._meta.fields:
                field_name = field.attname
                if field_name in excluded:
                    continue
                with contextlib.suppress(AttributeError, ObjectDoesNotExist):
                    original_data[field_name] = getattr(old_instance, field_name)
            instance._audit_original_data = original_data
        except sender.DoesNotExist:
            # Instance doesn't exist yet (race condition or new object)
            pass


def _get_audit_request_context(request: Any) -> tuple[Any, str | None, str]:
    """Extract user, IP address, and user agent from request.

    Args:
        request: Django HttpRequest or None.

    Returns:
        Tuple of (user_or_none, ip_address_or_none, user_agent).
    """
    if not request:
        return None, None, ""
    user = request.user if request.user.is_authenticated else None
    ip_address = get_client_ip(request)
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    return user, ip_address, user_agent


def _build_audit_summary(
    model_name: str,
    object_repr: str,
    created: bool,
    changes: dict[str, Any],
) -> str:
    """Build a human-readable audit log summary.

    Args:
        model_name: Name of the model.
        object_repr: String representation of the object.
        created: True if the object was newly created.
        changes: Dictionary of changed fields.

    Returns:
        Summary string, max 1000 chars.
    """
    if created:
        return f"Created {model_name}: {object_repr}"[:1000]
    changed_fields = list(changes.keys())
    summary = f"Updated {model_name}: {object_repr}"
    if changed_fields:
        summary += f" (Changed: {', '.join(changed_fields[:5])})"
    return summary[:1000]


def audit_post_save(
    sender: type[Model], instance: Model, created: bool, **kwargs: Any
) -> None:
    """Audit log for create and update operations.

    Connected per-model via connect_audit_signals() instead of global @receiver.

    Failure handling:
        If ``AuditLog.objects.create`` fails (DB locked, table corrupted,
        disk full, etc.) the behaviour depends on the saved model:

        * **Critical models** (``CRITICAL_AUDITED_MODELS`` — financial /
          state-changing rows: ``Donation``, ``StripePayment``,
          ``DonationBatch``, ``PayingInSlip``, ``Invoice``): the exception
          is reported to Sentry and re-raised so the parent transaction
          rolls back. We would rather fail the donation/payment write than
          accept it with no audit trail — silently losing financial audit
          history is a compliance gap.
        * **Non-critical audited models** (``Campaign``, ``Donor``,
          ``Client``, ``LetterTemplate``, ``User``, etc.): the exception is
          logged at ``ERROR`` and reported to Sentry, but swallowed so the
          parent save completes. Visibility without a self-inflicted outage.

    Args:
        sender: Model class that was saved.
        instance: The saved model instance.
        created: True if this is a new instance.
        **kwargs: Additional signal arguments.
    """
    from audit.models import AuditLog

    model_name = sender.__name__

    # Skip if model shouldn't be audited
    if not should_audit_model(model_name):
        return

    request = get_current_request()
    user, ip_address, user_agent = _get_audit_request_context(request)

    action = "CREATE" if created else "UPDATE"
    object_id = str(instance.pk) if instance.pk else None
    object_repr = str(instance)[:500]

    changes = {}
    if not created and hasattr(instance, "_audit_original_data"):
        changes = get_instance_changes(instance, instance._audit_original_data)

    summary = _build_audit_summary(model_name, object_repr, created, changes)

    try:
        AuditLog.objects.create(
            user=user,
            action=action,
            model_name=model_name,
            object_id=object_id,
            object_repr=object_repr,
            changes=changes,
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else "",
            summary=summary,
        )
    except Exception as e:
        sentry_sdk.capture_exception(e)
        logger.error("Failed to create audit log for %s %s: %s", action, model_name, e)
        if model_name in CRITICAL_AUDITED_MODELS:
            # Re-raise so the surrounding transaction rolls back — financial
            # audit history must never be silently dropped.
            raise


def audit_pre_delete(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Store instance data before deletion for audit log.

    Connected per-model via connect_audit_signals() instead of global @receiver.

    Args:
        sender: Model class that will be deleted.
        instance: The instance about to be deleted.
        **kwargs: Additional signal arguments.
    """
    model_name = sender.__name__

    # Skip if model shouldn't be audited
    if not should_audit_model(model_name):
        return

    # Store data for post_delete signal
    instance._audit_deleted_data = {
        "pk": instance.pk,
        "repr": str(instance)[:500],
    }


def audit_post_delete(sender: type[Model], instance: Model, **kwargs: Any) -> None:
    """Audit log for delete operations.

    Connected per-model via connect_audit_signals() instead of global @receiver.

    Args:
        sender: Model class that was deleted.
        instance: The deleted model instance.
        **kwargs: Additional signal arguments.
    """
    from audit.models import AuditLog

    model_name = sender.__name__

    # Skip if model shouldn't be audited
    if not should_audit_model(model_name):
        return

    # Get request context
    request = get_current_request()
    user = request.user if request and request.user.is_authenticated else None

    # Get IP and user agent
    ip_address = get_client_ip(request) if request else None
    user_agent = request.META.get("HTTP_USER_AGENT", "") if request else ""

    # Get stored data from pre_delete
    deleted_data = getattr(instance, "_audit_deleted_data", {})
    object_id = str(deleted_data.get("pk", ""))
    object_repr = deleted_data.get("repr", str(instance)[:500])

    summary = f"Deleted {model_name}: {object_repr}"

    # Create audit log (with error handling)
    try:
        AuditLog.objects.create(
            user=user,
            action="DELETE",
            model_name=model_name,
            object_id=object_id,
            object_repr=object_repr,
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else "",
            summary=summary[:1000],
        )
    except Exception as e:
        # Silent failure
        import logging

        logger = logging.getLogger(__name__)
        logger.error("Failed to create audit log for DELETE %s: %s", model_name, e)


def _format_m2m_items(related_model: type[Model], pk_set: set[Any] | None) -> list[str]:
    """Resolve a pk_set from an m2m_changed signal to human-readable labels."""
    if not pk_set:
        return []
    try:
        return [str(obj) for obj in related_model.objects.filter(pk__in=pk_set)]
    except Exception:
        return [str(pk) for pk in pk_set]


def audit_user_m2m_changed(
    sender: type[Model],
    instance: Model,
    action: str,
    reverse: bool,
    model: type[Model],
    pk_set: set[Any] | None,
    **kwargs: Any,
) -> None:
    """Audit ``User.groups`` / ``User.user_permissions`` membership changes.

    Field-level ``post_save`` does not fire on M2M updates, so group and
    direct-permission assignments would otherwise be invisible in ``AuditLog``.
    This handler records each add / remove / clear action with the affected
    labels.
    """
    if action not in {"post_add", "post_remove", "post_clear"}:
        return

    # Reverse side (Group → users, Permission → users) isn't interesting for
    # this audit; we only want to log changes scoped to a specific user.
    if reverse:
        return

    from audit.models import AuditLog

    request = get_current_request()
    actor, ip_address, user_agent = _get_audit_request_context(request)

    if model is Group:
        relation_label = "groups"
    elif model is Permission:
        relation_label = "user_permissions"
    else:
        relation_label = model.__name__.lower()

    items = _format_m2m_items(model, pk_set)
    if action == "post_add":
        verb = "Added"
    elif action == "post_remove":
        verb = "Removed"
    else:
        verb = "Cleared"

    detail = ", ".join(items[:10]) if items else "(all)"
    summary = (
        f"{verb} {relation_label} on User {instance}: {detail}"
        if action != "post_clear"
        else f"Cleared {relation_label} on User {instance}"
    )

    try:
        AuditLog.objects.create(
            user=actor,
            action="UPDATE",
            model_name="User",
            object_id=str(instance.pk) if instance.pk else "",
            object_repr=str(instance)[:500],
            changes={
                "relation": relation_label,
                "action": action,
                "items": items,
            },
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else "",
            summary=summary[:1000],
        )
    except Exception as e:
        import logging

        logger = logging.getLogger(__name__)
        logger.error(
            "Failed to create audit log for m2m change on User.%s: %s",
            relation_label,
            e,
        )


def _resolve_audited_model(model_name: str) -> type[Model] | None:
    """Find an audited model by name across every installed app.

    Models in ``AUDITED_MODELS`` migrate between apps as the domain-app
    refactor proceeds (Phase 2+). Hardcoding ``apps.get_model("core", ...)``
    would silently drop signal registration on the day a model moves; this
    helper instead scans all installed apps so the audit set survives moves
    without per-phase edits here.
    """
    from django.apps import apps

    for app_config in apps.get_app_configs():
        try:
            return app_config.get_model(model_name)
        except LookupError:
            continue
    return None


def connect_audit_signals() -> None:
    """Register audit signal handlers for each audited model.

    Called from ``AuditConfig.ready()`` to connect signals per-model instead
    of using a global ``@receiver(post_save)`` that fires for every model
    in the entire project (including Django internals).
    """
    for model_name in AUDITED_MODELS:
        model = _resolve_audited_model(model_name)
        if model is None:
            logger.warning(
                "Audit model %s not found, skipping signal registration", model_name
            )
            continue

        pre_save.connect(
            audit_pre_save,
            sender=model,
            dispatch_uid=f"audit_pre_save_{model_name}",
        )
        post_save.connect(
            audit_post_save, sender=model, dispatch_uid=f"audit_save_{model_name}"
        )
        pre_delete.connect(
            audit_pre_delete,
            sender=model,
            dispatch_uid=f"audit_pre_del_{model_name}",
        )
        post_delete.connect(
            audit_post_delete,
            sender=model,
            dispatch_uid=f"audit_post_del_{model_name}",
        )

    # M2M audit hooks for User.groups and User.user_permissions.
    # Field-level post_save doesn't fire for M2M changes, so without these the
    # audit log would miss group/permission assignments made from admin UIs.
    user_model = _resolve_audited_model("User")
    if user_model is not None:
        m2m_changed.connect(
            audit_user_m2m_changed,
            sender=user_model.groups.through,
            dispatch_uid="audit_user_groups_m2m",
        )
        m2m_changed.connect(
            audit_user_m2m_changed,
            sender=user_model.user_permissions.through,
            dispatch_uid="audit_user_permissions_m2m",
        )
