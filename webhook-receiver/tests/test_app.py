"""End-to-end FastAPI tests with the fake claude shim."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from amc_receiver.app import build_app
from amc_receiver.auth import compute_signature
from tests.fakes.fake_claude import make_fake_claude

SECRET = "shared-secret-1234567890"  # noqa: S105 — test fixture
BEARER = "test-bearer-token"  # noqa: S105 — test fixture
DEFAULT_MSG_ID = "msg_01HXYZ1234567890ABCDEFGHJK"
DEFAULT_CHANNEL = "+15551234567"


def _envelope(
    message_id: str = DEFAULT_MSG_ID,
    channel_id: str = DEFAULT_CHANNEL,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "source": "imessage",
        "channel_id": channel_id,
        "channel_type": "dm",
        "sender": {"id": channel_id, "display_name": "Alice", "person_id": "alice"},
        "text": "hello",
        "attachments": [],
        "reply_to": None,
        "timestamp": "2026-05-03T12:00:00Z",
        "direction": "inbound",
        "raw": {},
    }


def _signed_request_kwargs(body_bytes: bytes, *, delivery_id: str = "dlv_1") -> dict[str, Any]:
    return {
        "headers": {
            "X-AMC-Signature": compute_signature(SECRET, body_bytes),
            "X-AMC-Delivery-Id": delivery_id,
            "X-AMC-Message-Id": DEFAULT_MSG_ID,
            "X-AMC-Attempt": "1",
            "Content-Type": "application/json",
        },
        "content": body_bytes,
    }


@pytest.fixture
def fake_claude_log(tmp_path: Path) -> Path:
    return tmp_path / "invocations.jsonl"


@pytest.fixture
def configured_env(
    tmp_path: Path,
    fake_claude_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_claude = make_fake_claude(tmp_path, fake_claude_log)
    monkeypatch.setenv("AMC_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("AMC_BEARER_TOKEN", BEARER)
    monkeypatch.setenv("AMC_AGENT_ID", "test-agent")
    monkeypatch.setenv("AMC_RECEIVER_CLAUDE_BIN", str(fake_claude))
    monkeypatch.setenv("AMC_RECEIVER_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AMC_RECEIVER_IDLE_WORKER_TTL_SECONDS", "5")
    monkeypatch.setenv("AMC_RECEIVER_CLAUDE_TIMEOUT_SECONDS", "15")


@pytest_asyncio.fixture
async def client(configured_env: None) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client bound to a fresh app with its lifespan running."""
    app = build_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as http,
    ):
        yield http


async def _wait_for_invocation_count(
    log_path: Path, expected: int, *, timeout: float = 10.0
) -> list[dict[str, Any]]:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if log_path.exists():
            lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
            if len(lines) >= expected:
                return lines
        await asyncio.sleep(0.1)
    raise AssertionError(f"expected {expected} invocations within {timeout}s")


def _read_invocations(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


async def test_webhook_happy_path_invokes_claude(
    client: httpx.AsyncClient, fake_claude_log: Path
) -> None:
    body = json.dumps(_envelope()).encode("utf-8")
    resp = await client.post("/webhook", **_signed_request_kwargs(body))
    assert resp.status_code == 204

    invocations = await _wait_for_invocation_count(fake_claude_log, 1)
    inv = invocations[0]

    assert "-p" in inv["argv"]
    assert "--allowedTools" in inv["argv"]
    for tool in (
        "mcp__amc__list_unread_messages",
        "mcp__amc__send_message",
        "mcp__amc__mark_read",
        "mcp__amc__get_message_context",
    ):
        assert tool in inv["argv"]

    assert "--mcp-config" in inv["argv"]
    mcp_idx = inv["argv"].index("--mcp-config")
    mcp_blob = json.loads(inv["argv"][mcp_idx + 1])
    assert "amc" in mcp_blob["mcpServers"]
    assert mcp_blob["mcpServers"]["amc"]["env"]["AMC_BEARER_TOKEN"] == BEARER

    # Stdin should contain the rendered envelope.
    assert DEFAULT_MSG_ID in inv["stdin"]
    assert DEFAULT_CHANNEL in inv["stdin"]


async def test_webhook_rejects_invalid_signature(client: httpx.AsyncClient) -> None:
    body = json.dumps(_envelope()).encode("utf-8")
    kwargs = _signed_request_kwargs(body)
    kwargs["headers"]["X-AMC-Signature"] = "sha256=" + "0" * 64
    resp = await client.post("/webhook", **kwargs)
    assert resp.status_code == 401


async def test_webhook_dedupes_repeat_delivery(
    client: httpx.AsyncClient, fake_claude_log: Path
) -> None:
    body = json.dumps(_envelope()).encode("utf-8")
    kwargs = _signed_request_kwargs(body, delivery_id="dlv_dup")

    r1 = await client.post("/webhook", **kwargs)
    assert r1.status_code == 204
    r2 = await client.post("/webhook", **kwargs)
    assert r2.status_code == 204

    await _wait_for_invocation_count(fake_claude_log, 1)
    # Give the dispatcher extra time to spuriously double-invoke if the
    # dedupe is broken.
    await asyncio.sleep(0.3)
    assert len(_read_invocations(fake_claude_log)) == 1


async def test_webhook_rejects_invalid_json(client: httpx.AsyncClient) -> None:
    body = b"this is not json"
    resp = await client.post("/webhook", **_signed_request_kwargs(body))
    assert resp.status_code == 400


async def test_webhook_rejects_envelope_missing_channel_id(
    client: httpx.AsyncClient,
) -> None:
    body = json.dumps({"id": "msg_x", "text": "hi"}).encode("utf-8")
    resp = await client.post("/webhook", **_signed_request_kwargs(body))
    assert resp.status_code == 400


async def test_webhook_rejects_missing_delivery_id(client: httpx.AsyncClient) -> None:
    body = json.dumps(_envelope()).encode("utf-8")
    kwargs = _signed_request_kwargs(body)
    del kwargs["headers"]["X-AMC-Delivery-Id"]
    resp = await client.post("/webhook", **kwargs)
    assert resp.status_code == 400


async def test_healthz_returns_ok(client: httpx.AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["active_channels"] == 0
