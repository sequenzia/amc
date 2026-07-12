"""Top-level FastAPI app for the Agent Messaging Gateway adapter.

Spec references: §1 (overview), §6.2 (auth), §7.4 (header table — every
endpoint, including ``/openapi.json`` and ``/docs``, requires the bearer
token), §11.

Responsibilities of this module
-------------------------------

* Construct the :class:`fastapi.FastAPI` instance with the **default
  ``/openapi.json``, ``/docs``, and ``/redoc`` routes disabled** so we can
  re-register them behind :func:`amg.core.auth.require_bearer`. Per spec
  §7.4 every endpoint — schema and Swagger UI included — requires the
  bearer token; FastAPI's stock routes are anonymous and would leak the
  surface to anyone who can reach the bind address.
* Register the standard exception handlers (``AMGError``,
  ``RequestValidationError``, ``Exception``) via
  :func:`amg.core.errors.register_exception_handlers` so every error path
  emits the spec §7.4.12 envelope.
* Install the structured request-logging middleware via
  :func:`amg.core.logging.install_request_logging`. This also mutes
  uvicorn's duplicate access logger.
* Mount the Phase-1 endpoint routers (``messages_unread``,
  ``messages_get``, ``messages_context``, ``messages_mark_read``,
  ``messages_quarantine``, ``messages_send``, ``attachments_get``,
  ``healthz``). Each router declares its own ``Depends(require_bearer)``
  so app-level ``dependencies=[]`` would double-invoke; we rely on the
  router-level deps and only add a redundant ``require_bearer`` on the
  schema/UI routes that have no router of their own.
* Drive the runtime lifespan: load the bearer token, open the SQLite
  engine, hydrate the allowlist, build the message sink, start the
  webhook worker + idempotency/attachment sweepers, and start the
  Discord/iMessage connectors when their preconditions are met
  (``AMG_DISCORD_BOT_TOKEN`` set; ``darwin`` host with a readable
  ``chat.db``). Connectors that don't start surface as ``"disabled"``
  in ``/healthz``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse

from amg.api import (
    attachments_get,
    healthz,
    messages_context,
    messages_get,
    messages_mark_read,
    messages_quarantine,
    messages_send,
    messages_unread,
)
from amg.connectors.discord.connector import DiscordConnector
from amg.connectors.imessage.applescript import OsascriptAppleScriptSender
from amg.connectors.imessage.connector import ImessageConnector
from amg.connectors.imessage.reader import ChatDbPathMissingError, ChatDbReader
from amg.core.allowlist import load_allowlist
from amg.core.attachments import AttachmentStore
from amg.core.auth import DEFAULT_ENV_PATH, load_bearer_token, require_bearer
from amg.core.db import create_engine_from_env, create_session_factory
from amg.core.errors import register_exception_handlers
from amg.core.idempotency import (
    IdempotencyStore,
    configure_idempotency_store,
    register_idempotency_handlers,
)
from amg.core.logging import install_request_logging
from amg.core.message_sink import MessageSink
from amg.core.rate_limit import RateLimiter
from amg.core.sweepers import AttachmentSweeper, IdempotencySweeper
from amg.core.webhook import WebhookWorker, load_webhook_config_from_env

__all__ = ["APP_DESCRIPTION", "APP_TITLE", "APP_VERSION", "build_app"]

_log = logging.getLogger("amg.app")

APP_TITLE = "AMG Adapter"
APP_VERSION = "0.1.0"
APP_DESCRIPTION = (
    "Agent Messaging Gateway adapter — a single-Mac REST surface that "
    "normalizes iMessage and Discord behind one bearer-protected API. "
    "All endpoints, including this OpenAPI document and the Swagger UI, "
    "require `Authorization: Bearer <token>` per spec §6.2 and §7.4."
)

# URLs at which the (bearer-protected) schema + UIs are re-registered.
_OPENAPI_URL = "/openapi.json"
_DOCS_URL = "/docs"
_REDOC_URL = "/redoc"

_DEFAULT_CHAT_DB_PATH = Path("~/Library/Messages/chat.db").expanduser()


def _resolve_bind_url() -> str:
    """Build the externally-visible bind URL from ``AMG_BIND_HOST/PORT``.

    Used as the ``AttachmentStore.bind_url`` so re-hosted attachments
    expose stable absolute URLs in inbound envelopes.
    """
    host = os.environ.get("AMG_BIND_HOST", "").strip() or "127.0.0.1"
    port = os.environ.get("AMG_BIND_PORT", "").strip() or "8080"
    return f"http://{host}:{port}"


def build_app(*, bootstrap: bool = True) -> FastAPI:
    """Construct and return the configured FastAPI application.

    Calling this at import time would force every consumer (including
    tests that build their own minimal app) to pay the wiring cost, so we
    expose a builder. Production entry points (uvicorn / launchd) call it
    once and bind to ``amg.app:app`` (defined below) for the common case.

    Args:
        bootstrap: When ``True`` (default, production path) the FastAPI
            lifespan loads the bearer token + allowlist from env, opens
            the SQLite engine, builds the message sink, starts workers
            + sweepers, and starts the Discord/iMessage connectors when
            their preconditions are met. Tests that pre-configure the
            module-level singletons via the route-level ``configure_*``
            helpers pass ``bootstrap=False`` to keep the lifespan inert
            so the test wiring isn't clobbered.
    """

    @asynccontextmanager
    async def _empty_lifespan(_app: FastAPI):
        yield

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # ---- Config + secrets --------------------------------------------
        # Hydrate ``os.environ`` from ~/.config/messaging-agent/.env so the
        # downstream loaders (allowlist, webhook, DB path, Discord bot
        # token, rate limit, etc.) see values that operators put in the
        # .env file. ``load_bearer_token()`` reads the file directly, but
        # nothing else does. Process-env wins on conflict (12-factor).
        if DEFAULT_ENV_PATH.exists():
            from dotenv import dotenv_values

            for key, value in dotenv_values(DEFAULT_ENV_PATH).items():
                if value is not None and key not in os.environ:
                    os.environ[key] = value

        load_bearer_token()
        allowlist = load_allowlist()
        webhook_config = load_webhook_config_from_env()

        # ---- Storage -----------------------------------------------------
        engine = create_engine_from_env()
        session_factory = create_session_factory(engine)
        _app.state.engine = engine
        _app.state.session_factory = session_factory

        # ---- Webhook worker (built before sink so they share url_snapshot)-
        webhook_worker = WebhookWorker(
            session_factory=session_factory,
            config=webhook_config,
        )
        sink = MessageSink(
            session_factory=session_factory,
            allowlist=allowlist,
            webhook_config=webhook_config,
            url_snapshot=webhook_worker.url_snapshot,
        )
        _app.state.sink = sink

        # ---- Attachment store + sweepers + rate limiter + idempotency ----
        attachment_store = AttachmentStore(bind_url=_resolve_bind_url())
        rate_limiter = RateLimiter.from_env()
        idempotency_store = IdempotencyStore(session_factory=session_factory)
        idempotency_sweeper = IdempotencySweeper(session_factory=session_factory)
        attachment_sweeper = AttachmentSweeper(session_factory=session_factory)

        # ---- Discord connector (gated on bot token) ----------------------
        discord_token = os.environ.get("AMG_DISCORD_BOT_TOKEN", "").strip()
        discord_connector: DiscordConnector | None = None
        discord_task: asyncio.Task[None] | None = None
        if discord_token:
            discord_connector = DiscordConnector(
                bot_token=discord_token,
                message_sink=sink,
                allowlist=allowlist,
            )
            discord_task = asyncio.create_task(
                discord_connector.start(discord_token),
                name="amg.discord.start",
            )
            healthz.register_connector("discord", discord_connector)
            _log.info("discord_connector_started")
        else:
            _log.info("discord_connector_skipped_no_token")

        # ---- iMessage connector (darwin + chat.db) -----------------------
        imessage_connector: ImessageConnector | None = None
        if sys.platform == "darwin" and _DEFAULT_CHAT_DB_PATH.exists():
            try:
                reader = ChatDbReader(_DEFAULT_CHAT_DB_PATH)
                imessage_connector = ImessageConnector(
                    reader=reader,
                    sender=OsascriptAppleScriptSender(),
                    sink=sink,
                    allowlist=allowlist,
                    session_factory=session_factory,
                    attachment_store=attachment_store,
                )
                await imessage_connector.start()
                healthz.register_connector("imessage", imessage_connector)
                _log.info("imessage_connector_started")
            except ChatDbPathMissingError as exc:
                _log.warning("imessage_connector_skipped_chatdb_missing: %s", exc)
                imessage_connector = None
        else:
            _log.info("imessage_connector_skipped_not_darwin_or_no_chatdb")

        # ---- Wire route-level singletons ---------------------------------
        messages_unread.configure_session_factory(session_factory)
        messages_get.configure_session_factory(session_factory)
        messages_mark_read.configure_session_factory(session_factory)
        messages_quarantine.configure_session_factory(session_factory)
        messages_send.configure_session_factory(session_factory)
        attachments_get.configure_session_factory(session_factory)
        messages_send.configure_rate_limiter(rate_limiter)
        messages_send.configure_discord_connector(discord_connector)
        messages_send.configure_imessage_connector(imessage_connector)
        configure_idempotency_store(idempotency_store)

        # ---- Start workers -----------------------------------------------
        await webhook_worker.start()
        await idempotency_sweeper.start()
        await attachment_sweeper.start()

        try:
            yield
        finally:
            # Stop in reverse order of start so in-flight work drains first.
            await attachment_sweeper.stop()
            await idempotency_sweeper.stop()
            await webhook_worker.stop()
            if imessage_connector is not None:
                await imessage_connector.stop()
            if discord_connector is not None:
                try:
                    await discord_connector.close()
                except Exception:  # noqa: BLE001
                    _log.exception("discord_connector_close_failed")
                if discord_task is not None and not discord_task.done():
                    discord_task.cancel()
            await engine.dispose()

    app = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        description=APP_DESCRIPTION,
        # Disable FastAPI's anonymous defaults; we re-register protected
        # versions below.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan if bootstrap else _empty_lifespan,
    )

    register_exception_handlers(app)
    register_idempotency_handlers(app)
    install_request_logging(app)

    # Mount the Phase-1 endpoint routers. Each router carries its own
    # router/route-level ``Depends(require_bearer)`` so we do not add
    # ``dependencies=[Depends(require_bearer)]`` to ``include_router``
    # (which would double-invoke the dependency).
    app.include_router(messages_unread.router)
    app.include_router(messages_get.router)
    app.include_router(messages_context.router)
    app.include_router(messages_mark_read.router)
    app.include_router(messages_quarantine.router)
    app.include_router(messages_send.router)
    app.include_router(attachments_get.router)

    healthz.record_start_time(time.monotonic())
    healthz.load_version()
    app.state.start_time = healthz.get_recorded_start_time()
    app.include_router(healthz.router)

    _register_protected_schema_routes(app)

    return app


def _register_protected_schema_routes(app: FastAPI) -> None:
    """Re-register ``/openapi.json``, ``/docs``, ``/redoc`` behind bearer.

    FastAPI's stock setup() bound these to anonymous routes; we disabled
    them via ``openapi_url=None`` etc. and re-add them here with
    ``dependencies=[Depends(require_bearer)]`` so the schema and Swagger
    UI are gated by the same bearer token as every other endpoint
    (spec §7.4 header table — last row).
    """

    async def openapi_endpoint(request: Request) -> JSONResponse:
        # Mirror FastAPI's stock /openapi.json behavior: cache the schema
        # on the app and respect ``root_path`` for reverse-proxy setups.
        if not app.openapi_schema:
            app.openapi_schema = get_openapi(
                title=app.title,
                version=app.version,
                openapi_version=app.openapi_version,
                summary=app.summary,
                description=app.description,
                routes=app.routes,
                tags=app.openapi_tags,
                servers=app.servers,
            )
        schema: dict[str, Any] = app.openapi_schema
        root_path = request.scope.get("root_path", "").rstrip("/")
        if root_path and app.root_path_in_servers:
            server_urls = {s.get("url") for s in schema.get("servers", [])}
            if root_path not in server_urls:
                schema = dict(schema)
                schema["servers"] = [{"url": root_path}, *schema.get("servers", [])]
        return JSONResponse(schema)

    async def swagger_ui_endpoint(request: Request) -> HTMLResponse:
        root_path = request.scope.get("root_path", "").rstrip("/")
        return get_swagger_ui_html(
            openapi_url=root_path + _OPENAPI_URL,
            title=f"{app.title} - Swagger UI",
        )

    async def redoc_endpoint(request: Request) -> HTMLResponse:
        root_path = request.scope.get("root_path", "").rstrip("/")
        return get_redoc_html(
            openapi_url=root_path + _OPENAPI_URL,
            title=f"{app.title} - ReDoc",
        )

    bearer_dep = [Depends(require_bearer)]

    # ``include_in_schema=False`` matches FastAPI's stock behavior so the
    # docs routes don't show up *inside* their own document.
    app.add_api_route(
        _OPENAPI_URL,
        openapi_endpoint,
        methods=["GET"],
        include_in_schema=False,
        dependencies=bearer_dep,
    )
    app.add_api_route(
        _DOCS_URL,
        swagger_ui_endpoint,
        methods=["GET"],
        include_in_schema=False,
        dependencies=bearer_dep,
    )
    app.add_api_route(
        _REDOC_URL,
        redoc_endpoint,
        methods=["GET"],
        include_in_schema=False,
        dependencies=bearer_dep,
    )


# Module-level instance for ``uvicorn amg.app:app`` and other production
# entry points. Constructed at import time; tests that need a fresh app
# (e.g. to install an alternate request-logging middleware) call
# :func:`build_app` directly.
app = build_app()
