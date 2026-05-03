"""Tests for the real ``OsascriptAppleScriptSender``.

The real class shells out to ``osascript`` — these tests inject a fake
``runner`` (subprocess substitute) and a deterministic ``sleep`` so that
no actual subprocess is spawned and the test suite stays fast and
hermetic. Acceptance criteria from spec §5.2 / §7.5 / §13 R-2/R-11.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from amc.connectors.imessage.applescript import (
    DEFAULT_BACKOFF,
    AppleScriptSender,
    OsascriptAppleScriptSender,
    SendResult,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _completed(returncode: int, *, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Build a ``CompletedProcess`` mimicking what ``subprocess.run`` returns."""
    return subprocess.CompletedProcess(args=["osascript"], returncode=returncode, stderr=stderr)


def _make_runner(
    outcomes: list[subprocess.CompletedProcess[bytes] | BaseException],
    *,
    captured_calls: list[dict] | None = None,
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    """Build a fake subprocess runner that replays ``outcomes`` per call.

    Each entry is either a ``CompletedProcess`` to return, or an exception
    to raise (e.g. ``subprocess.TimeoutExpired``).
    """

    iterator = iter(outcomes)

    def runner(
        cmd: list[str],
        *,
        input: bytes | None = None,
        capture_output: bool = False,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        if captured_calls is not None:
            captured_calls.append(
                {
                    "cmd": list(cmd),
                    "input": input,
                    "capture_output": capture_output,
                    "timeout": timeout,
                }
            )
        try:
            outcome = next(iterator)
        except StopIteration as exc:
            raise AssertionError("runner called more times than scripted") from exc
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return runner


def _make_sleep_recorder(sleeps: list[float]):
    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    return fake_sleep


def _make_sender(
    *,
    runner,
    sleeps: list[float] | None = None,
    backoff=DEFAULT_BACKOFF,
    timeout: float = 10.0,
) -> OsascriptAppleScriptSender:
    sleeps = sleeps if sleeps is not None else []
    return OsascriptAppleScriptSender(
        timeout=timeout,
        backoff=backoff,
        sleep=_make_sleep_recorder(sleeps),
        runner=runner,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_protocol() -> None:
    """The real sender must satisfy ``AppleScriptSender`` structurally."""
    sender = OsascriptAppleScriptSender()
    assert isinstance(sender, AppleScriptSender)


# ---------------------------------------------------------------------------
# Functional acceptance criteria
# ---------------------------------------------------------------------------


async def test_success_first_attempt_no_retries() -> None:
    """Functional: exit 0 returns ok=True with NO sleeps and NO retries."""
    calls: list[dict] = []
    sleeps: list[float] = []
    runner = _make_runner([_completed(0)], captured_calls=calls)
    sender = _make_sender(runner=runner, sleeps=sleeps)

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result == SendResult(ok=True)
    assert len(calls) == 1
    assert sleeps == []  # no backoff sleep on first-attempt success


async def test_command_invocation_shape() -> None:
    """Functional: osascript invoked with stdin script + positional argv."""
    calls: list[dict] = []
    runner = _make_runner([_completed(0)], captured_calls=calls)
    sender = _make_sender(runner=runner)

    await sender.send("iMessage;-;+15555550100", "hello world")

    assert len(calls) == 1
    invoked = calls[0]
    assert invoked["cmd"][0] == "osascript"
    # argv tail must include the chat_guid and text positionally.
    assert "iMessage;-;+15555550100" in invoked["cmd"]
    assert "hello world" in invoked["cmd"]
    # Script body is fed via stdin (so we don't escape it inline).
    assert invoked["input"] is not None
    assert b"on run argv" in invoked["input"]
    assert invoked["capture_output"] is True
    assert invoked["timeout"] == 10.0


async def test_command_includes_attachment_when_provided() -> None:
    """Functional: attachment_path is passed positionally to osascript."""
    calls: list[dict] = []
    runner = _make_runner([_completed(0)], captured_calls=calls)
    sender = _make_sender(runner=runner)

    await sender.send("iMessage;-;+15555550100", "see attached", "/tmp/foo.png")

    invoked = calls[0]
    assert "/tmp/foo.png" in invoked["cmd"]
    assert b"POSIX file" in invoked["input"]


async def test_nonzero_exit_retries_three_times_then_gives_up() -> None:
    """Functional: persistent non-zero exit triggers exactly 3 retries (4 total)."""
    sleeps: list[float] = []
    runner = _make_runner(
        [
            _completed(1, stderr=b"err1"),
            _completed(1, stderr=b"err2"),
            _completed(1, stderr=b"err3"),
            _completed(1, stderr=b"err4-final"),
        ]
    )
    sender = _make_sender(runner=runner, sleeps=sleeps)

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result.ok is False
    assert result.error == "applescript_error"
    assert result.raw_stderr is not None
    assert "err4-final" in result.raw_stderr
    # 3 sleeps between 4 attempts, matching DEFAULT_BACKOFF.
    assert sleeps == list(DEFAULT_BACKOFF)


async def test_recovers_after_one_failure() -> None:
    """Functional: ok after some failures returns ok=True without further retries."""
    sleeps: list[float] = []
    runner = _make_runner(
        [
            _completed(1, stderr=b"transient"),
            _completed(0),
        ]
    )
    sender = _make_sender(runner=runner, sleeps=sleeps)

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result.ok is True
    # One sleep before the successful retry; no further sleeps.
    assert sleeps == [DEFAULT_BACKOFF[0]]


async def test_timeout_counted_as_failed_attempt_and_retried() -> None:
    """Functional: TimeoutExpired counts as a failed attempt and retries."""
    sleeps: list[float] = []
    runner = _make_runner(
        [
            subprocess.TimeoutExpired(cmd="osascript", timeout=10.0, stderr=b"hung"),
            _completed(0),
        ]
    )
    sender = _make_sender(runner=runner, sleeps=sleeps)

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result.ok is True
    assert sleeps == [DEFAULT_BACKOFF[0]]


async def test_persistent_timeout_returns_send_failed() -> None:
    """Functional: 4 timeouts in a row -> SendResult(ok=False, error='timeout')."""
    sleeps: list[float] = []
    runner = _make_runner(
        [subprocess.TimeoutExpired(cmd="osascript", timeout=10.0) for _ in range(4)]
    )
    sender = _make_sender(runner=runner, sleeps=sleeps)

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result.ok is False
    assert result.error == "timeout"
    assert sleeps == list(DEFAULT_BACKOFF)


async def test_applescript_injection_quotes_do_not_corrupt_script() -> None:
    """Functional: text containing " and \\n is passed verbatim, not spliced.

    The injection vector is closed because the text travels as a positional
    argv item; the script body (fed via stdin) is a fixed template that
    references ``item 2 of argv``. Hostile text never appears as source.
    """
    calls: list[dict] = []
    runner = _make_runner([_completed(0)], captured_calls=calls)
    sender = _make_sender(runner=runner)

    hostile = 'hi"\nset x to do shell script "rm -rf /"'
    result = await sender.send("iMessage;-;+15555550100", hostile)

    assert result.ok is True
    invoked = calls[0]
    # Hostile text appears as a discrete argv element — never inside the script.
    assert hostile in invoked["cmd"]
    assert hostile.encode("utf-8") not in invoked["input"]
    # Script template is unchanged.
    assert invoked["input"].startswith(b"on run argv")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_empty_text_rejected_without_invoking_subprocess() -> None:
    """Edge case: empty text returns SEND_FAILED without spawning osascript."""
    calls: list[dict] = []
    sleeps: list[float] = []
    runner = _make_runner([], captured_calls=calls)  # not allowed to be called
    sender = _make_sender(runner=runner, sleeps=sleeps)

    result = await sender.send("iMessage;-;+15555550100", "")

    assert result.ok is False
    assert result.error == "SEND_FAILED"
    assert calls == []
    assert sleeps == []


async def test_unicode_text_round_trips() -> None:
    """Edge case: emoji + non-Latin survive verbatim in argv."""
    calls: list[dict] = []
    runner = _make_runner([_completed(0)], captured_calls=calls)
    sender = _make_sender(runner=runner)

    text = "Hello 你好 👋🏽 — café"
    result = await sender.send("iMessage;-;+15555550100", text)

    assert result.ok is True
    assert text in calls[0]["cmd"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_oserror_counts_as_failed_attempt() -> None:
    """Error handling: OSError (e.g. osascript missing) is retried, not raised."""
    sleeps: list[float] = []
    runner = _make_runner(
        [
            FileNotFoundError("osascript not found"),
            _completed(0),
        ]
    )
    sender = _make_sender(runner=runner, sleeps=sleeps)

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result.ok is True
    assert sleeps == [DEFAULT_BACKOFF[0]]


async def test_final_failure_propagates_send_result_for_502() -> None:
    """Error handling: exhausted retries -> SendResult(ok=False) for 502 mapping.

    The /messages/send endpoint translates this to 502 PLATFORM_SEND_FAILED.
    """
    runner = _make_runner(
        [_completed(2, stderr=b"automation denied") for _ in range(4)]
    )
    sender = _make_sender(runner=runner)

    result = await sender.send("iMessage;-;+15555550100", "hi")

    assert isinstance(result, SendResult)
    assert result.ok is False
    assert result.error == "applescript_error"
    assert result.raw_stderr is not None
    assert "automation denied" in result.raw_stderr


async def test_stderr_excerpt_decoded_with_replace() -> None:
    """Error handling: invalid UTF-8 in stderr does not raise; gets replacement chars."""
    runner = _make_runner(
        [_completed(1, stderr=b"\xff\xfeinvalid\x80")] * 4
    )
    sender = _make_sender(runner=runner)

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result.ok is False
    assert result.raw_stderr is not None
    # Must NOT raise on decode; "invalid" word survives.
    assert "invalid" in result.raw_stderr


# ---------------------------------------------------------------------------
# Configurability
# ---------------------------------------------------------------------------


async def test_custom_backoff_controls_retry_count() -> None:
    """Custom backoff length defines retry count: 1 entry -> 2 total attempts."""
    sleeps: list[float] = []
    runner = _make_runner(
        [
            _completed(1, stderr=b"nope"),
            _completed(1, stderr=b"still nope"),
        ]
    )
    sender = _make_sender(runner=runner, sleeps=sleeps, backoff=(0.1,))

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result.ok is False
    assert sleeps == [0.1]


async def test_zero_backoff_means_no_retries() -> None:
    """Empty backoff sequence -> single attempt only."""
    sleeps: list[float] = []
    runner = _make_runner([_completed(1, stderr=b"oof")])
    sender = _make_sender(runner=runner, sleeps=sleeps, backoff=())

    result = await sender.send("iMessage;-;+15555550100", "hello")

    assert result.ok is False
    assert sleeps == []


async def test_custom_timeout_passed_to_runner() -> None:
    """Custom timeout reaches the subprocess runner."""
    calls: list[dict] = []
    runner = _make_runner([_completed(0)], captured_calls=calls)
    sender = _make_sender(runner=runner, timeout=3.5)

    await sender.send("iMessage;-;+15555550100", "hi")

    assert calls[0]["timeout"] == 3.5


