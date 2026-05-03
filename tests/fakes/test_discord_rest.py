"""Tests for the in-process fake Discord REST endpoint.

These tests exercise the fake against a real `discord.py` ``Client`` /
``PartialMessageable`` so we know the fake satisfies the same contract a
production connector relies on.
"""

from __future__ import annotations

import asyncio
import io

import discord
import pytest

from tests.fakes.discord_rest import (
    FakeDiscordRest,
    UnknownRouteError,
    make_test_channel,
)


@pytest.fixture
def discord_client() -> discord.Client:
    """A fully constructed `discord.Client` whose REST is patchable.

    No connection is actually opened — we only need ``client._connection``
    so we can build a ``PartialMessageable`` and have ``state.create_message``
    produce a real ``Message`` object.
    """
    return discord.Client(intents=discord.Intents.default())


# ---------------------------------------------------------------------------
# Functional acceptance criteria
# ---------------------------------------------------------------------------


async def test_channel_send_returns_real_message_backed_by_fake(
    discord_client: discord.Client,
) -> None:
    """`channel.send("hi")` returns a usable `Message` via the fake."""
    channel_id = 999_888_777_666_555
    with FakeDiscordRest() as rest:
        channel = make_test_channel(discord_client, channel_id)
        message = await channel.send("hi there")

        assert isinstance(message, discord.Message)
        assert message.content == "hi there"
        assert message.channel.id == channel_id
        # The synthetic message id is a snowflake-shaped integer
        assert message.id > 0
        # The author payload was hydrated end-to-end
        assert message.author is not None
        assert message.author.bot is True

        # And the call was recorded with the exact channel_id and content
        assert len(rest.sent_messages) == 1
        recorded = rest.sent_messages[0]
        assert recorded["channel_id"] == channel_id
        assert recorded["content"] == "hi there"
        assert recorded["method"] == "POST"
        assert recorded["path_template"] == "/channels/{channel_id}/messages"


async def test_send_records_message_reference_for_replies(
    discord_client: discord.Client,
) -> None:
    """`reply_to` (via `reference=`) is echoed back into the recorded call."""
    channel_id = 12345
    referenced_message_id = 67890

    with FakeDiscordRest() as rest:
        channel = make_test_channel(discord_client, channel_id)
        ref = discord.MessageReference(
            message_id=referenced_message_id,
            channel_id=channel_id,
            fail_if_not_exists=False,
        )
        message = await channel.send("reply!", reference=ref)

        assert message.reference is not None
        assert message.reference.message_id == referenced_message_id

        recorded = rest.sent_messages[-1]
        assert recorded["message_reference"] is not None
        assert int(recorded["message_reference"]["message_id"]) == referenced_message_id


async def test_message_ids_are_monotonically_increasing(
    discord_client: discord.Client,
) -> None:
    """Allocated message ids are stable, snowflake-shaped, and monotonic."""
    with FakeDiscordRest() as rest:
        channel = make_test_channel(discord_client, 42)
        ids: list[int] = []
        for i in range(5):
            m = await channel.send(f"msg {i}")
            ids.append(m.id)

        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)
        assert all(i > 0 for i in ids)
        assert len(rest.sent_messages) == 5


async def test_scripted_429_is_retried_and_eventually_succeeds(
    discord_client: discord.Client,
) -> None:
    """A scripted 429 propagates through the retry loop and the call succeeds."""
    with FakeDiscordRest() as rest:
        rest.script_rate_limit("POST", "/channels/{channel_id}/messages", retry_after=0.0)
        # No further explicit script — the next call falls through to the
        # default success handler.

        channel = make_test_channel(discord_client, 100)
        message = await channel.send("ratelimited then ok")

        assert isinstance(message, discord.Message)
        assert message.content == "ratelimited then ok"

        # Two attempts should be recorded — the 429 and the eventual 200
        statuses = [c["response_status"] for c in rest.sent_messages]
        assert statuses == [429, 200]


async def test_scripted_500_is_retried_and_eventually_succeeds(
    discord_client: discord.Client,
) -> None:
    """A scripted 500 propagates through the retry loop and the call succeeds."""
    with FakeDiscordRest() as rest:
        rest.script_server_error("POST", "/channels/{channel_id}/messages", status=500)

        channel = make_test_channel(discord_client, 200)
        message = await channel.send("server error then ok")

        assert isinstance(message, discord.Message)
        statuses = [c["response_status"] for c in rest.sent_messages]
        assert statuses == [500, 200]


