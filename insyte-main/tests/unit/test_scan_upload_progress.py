"""Concurrency tests for the scan-upload progress webhook.

These tests guard the atomic ``F()`` / ``Greatest()`` counter update added in
``core/webhooks.py:scan_upload_webhook``. The scanner workstation reports
*absolute* (cumulative) counts on every webhook delivery, so when five POSTs
arrive concurrently from a multi-threaded sync script we can no longer rely on
``update_or_create``'s SELECT-then-UPDATE — concurrent writers with values
``[12, 13, 14, 15, 16]`` would otherwise commit in whichever order the DB
schedules and silently lose increments.

The fix wraps the counter columns in ``Greatest(F("total_uploaded"),
Value(N))`` inside ``transaction.atomic()``. The DB resolves the read and the
write inside a single statement, so:

    final_total_uploaded == max(all_incoming_total_uploaded_values)

regardless of commit order, threading, or out-of-order delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from django.db import connection, connections
from django.test import Client
from pytest_django.fixtures import SettingsWrapper

from scans.models import ScanUploadProgress
from tests.factories import CampaignFactory, ClientFactory

SCAN_SECRET = "scan-secret-test"


def _scan_signature(payload: bytes, timestamp: str) -> str:
    """Return HMAC-SHA256 signature over ``f"{timestamp}.{payload}"``."""
    signed = timestamp.encode() + b"." + payload
    return hmac.new(SCAN_SECRET.encode(), signed, hashlib.sha256).hexdigest()


def _build_payload(campaign_id: str, client_id: str, total_uploaded: int) -> bytes:
    """Build a canonical scan-upload webhook payload as raw bytes."""
    return json.dumps(
        {
            "campaign_id": campaign_id,
            "client_id": client_id,
            "total_uploaded": total_uploaded,
            "total_expected": 50,
            "latest_urn": f"IMG_{total_uploaded:04d}.tiff",
            "status": "scanning",
        }
    ).encode()


def _post_progress(
    client: Client, campaign_id: str, client_id: str, total_uploaded: int
) -> int:
    """POST a scanning-status webhook for ``total_uploaded`` and return status."""
    payload = _build_payload(campaign_id, client_id, total_uploaded)
    # Each call uses a unique timestamp (still within the 60s skew window)
    # so the (client_id, ts) replay guard doesn't 401 sequential posts from
    # the same client. The deliveries used here are small integers so
    # adding them stays well within the allowed window.
    ts_str = str(int(time.time()) + int(total_uploaded))
    signature = _scan_signature(payload, ts_str)
    response = client.post(
        "/webhooks/scan-upload/",
        data=payload,
        content_type="application/json",
        HTTP_X_SIGNATURE=signature,
        HTTP_X_SCAN_TIMESTAMP=ts_str,
    )
    return int(response.status_code)


@pytest.mark.django_db()
class TestScanUploadProgressMonotonic:
    """Sequential out-of-order delivery still converges to ``max(incoming)``.

    These cases run on the standard ``django_db`` fixture because they don't
    need real cross-thread connections — they exercise the same code path the
    threaded test does, just deterministically.
    """

    @pytest.fixture(autouse=True)
    def _set_scan_secret(self, settings: SettingsWrapper) -> None:
        """Pin the scanner HMAC secret so signed payloads validate."""
        from django.core.cache import cache

        settings.SCAN_WEBHOOK_SECRET = SCAN_SECRET
        cache.clear()

    def test_out_of_order_deliveries_keep_max_total_uploaded(
        self, client: Client
    ) -> None:
        """Five out-of-order webhook payloads must converge to the max value."""
        owner = ClientFactory()
        campaign = CampaignFactory(client=owner, status="active")

        # Deliberately scrambled so the largest value is NOT the last write —
        # this is exactly the failure mode update_or_create exhibits when
        # network/thread scheduling reorders concurrent POSTs.
        deliveries = [20, 18, 25, 15, 22]
        for total_uploaded in deliveries:
            status = _post_progress(
                client, str(campaign.id), str(owner.id), total_uploaded
            )
            assert status == 200, f"webhook delivery {total_uploaded} failed"

        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.total_uploaded == max(deliveries)

    def test_lower_count_does_not_overwrite_higher_stored_value(
        self, client: Client
    ) -> None:
        """A delayed lower-count webhook must not regress the stored counter."""
        owner = ClientFactory()
        campaign = CampaignFactory(client=owner, status="active")

        assert _post_progress(client, str(campaign.id), str(owner.id), 42) == 200
        assert _post_progress(client, str(campaign.id), str(owner.id), 5) == 200

        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.total_uploaded == 42

    def test_first_webhook_creates_row(self, client: Client) -> None:
        """The first webhook delivery must INSERT the progress row."""
        owner = ClientFactory()
        campaign = CampaignFactory(client=owner, status="active")

        assert not ScanUploadProgress.objects.filter(campaign=campaign).exists()
        assert _post_progress(client, str(campaign.id), str(owner.id), 7) == 200

        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.total_uploaded == 7
        assert progress.latest_urn == "IMG_0007.tiff"


@pytest.mark.django_db(transaction=True)
class TestScanUploadProgressConcurrent:
    """Real concurrent POSTs from five worker threads must not lose increments.

    Uses ``transaction=True`` because the per-thread connection model only
    works on a non-wrapped database — pytest-django's default ``django_db``
    wraps every test in a transaction and shares a single connection, which
    would deadlock five worker threads.
    """

    @pytest.fixture(autouse=True)
    def _set_scan_secret(self, settings: SettingsWrapper) -> None:
        """Pin the scanner HMAC secret so signed payloads validate."""
        from django.core.cache import cache

        settings.SCAN_WEBHOOK_SECRET = SCAN_SECRET
        cache.clear()

    def test_five_concurrent_webhooks_keep_highest_count(self) -> None:
        """Five threads firing webhook POSTs must converge to ``max(incoming)``.

        This is the live regression for the lost-increment race: under the old
        ``update_or_create``-with-defaults code, two threads could read the
        same prior value and both write back, dropping at least one
        increment. With ``Greatest(F(...), Value(...))`` the database
        guarantees the final stored value equals the largest incoming count.

        SQLite serializes all writers via database-level locking and an
        in-memory database can't be shared across threads, so we skip there
        and rely on the deterministic out-of-order test above to cover the
        same SQL contract. Postgres exercises the real concurrent path.
        """
        if connection.vendor == "sqlite":
            pytest.skip(
                "SQLite serializes writers and can't share :memory: across "
                "threads — the deterministic out-of-order test covers the "
                "same Greatest(F, Value) contract."
            )

        owner = ClientFactory()
        campaign = CampaignFactory(client=owner, status="active")
        campaign_id = str(campaign.id)
        client_id = str(owner.id)

        deliveries = [12, 13, 14, 15, 16]
        start_barrier = threading.Barrier(len(deliveries))

        def fire(total_uploaded: int) -> int:
            # Each worker thread gets a fresh Django Client (and so a fresh
            # request-scoped connection) — Django's test client is cheap to
            # construct and avoids sharing state across threads.
            local_client = Client()
            try:
                start_barrier.wait(timeout=5)
                return _post_progress(
                    local_client, campaign_id, client_id, total_uploaded
                )
            finally:
                # Close any per-thread DB connections opened by the view so
                # SQLite doesn't accumulate handles across threads.
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(deliveries)) as pool:
            statuses: list[Any] = list(pool.map(fire, deliveries))

        assert all(status == 200 for status in statuses), statuses

        progress = ScanUploadProgress.objects.get(campaign=campaign)
        assert progress.total_uploaded == max(deliveries), (
            f"lost increment: stored {progress.total_uploaded}, "
            f"expected max({deliveries}) = {max(deliveries)}"
        )
