"""Guards that gate scan ingestion based on campaign / client lifecycle.

Historically only the scan-upload webhook (``core/webhooks.py``) refused
inactive campaigns; the admin "Pending R2 Folders" flow, the
``/admin/api/scan-processing/create/`` API, and the folder-watcher all
happily created scan batches against draft / closed campaigns or
deactivated clients. Centralising the check here keeps every entry
point in sync.
"""

from __future__ import annotations

from typing import Any


def campaign_scan_block_reason(campaign: Any | None) -> str | None:
    """Return a user-readable reason why scans must be rejected, else None.

    Args:
        campaign: A ``Campaign`` instance (with ``client`` typically
            ``select_related``-loaded) or ``None``.

    Returns:
        A reason string suitable for surfacing to a staff user / API
        client, or ``None`` if scans should be accepted.
    """
    from campaigns.models import Campaign

    if campaign is None:
        return "Campaign not found."

    if campaign.status != Campaign.STATUS_ACTIVE:
        display = (
            campaign.get_status_display()
            if hasattr(campaign, "get_status_display")
            else campaign.status
        )
        return (
            f"Campaign '{campaign.name}' is not active (status: {display}). "
            "Reactivate the campaign before ingesting more scans."
        )

    client = getattr(campaign, "client", None)
    if client is not None and not getattr(client, "is_active", True):
        return (
            f"Client '{client.name}' is deactivated. "
            "Reactivate the client before ingesting scans for its campaigns."
        )

    return None