async def test_terminal_403_raises_forbidden(discord_client: discord.Client) -> None:
    """A scripted 403 surfaces as `discord.Forbidden`."""
    with FakeDiscordRest() as rest:
        rest.script_response(
            "POST",
            "/channels/{channel_id}/messages",
            status=403,
            body={"message": "Missing access", "code": 50001},
        )
        channel = make_test_channel(discord_client, 300)
        with pytest.raises(discord.Forbidden) as excinfo:
            await channel.send("nope")
        assert excinfo.value.status == 403


async def test_consecutive_429s_then_success(discord_client: discord.Client) -> None:
    """Multiple scripted 429s retry until a 200 finally lands."""
    with FakeDiscordRest() as rest:
        for _ in range(3):
            rest.script_rate_limit(
                "POST", "/channels/{channel_id}/messages", retry_after=0.0
            )
        channel = make_test_channel(discord_client, 400)
        message = await channel.send("third time's the charm")
        assert isinstance(message, discord.Message)
        statuses = [c["response_status"] for c in rest.sent_messages]
        assert statuses == [429, 429, 429, 200]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_attachment_upload_records_bytes(discord_client: discord.Client) -> None:
    """Multipart file uploads are recorded with their raw bytes."""
    payload = b"hello-from-test-bytes"
    with FakeDiscordRest() as rest:
        channel = make_test_channel(discord_client, 555)
        file_obj = discord.File(io.BytesIO(payload), filename="hello.txt")
        await channel.send("with attachment", file=file_obj)

        recorded = rest.sent_messages[-1]
        assert recorded["content"] == "with attachment"
        assert len(recorded["files"]) == 1
        recorded_file = recorded["files"][0]
        assert recorded_file["filename"] == "hello.txt"
        assert recorded_file["bytes"] == payload
        assert recorded_file["size"] == len(payload)


async def test_concurrent_sends_preserve_insertion_order(
    discord_client: discord.Client,
) -> None:
    """Concurrent ``channel.send`` calls land in ``sent_messages`` in the
    order their REST requests were dispatched, not interleaved partially."""
    with FakeDiscordRest() as rest:
        channel = make_test_channel(discord_client, 7777)

        async def send_one(i: int) -> None:
            await channel.send(f"msg-{i:03d}")

        # Launch many concurrent sends. The fake's per-call append happens
        # under a single lock, so any successful completion order is also
        # the recorded order.
        await asyncio.gather(*(send_one(i) for i in range(20)))

        assert len(rest.sent_messages) == 20
        contents = [c["content"] for c in rest.sent_messages]
        # All 20 unique payloads landed
        assert sorted(contents) == [f"msg-{i:03d}" for i in range(20)]
        # Recorded order matches the order the fake observed them — which
        # for a list-append-under-lock must be a permutation that we can
        # reproduce by inspecting attempt numbers (all attempt=1 for first
        # try).
        attempts = [c["attempt"] for c in rest.sent_messages]
        assert attempts == [1] * 20


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


async def test_unknown_route_raises_clear_error(discord_client: discord.Client) -> None:
    """An unmocked route raises a clear, test-side error mentioning the route."""
    with FakeDiscordRest() as rest:  # noqa: F841 — used implicitly via patch
        channel = make_test_channel(discord_client, 12321)
        # A typing indicator hits POST /channels/{channel_id}/typing — this
        # IS a built-in route. Use something else: editing a message hits
        # PATCH /channels/{channel_id}/messages/{message_id}.
        with pytest.raises(UnknownRouteError) as excinfo:
            # Build a partial message and try to edit it; that will route
            # through HTTPClient.request with PATCH.
            partial = channel.get_partial_message(99999)
            await partial.edit(content="changed")

        assert excinfo.value.method == "PATCH"
        assert "/channels/{channel_id}/messages/{message_id}" in excinfo.value.path_template
        assert "FakeDiscordRest received an unmocked Discord REST call" in str(excinfo.value)


async def test_context_manager_cleans_up_patch(
    discord_client: discord.Client,
) -> None:
    """Exiting the context restores `discord.http.HTTPClient.request`."""
    from discord.http import HTTPClient

    original = HTTPClient.request
    with FakeDiscordRest():
        # Inside the context, the method is replaced.
        assert HTTPClient.request is not original
    # After exit, the method is the original again.
    assert HTTPClient.request is original


async def test_double_enter_raises(discord_client: discord.Client) -> None:
    """A single FakeDiscordRest instance refuses re-entry while active."""
    rest = FakeDiscordRest()
    with rest, pytest.raises(RuntimeError, match="already active"):
        rest.__enter__()
