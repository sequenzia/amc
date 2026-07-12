"""AppleScript send abstraction for the iMessage connector.

This module defines the seam between the connector and the macOS
``osascript`` subprocess used to drive Messages.app. Tests substitute
``FakeAppleScriptSender`` (in ``tests/fakes/applescript.py``) so that
the iMessage send path is exercisable without granting Automation
permission to the test process.

The real implementation (``OsascriptAppleScriptSender``) shells out to
``osascript`` with bounded retries, per spec §5.2 (up to 4 total
attempts: 1 initial + 3 retries) and §7.5 (iMessage send path). On
final failure the resulting ``SendResult(ok=False)`` propagates to
``POST /messages/send`` which returns ``502 PLATFORM_SEND_FAILED``
(spec §7.4 / §13 R-2).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import structlog

__all__ = [
    "DEFAULT_BACKOFF",
    "DEFAULT_TIMEOUT_SECONDS",
    "AppleScriptSender",
    "OsascriptAppleScriptSender",
    "SendResult",
]

# Spec §5.2: up to 4 total attempts -> 3 retries between attempts.
# Backoff sequence is consumed pairwise: sleep(backoff[i]) BETWEEN attempts.
DEFAULT_BACKOFF: tuple[float, ...] = (0.5, 2.0, 5.0)

# Spec §5.2 edge case: "AppleScript times out | osascript hangs > 10 s".
DEFAULT_TIMEOUT_SECONDS: float = 10.0

# Maximum stderr bytes to retain in the SendResult for diagnostics.
_STDERR_EXCERPT_LIMIT = 2048

_logger = structlog.get_logger("amg.connectors.imessage.applescript")

# AppleScript template. Substitutions are passed as positional ``-e`` arguments
# OR as runtime arguments to a ``run`` handler — we use the latter so that
# the *text* never appears as raw source, eliminating the injection vector.
_SCRIPT_TEMPLATE_TEXT_ONLY = """\
on run argv
    set chatGuid to item 1 of argv
    set messageText to item 2 of argv
    tell application "Messages"
        set targetService to first service whose service type = iMessage
        set targetChat to chat id chatGuid
        send messageText to targetChat
    end tell
end run
"""

_SCRIPT_TEMPLATE_WITH_ATTACHMENT = """\
on run argv
    set chatGuid to item 1 of argv
    set messageText to item 2 of argv
    set attachmentPath to item 3 of argv
    tell application "Messages"
        set targetService to first service whose service type = iMessage
        set targetChat to chat id chatGuid
        send messageText to targetChat
        send (POSIX file attachmentPath) to targetChat
    end tell
end run
"""


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outcome of an AppleScript send attempt.

    Attributes:
        ok: ``True`` if the send succeeded.
        error: Short, human-readable error category (e.g. ``"timeout"``,
            ``"applescript_error"``) when ``ok`` is ``False``; ``None`` on success.
        raw_stderr: Raw ``stderr`` bytes from ``osascript``, decoded as UTF-8,
            preserved for diagnostics. ``None`` when there is nothing to record.
    """

    ok: bool
    error: str | None = None
    raw_stderr: str | None = None


@runtime_checkable
class AppleScriptSender(Protocol):
    """Injectable abstraction over ``osascript`` invocation.

    Implementations must be safe to call concurrently from asyncio tasks.
    The real implementation runs the subprocess in ``asyncio.to_thread``;
    fakes simply append to an in-memory list.
    """

    async def send(
        self,
        chat_guid: str,
        text: str,
        attachment_path: str | None = None,
    ) -> SendResult:
        """Send ``text`` (and optional attachment) to the chat ``chat_guid``."""
        ...


