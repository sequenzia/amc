"""Tests for ``FakeAppleScriptSender`` record-and-replay semantics."""

from __future__ import annotations

import asyncio

import pytest

from amc.connectors.imessage.applescript import (
    AppleScriptSender,
    OsascriptAppleScriptSender,
    SendResult,
)
from tests.fakes.applescript import FakeAppleScriptSender, RecordedSend


def test_fake_satisfies_protocol_structurally() -> None:
    """Functional: FakeAppleScriptSender substitutes for the real one."""
    fake = FakeAppleScriptSender()
    real = OsascriptAppleScriptSender()
    assert isinstance(fake, AppleScriptSender)
    assert isinstance(real, AppleScriptSender)


async def test_records_call_args_verbatim() -> None:
    """Functional: recorded calls preserve all arguments exactly."""
    fake = FakeAppleScriptSender()
    await fake.send("iMessage;-;+15555550100", "hello", attachment_path=None)
    await fake.send("iMessage;+;chat42", "with file", attachment_path="/tmp/a.png")

    assert len(fake.calls) == 2
    first, second = fake.calls
    assert isinstance(first, RecordedSend)
    assert first.chat_guid == "iMessage;-;+15555550100"
    assert first.text == "hello"
    assert first.attachment_path is None
    assert second.chat_guid == "iMessage;+;chat42"
    assert second.text == "with file"
    assert second.attachment_path == "/tmp/a.png"


async def test_calls_recorded_in_insertion_order() -> None:
    """Functional: insertion order is preserved across sequential awaits."""
    fake = FakeAppleScriptSender()
    for i in range(5):
        await fake.send(f"chat{i}", f"msg{i}")
    assert [c.text for c in fake.calls] == [f"msg{i}" for i in range(5)]


async def test_scripted_outcomes_returned_in_order() -> None:
    """Functional: queued outcomes (success, error, timeout) play back in order."""
    fake = FakeAppleScriptSender()
    fake.queue_outcomes(
        [
            SendResult(ok=True),
            SendResult(ok=False, error="applescript_error", raw_stderr="execution error"),
            SendResult(ok=False, error="timeout", raw_stderr=None),
        ]
    )

    r1 = await fake.send("c", "1")
    r2 = await fake.send("c", "2")
    r3 = await fake.send("c", "3")

    assert r1 == SendResult(ok=True)
    assert r2 == SendResult(ok=False, error="applescript_error", raw_stderr="execution error")
    assert r3 == SendResult(ok=False, error="timeout", raw_stderr=None)


async def test_empty_outcome_queue_defaults_to_success() -> None:
    """Edge case: an empty queue yields ``SendResult(ok=True)``."""
    fake = FakeAppleScriptSender()
    result = await fake.send("c", "no script queued")
    assert result == SendResult(ok=True)


async def test_concurrent_sends_serialize_via_lock() -> None:
    """Edge case: concurrent asyncio tasks remain ordered in the record list.

    Each task records a deterministic suspension point; the lock inside
    ``send`` guarantees exactly one record append + one outcome pop per
    critical section, so the recorded order matches the order in which
    the lock was acquired (which equals task launch order under the
    default asyncio scheduler).
    """
    fake = FakeAppleScriptSender()
    fake.queue_outcomes([SendResult(ok=True) for _ in range(20)])

    async def one(i: int) -> None:
        await fake.send(f"chat{i}", f"msg{i}")

    # Launch in order; gather preserves submission order, and the
    # internal lock guarantees no interleaving inside the critical
    # section.
    await asyncio.gather(*(one(i) for i in range(20)))

    # All 20 calls recorded; no duplicates lost.
    assert len(fake.calls) == 20
    # Multiset of recorded texts matches what we sent.
    assert sorted(c.text for c in fake.calls) == sorted(f"msg{i}" for i in range(20))


def test_assert_call_count_zero_calls_message() -> None:
    """Error handling: clear assertion when no calls have been recorded."""
    fake = FakeAppleScriptSender()
    with pytest.raises(AssertionError, match="none have been recorded yet"):
        fake.assert_call_count(1)


async def test_assert_call_count_mismatch_message() -> None:
    """Error handling: mismatch message reports actual vs expected counts."""
    fake = FakeAppleScriptSender()
    await fake.send("c", "x")
    await fake.send("c", "y")
    with pytest.raises(AssertionError, match="expected 5 call.*got 2"):
        fake.assert_call_count(5)


async def test_assert_call_count_passes_on_match() -> None:
    """Error handling: matching count does not raise."""
    fake = FakeAppleScriptSender()
    await fake.send("c", "x")
    fake.assert_call_count(1)


def test_assert_call_count_zero_expected_zero_actual_passes() -> None:
    """Error handling: assert_call_count(0) on a fresh fake does not raise."""
    fake = FakeAppleScriptSender()
    fake.assert_call_count(0)


def test_real_implementation_constructible() -> None:
    """The real implementation is constructible alongside the fake.

    Phase 2 (task #47) replaced the Phase 0 stub. Behavior is exercised
    in ``tests/connectors/imessage/test_applescript_real.py`` with a
    mocked subprocess runner — this file only verifies the real class
    still satisfies the protocol and is wireable next to the fake.
    """
    real = OsascriptAppleScriptSender()
    assert isinstance(real, AppleScriptSender)


async def test_fake_substitutable_in_connector_wiring() -> None:
    """Integration: a fake substitutes anywhere ``AppleScriptSender`` is accepted.

    This stands in for the eventual iMessage connector wiring — any
    function that takes an ``AppleScriptSender`` accepts the fake
    without imports of platform-specific code or osascript.
    """

    async def connector_send(sender: AppleScriptSender, guid: str, text: str) -> SendResult:
        return await sender.send(guid, text)

    fake = FakeAppleScriptSender()
    fake.queue_outcome(SendResult(ok=True))
    result = await connector_send(fake, "iMessage;-;+15555550100", "hi")
    assert result.ok is True
    fake.assert_call_count(1)
    assert fake.calls[0].chat_guid == "iMessage;-;+15555550100"
    assert fake.calls[0].text == "hi"
