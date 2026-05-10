"""Per-channel dispatcher + delivery dedupe tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from amc_receiver.dispatcher import ChannelDispatcher, DeliveryDedupe

# ---------------------------------------------------------------------------
# DeliveryDedupe
# ---------------------------------------------------------------------------


def test_dedupe_first_delivery_returns_true() -> None:
    d = DeliveryDedupe(max_size=10)
    assert d.add_if_new("dlv_1") is True


def test_dedupe_repeat_delivery_returns_false() -> None:
    d = DeliveryDedupe(max_size=10)
    d.add_if_new("dlv_1")
    assert d.add_if_new("dlv_1") is False


def test_dedupe_evicts_oldest_when_full() -> None:
    d = DeliveryDedupe(max_size=3)
    for i in range(3):
        d.add_if_new(f"dlv_{i}")
    # Adding a 4th evicts dlv_0 (oldest).
    d.add_if_new("dlv_3")
    assert d.add_if_new("dlv_0") is True  # treated as new again
    # dlv_3 still present.
    assert d.add_if_new("dlv_3") is False


def test_dedupe_repeat_keeps_entry_warm() -> None:
    """Re-seeing an item should mark it as recently used so it's not the next eviction."""
    d = DeliveryDedupe(max_size=3)
    d.add_if_new("a")
    d.add_if_new("b")
    d.add_if_new("c")
    d.add_if_new("a")  # touch
    d.add_if_new("d")  # should evict 'b' (now oldest), not 'a'
    assert d.add_if_new("a") is False  # still cached
    assert d.add_if_new("b") is True  # was evicted


def test_dedupe_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError):
        DeliveryDedupe(max_size=0)


# ---------------------------------------------------------------------------
# ChannelDispatcher
# ---------------------------------------------------------------------------


async def test_dispatcher_processes_messages_serially_per_channel() -> None:
    seen: list[tuple[str, int]] = []
    started = asyncio.Event()

    async def runner(env: dict[str, Any]) -> None:
        seen.append((env["channel_id"], env["seq"]))
        # Yield the loop so concurrent tasks would interleave if they could.
        await asyncio.sleep(0)
        started.set()

    dispatcher = ChannelDispatcher(runner, idle_ttl_seconds=60)
    try:
        for i in range(5):
            await dispatcher.submit({"channel_id": "A", "seq": i, "id": f"m_{i}"})
        # Wait until processing completes.
        await asyncio.wait_for(started.wait(), timeout=2.0)
        # Drain the queue.
        await asyncio.sleep(0.05)
        for _ in range(20):
            if len(seen) == 5:
                break
            await asyncio.sleep(0.05)
    finally:
        await dispatcher.stop(drain=True, timeout=5.0)

    assert seen == [("A", 0), ("A", 1), ("A", 2), ("A", 3), ("A", 4)]


async def test_dispatcher_runs_different_channels_in_parallel() -> None:
    """Two channels should overlap — neither blocks the other."""
    started_a = asyncio.Event()
    started_b = asyncio.Event()
    proceed = asyncio.Event()
    finished: list[str] = []

    async def runner(env: dict[str, Any]) -> None:
        ch = env["channel_id"]
        if ch == "A":
            started_a.set()
        else:
            started_b.set()
        await proceed.wait()
        finished.append(ch)

    dispatcher = ChannelDispatcher(runner, idle_ttl_seconds=60)
    try:
        await dispatcher.submit({"channel_id": "A", "id": "m1"})
        await dispatcher.submit({"channel_id": "B", "id": "m2"})
        # Both runners should reach the wait.
        await asyncio.wait_for(started_a.wait(), timeout=2.0)
        await asyncio.wait_for(started_b.wait(), timeout=2.0)
        # Now release them.
        proceed.set()
        for _ in range(20):
            if len(finished) == 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await dispatcher.stop(drain=True, timeout=5.0)

    assert sorted(finished) == ["A", "B"]


async def test_dispatcher_runner_exception_does_not_kill_channel() -> None:
    counter: list[int] = []

    async def runner(env: dict[str, Any]) -> None:
        counter.append(env["seq"])
        if env["seq"] == 1:
            raise RuntimeError("boom")

    dispatcher = ChannelDispatcher(runner, idle_ttl_seconds=60)
    try:
        for i in range(3):
            await dispatcher.submit({"channel_id": "X", "seq": i, "id": f"m{i}"})
        for _ in range(20):
            if len(counter) == 3:
                break
            await asyncio.sleep(0.05)
    finally:
        await dispatcher.stop(drain=True, timeout=5.0)

    assert counter == [0, 1, 2]


async def test_dispatcher_idle_eviction() -> None:
    async def runner(env: dict[str, Any]) -> None:
        return None

    # Use a fake clock so we can fast-forward past the TTL deterministically.
    clock = {"t": 0.0}
    dispatcher = ChannelDispatcher(
        runner,
        idle_ttl_seconds=1.0,
        time_provider=lambda: clock["t"],
    )
    try:
        await dispatcher.submit({"channel_id": "Z", "id": "m1"})
        # Wait for the runner to pick it up.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if dispatcher.active_channel_count == 1:
                break
        # Advance past TTL and let the channel loop notice on its next poll.
        clock["t"] = 5.0
        for _ in range(20):
            await asyncio.sleep(0.1)
            if dispatcher.active_channel_count == 0:
                break
        assert dispatcher.active_channel_count == 0
    finally:
        await dispatcher.stop(drain=False, timeout=2.0)


async def test_dispatcher_rejects_envelope_without_channel_id() -> None:
    async def runner(env: dict[str, Any]) -> None:
        return None

    dispatcher = ChannelDispatcher(runner, idle_ttl_seconds=60)
    try:
        with pytest.raises(ValueError):
            await dispatcher.submit({"id": "m1"})
        with pytest.raises(ValueError):
            await dispatcher.submit({"channel_id": "", "id": "m1"})
    finally:
        await dispatcher.stop(drain=False, timeout=2.0)


async def test_dispatcher_refuses_new_work_after_stop() -> None:
    async def runner(env: dict[str, Any]) -> None:
        return None

    dispatcher = ChannelDispatcher(runner, idle_ttl_seconds=60)
    await dispatcher.stop(drain=False, timeout=2.0)
    with pytest.raises(RuntimeError, match="shutting down"):
        await dispatcher.submit({"channel_id": "A", "id": "m1"})
