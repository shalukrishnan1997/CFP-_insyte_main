"""Tenancy regression: every tenant-scoped model is reachable from Client.

Walks forward FK chains from each non-exempt model and asserts it can reach
``clients.Client`` within a small number of hops. A model with no path to
Client is a candidate for cross-tenant data leakage — every row needs a
way to be attributed to a tenant.

If this test fails, a new model was added without any FK path to a client.
Either:

1. Add a ``client`` FK directly, or a FK to a model that itself has one
   (e.g. ``campaign`` works because ``Campaign`` → ``Client``), or
2. If the model is genuinely cross-tenant or system-level, append its
   ``app_label.ModelName`` to ``EXEMPT_MODELS`` with a short comment
   explaining why it is exempt.
"""

import pytest
from django.apps import apps
from django.db.models import ForeignKey, OneToOneField

EXEMPT_APPS = frozenset(
    {
        "admin",
        "auth",
        "contenttypes",
        "sessions",
        "messages",
        "staticfiles",
        "authtoken",
        "corsheaders",
        "django_celery_beat",
        "django_celery_results",
        "axes",
        "otp_totp",
        "otp_static",
        "authApp",
        "custom_admin",
    }
)

# Genuinely cross-tenant or system-level models. Anything listed here
# must have a real justification — tenancy is assumed by default.
EXEMPT_MODELS = frozenset(
    {
        "clients.Client",  # The tenant itself.
        "core.User",  # Global; membership/access via other tables.
        "audit.AuditLog",  # Cross-tenant log; scoping is per-row via content_type.
        "audit.ApprovalLog",  # Same as AuditLog.
        "audit.ExportLog",  # Same as AuditLog.
        "notifications.Notification",  # User-scoped, not client-scoped.
        # Bureau-level reference data, shared across every client.
        "invoices.InvoiceSettings",  # Singleton default pricing.
        "invoices.ServiceCategory",  # Shared service catalogue.
        "invoices.ServiceItem",  # Shared service catalogue items.
        "campaigns.PackageCode",  # M2M label re-used across clients/campaigns.
    }
)

_CLIENT_LABEL = "clients.Client"
_MAX_DEPTH = 3


def _reaches_client(
    model: type, visited: set[type] | None = None, depth: int = 0
) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if visited is None:
        visited = set()
    if model in visited:
        return False
    visited.add(model)

    for field in model._meta.get_fields():
        if not isinstance(field, (ForeignKey, OneToOneField)):
            continue
        related = field.related_model
        if related == "self" or related is model:
            continue
        if related._meta.label == _CLIENT_LABEL:
            return True
        if _reaches_client(related, visited, depth + 1):
            return True
    return False


@pytest.mark.django_db()
def test_every_tenant_model_reaches_client_via_fk_chain() -> None:
    offenders: list[str] = []
    for model in apps.get_models():
        if model._meta.app_label in EXEMPT_APPS:
            continue

        label = model._meta.label
        if label in EXEMPT_MODELS:
            continue

        if not _reaches_client(model):
            offenders.append(label)

    assert not offenders, (
        "The following tenant-scoped models have no ForeignKey path to "
        "clients.Client. Add a FK (direct or transitive) or add the model "
        "to EXEMPT_MODELS with a justification comment:\n  - "
        + "\n  - ".join(sorted(offenders))
    )
