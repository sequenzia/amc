"""Top-level FastAPI app for the Agent Messaging Channel adapter.

Spec references: §1 (overview), §6.2 (auth), §7.4 (header table — every
endpoint, including ``/openapi.json`` and ``/docs``, requires the bearer
token), §11.

Responsibilities of this module
-------------------------------

* Construct the :class:`fastapi.FastAPI` instance with the **default
  ``/openapi.json``, ``/docs``, and ``/redoc`` routes disabled** so we can
  re-register them behind :func:`amc.core.auth.require_bearer`. Per spec
  §7.4 every endpoint — schema and Swagger UI included — requires the
  bearer token; FastAPI's stock routes are anonymous and would leak the
  surface to anyone who can reach the bind address.
* Register the standard exception handlers (``AMCError``,
  ``RequestValidationError``, ``Exception``) via
  :func:`amc.core.errors.register_exception_handlers` so every error path
  emits the spec §7.4.12 envelope.
* Install the structured request-logging middleware via
  :func:`amc.core.logging.install_request_logging`. This also mutes
  uvicorn's duplicate access logger.
* Mount the five Phase-1 endpoint routers (``messages_unread``,
  ``messages_get``, ``messages_context``, ``messages_mark_read``,
  ``messages_quarantine``). Each router already declares its own
  ``Depends(require_bearer)`` (and ``Depends(require_agent_id)`` where
  applicable), so app-level ``dependencies=[]`` would double-invoke; we
  rely on the router-level deps and only add a redundant ``require_bearer``
  on the schema/UI routes that have no router of their own.

What this module does **not** do (yet)
--------------------------------------

* Wire the SQLite database lifespan (Alembic migrations, pool startup).
* Load the allowlist file (#34) and install the SIGHUP reload handler.
* Start the webhook delivery worker (#48) or the Discord/iMessage
  connectors as background tasks.
* Bind a startup hook for :func:`amc.core.auth.load_bearer_token`.

These belong to later waves once the dependent components have landed.
TODO(post-#34/#48): wire startup/shutdown lifespan that loads the bearer
token, opens the DB pool, hydrates the allowlist, and starts the webhook
+ connector workers; tests will use a lifespan-suppressing harness.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse

from amc.api import (
    attachments_get,
    messages_context,
    messages_get,
    messages_mark_read,
    messages_quarantine,
    messages_send,
    messages_unread,
)
from amc.core.auth import require_bearer
from amc.core.errors import register_exception_handlers
from amc.core.logging import install_request_logging

__all__ = ["APP_DESCRIPTION", "APP_TITLE", "APP_VERSION", "build_app"]


APP_TITLE = "AMC Adapter"
APP_VERSION = "0.1.0"
APP_DESCRIPTION = (
    "Agent Messaging Channel adapter — a single-Mac REST surface that "
    "normalizes iMessage and Discord behind one bearer-protected API. "
    "All endpoints, including this OpenAPI document and the Swagger UI, "
    "require `Authorization: Bearer <token>` per spec §6.2 and §7.4."
)

# URLs at which the (bearer-protected) schema + UIs are re-registered.
_OPENAPI_URL = "/openapi.json"
_DOCS_URL = "/docs"
_REDOC_URL = "/redoc"


def build_app() -> FastAPI:
    """Construct and return the configured FastAPI application.

    Calling this at import time would force every consumer (including
    tests that build their own minimal app) to pay the wiring cost, so we
    expose a builder. Production entry points (uvicorn / launchd) call it
    once and bind to ``amc.app:app`` (defined below) for the common case.
    """
    app = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        description=APP_DESCRIPTION,
        # Disable FastAPI's anonymous defaults; we re-register protected
        # versions below.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    register_exception_handlers(app)
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


# Module-level instance for ``uvicorn amc.app:app`` and other production
# entry points. Constructed at import time; tests that need a fresh app
# (e.g. to install an alternate request-logging middleware) call
# :func:`build_app` directly.
app = build_app()
