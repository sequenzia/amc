"""Tests for the per-channel token-bucket rate limiter (`amg.core.rate_limit`).

Covers spec §5.2 (outbound rate-limit acceptance criteria), §6.4
(reliability), and §11.2 (env-var configuration).

Timing strategy: a monkeypatchable fake clock is injected into
``RateLimiter`` so the tests are deterministic and fast — no
``asyncio.sleep`` waiting on real wall time.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from amg.core.rate_limit import (
    ENV_BURST,
    ENV_RPS,
    RateLimitConfigError,
    RateLimitedError,
    RateLimiter,
)


class _FakeClock:
    """Manually-advanced monotonic clock for deterministic tests."""

    __slots__ = ("now",)

    def __init__(self, start: float = 1000.0) -> None:
        self.now: float = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Functional acceptance criteria
# ---------------------------------------------------------------------------


async def test_burst_then_429_with_retry_after():
    """6th send within burst window returns retry_after; sustained 1/s passes."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=1.0, burst=5, time_source=clock)

    # The first `burst` (5) calls should succeed without consuming time.
    for _ in range(5):
        assert await limiter.acquire("c1") is None

    # The 6th call exhausts the bucket → returns retry_after seconds.
    retry_after = await limiter.acquire("c1")
    assert retry_after is not None
    assert retry_after == pytest.approx(1.0, abs=1e-6)


async def test_independent_buckets_per_channel():
    """Different channel_ids have independent buckets."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=1.0, burst=3, time_source=clock)

    # Drain channel A.
    for _ in range(3):
        assert await limiter.acquire("A") is None
    assert await limiter.acquire("A") is not None

    # Channel B is untouched and should still have a full burst.
    for _ in range(3):
        assert await limiter.acquire("B") is None
    assert await limiter.acquire("B") is not None


async def test_send_succeeds_after_retry_after_elapsed():
    """After the reported retry_after, the next acquire succeeds."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=2.0, burst=2, time_source=clock)

    assert await limiter.acquire("c1") is None
    assert await limiter.acquire("c1") is None
    retry_after = await limiter.acquire("c1")
    assert retry_after is not None
    assert retry_after == pytest.approx(0.5, abs=1e-6)

    # Advance the clock by exactly retry_after; next acquire must succeed.
    clock.advance(retry_after)
    assert await limiter.acquire("c1") is None


async def test_sustained_rate_passes():
    """Sustained 1 msg/s after burst stays under the limit indefinitely."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=1.0, burst=5, time_source=clock)

    # Drain the burst.
    for _ in range(5):
        assert await limiter.acquire("c1") is None

    # Then 10 ticks at 1s each — every one must succeed.
    for _ in range(10):
        clock.advance(1.0)
        assert await limiter.acquire("c1") is None


async def test_partial_refill_accumulates():
    """Sub-second elapsed time accumulates fractional tokens correctly."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=4.0, burst=1, time_source=clock)

    assert await limiter.acquire("c1") is None
    # Bucket empty. Wait 0.25s = 1 token at 4 rps.
    clock.advance(0.25)
    assert await limiter.acquire("c1") is None
    # Bucket empty again. 0.1s = 0.4 tokens; still rejects.
    clock.advance(0.1)
    retry_after = await limiter.acquire("c1")
    assert retry_after is not None
    assert retry_after == pytest.approx(0.15, abs=1e-6)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_concurrent_acquire_same_channel_no_overcommit():
    """Concurrent acquire on the same channel: only burst tokens are granted."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=1.0, burst=5, time_source=clock)

    results = await asyncio.gather(*(limiter.acquire("hot") for _ in range(20)))

    granted = sum(1 for r in results if r is None)
    rejected = sum(1 for r in results if r is not None)
    assert granted == 5
    assert rejected == 15
    # No exceptions; all rejections include a positive retry_after.
    assert all(r > 0 for r in results if r is not None)


async def test_concurrent_acquire_different_channels_independent():
    """Concurrent acquire across many channels each gets full burst."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=1.0, burst=2, time_source=clock)

    async def hammer(cid: str) -> list[float | None]:
        return [await limiter.acquire(cid) for _ in range(2)]

    channels = [f"chan{i}" for i in range(10)]
    results = await asyncio.gather(*(hammer(c) for c in channels))

    for per_channel in results:
        assert all(r is None for r in per_channel)


async def test_env_configures_rps_and_burst(monkeypatch: pytest.MonkeyPatch):
    """`from_env()` honors AMG_RATE_LIMIT_PER_CHANNEL_{RPS,BURST}."""

    monkeypatch.setenv(ENV_RPS, "2.5")
    monkeypatch.setenv(ENV_BURST, "7")
    limiter = RateLimiter.from_env()
    assert limiter.rps == 2.5
    assert limiter.burst == 7


async def test_env_defaults(monkeypatch: pytest.MonkeyPatch):
    """`from_env()` with no env vars uses spec defaults (rps=1, burst=5)."""

    monkeypatch.delenv(ENV_RPS, raising=False)
    monkeypatch.delenv(ENV_BURST, raising=False)
    limiter = RateLimiter.from_env()
    assert limiter.rps == 1.0
    assert limiter.burst == 5


# ---------------------------------------------------------------------------
# Error handling — config rejection at startup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_rps", [0, -0.1, -1, -100.0])
def test_negative_or_zero_rps_rejected(bad_rps: float):
    """Direct construction with rps <= 0 raises RateLimitConfigError."""

    with pytest.raises(RateLimitConfigError):
        RateLimiter(rps=bad_rps, burst=5)


