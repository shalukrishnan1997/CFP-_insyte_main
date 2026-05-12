"""Unit tests for the QA dashboard versioned-cache stampede fix.

Today's pre-fix behaviour: every batch approval ran ``cache.delete`` against a
single shared key, so when 100 reviewers reload the dashboard immediately after
a state change all 100 requests recompute the four COUNT queries simultaneously
— a thundering-herd cache stampede.

The fix in :mod:`custom_admin.views.qa_review` swaps the deletion for an
atomic version-counter bump, switches readers to ``f"qa_dashboard_stats:v{n}"``
keys, and adds a Redis-locked recompute path so concurrent cold-cache loads
collapse to one underlying compute call.
"""

from __future__ import annotations

from collections.abc import Iterator
from threading import Barrier, Thread

import pytest
from django.core.cache import cache

from custom_admin.views import qa_review


@pytest.fixture(autouse=True)
def _clear_cache_between_tests() -> Iterator[None]:
    """Reset the locmem cache before and after each test.

    The version counter is module-global state inside the cache; without this
    fixture, tests can leak counter values into each other and yield flaky
    ``v0`` vs ``v1`` mismatches.
    """
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Version counter behaviour
# ---------------------------------------------------------------------------


class TestDashboardStatsVersionCounter:
    """``_bump_dashboard_stats_version`` must be atomic and self-seeding."""

    def test_first_bump_seeds_counter(self) -> None:
        """When the counter key is absent, the first bump must seed it."""
        assert cache.get(qa_review._QA_DASHBOARD_STATS_VERSION_KEY) is None

        qa_review._bump_dashboard_stats_version()

        # We don't pin the exact starting integer — both ``incr`` (which raises
        # ValueError on cold keys for stdlib backends) and ``add`` seed paths
        # converge on a positive small integer (1).
        version = cache.get(qa_review._QA_DASHBOARD_STATS_VERSION_KEY)
        assert isinstance(version, int)
        assert version >= 1

    def test_subsequent_bumps_increment_monotonically(self) -> None:
        """Each bump must increase the counter by exactly one."""
        qa_review._bump_dashboard_stats_version()
        first = cache.get(qa_review._QA_DASHBOARD_STATS_VERSION_KEY)
        assert isinstance(first, int)

        qa_review._bump_dashboard_stats_version()
        qa_review._bump_dashboard_stats_version()

        second = cache.get(qa_review._QA_DASHBOARD_STATS_VERSION_KEY)
        assert isinstance(second, int)
        assert second == first + 2

    def test_concurrent_bumps_do_not_lose_increments(self) -> None:
        """Threaded bumps must collapse onto an atomic counter (no lost writes)."""
        thread_count = 25
        barrier = Barrier(thread_count)

        def _hammer() -> None:
            barrier.wait()
            qa_review._bump_dashboard_stats_version()

        threads = [Thread(target=_hammer) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        final = cache.get(qa_review._QA_DASHBOARD_STATS_VERSION_KEY)
        assert isinstance(final, int)
        assert final == thread_count, (
            f"Expected {thread_count} successful increments, got {final}"
        )


# ---------------------------------------------------------------------------
# Lock-on-rebuild stampede defence
# ---------------------------------------------------------------------------


class _ComputeRecorder:
    """Wrap ``_compute_qa_dashboard_stats`` while counting invocations.

    Used to assert that 100 concurrent dashboard loads collapse to exactly
    one underlying compute when the version was just bumped.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dict[str, int]:
        self.calls += 1
        return {
            "pending_qa": 7,
            "in_review": 0,
            "approved_today": 3,
            "rejected": 1,
        }


class TestDashboardStatsStampedeDefence:
    """100 concurrent loads + 1 approval must trigger only one compute."""

    def test_concurrent_loads_after_approval_compute_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock-on-rebuild must collapse 100 post-approval readers to one compute.

        Models the production scenario: the dashboard already had its stats
        cached (steady state), an operator approves a batch (version bumps),
        and 100 reviewers reload simultaneously. Exactly one of those 100
        reloads must run the underlying compute; the other 99 must fall back
        to the previous version's still-fresh payload via the lock-loser
        branch.
        """
        recorder = _ComputeRecorder()
        monkeypatch.setattr(qa_review, "_compute_qa_dashboard_stats", recorder)

        # Steady-state: warm the v0 key so lock losers have something to
        # serve when v1 is cold. This compute happens BEFORE the reader
        # storm and is excluded from the stampede assertion.
        qa_review._load_qa_dashboard_stats()
        warmup_calls = recorder.calls
        assert warmup_calls == 1

        # Approval fires — version flips to v1, leaving v0's payload still
        # cached and v1 cold.
        qa_review._bump_dashboard_stats_version()

        thread_count = 100
        barrier = Barrier(thread_count)
        results: list[dict[str, int]] = []

        def _load() -> None:
            barrier.wait()
            stats = qa_review._load_qa_dashboard_stats()
            results.append(stats)

        threads = [Thread(target=_load) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        recompute_calls = recorder.calls - warmup_calls
        assert recompute_calls == 1, (
            f"Stampede protection failed: {recompute_calls} compute calls "
            "during the reader storm, expected 1"
        )
        assert len(results) == thread_count

    def test_warm_cache_serves_without_compute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once a version is cached, repeated loads must not recompute."""
        recorder = _ComputeRecorder()
        monkeypatch.setattr(qa_review, "_compute_qa_dashboard_stats", recorder)

        # Warm the cache with the first reader.
        first = qa_review._load_qa_dashboard_stats()
        assert recorder.calls == 1
        assert first == {
            "pending_qa": 7,
            "in_review": 0,
            "approved_today": 3,
            "rejected": 1,
        }

        # All subsequent reads at the same version must hit the cache.
        for _ in range(50):
            qa_review._load_qa_dashboard_stats()
        assert recorder.calls == 1, (
            f"Warm cache must not recompute, got {recorder.calls} extra calls"
        )

    def test_version_bump_invalidates_without_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bumping the version must force the next load to recompute."""
        recorder = _ComputeRecorder()
        monkeypatch.setattr(qa_review, "_compute_qa_dashboard_stats", recorder)

        qa_review._load_qa_dashboard_stats()
        assert recorder.calls == 1

        qa_review._bump_dashboard_stats_version()
        qa_review._load_qa_dashboard_stats()

        assert recorder.calls == 2, (
            "Version bump must invalidate the previously cached payload"
        )

    def test_lock_loser_returns_previous_version_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reader that loses the lock race must reuse the prior version's value.

        Avoids the worst-case "fall through to a fresh compute" path; that
        backstop only fires when no previous payload exists at all (truly
        cold cache on a brand-new replica).
        """
        recorder = _ComputeRecorder()
        monkeypatch.setattr(qa_review, "_compute_qa_dashboard_stats", recorder)

        # Warm version 0 with a known payload.
        first = qa_review._load_qa_dashboard_stats()
        assert recorder.calls == 1

        # Bump to version 1 and pre-acquire that version's lock so the next
        # call walks the lock-loser branch.
        qa_review._bump_dashboard_stats_version()
        version = qa_review._current_qa_dashboard_stats_version()
        lock_acquired = cache.add(
            f"qa_dashboard_stats:v{version}:lock",
            "1",
            qa_review._QA_DASHBOARD_STATS_LOCK_TTL_SECONDS,
        )
        assert lock_acquired, "Test setup: failed to acquire the version lock"

        try:
            served = qa_review._load_qa_dashboard_stats()
        finally:
            cache.delete(f"qa_dashboard_stats:v{version}:lock")

        # The lock loser must NOT have recomputed.
        assert recorder.calls == 1, (
            f"Lock loser must reuse the previous version's payload, "
            f"but compute ran {recorder.calls} times"
        )
        assert served == first
