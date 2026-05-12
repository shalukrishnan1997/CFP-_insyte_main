"""Unit tests for the JSON-coordinate ``qa_save_scan_redaction`` view.

The view persists two layers of operator-drawn rectangles without writing
to R2:

* ``cvv_pages`` — applied immediately on the next Stripe authorization
  attempt (PCI DSS Requirement 3.2).
* ``pages`` — applied once the donation is settled (charge succeeded,
  operator rejected, or retention TTL expired).

Status flips to ``REDACTION_CVV_PENDING``; the actual blackout fires from
the Celery tasks downstream.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from custom_admin.views import qa_review
from donations.models import DonationBatch
from scans.models import ScanPlaceholder
from tests.factories import (
    DonationFactory,
    ScanPlaceholderFactory,
    UserFactory,
)


def _patch_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Silence audit-log + flash-message side effects in the QA view."""
    monkeypatch.setattr(qa_review, "log_request_action", lambda *_a, **_k: None)


def _require_redaction_for(method: str) -> None:
    """Flip the singleton so *method* donations require manual redaction."""
    from scans.models import RedactionSettings

    obj = RedactionSettings.get_settings()
    field_map = {
        "card": "require_for_card",
        "cheque": "require_for_cheque",
        "direct_debit": "require_for_direct_debit",
    }
    setattr(obj, field_map[method], True)
    obj.save()


