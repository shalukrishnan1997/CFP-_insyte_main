"""Donation scan service for scanned form URL resolution.

Centralises all scanned-form path and URL logic that was previously
on the :model:`core.Donation` model, keeping that model thin.
All scans are stored in Cloudflare R2.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from django.conf import settings
from django.utils.text import slugify

if TYPE_CHECKING:
    from donations.models import Donation
    from scans.models import ScanPlaceholder


class DonationScanService:
    """Service for resolving scanned form paths and URLs for a donation."""

    _EXTENSIONS = ("jpg", "jpeg", "png", "pdf", "tiff", "bmp", "webp")

    @staticmethod
    def _scanned_form_new_path_components(
        donation: Donation,
    ) -> tuple[str, str, str, str]:
        """Return path components for the new storage format.

        Format: client/appeal_code/package_code/urn.ext

        Args:
            donation: Donation instance.

        Returns:
            Tuple of (client_slug, appeal_slug, pkg_slug, urn).
        """
        client_name = (
            donation.campaign.client.name
            if donation.campaign and donation.campaign.client
            else "client"
        )
        appeal_code = (
            donation.campaign.appeal_code
            if donation.campaign and getattr(donation.campaign, "appeal_code", None)
            else (donation.campaign.name if donation.campaign else "appeal")
        )
        pkg_code = getattr(donation, "package_code", "") or "package"

        urn_value: str
        if donation.system_donor and donation.system_donor.external_urn:
            urn_value = donation.system_donor.external_urn
        elif donation.donor and donation.donor.urn:
            urn_value = donation.donor.urn
        elif donation.data_file_donor and donation.data_file_donor.urn:
            urn_value = donation.data_file_donor.urn
        else:
            urn_value = "unknown_urn"

        return (
            slugify(client_name) or "client",
            slugify(appeal_code) or "appeal",
            slugify(pkg_code) or "package",
            urn_value,
        )

    @staticmethod
    def _resolve_scanned_form_path(donation: Donation) -> tuple[str, bool]:
        """Construct the relative scanned form path and determine if it exists.

        Uses the current storage format only.

        Args:
            donation: Donation instance.

        Returns:
            Tuple of (relative_path, file_exists).
        """
        media_root_value = getattr(settings, "MEDIA_ROOT", None)
        media_root = Path(media_root_value) if media_root_value else None

        client_slug, appeal_slug, pkg_slug, urn_value = (
            DonationScanService._scanned_form_new_path_components(donation)
        )
        new_base = Path(client_slug) / appeal_slug / pkg_slug

        if media_root and media_root.exists():
            for ext in DonationScanService._EXTENSIONS:
                candidate = new_base / f"{urn_value}.{ext}"
                if (media_root / candidate).exists():
                    return str(candidate), True

        default_candidate = new_base / f"{urn_value}.jpg"
        return str(default_candidate), False

    @staticmethod
    def get_scanned_form_path(
        donation: Donation, require_existing: bool = False
    ) -> str | None:
        """Return relative scanned form path, optionally only when it exists.

        Args:
            donation: Donation instance.
            require_existing: If True, only return path when file exists.

        Returns:
            str | None: Relative path to scanned form, or None.
        """
        path, exists = DonationScanService._resolve_scanned_form_path(donation)
        if require_existing and not exists:
            return None
        return path

    @staticmethod
    def get_scanned_form_url(
        donation: Donation,
        require_existing: bool = False,
        user: Any | None = None,
        *,
        allow_pending_redaction: bool = False,
    ) -> str | None:
        """Return the URL for the scanned form if available.

        All scans are stored in Cloudflare R2. Existing scans are viewed through
        the internal page-image proxy, and this method returns the first page URL.

        When ``require_existing=True``: returns the first internal page-image
        proxy URL or ``None`` if no placeholder exists.

        When ``require_existing=False``: returns the predicted local-style
        path string for the viewer hint UI.

        Args:
            donation: Donation instance.
            require_existing: If True, only return URL when scan exists in R2.
            user: Optional request user for redaction-aware URL visibility.

        Returns:
            str | None: Proxy URL, predicted path, or None.
        """
        if require_existing:
            page_urls = DonationScanService.get_scanned_form_page_urls(
                donation,
                require_existing=True,
                user=user,
                allow_pending_redaction=allow_pending_redaction,
            )
            return page_urls[0] if page_urls else None

        relative_path = DonationScanService.get_scanned_form_path(
            donation, require_existing=False
        )
        if not relative_path:
            return None
        media_url = getattr(settings, "MEDIA_URL", "") or ""
        prefix = media_url.rstrip("/")
        normalized_path = relative_path.replace("\\", "/")
        return f"{prefix}/{normalized_path}"

    @staticmethod
    def has_scanned_form(donation: Donation) -> bool:
        """Return True when a scan exists in R2 (scan_placeholder.image_path set).

        Args:
            donation: Donation instance.

        Returns:
            bool: True if a scanned form is available.
        """
        try:
            placeholder = donation.scan_placeholder  # type: ignore[union-attr]
            return bool(placeholder.image_path or placeholder.image_url)
        except Exception:
            return False

    @staticmethod
    def _placeholder_page_keys(placeholder: ScanPlaceholder | None) -> list[str]:
        """Return the ordered storage keys for a placeholder's pages."""
        if not placeholder:
            return []

        page_keys = [key for key in (placeholder.page_keys or []) if key]
        if page_keys:
            return page_keys

        if placeholder.image_path:
            return [placeholder.image_path]

        return []

    @staticmethod
    def get_placeholder_page_urls(
        placeholder: ScanPlaceholder | None,
        user: Any | None = None,
        *,
        allow_pending_redaction: bool = False,
    ) -> list[str]:
        """Return signed proxy URLs for each stored page image.

        When the placeholder's payment method requires manual redaction
        (per ``RedactionSettings``) and redaction is not complete, URLs are
        omitted unless *user* may view unredacted scans.
        """
        if not placeholder:
            return []

        from scans.scan_redaction import scan_urls_hidden_for_user

        if scan_urls_hidden_for_user(
            placeholder, user, allow_pending_redaction=allow_pending_redaction
        ):
            return []

        page_keys = DonationScanService._placeholder_page_keys(placeholder)
        if not page_keys:
            return []

        from django.urls import reverse

        from scans.api_views import build_scan_view_token

        base_url = reverse("custom_admin:scan_image_serve")
        token = build_scan_view_token(str(placeholder.id))
        placeholder_id = str(placeholder.id)
        safe_key_chars = "/:@!$&'()*+,;=-._~"

        query_tail = ""
        if allow_pending_redaction:
            query_tail = "&allow_pending_redaction=1&inline=1"

        return [
            f"{base_url}?key={quote(key, safe=safe_key_chars)}&id={placeholder_id}&token={token}{query_tail}"
            for key in page_keys
        ]

    @staticmethod
    def get_scanned_form_page_urls(
        donation: Donation,
        require_existing: bool = False,
        user: Any | None = None,
        *,
        allow_pending_redaction: bool = False,
    ) -> list[str]:
        """Return ordered page-image URLs for a donation's scan."""
        try:
            placeholder = donation.scan_placeholder  # type: ignore[union-attr]
        except Exception:
            placeholder = None

        if placeholder:
            from scans.scan_redaction import scan_urls_hidden_for_user

            if scan_urls_hidden_for_user(
                placeholder, user, allow_pending_redaction=allow_pending_redaction
            ):
                return []

        page_urls = DonationScanService.get_placeholder_page_urls(
            placeholder,
            user=user,
            allow_pending_redaction=allow_pending_redaction,
        )
        if page_urls or require_existing:
            return page_urls

        predicted_url = DonationScanService.get_scanned_form_url(
            donation,
            require_existing=False,
            user=user,
            allow_pending_redaction=allow_pending_redaction,
        )
        return [predicted_url] if predicted_url else []

    @staticmethod
    def get_scanned_form_url_from_r2(donation: Donation) -> str | None:
        """Return R2 public URL for the scanned form, or None.

        Args:
            donation: Donation instance.

        Returns:
            str | None: Public R2 URL for the scanned form, or None.
        """
        try:
            from core.storage_backends import (
                r2_enabled,
                r2_list_prefix,
                r2_public_url,
            )

            if not r2_enabled():
                return None

            client_slug, appeal_slug, pkg_slug, urn_value = (
                DonationScanService._scanned_form_new_path_components(donation)
            )
            prefix = f"{client_slug}/{appeal_slug}/{pkg_slug}/{urn_value}"
            keys = r2_list_prefix(prefix, max_keys=1)
            if keys:
                return r2_public_url(keys[0])
        except Exception:
            pass
        return None