@pytest.mark.parametrize("bad_burst", [0, -1, -100])
def test_zero_or_negative_burst_rejected(bad_burst: int):
    """Direct construction with burst <= 0 raises RateLimitConfigError."""

    with pytest.raises(RateLimitConfigError):
        RateLimiter(rps=1.0, burst=bad_burst)


def test_env_negative_rps_rejected(monkeypatch: pytest.MonkeyPatch):
    """`from_env()` rejects AMG_RATE_LIMIT_PER_CHANNEL_RPS=0 at startup."""

    monkeypatch.setenv(ENV_RPS, "0")
    monkeypatch.setenv(ENV_BURST, "5")
    with pytest.raises(RateLimitConfigError) as excinfo:
        RateLimiter.from_env()
    assert ENV_RPS in str(excinfo.value)


def test_env_negative_burst_rejected(monkeypatch: pytest.MonkeyPatch):
    """`from_env()` rejects AMG_RATE_LIMIT_PER_CHANNEL_BURST=-3 at startup."""

    monkeypatch.setenv(ENV_RPS, "1.0")
    monkeypatch.setenv(ENV_BURST, "-3")
    with pytest.raises(RateLimitConfigError) as excinfo:
        RateLimiter.from_env()
    assert ENV_BURST in str(excinfo.value)


def test_env_non_numeric_rps_rejected(monkeypatch: pytest.MonkeyPatch):
    """Non-numeric RPS raises a clear config error rather than silently defaulting."""

    monkeypatch.setenv(ENV_RPS, "abc")
    with pytest.raises(RateLimitConfigError) as excinfo:
        RateLimiter.from_env()
    assert ENV_RPS in str(excinfo.value)


# ---------------------------------------------------------------------------
# Stale-channel cleanup
# ---------------------------------------------------------------------------


async def test_stale_channels_evicted_after_one_hour():
    """Buckets idle > 1h are dropped on next acquire; new bucket starts full."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=1.0, burst=2, time_source=clock)

    # Drain channel A.
    assert await limiter.acquire("A") is None
    assert await limiter.acquire("A") is None
    assert await limiter.acquire("A") is not None

    # Touch a different channel an hour and one second later. This triggers
    # the eviction sweep in `_get_or_create_bucket`.
    clock.advance(3601.0)
    assert await limiter.acquire("B") is None

    # A's bucket should have been evicted; a fresh acquire on A starts with
    # a full burst again.
    assert "A" not in limiter._buckets
    assert await limiter.acquire("A") is None
    assert await limiter.acquire("A") is None
    assert await limiter.acquire("A") is not None


async def test_recently_used_channel_not_evicted():
    """Buckets used within the TTL stay alive."""

    clock = _FakeClock()
    limiter = RateLimiter(rps=1.0, burst=2, time_source=clock)

    assert await limiter.acquire("keep") is None
    clock.advance(1800.0)  # 30 min — under TTL
    assert await limiter.acquire("other") is None
    assert "keep" in limiter._buckets


# ---------------------------------------------------------------------------
# RateLimitedError shape (consumed by the HTTP boundary in #27/#35)
# ---------------------------------------------------------------------------


def test_rate_limited_error_carries_retry_after():
    err = RateLimitedError(retry_after=2.5)
    assert err.retry_after == 2.5
    assert "2.500" in str(err)


# ---------------------------------------------------------------------------
# Hypothesis property test: random concurrent acquire patterns satisfy
#   total_allowed_in_window <= burst + (rate * window)
# ---------------------------------------------------------------------------


@given(
    rps=st.floats(min_value=0.5, max_value=20.0, allow_nan=False, allow_infinity=False),
    burst=st.integers(min_value=1, max_value=20),
    n_calls=st.integers(min_value=1, max_value=200),
    window=st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False),
)
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_total_allowed_within_budget(rps: float, burst: int, n_calls: int, window: float):
    """Across any concurrent burst within a `window`, allowed calls obey budget.

    Token-bucket invariant: in a window of W seconds at rate R with capacity B,
    the maximum number of admitted requests is `B + R*W` (burst + refill).
    """

    async def run() -> int:
        clock = _FakeClock()
        limiter = RateLimiter(rps=rps, burst=burst, time_source=clock)

        async def one_call(delay: float) -> float | None:
            # Simulate a request that arrives `delay` into the window. We
            # advance the shared clock before each call; sequential ordering
            # under the single asyncio event loop keeps this deterministic.
            return await limiter.acquire("c")

        # Spread the calls evenly across the window. Using a fake clock means
        # this stays deterministic regardless of n_calls or window.
        if n_calls == 1:
            offsets = [0.0]
        else:
            step = window / (n_calls - 1) if window > 0 else 0.0
            offsets = [i * step for i in range(n_calls)]

        granted = 0
        prev = 0.0
        for off in offsets:
            clock.advance(off - prev)
            prev = off
            r = await limiter.acquire("c")
            if r is None:
                granted += 1
        return granted

    granted = asyncio.run(run())

    # Budget bound. Add a tiny epsilon to absorb float rounding (e.g. when
    # `rps * window` ends up at 4.9999999 due to division).
    budget = burst + rps * window + 1e-9
    assert granted <= int(budget) + 1, (
        f"granted={granted} exceeded budget={budget} "
        f"(rps={rps}, burst={burst}, n_calls={n_calls}, window={window})"
    )
