"""Per-channel async-safe token-bucket rate limiter for outbound sends.

This module enforces the per-`channel_id` rate limit required by spec §5.2
(outbound message sending) and §6.4 (reliability), with defaults sourced from
the env variables documented in spec §11.2:

* ``AMG_RATE_LIMIT_PER_CHANNEL_RPS`` — sustained refill rate (default ``1.0``)
* ``AMG_RATE_LIMIT_PER_CHANNEL_BURST`` — bucket capacity (default ``5``)

The classic token-bucket model is used: each ``acquire(channel_id)`` lazily
refills the bucket based on elapsed wall-clock time, then either deducts a
token (returns ``None``) or computes how many seconds the caller must wait
for a token to materialize (returns that float).

Concurrency
-----------
A single ``asyncio.Lock`` is held per channel so that the read-modify-write
sequence of ``(refill, decrement-or-reject)`` is atomic across coroutines.
Buckets are stored in-memory; this matches the spec's single-process scope.

Stale-channel cleanup
---------------------
Buckets idle for more than one hour are dropped on the next ``acquire``.
This keeps the bookkeeping bounded in long-running processes that fan out
across many channels over a day.

Error translation
-----------------
This module raises a small placeholder ``RateLimitedError`` carrying the
``retry_after`` seconds. The HTTP boundary (introduced in task #27 / #35)
re-raises this as the canonical ``AMGError(code=RATE_LIMITED, status=429,
headers={"Retry-After": str(int(retry_after))})``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

# Module-level constants -------------------------------------------------------

# Idle threshold after which a per-channel bucket is dropped on next acquire.
_STALE_CHANNEL_TTL_SECONDS: float = 3600.0

# Env-var names; mirrored in `.env.example` and spec §11.2.
ENV_RPS: str = "AMG_RATE_LIMIT_PER_CHANNEL_RPS"
ENV_BURST: str = "AMG_RATE_LIMIT_PER_CHANNEL_BURST"

_DEFAULT_RPS: float = 1.0
_DEFAULT_BURST: int = 5


class RateLimitConfigError(ValueError):
    """Raised at startup when rate-limiter config values are invalid.

    Distinct from ``RateLimitedError`` (which is raised on a single
    over-budget request).
    """


class RateLimitedError(Exception):
    """Raised when a caller has exhausted its per-channel token budget.

    The HTTP boundary translates this into ``AMGError(code=RATE_LIMITED,
    status=429, headers={"Retry-After": str(int(retry_after))})`` once
    task #27 / #35 lands.

    Attributes:
        retry_after: Seconds the caller must wait before retrying.
    """

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"rate_limited; retry_after={retry_after:.3f}s")


@dataclass(slots=True)
class _Bucket:
    """Per-channel token-bucket state.

    ``tokens`` is a float so partial refills accumulate correctly between
    sub-second polls. ``last_refill`` and ``last_used`` use ``time.monotonic``
    so the limiter is immune to wall-clock jumps.
    """

    tokens: float
    last_refill: float
    last_used: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _read_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - exercised via config error path
        raise RateLimitConfigError(f"{name} must be a number, got {raw!r}") from exc


def _read_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - exercised via config error path
        raise RateLimitConfigError(f"{name} must be an integer, got {raw!r}") from exc


class RateLimiter:
    """Async-safe per-channel token-bucket limiter.

    Use ``RateLimiter.from_env()`` to construct from configured env vars.
    Construct directly only in tests.
    """

    __slots__ = ("rps", "burst", "_buckets", "_buckets_lock", "_now")

    def __init__(
        self,
        rps: float,
        burst: int,
        *,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(rps, int | float) or rps != rps:  # NaN check
            raise RateLimitConfigError(f"{ENV_RPS} must be a positive number, got {rps!r}")
        if rps <= 0:
            raise RateLimitConfigError(f"{ENV_RPS} must be > 0, got {rps!r}")
        if not isinstance(burst, int) or isinstance(burst, bool):
            raise RateLimitConfigError(f"{ENV_BURST} must be a positive integer, got {burst!r}")
        if burst <= 0:
            raise RateLimitConfigError(f"{ENV_BURST} must be > 0, got {burst!r}")

        self.rps: float = float(rps)
        self.burst: int = int(burst)
        self._buckets: dict[str, _Bucket] = {}
        # Guards bucket-dict mutation (insert / stale eviction). Per-bucket
        # locks guard refill math; this lock is only held briefly.
        self._buckets_lock: asyncio.Lock = asyncio.Lock()
        self._now = time_source if time_source is not None else time.monotonic

    @classmethod
    def from_env(cls) -> RateLimiter:
        """Construct a limiter from ``AMG_RATE_LIMIT_PER_CHANNEL_*`` env vars.

        Raises ``RateLimitConfigError`` for non-positive or non-numeric values
        so misconfiguration fails loudly at startup rather than silently
        disabling the limiter.
        """

        rps = _read_env_float(ENV_RPS, _DEFAULT_RPS)
        burst = _read_env_int(ENV_BURST, _DEFAULT_BURST)
        return cls(rps=rps, burst=burst)

    async def acquire(self, channel_id: str) -> float | None:
        """Try to consume one token for ``channel_id``.

        Returns ``None`` on success (token deducted). Returns the number of
        seconds the caller must wait before a retry would succeed if the
        bucket is empty.
        """

        bucket = await self._get_or_create_bucket(channel_id)
        async with bucket.lock:
            now = self._now()
            self._refill(bucket, now)
            bucket.last_used = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return None

            # Tokens are short by `(1 - tokens)`. With refill rate `rps`
            # tokens/sec, the wait is `(1 - tokens) / rps` seconds.
            deficit = 1.0 - bucket.tokens
            return deficit / self.rps

    def _refill(self, bucket: _Bucket, now: float) -> None:
        elapsed = now - bucket.last_refill
        if elapsed <= 0:
            # Time source moved backwards or stayed put; just bump the
            # marker so we don't accumulate a giant pseudo-refill on next
            # call.
            bucket.last_refill = now
            return
        bucket.tokens = min(self.burst, bucket.tokens + elapsed * self.rps)
        bucket.last_refill = now

    async def _get_or_create_bucket(self, channel_id: str) -> _Bucket:
        async with self._buckets_lock:
            now = self._now()
            self._evict_stale(now)
            bucket = self._buckets.get(channel_id)
            if bucket is None:
                bucket = _Bucket(
                    tokens=float(self.burst),
                    last_refill=now,
                    last_used=now,
                )
                self._buckets[channel_id] = bucket
            return bucket

    def _evict_stale(self, now: float) -> None:
        """Drop buckets idle longer than ``_STALE_CHANNEL_TTL_SECONDS``.

        Caller must hold ``self._buckets_lock``. We avoid evicting buckets
        whose lock is held (an unlikely race, but cheap to check) so we
        never strand a coroutine waiting on a discarded lock.
        """

        if not self._buckets:
            return
        cutoff = now - _STALE_CHANNEL_TTL_SECONDS
        stale = [
            cid for cid, b in self._buckets.items() if b.last_used < cutoff and not b.lock.locked()
        ]
        for cid in stale:
            del self._buckets[cid]
