"""Audit logging utilities for automatic CRUD tracking.

This module provides helper functions to create audit log entries
for all system operations with optimized performance.
"""

from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.db.models import Model
from django.http import HttpRequest

User = get_user_model()


def _normalize_audit_text_value(value: str | None) -> str:
    """Normalize optional audit text fields for NOT NULL database columns.

    Args:
        value: Text value supplied by the caller.

    Returns:
        A non-null string suitable for AuditLog text columns.
    """
    return value or ""


def log_action(
    user: AbstractBaseUser | AnonymousUser | None,
    action: str,
    model_name: str,
    object_id: str | None = None,
    object_repr: str = "",
    changes: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str = "",
    batch_id: str | None = None,
    batch_size: int | None = None,
    summary: str = "",
) -> None:
    """Create an audit log entry.

    Args:
        user: User who performed the action.
        action: Type of action (CREATE, UPDATE, DELETE, etc.).
        model_name: Name of the model affected.
        object_id: ID of the affected object.
        object_repr: String representation of the object.
        changes: Dictionary of before/after values.
        ip_address: IP address of the user.
        user_agent: Browser user agent.
        batch_id: Batch identifier for bulk operations.
        batch_size: Number of records affected in bulk operation.
        summary: Human-readable summary of the action.
    """
    from audit.models import AuditLog

    # Only store real authenticated users (discard AnonymousUser)
    user_for_log = user if isinstance(user, User) else None

    try:
        AuditLog.objects.create(
            user=user_for_log,
            action=action,
            model_name=model_name,
            object_id=_normalize_audit_text_value(
                str(object_id) if object_id else None
            ),
            object_repr=object_repr[:500],  # Truncate to field length
            changes=changes or {},
            ip_address=ip_address,
            user_agent=user_agent[:500] if user_agent else "",  # Truncate
            batch_id=_normalize_audit_text_value(batch_id),
            batch_size=batch_size,
            summary=summary[:1000] if summary else "",  # Truncate
        )
    except Exception as e:
        # Log audit errors but don't break application flow
        import logging

        logger = logging.getLogger(__name__)
        logger.error("Failed to create audit log: %s", e)


def log_model_create(
    instance: Model,
    user: AbstractBaseUser | AnonymousUser | None = None,
    request: Any = None,
) -> None:
    """Log model creation.

    Args:
        instance: The created model instance.
        user: User who created the object.
        request: HTTP request object (optional).
    """
    model_name = instance.__class__.__name__
    object_id = str(instance.pk) if instance.pk else None
    object_repr = str(instance)

    ip_address = None
    user_agent = ""

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    summary = f"Created {model_name}: {object_repr}"

    log_action(
        user=user,
        action="CREATE",
        model_name=model_name,
        object_id=object_id,
        object_repr=object_repr,
        ip_address=ip_address,
        user_agent=user_agent,
        summary=summary,
    )


def log_model_update(
    instance: Model,
    user: AbstractBaseUser | AnonymousUser | None = None,
    request: Any = None,
    changes: dict[str, Any] | None = None,
) -> None:
    """Log model update.

    Args:
        instance: The updated model instance.
        user: User who updated the object.
        request: HTTP request object (optional).
        changes: Dictionary of changed fields with before/after values.
    """
    model_name = instance.__class__.__name__
    object_id = str(instance.pk) if instance.pk else None
    object_repr = str(instance)

    ip_address = None
    user_agent = ""

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    changed_fields = list(changes.keys()) if changes else []
    summary = f"Updated {model_name}: {object_repr}"
    if changed_fields:
        summary += f" (Changed: {', '.join(changed_fields)})"

    log_action(
        user=user,
        action="UPDATE",
        model_name=model_name,
        object_id=object_id,
        object_repr=object_repr,
        changes=changes,
        ip_address=ip_address,
        user_agent=user_agent,
        summary=summary,
    )


def log_model_delete(
    instance: Model,
    user: AbstractBaseUser | AnonymousUser | None = None,
    request: Any = None,
) -> None:
    """Log model deletion.

    Args:
        instance: The deleted model instance.
        user: User who deleted the object.
        request: HTTP request object (optional).
    """
    model_name = instance.__class__.__name__
    object_id = str(instance.pk) if instance.pk else None
    object_repr = str(instance)

    ip_address = None
    user_agent = ""

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    summary = f"Deleted {model_name}: {object_repr}"

    log_action(
        user=user,
        action="DELETE",
        model_name=model_name,
        object_id=object_id,
        object_repr=object_repr,
        ip_address=ip_address,
        user_agent=user_agent,
        summary=summary,
    )


