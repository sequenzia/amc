"""Per-channel serial dispatcher with parallel-across-channel execution.

Design
------
Each ``channel_id`` gets its own :class:`asyncio.Queue` and a single worker
:class:`asyncio.Task` that drains it. Messages from different channels
process in parallel because each channel has its own task; messages from
the *same* channel process strictly in arrival order because there is only
one consumer.

The ``submit`` call is non-blocking — it pushes onto the queue and returns
immediately. The webhook handler ACKs the adapter as soon as ``submit``
returns, so the adapter does not wait on Claude (which can take minutes).

Idle eviction
-------------
Long-running processes accumulate channel workers as the agent talks to
many people. When a worker's queue stays empty for ``idle_ttl_seconds``
the worker exits and is removed from the registry. The next message to
that channel reconstitutes it.

Delivery-id dedupe
------------------
The adapter retries the same delivery up to 5 times on transport errors.
Each retry carries the same ``X-AMG-Delivery-Id``. We keep a bounded LRU
of recently-seen delivery IDs so a duplicate retry returns ``204`` without
a second Claude invocation. The cache is in-process only — restarts can
double-process at the boundary, but the underlying message DB row already
exists in the adapter and Claude's reply tools are themselves idempotent
(``send_message`` deduplicates by request body in the adapter; ``mark_read``
is naturally idempotent).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import structlog

__all__ = [
    "ChannelDispatcher",
    "DeliveryDedupe",
    "EnvelopeRunner",
]


_log = structlog.get_logger("amg.receiver.dispatcher")


# ---------------------------------------------------------------------------
# Delivery-id dedupe
# ---------------------------------------------------------------------------


class DeliveryDedupe:
    """Bounded LRU of recently-seen delivery IDs.

    Thread/async-safety: not safe for concurrent calls from multiple threads,
    but the receiver is single-threaded asyncio so individual ``add_if_new``
    calls run atomically between awaits.
    """

    def __init__(self, max_size: int) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        self._max_size = max_size
        self._seen: OrderedDict[str, None] = OrderedDict()

    def add_if_new(self, delivery_id: str) -> bool:
        """Return ``True`` if ``delivery_id`` is new (and record it).

        Returns ``False`` if it was already in the cache.
        """
        if delivery_id in self._seen:
            self._seen.move_to_end(delivery_id)
            return False
        self._seen[delivery_id] = None
        if len(self._seen) > self._max_size:
            self._seen.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._seen)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


# A runner is anything that processes one envelope. The dispatcher does not
# know about claude_runner directly so tests can swap in a fake.
EnvelopeRunner = Callable[[dict[str, Any]], Awaitable[None]]


_QUEUE_POLL_INTERVAL: Final[float] = 0.5


@dataclass(slots=True)
class _ChannelWorker:
    queue: asyncio.Queue[dict[str, Any]]
    task: asyncio.Task[None]


class ChannelDispatcher:
    """Routes envelopes to per-channel serial workers."""

    def __init__(
        self,
        runner: EnvelopeRunner,
        *,
        idle_ttl_seconds: float = 300.0,
        time_provider: Callable[[], float] | None = None,
    ) -> None:
        self._runner = runner
        self._idle_ttl = idle_ttl_seconds
        self._time = time_provider or time.monotonic
        self._workers: dict[str, _ChannelWorker] = {}
        self._lock = asyncio.Lock()
        self._stopping = False

    async def submit(self, envelope: dict[str, Any]) -> None:
        """Enqueue ``envelope`` for its channel; create a worker if needed."""
        channel_id = envelope.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id:
            raise ValueError("envelope.channel_id must be a non-empty string")

        async with self._lock:
            if self._stopping:
                raise RuntimeError("dispatcher is shutting down; refusing new work")
            worker = self._workers.get(channel_id)
            if worker is None or worker.task.done():
                worker = self._spawn_worker(channel_id)
                self._workers[channel_id] = worker
            await worker.queue.put(envelope)

    def _spawn_worker(self, channel_id: str) -> _ChannelWorker:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        task = asyncio.create_task(
            self._channel_loop(channel_id, queue),
            name=f"amg.receiver.channel.{channel_id}",
        )
        return _ChannelWorker(queue=queue, task=task)

    async def _channel_loop(self, channel_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        last_active = self._time()
        log = _log.bind(channel_id=channel_id)
        log.info("channel_worker_started")
        try:
            while True:
                # Use wait_for with a short poll so we can self-terminate on
                # idle without sleeping for the full TTL when work arrives.
                try:
                    envelope = await asyncio.wait_for(queue.get(), timeout=_QUEUE_POLL_INTERVAL)
                except TimeoutError:
                    if self._time() - last_active >= self._idle_ttl:
                        log.info("channel_worker_idle_evict")
                        return
                    continue

                last_active = self._time()
                try:
                    await self._runner(envelope)
                except Exception:  # noqa: BLE001 — never kill the worker on runner errors
                    log.exception(
                        "channel_runner_failed",
                        message_id=envelope.get("id"),
                    )
                finally:
                    queue.task_done()
                    last_active = self._time()
        except asyncio.CancelledError:
            log.info("channel_worker_cancelled")
            raise
        finally:
            # Best-effort: drop ourselves from the registry if still listed.
            current = self._workers.get(channel_id)
            if current is not None and current.queue is queue:
                self._workers.pop(channel_id, None)

    async def stop(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """Stop accepting new work and (optionally) wait for in-flight to drain."""
        async with self._lock:
            self._stopping = True
            workers = list(self._workers.values())

        if drain:
            for w in workers:
                await w.queue.join()

        # Cancel any survivors. Idle workers may have self-evicted already.
        for w in workers:
            if not w.task.done():
                w.task.cancel()
        for w in workers:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(w.task, timeout=timeout)

    @property
    def active_channel_count(self) -> int:
        return sum(1 for w in self._workers.values() if not w.task.done())
