"""In-process fake for the Discord REST API.

`discord.py` performs all REST traffic through `aiohttp` (not `httpx`), so the
spec's hint of "respx (or equivalent HTTP mocker)" cannot literally use respx
here — respx is bound to httpx. Instead this fake patches
`discord.http.HTTPClient.request` directly. That is the narrowest interception
point that:

* records every outbound REST call in ``rest.sent_messages`` for assertions,
* returns realistic, snowflake-shaped payloads so `discord.py` can hydrate a
  real `discord.Message` object,
* honors a script of ``429`` / ``5xx`` outcomes by performing the exact same
  retry semantics that `discord.py`'s real ``request()`` performs (retry on
  ``429`` / ``500`` / ``502`` / ``504`` / ``524``, raise the matching
  ``discord.HTTPException`` subclass on terminal errors).

Because the fake replaces the request loop, retries do not pause the test.

Usage:

    with FakeDiscordRest() as rest:
        await channel.send("hi")
        assert rest.sent_messages[-1]["content"] == "hi"
"""

from __future__ import annotations

import datetime as _dt
import itertools
import threading
from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any
from unittest.mock import patch

import discord
from discord import http as _discord_http
from discord.errors import (
    DiscordServerError,
    Forbidden,
    HTTPException,
    NotFound,
    RateLimited,
)

# ---------------------------------------------------------------------------
# Snowflake allocator
# ---------------------------------------------------------------------------

# Discord snowflakes are 64-bit ints with the top 42 bits being a millisecond
# timestamp relative to the Discord epoch. We don't need real bit packing for
# tests — only stable, monotonically increasing 64-bit ids that look like
# snowflakes when consumers cast them to int.
_DISCORD_EPOCH_MS = 1_420_070_400_000


class _SnowflakeAllocator:
    """Thread-safe monotonically increasing snowflake-shaped id allocator."""

    def __init__(self, *, base_offset_ms: int = 0) -> None:
        self._lock = threading.Lock()
        self._counter = itertools.count(start=1)
        self._base_offset_ms = base_offset_ms

    def next(self) -> int:
        with self._lock:
            seq = next(self._counter)
        # Pack a fake snowflake: (ms_offset << 22) | seq
        # The exact layout doesn't matter — we just need uniqueness and
        # monotonic ordering when sorted as ints.
        ms = int(_dt.datetime.now(tz=_dt.UTC).timestamp() * 1000) - _DISCORD_EPOCH_MS
        ms += self._base_offset_ms
        # Ensure monotonicity even if many calls land in the same millisecond
        # by mixing the sequence into the low bits.
        return (ms << 22) | (seq & ((1 << 22) - 1))


# ---------------------------------------------------------------------------
# Recorded call type
# ---------------------------------------------------------------------------