def log_bulk_create(
    model_name: str,
    count: int,
    user: AbstractBaseUser | AnonymousUser | None = None,
    request: Any = None,
    batch_id: str | None = None,
    details: str = "",
) -> None:
    """Log bulk create operation.

    Args:
        model_name: Name of the model.
        count: Number of records created.
        user: User who performed the operation.
        request: HTTP request object (optional).
        batch_id: Batch identifier.
        details: Additional details about the operation.
    """
    ip_address = None
    user_agent = ""

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    summary = f"Bulk created {count} {model_name} records"
    if details:
        summary += f": {details}"

    log_action(
        user=user,
        action="BULK_CREATE",
        model_name=model_name,
        ip_address=ip_address,
        user_agent=user_agent,
        batch_id=batch_id,
        batch_size=count,
        summary=summary,
    )


def log_bulk_update(
    model_name: str,
    count: int,
    user: AbstractBaseUser | AnonymousUser | None = None,
    request: Any = None,
    batch_id: str | None = None,
    details: str = "",
) -> None:
    """Log bulk update operation.

    Args:
        model_name: Name of the model.
        count: Number of records updated.
        user: User who performed the operation.
        request: HTTP request object (optional).
        batch_id: Batch identifier.
        details: Additional details about the operation.
    """
    ip_address = None
    user_agent = ""

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    summary = f"Bulk updated {count} {model_name} records"
    if details:
        summary += f": {details}"

    log_action(
        user=user,
        action="BULK_UPDATE",
        model_name=model_name,
        ip_address=ip_address,
        user_agent=user_agent,
        batch_id=batch_id,
        batch_size=count,
        summary=summary,
    )


def log_bulk_delete(
    model_name: str,
    count: int,
    user: AbstractBaseUser | AnonymousUser | None = None,
    request: Any = None,
    batch_id: str | None = None,
    details: str = "",
) -> None:
    """Log bulk delete operation.

    Args:
        model_name: Name of the model.
        count: Number of records deleted.
        user: User who performed the operation.
        request: HTTP request object (optional).
        batch_id: Batch identifier.
        details: Additional details about the operation.
    """
    ip_address = None
    user_agent = ""

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    summary = f"Bulk deleted {count} {model_name} records"
    if details:
        summary += f": {details}"

    log_action(
        user=user,
        action="BULK_DELETE",
        model_name=model_name,
        ip_address=ip_address,
        user_agent=user_agent,
        batch_id=batch_id,
        batch_size=count,
        summary=summary,
    )


def log_import(
    model_name: str,
    count: int,
    user: AbstractBaseUser | AnonymousUser | None = None,
    request: Any = None,
    batch_id: str | None = None,
    filename: str = "",
) -> None:
    """Log data import operation.

    Args:
        model_name: Name of the model.
        count: Number of records imported.
        user: User who performed the import.
        request: HTTP request object (optional).
        batch_id: Batch identifier.
        filename: Name of imported file.
    """
    ip_address = None
    user_agent = ""

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    summary = f"Imported {count} {model_name} records"
    if filename:
        summary += f" from {filename}"

    log_action(
        user=user,
        action="IMPORT",
        model_name=model_name,
        ip_address=ip_address,
        user_agent=user_agent,
        batch_id=batch_id,
        batch_size=count,
        summary=summary,
    )


def log_export(
    model_name: str,
    count: int,
    user: AbstractBaseUser | AnonymousUser | None = None,
    request: Any = None,
    export_type: str = "CSV",
) -> None:
    """Log data export operation.

    Args:
        model_name: Name of the model.
        count: Number of records exported.
        user: User who performed the export.
        request: HTTP request object (optional).
        export_type: Type of export (CSV, Excel, PDF).
    """
    ip_address = None
    user_agent = ""

    if request:
        ip_address = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

    summary = f"Exported {count} {model_name} records as {export_type}"

    log_action(
        user=user,
        action="EXPORT",
        model_name=model_name,
        ip_address=ip_address,
        user_agent=user_agent,
        batch_size=count,
        summary=summary,
    )


def get_client_ip(request: Any) -> str | None:
    """Extract client IP address from request."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip


def log_request_action(
    request: HttpRequest,
    action: str,
    model_name: str,
    object_id: str | None = None,
    object_repr: str = "",
    changes: dict[str, Any] | None = None,
    batch_id: str | None = None,
    batch_size: int | None = None,
    summary: str = "",
) -> None:
    """Shortcut for log_action that extracts ip/user_agent from the request."""
    log_action(
        user=request.user,
        action=action,
        model_name=model_name,
        object_id=object_id,
        object_repr=object_repr,
        changes=changes,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        batch_id=batch_id,
        batch_size=batch_size,
        summary=summary,
    )
