"""FastAPI app exposing ``POST /webhook`` for AMC outbound deliveries.

Lifecycle
---------
On startup the lifespan loads ``ReceiverConfig`` from the environment,
configures structured logging, and constructs the dispatcher + runner.
On shutdown the dispatcher drains in-flight envelopes (best effort) and
cancels lingering channel workers.

Request flow
------------
1. Read raw bytes — ``await request.body()``. We must NOT use
   ``await request.json()`` first because the HMAC is computed over the
   exact bytes on the wire.
2. Verify ``X-AMC-Signature`` against ``AMC_WEBHOOK_SECRET``. Mismatch
   returns ``401`` (a 4xx tells the adapter to dead-letter — correct, since
   signature failures are permanent).
3. Dedupe by ``X-AMC-Delivery-Id``. Duplicate retries return ``204`` and
   skip enqueue.
4. Parse JSON, hand the envelope to the channel dispatcher, return ``204``.

Errors during downstream processing (Claude crash, MCP timeout, etc.) are
logged but do not surface back to the adapter — those are not transport
errors and we already accepted the delivery.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from amc_receiver.auth import verify_signature
from amc_receiver.claude_runner import ClaudeRunner
from amc_receiver.config import ReceiverConfig
from amc_receiver.dispatcher import ChannelDispatcher, DeliveryDedupe
from amc_receiver.logging import configure_logging

__all__ = ["app", "build_app"]

_log = structlog.get_logger("amc.receiver.app")

_HEADER_SIGNATURE = "X-AMC-Signature"
_HEADER_DELIVERY_ID = "X-AMC-Delivery-Id"
_HEADER_MESSAGE_ID = "X-AMC-Message-Id"
_HEADER_ATTEMPT = "X-AMC-Attempt"


def _hydrate_env_from_dotenv() -> None:
    """Best-effort load of ``~/.config/messaging-agent/.env`` into ``os.environ``.

    Mirrors the adapter's lifespan (``amc/app.py:148-153``) so operators put
    one .env file in the standard location and both services pick it up.
    Process env wins on conflict.
    """
    env_path = os.path.expanduser("~/.config/messaging-agent/.env")
    if not os.path.exists(env_path):
        return
    try:
        from dotenv import dotenv_values  # type: ignore[import-not-found]
    except ImportError:
        return
    for key, value in dotenv_values(env_path).items():
        if value is not None and key not in os.environ:
            os.environ[key] = value


def build_app(*, bootstrap: bool = True) -> FastAPI:
    """Construct the FastAPI app. ``bootstrap=False`` skips lifespan wiring (for tests)."""

    @asynccontextmanager
    async def _empty_lifespan(_app: FastAPI):
        yield

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _hydrate_env_from_dotenv()
        config = ReceiverConfig.from_env()
        configure_logging(log_dir=config.log_dir)
        runner = ClaudeRunner(config)
        dispatcher = ChannelDispatcher(
            runner.run, idle_ttl_seconds=float(config.idle_worker_ttl_seconds)
        )
        dedupe = DeliveryDedupe(config.dedupe_cache_size)

        _app.state.config = config
        _app.state.runner = runner
        _app.state.dispatcher = dispatcher
        _app.state.dedupe = dedupe

        _log.info(
            "receiver_started",
            bind_host=config.bind_host,
            bind_port=config.bind_port,
            dangerous=config.dangerous,
            agent_id=config.agent_id,
        )
        try:
            yield
        finally:
            await dispatcher.stop(drain=True)
            _log.info("receiver_stopped")

    app = FastAPI(
        title="AMC Webhook Receiver",
        version="0.1.0",
        description=(
            "Receives AMC outbound webhooks and runs `claude -p` per inbound "
            "message. All endpoints require the same HMAC signature the adapter "
            "produces (see X-AMC-Signature)."
        ),
        lifespan=lifespan if bootstrap else _empty_lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        dispatcher: ChannelDispatcher | None = getattr(app.state, "dispatcher", None)
        return {
            "status": "ok",
            "active_channels": dispatcher.active_channel_count if dispatcher else 0,
        }

    @app.post("/webhook", status_code=status.HTTP_204_NO_CONTENT)
    async def receive_webhook(request: Request) -> Response:
        body = await request.body()
        signature = request.headers.get(_HEADER_SIGNATURE)
        delivery_id = request.headers.get(_HEADER_DELIVERY_ID, "")
        message_id = request.headers.get(_HEADER_MESSAGE_ID, "")
        attempt = request.headers.get(_HEADER_ATTEMPT, "")

        log = _log.bind(
            delivery_id=delivery_id,
            message_id=message_id,
            attempt=attempt,
        )

        config: ReceiverConfig | None = getattr(request.app.state, "config", None)
        dispatcher: ChannelDispatcher | None = getattr(request.app.state, "dispatcher", None)
        dedupe: DeliveryDedupe | None = getattr(request.app.state, "dedupe", None)
        if config is None or dispatcher is None or dedupe is None:
            log.error("receiver_not_initialized")
            return JSONResponse(
                {"error": "receiver not initialized"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not verify_signature(config.webhook_secret, body, signature):
            log.warning("webhook_signature_invalid")
            return JSONResponse(
                {"error": "invalid signature"}, status_code=status.HTTP_401_UNAUTHORIZED
            )

        if not delivery_id:
            log.warning("webhook_missing_delivery_id")
            return JSONResponse(
                {"error": "X-AMC-Delivery-Id required"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if not dedupe.add_if_new(delivery_id):
            log.info("webhook_duplicate_delivery_ignored")
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        try:
            envelope = json.loads(body)
        except ValueError:
            log.warning("webhook_body_invalid_json")
            return JSONResponse(
                {"error": "body is not valid JSON"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        if not isinstance(envelope, dict) or not envelope.get("channel_id"):
            log.warning("webhook_envelope_missing_channel_id")
            return JSONResponse(
                {"error": "envelope missing channel_id"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        try:
            await dispatcher.submit(envelope)
        except RuntimeError as exc:
            # Dispatcher is shutting down; transient: ask the adapter to retry.
            log.warning("webhook_dispatcher_unavailable", reason=str(exc))
            return JSONResponse(
                {"error": "shutting down"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        log.info("webhook_accepted", channel_id=envelope.get("channel_id"))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


app = build_app()
