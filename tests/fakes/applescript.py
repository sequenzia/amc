"""Test double for ``AppleScriptSender``.

``FakeAppleScriptSender`` records every ``send(...)`` call (chat_guid,
text, attachment_path, timestamp) and returns scripted outcomes from a
queue supplied by the test. It satisfies the ``AppleScriptSender``
protocol structurally, so it substitutes for the real implementation
anywhere the protocol is accepted.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from amg.connectors.imessage.applescript import SendResult


@dataclass(frozen=True, slots=True)
class RecordedSend:
    """One observed call to ``FakeAppleScriptSender.send``."""

    chat_guid: str
    text: str
    attachment_path: str | None
    timestamp: datetime


@dataclass
class FakeAppleScriptSender:
    """In-memory fake that records calls and replays scripted outcomes.

    Usage::

        fake = FakeAppleScriptSender()
        fake.queue_outcomes([
            SendResult(ok=True),
            SendResult(ok=False, error="timeout", raw_stderr="..."),
        ])
        await fake.send("iMessage;-;+15555550100", "hello")
        fake.assert_call_count(1)
        assert fake.calls[0].text == "hello"

    The class is *not* an ``asyncio.Lock``-protected critical section;
    it relies on the cooperative single-threaded asyncio scheduler. The
    only ``await`` inside ``send`` is ``asyncio.sleep(0)`` to provide a
    deterministic suspension point for tests that interleave tasks. A
    single internal lock guards the record-and-pop step so sequential
    asyncio resumption order is preserved in the recorded list.
    """

    calls: list[RecordedSend] = field(default_factory=list)
    _outcomes: deque[SendResult] = field(default_factory=deque)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def queue_outcomes(self, outcomes: Iterable[SendResult]) -> None:
        """Append scripted ``SendResult`` values to the outcome queue."""
        self._outcomes.extend(outcomes)

    def queue_outcome(self, outcome: SendResult) -> None:
        """Append a single scripted ``SendResult``."""
        self._outcomes.append(outcome)

    async def send(
        self,
        chat_guid: str,
        text: str,
        attachment_path: str | None = None,
    ) -> SendResult:
        """Record the call and return the next scripted outcome.

        If the outcome queue is empty, a successful ``SendResult`` is
        returned (``ok=True``).
        """
        async with self._lock:
            self.calls.append(
                RecordedSend(
                    chat_guid=chat_guid,
                    text=text,
                    attachment_path=attachment_path,
                    timestamp=datetime.now(UTC),
                )
            )
            outcome = self._outcomes.popleft() if self._outcomes else SendResult(ok=True)
        return outcome

    def assert_call_count(self, expected: int) -> None:
        """Assert that ``send`` has been called exactly ``expected`` times.

        Raises ``AssertionError`` with a message that distinguishes the
        "no calls recorded yet" case from a normal mismatch, so tests
        that inspect calls before any have been made get a clear error.
        """
        actual = len(self.calls)
        if actual == 0 and expected != 0:
            raise AssertionError(
                f"FakeAppleScriptSender: expected {expected} call(s) but none "
                "have been recorded yet — did you forget to await send()?"
            )
        if actual != expected:
            raise AssertionError(
                f"FakeAppleScriptSender: expected {expected} call(s), got {actual}"
            )