class OsascriptAppleScriptSender:
    """Real ``osascript``-backed implementation.

    Invokes ``osascript`` in a worker thread (``asyncio.to_thread``) so the
    asyncio loop is never blocked. The script body is fixed; user-supplied
    ``chat_guid``, ``text``, and ``attachment_path`` are passed as positional
    arguments to an AppleScript ``run`` handler — never spliced into the
    script source. This closes the AppleScript injection vector.

    Retry policy (spec §5.2, §13 R-2):
      * 1 initial attempt + up to 3 retries (4 total).
      * Retried on non-zero exit code AND on ``subprocess.TimeoutExpired``.
      * Linear/exponential backoff between attempts (default ``DEFAULT_BACKOFF``).
      * On final failure returns ``SendResult(ok=False)`` so the caller
        (``POST /messages/send``) can surface ``502 PLATFORM_SEND_FAILED``.

    Args:
        timeout: Per-attempt subprocess timeout in seconds. Defaults to 10 s.
        backoff: Sleep durations BETWEEN attempts. ``len(backoff)`` defines the
            retry count: 3 entries -> 4 total attempts. Defaults to (0.5, 2, 5).
        sleep: Async sleep callable (injected for deterministic tests). Defaults
            to ``asyncio.sleep``.
        runner: Synchronous subprocess runner (injected for tests). Defaults
            to ``subprocess.run``. The real implementation invokes it inside
            ``asyncio.to_thread``.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        backoff: Sequence[float] = DEFAULT_BACKOFF,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] | None = None,
    ) -> None:
        self._timeout = timeout
        self._backoff = tuple(backoff)
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._runner = runner if runner is not None else subprocess.run

    async def send(
        self,
        chat_guid: str,
        text: str,
        attachment_path: str | None = None,
    ) -> SendResult:
        # Edge case: empty text is rejected before invoking the subprocess.
        # AppleScript would silently no-op on an empty string and we'd waste
        # 4 attempts diagnosing nothing.
        if text == "":
            _logger.warning(
                "applescript_send_rejected",
                reason="empty_text",
                chat_guid=chat_guid,
            )
            return SendResult(
                ok=False,
                error="SEND_FAILED",
                raw_stderr="empty text rejected",
            )

        if attachment_path is None:
            script = _SCRIPT_TEMPLATE_TEXT_ONLY
            argv: list[str] = [chat_guid, text]
        else:
            script = _SCRIPT_TEMPLATE_WITH_ATTACHMENT
            argv = [chat_guid, text, attachment_path]

        # Build the osascript command. ``-`` reads the script from stdin so
        # we never have to escape the script body itself; positional argv
        # follows the ``-`` separator and is delivered to ``on run argv``.
        cmd: list[str] = ["osascript", "-", *argv]

        # max_attempts = 1 + len(backoff). With DEFAULT_BACKOFF that's 4.
        max_attempts = 1 + len(self._backoff)
        last_error: str = "unknown"
        last_stderr: str | None = None

        for attempt in range(1, max_attempts + 1):
            outcome = await asyncio.to_thread(self._invoke_once, cmd, script)
            ok, error, stderr_excerpt = outcome
            if ok:
                _logger.info(
                    "applescript_send_attempt",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    chat_guid=chat_guid,
                    outcome="ok",
                )
                return SendResult(ok=True)

            last_error = error
            last_stderr = stderr_excerpt
            _logger.warning(
                "applescript_send_attempt",
                attempt=attempt,
                max_attempts=max_attempts,
                chat_guid=chat_guid,
                outcome="failed",
                error=error,
                stderr=stderr_excerpt,
            )

            # Sleep BEFORE next attempt, but skip after the final attempt.
            if attempt <= len(self._backoff):
                await self._sleep(self._backoff[attempt - 1])

        _logger.error(
            "applescript_send_failed",
            attempts=max_attempts,
            chat_guid=chat_guid,
            error=last_error,
        )
        return SendResult(ok=False, error=last_error, raw_stderr=last_stderr)

    def _invoke_once(
        self,
        cmd: list[str],
        script: str,
    ) -> tuple[bool, str, str | None]:
        """Run osascript once. Returns (ok, error_category, stderr_excerpt).

        ``error_category`` is one of ``"timeout"``, ``"applescript_error"``,
        ``"oserror"``. ``stderr_excerpt`` is the first ``_STDERR_EXCERPT_LIMIT``
        bytes of stderr decoded as UTF-8 (replace errors), or ``None`` when
        stderr is empty / unavailable.
        """
        try:
            completed = self._runner(
                cmd,
                input=script.encode("utf-8"),
                capture_output=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = _decode_stderr(exc.stderr)
            return False, "timeout", stderr
        except OSError as exc:
            return False, "oserror", str(exc)[:_STDERR_EXCERPT_LIMIT]

        if completed.returncode == 0:
            return True, "", None

        return False, "applescript_error", _decode_stderr(completed.stderr)


def _decode_stderr(raw: bytes | str | None) -> str | None:
    """Decode subprocess stderr to a bounded UTF-8 string."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
    else:
        text = raw
    if not text:
        return None
    return text[:_STDERR_EXCERPT_LIMIT]