def _block_r2(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Make every R2 call raise so we can prove the view never touches storage."""
    from core import storage_backends

    calls: dict[str, list[Any]] = {
        "get": [],
        "put": [],
        "copy": [],
        "delete": [],
    }

    def boom_get(key: str) -> bytes:
        calls["get"].append(key)
        raise AssertionError("qa_save_scan_redaction must not read from R2")

    def boom_put(key: str, *_a: Any, **_k: Any) -> None:
        calls["put"].append(key)
        raise AssertionError("qa_save_scan_redaction must not write to R2")

    def boom_copy(src: str, dst: str) -> None:
        calls["copy"].append((src, dst))
        raise AssertionError("qa_save_scan_redaction must not copy in R2")

    def boom_delete(key: str) -> None:
        calls["delete"].append(key)
        raise AssertionError("qa_save_scan_redaction must not delete from R2")

    monkeypatch.setattr(storage_backends, "r2_get_object", boom_get)
    monkeypatch.setattr(storage_backends, "r2_put_object", boom_put)
    monkeypatch.setattr(storage_backends, "r2_copy_object", boom_copy)
    monkeypatch.setattr(storage_backends, "r2_delete_object", boom_delete)
    monkeypatch.setattr(storage_backends, "r2_enabled", lambda: True)
    monkeypatch.setattr(
        storage_backends, "r2_public_url", lambda key: f"https://cdn.test/{key}"
    )
    return calls


def _make_redaction_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payment_method: str = "card",
    page_count: int = 1,
) -> tuple[Any, ScanPlaceholder, str]:
    """Build a staff user + donation + placeholder ready for the JSON POST."""
    _require_redaction_for(payment_method)
    _patch_messages(monkeypatch)

    staff = UserFactory(is_staff=True, is_superuser=True)
    donation = DonationFactory(payment_method=payment_method, payment_status="pending")
    keys = [f"ScanOutput/demo/page_{i}.png" for i in range(page_count)]
    placeholder = ScanPlaceholderFactory(
        donation=donation,
        redaction_status=ScanPlaceholder.REDACTION_PENDING,
        image_url="",
        image_path=keys[0],
        page_keys=list(keys),
        original_page_keys=list(keys),
    )
    url = reverse(
        "custom_admin:qa_save_scan_redaction",
        args=[donation.batch_id, donation.id],
    )
    if donation.batch.status != DonationBatch.STATUS_PENDING_QA:
        donation.batch.status = DonationBatch.STATUS_PENDING_QA
        donation.batch.save(update_fields=["status", "updated_at"])
    return staff, placeholder, url


def _post_json(client: Client, url: str, payload: dict[str, Any]) -> Any:
    """POST *payload* as JSON via the Django test client."""
    return client.post(
        url,
        data=json.dumps(payload),
        content_type="application/json",
    )


@pytest.mark.django_db()
class TestQaSaveScanRedactionDeferred:
    """The two-layer deferred-coords flow: save without touching R2."""

    def test_card_happy_path_persists_both_layers(
        self, monkeypatch: pytest.MonkeyPatch, client: Client
    ) -> None:
        """Card method: cvv_pages + pages saved, status flips to CVV_PENDING."""
        staff, placeholder, url = _make_redaction_setup(monkeypatch)
        r2_calls = _block_r2(monkeypatch)

        client.force_login(staff)
        response = _post_json(
            client,
            url,
            {
                "redaction_notes": "card details",
                "cvv_pages": [
                    {
                        "page_index": 0,
                        "rects": [{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}],
                    }
                ],
                "pages": [
                    {
                        "page_index": 0,
                        "rects": [{"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.05}],
                    }
                ],
            },
        )

        assert response.status_code == 200, response.content
        body = response.json()
        assert body["success"] is True
        assert body["redaction_status"] == ScanPlaceholder.REDACTION_CVV_PENDING

        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_CVV_PENDING
        assert placeholder.redaction_coords_cvv == [
            [{"x": 0.7, "y": 0.5, "width": 0.05, "height": 0.03}]
        ]
        assert placeholder.redaction_coords_post_charge == [
            [{"x": 0.1, "y": 0.2, "width": 0.4, "height": 0.05}]
        ]
        # Image stays readable — neither layer has been applied yet.
        assert placeholder.image_path == "ScanOutput/demo/page_0.png"
        # No R2 traffic.
        assert r2_calls == {"get": [], "put": [], "copy": [], "delete": []}

    def test_card_without_cvv_rect_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, client: Client
    ) -> None:
        """PCI 3.2: a card donation must include at least one CVV rectangle."""
        staff, placeholder, url = _make_redaction_setup(monkeypatch)
        _block_r2(monkeypatch)

        client.force_login(staff)
        response = _post_json(
            client,
            url,
            {
                "redaction_notes": "",
                "cvv_pages": [{"page_index": 0, "rects": []}],
                "pages": [
                    {
                        "page_index": 0,
                        "rects": [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}],
                    }
                ],
            },
        )

        assert response.status_code == 400
        body = response.json()
        assert body["success"] is False
        assert "CVV" in body["error"]
        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_PENDING

    def test_non_card_method_no_cvv_required(
        self, monkeypatch: pytest.MonkeyPatch, client: Client
    ) -> None:
        """Cheque has no CVV — empty cvv_pages is valid."""
        staff, placeholder, url = _make_redaction_setup(
            monkeypatch, payment_method="cheque"
        )
        _block_r2(monkeypatch)

        client.force_login(staff)
        response = _post_json(
            client,
            url,
            {
                "redaction_notes": "cheque sig",
                "cvv_pages": [{"page_index": 0, "rects": []}],
                "pages": [
                    {
                        "page_index": 0,
                        "rects": [{"x": 0.1, "y": 0.5, "width": 0.4, "height": 0.05}],
                    }
                ],
            },
        )

        assert response.status_code == 200
        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_CVV_PENDING
        assert placeholder.redaction_coords_cvv == [[]]
        assert placeholder.redaction_coords_post_charge == [
            [{"x": 0.1, "y": 0.5, "width": 0.4, "height": 0.05}]
        ]

    def test_missing_cvv_pages_field_defaults_to_empty(
        self, monkeypatch: pytest.MonkeyPatch, client: Client
    ) -> None:
        """Cheque clients that don't send cvv_pages still validate."""
        staff, placeholder, url = _make_redaction_setup(
            monkeypatch, payment_method="cheque"
        )
        _block_r2(monkeypatch)

        client.force_login(staff)
        response = _post_json(
            client,
            url,
            {
                "redaction_notes": "",
                "pages": [
                    {
                        "page_index": 0,
                        "rects": [{"x": 0.1, "y": 0.5, "width": 0.4, "height": 0.05}],
                    }
                ],
            },
        )

        assert response.status_code == 200
        placeholder.refresh_from_db()
        assert placeholder.redaction_coords_cvv == [[]]

    def test_page_count_mismatch_returns_400(
        self, monkeypatch: pytest.MonkeyPatch, client: Client
    ) -> None:
        """Posting fewer pages than the placeholder owns must reject with 400."""
        staff, placeholder, url = _make_redaction_setup(monkeypatch, page_count=2)

        client.force_login(staff)
        response = _post_json(
            client,
            url,
            {
                "redaction_notes": "",
                "cvv_pages": [
                    {"page_index": 0, "rects": []},
                    {"page_index": 1, "rects": []},
                ],
                "pages": [
                    {
                        "page_index": 0,
                        "rects": [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.2}],
                    }
                ],
            },
        )
        assert response.status_code == 400
        assert response.json()["success"] is False
        placeholder.refresh_from_db()
        assert placeholder.redaction_status == ScanPlaceholder.REDACTION_PENDING

    def test_invalid_json_returns_400(
        self, monkeypatch: pytest.MonkeyPatch, client: Client
    ) -> None:
        """A malformed JSON body is reported as a 400, not a 500."""
        staff, _placeholder, url = _make_redaction_setup(monkeypatch)

        client.force_login(staff)
        response = client.post(url, data="{not json", content_type="application/json")
        assert response.status_code == 400
        assert response.json() == {"success": False, "error": "Invalid JSON body"}