class _ScriptedOutcome:
    """A scripted REST response queued for the next matching request.

    Use the ``rest.script_*`` helpers rather than constructing this directly.
    """

    __slots__ = ("status", "body", "headers")

    def __init__(
        self,
        status: int,
        body: dict[str, Any] | str | None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body if body is not None else {}
        self.headers = dict(headers) if headers else {}


# ---------------------------------------------------------------------------
# Fake REST client
# ---------------------------------------------------------------------------


class FakeDiscordRest(AbstractContextManager["FakeDiscordRest"]):
    """In-process fake of the Discord REST API for `discord.py`.

    Patches `discord.http.HTTPClient.request` for the duration of the
    ``with`` block. Records every outbound send into :attr:`sent_messages`
    and returns realistic message payloads so the caller's
    `discord.Message` object is fully populated.

    Parameters
    ----------
    bot_user_id:
        Snowflake the fake bot will report as the message author.
    bot_username:
        Username string returned in the synthetic ``author`` payload.
    """

    def __init__(
        self,
        *,
        bot_user_id: int = 1_000_000_000_000_000_001,
        bot_username: str = "fake-bot",
    ) -> None:
        self.bot_user_id = bot_user_id
        self.bot_username = bot_username

        # Public, append-only list of recorded outbound sends. Entries are
        # plain dicts with keys: method, path, channel_id, content,
        # message_reference, files, payload, response_status.
        self.sent_messages: list[dict[str, Any]] = []
        # Generic record of every routed call (including non-message routes).
        self.calls: list[dict[str, Any]] = []

        self._snowflake = _SnowflakeAllocator()
        # Keyed by (METHOD, path_template) — e.g. ("POST",
        # "/channels/{channel_id}/messages"). FIFO queue.
        self._scripts: dict[tuple[str, str], deque[_ScriptedOutcome]] = defaultdict(deque)
        # Custom route handlers registered by tests.
        self._custom_handlers: dict[
            tuple[str, str],
            Any,  # callable(request_ctx) -> _ScriptedOutcome | dict
        ] = {}

        self._patcher: Any | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> FakeDiscordRest:
        if self._patcher is not None:
            raise RuntimeError("FakeDiscordRest is already active")

        fake = self

        async def _request_replacement(
            http_client_self: Any,
            route: _discord_http.Route,
            *,
            files: Sequence[Any] | None = None,
            form: Iterable[dict[str, Any]] | None = None,
            **kwargs: Any,
        ) -> Any:
            return await fake._dispatch(
                http_client_self, route, files=files, form=form, **kwargs
            )

        self._patcher = patch.object(
            _discord_http.HTTPClient, "request", _request_replacement
        )
        self._patcher.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._patcher is not None:
            self._patcher.stop()
            self._patcher = None

    # ------------------------------------------------------------------
    # Scripting API
    # ------------------------------------------------------------------

    def script_response(
        self,
        method: str,
        path_template: str,
        *,
        status: int = 200,
        body: dict[str, Any] | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Queue a scripted outcome for the next matching request."""
        key = (method.upper(), path_template)
        self._scripts[key].append(_ScriptedOutcome(status, body, headers))

    def script_rate_limit(
        self,
        method: str,
        path_template: str,
        *,
        retry_after: float = 0.0,
    ) -> None:
        """Queue a 429 response that triggers `discord.py`'s retry logic."""
        body = {
            "message": "You are being rate limited.",
            "retry_after": retry_after,
            "global": False,
        }
        # `Via` header + non-string body is required for discord.py to
        # treat the 429 as a retryable rate limit (vs. a Cloudflare ban).
        headers = {"Via": "1.1 fake-discord-rest", "X-Ratelimit-Remaining": "0"}
        self.script_response(
            method, path_template, status=429, body=body, headers=headers
        )

    def script_server_error(
        self,
        method: str,
        path_template: str,
        *,
        status: int = 500,
    ) -> None:
        """Queue a 5xx response that triggers `discord.py`'s retry logic."""
        if status not in {500, 502, 504, 524}:
            raise ValueError(
                "Only 500/502/504/524 trigger discord.py's automatic retry path; "
                f"got {status}"
            )
        self.script_response(
            method,
            path_template,
            status=status,
            body={"message": "fake server error"},
        )

    def register_handler(
        self,
        method: str,
        path_template: str,
        handler: Any,
    ) -> None:
        """Register a custom handler ``(request_ctx) -> dict`` for a route."""
        self._custom_handlers[(method.upper(), path_template)] = handler

    # ------------------------------------------------------------------
    # Patched request implementation
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        _http_client: Any,
        route: _discord_http.Route,
        *,
        files: Sequence[Any] | None = None,
        form: Iterable[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        method = route.method
        path_template = route.path
        # Concrete URL (with channel_id etc. interpolated).
        url = route.url
        json_body = kwargs.get("json")

        attempts_for_this_call = 0
        while True:
            attempts_for_this_call += 1
            outcome = self._next_outcome(method, path_template, route, json_body, files, form)
            recorded = self._record_call(
                method=method,
                path_template=path_template,
                url=url,
                route=route,
                json_body=json_body,
                files=files,
                form=form,
                outcome=outcome,
                attempt=attempts_for_this_call,
            )

            status = outcome.status

            # Success path
            if 200 <= status < 300:
                return outcome.body

            # Retry-on-429 — mimic discord.py's loop, but instantaneously.
            if status == 429:
                # Cloudflare-shaped 429 (no Via header / string body) => terminal.
                if not outcome.headers.get("Via") or isinstance(outcome.body, str):
                    raise HTTPException(_FakeAioResponse(status, outcome.headers), outcome.body)
                # Honor a max_ratelimit_timeout configured on the HTTPClient.
                retry_after = (
                    float(outcome.body.get("retry_after", 0.0))
                    if isinstance(outcome.body, dict)
                    else 0.0
                )
                max_timeout = getattr(_http_client, "max_ratelimit_timeout", None)
                if max_timeout and retry_after > max_timeout:
                    raise RateLimited(retry_after)
                # If we have another scripted outcome queued, retry; otherwise
                # treat as terminal so the caller can observe the 429.
                if self._has_more_outcomes(method, path_template):
                    recorded["retried"] = True
                    continue
                raise HTTPException(_FakeAioResponse(status, outcome.headers), outcome.body)

            # Retry-on-5xx (per discord.py: 500/502/504/524 only).
            if status in {500, 502, 504, 524} and self._has_more_outcomes(method, path_template):
                recorded["retried"] = True
                continue

            # Terminal error mapping — match discord.py's request() switch.
            response = _FakeAioResponse(status, outcome.headers)
            if status == 403:
                raise Forbidden(response, outcome.body)
            if status == 404:
                raise NotFound(response, outcome.body)
            if status >= 500:
                raise DiscordServerError(response, outcome.body)
            raise HTTPException(response, outcome.body)

    # ------------------------------------------------------------------
    # Outcome / route resolution
    # ------------------------------------------------------------------

    def _next_outcome(
        self,
        method: str,
        path_template: str,
        route: _discord_http.Route,
        json_body: dict[str, Any] | None,
        files: Sequence[Any] | None,
        form: Iterable[dict[str, Any]] | None,
    ) -> _ScriptedOutcome:
        key = (method.upper(), path_template)

        with self._lock:
            queue = self._scripts.get(key)
            if queue:
                return queue.popleft()

        if key in self._custom_handlers:
            ctx = {
                "method": method,
                "path_template": path_template,
                "url": route.url,
                "channel_id": route.channel_id,
                "guild_id": route.guild_id,
                "json": json_body,
                "files": files,
                "form": form,
            }
            result = self._custom_handlers[key](ctx)
            if isinstance(result, _ScriptedOutcome):
                return result
            if isinstance(result, dict):
                return _ScriptedOutcome(200, result)
            raise TypeError(
                f"Custom handler for {method} {path_template} must return a dict "
                f"or _ScriptedOutcome; got {type(result).__name__}"
            )

        # Built-in default behavior for the routes we know about.
        if key == ("POST", "/channels/{channel_id}/messages"):
            return _ScriptedOutcome(200, self._build_message_payload(route, json_body, form))
        if key == ("POST", "/channels/{channel_id}/typing"):
            return _ScriptedOutcome(204, None)

        # Unknown route — surface a clear, test-side 404 so misconfigured
        # tests fail loudly rather than silently mocking too much.
        raise _UnknownRouteError(method, path_template, route.url)

    def _has_more_outcomes(self, method: str, path_template: str) -> bool:
        key = (method.upper(), path_template)
        with self._lock:
            queue = self._scripts.get(key)
            return bool(queue) or key in self._custom_handlers or self._has_default(key)

    @staticmethod
    def _has_default(key: tuple[str, str]) -> bool:
        return key in {
            ("POST", "/channels/{channel_id}/messages"),
            ("POST", "/channels/{channel_id}/typing"),
        }

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _record_call(
        self,
        *,
        method: str,
        path_template: str,
        url: str,
        route: _discord_http.Route,
        json_body: dict[str, Any] | None,
        files: Sequence[Any] | None,
        form: Iterable[dict[str, Any]] | None,
        outcome: _ScriptedOutcome,
        attempt: int,
    ) -> dict[str, Any]:
        # Extract content / message_reference whether the caller went through
        # the JSON path or the multipart (files) path. discord.py packs the
        # message body into the ``payload_json`` field when files are present.
        content: str | None = None
        message_reference: dict[str, Any] | None = None
        attachments_meta: list[dict[str, Any]] = []
        recorded_files: list[dict[str, Any]] = []

        if json_body is not None:
            content = json_body.get("content") if isinstance(json_body, dict) else None
            if isinstance(json_body, dict):
                message_reference = json_body.get("message_reference")

        if form is not None:
            form_list = list(form)
            for part in form_list:
                if part.get("name") == "payload_json":
                    payload_value = part.get("value")
                    if isinstance(payload_value, (bytes, bytearray)):
                        payload_value = payload_value.decode("utf-8")
                    if isinstance(payload_value, str):
                        try:
                            import json as _json

                            payload = _json.loads(payload_value)
                            content = payload.get("content", content)
                            message_reference = payload.get("message_reference", message_reference)
                            attachments_meta = list(payload.get("attachments", []))
                        except Exception:
                            pass

        if files:
            for f in files:
                buf = getattr(f, "fp", None)
                blob: bytes | None = None
                if buf is not None:
                    try:
                        pos = buf.tell()
                        buf.seek(0)
                        blob = buf.read()
                        buf.seek(pos)
                    except Exception:
                        blob = None
                recorded_files.append(
                    {
                        "filename": getattr(f, "filename", None),
                        "bytes": blob,
                        "size": len(blob) if blob is not None else None,
                    }
                )

        entry = {
            "method": method,
            "path_template": path_template,
            "url": url,
            "channel_id": route.channel_id,
            "guild_id": route.guild_id,
            "content": content,
            "message_reference": message_reference,
            "json": json_body,
            "files": recorded_files,
            "attachments_meta": attachments_meta,
            "response_status": outcome.status,
            "attempt": attempt,
            "retried": False,
        }

        with self._lock:
            self.calls.append(entry)
            if (method.upper(), path_template) == ("POST", "/channels/{channel_id}/messages"):
                self.sent_messages.append(entry)

        return entry

    # ------------------------------------------------------------------
    # Synthetic payload builders
    # ------------------------------------------------------------------

    def _build_message_payload(
        self,
        route: _discord_http.Route,
        json_body: dict[str, Any] | None,
        form: Iterable[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        # Pull request fields from either the JSON body or the multipart
        # ``payload_json`` part — discord.py uses the latter when files are
        # attached.
        content = ""
        message_reference: dict[str, Any] | None = None
        nonce: int | str | None = None
        tts = False

        if isinstance(json_body, dict):
            content = json_body.get("content") or ""
            message_reference = json_body.get("message_reference")
            nonce = json_body.get("nonce")
            tts = bool(json_body.get("tts", False))

        if form is not None:
            for part in list(form):
                if part.get("name") == "payload_json":
                    payload_value = part.get("value")
                    if isinstance(payload_value, (bytes, bytearray)):
                        payload_value = payload_value.decode("utf-8")
                    if isinstance(payload_value, str):
                        try:
                            import json as _json

                            payload = _json.loads(payload_value)
                            content = payload.get("content", content) or content
                            message_reference = payload.get(
                                "message_reference", message_reference
                            )
                            nonce = payload.get("nonce", nonce)
                            tts = bool(payload.get("tts", tts))
                        except Exception:
                            pass

        message_id = self._snowflake.next()
        now_iso = _dt.datetime.now(tz=_dt.UTC).isoformat()
        channel_id = route.channel_id

        payload: dict[str, Any] = {
            "id": str(message_id),
            "type": 0,  # default Message type
            "channel_id": str(channel_id) if channel_id is not None else None,
            "content": content,
            "timestamp": now_iso,
            "edited_timestamp": None,
            "tts": tts,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": [],
            "pinned": False,
            "flags": 0,
            "nonce": nonce,
            "author": {
                "id": str(self.bot_user_id),
                "username": self.bot_username,
                "global_name": self.bot_username,
                "discriminator": "0",
                "avatar": None,
                "bot": True,
                "system": False,
                "public_flags": 0,
            },
        }
        if message_reference is not None:
            payload["message_reference"] = message_reference
        return payload


# ---------------------------------------------------------------------------
# Helper exception + fake aiohttp.ClientResponse stub
# ---------------------------------------------------------------------------


class _UnknownRouteError(AssertionError):
    """Raised when a test exercises a route the fake doesn't know about."""

    def __init__(self, method: str, path_template: str, url: str) -> None:
        super().__init__(
            f"FakeDiscordRest received an unmocked Discord REST call: "
            f"{method} {path_template} (concrete url: {url}). "
            f"Register a handler with `rest.register_handler({method!r}, {path_template!r}, ...)` "
            f"or queue a response with `rest.script_response(...)`."
        )
        self.method = method
        self.path_template = path_template
        self.url = url
        # The 404 status is what the original spec asks the fake to surface
        # for unknown routes; tests can `pytest.raises(_UnknownRouteError)`
        # or check the attached status.
        self.status = 404


class _FakeAioResponse:
    """Minimal duck-typed `aiohttp.ClientResponse` for `discord.HTTPException`.

    `discord.errors.HTTPException` reads only ``status`` and ``reason`` off
    the response object plus uses it in an f-string format. We provide both.
    """

    _STATUS_REASONS = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        429: "Too Many Requests",
        500: "Internal Server Error",
        502: "Bad Gateway",
        503: "Service Unavailable",
        504: "Gateway Timeout",
        524: "A Timeout Occurred",
    }

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.reason = self._STATUS_REASONS.get(status, "Unknown")
        self.headers = headers or {}

    def __format__(self, spec: str) -> str:  # pragma: no cover - defensive
        return f"<FakeAioResponse {self.status} {self.reason}>"


# ---------------------------------------------------------------------------
# Test helpers — building a sendable channel without a live gateway
# ---------------------------------------------------------------------------


def make_test_channel(
    client: discord.Client,
    channel_id: int,
    *,
    guild_id: int | None = None,
) -> discord.PartialMessageable:
    """Build a `discord.PartialMessageable` bound to ``client``'s state.

    Useful for tests that want to call ``await channel.send(...)`` without
    spinning up a real gateway.
    """
    return discord.PartialMessageable(
        state=client._connection,  # type: ignore[attr-defined]
        id=channel_id,
        guild_id=guild_id,
        type=discord.ChannelType.text,
    )


__all__ = [
    "FakeDiscordRest",
    "UnknownRouteError",
    "make_test_channel",
]


# Public alias so tests can `from tests.fakes.discord_rest import UnknownRouteError`.
UnknownRouteError = _UnknownRouteError
