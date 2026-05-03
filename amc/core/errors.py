"""Standard error response envelope and FastAPI exception handlers.

Implements the canonical error JSON shape from spec §7.4.12 and the stable
``code`` constants referenced throughout §5 and §7.4.

Envelope shape (§7.4.12)::

    {
      "error": {
        "code": "STABLE_CODE",
        "message": "Human-readable explanation",
        "details": {...}   # optional; omitted entirely when None
      }
    }

Public surface
--------------
* :class:`ErrorCode` — ``StrEnum`` of every documented error code.
* :data:`ERROR_STATUS` — code → default HTTP status code mapping.
* :class:`AMCError` — the canonical exception. Routes raise this instead of
  ``HTTPException`` so the global handler produces the standard envelope.
* :func:`register_exception_handlers` — wires the three handlers
  (``AMCError``, ``RequestValidationError``, ``Exception``) into a FastAPI
  app.

Translating subsystem-specific errors
-------------------------------------
The rate limiter raises :class:`amc.core.rate_limit.RateLimitedError` with a
``retry_after`` float. Route handlers (task #35) translate it to::

    raise AMCError(
        code=ErrorCode.RATE_LIMITED,
        message="Per-channel rate limit exceeded.",
        headers={"Retry-After": str(int(rl.retry_after))},
    )

The same pattern applies to other subsystem errors: catch the local
exception at the route boundary, build an ``AMCError`` with the matching
``ErrorCode``, and re-raise.
"""

from __future__ import annotations

import enum
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

__all__ = [
    "ERROR_STATUS",
    "AMCError",
    "ErrorCode",
    "build_error_body",
    "register_exception_handlers",
]


class ErrorCode(enum.StrEnum):
    """Stable error codes used in the standard error envelope.

    Each value is the literal string surfaced in ``error.code``. Sourced from
    spec §5 (error handling rows) and §7.4.12 (canonical list).
    """

    UNAUTHORIZED = "UNAUTHORIZED"
    AGENT_ID_REQUIRED = "AGENT_ID_REQUIRED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
    CHANNEL_NOT_FOUND = "CHANNEL_NOT_FOUND"
    IDEMPOTENCY_KEY_REUSE = "IDEMPOTENCY_KEY_REUSE"
    RATE_LIMITED = "RATE_LIMITED"
    PLATFORM_SEND_FAILED = "PLATFORM_SEND_FAILED"
    PLATFORM_AUTH = "PLATFORM_AUTH"
    SEND_FAILED = "SEND_FAILED"
    ATTACHMENT_TOO_LARGE_FOR_PLATFORM = "ATTACHMENT_TOO_LARGE_FOR_PLATFORM"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# Default HTTP status for each error code. Routes may pass an explicit
# ``status`` to ``AMCError`` to override (e.g. a connector-specific 503).
ERROR_STATUS: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.AGENT_ID_REQUIRED: 400,
    ErrorCode.VALIDATION_FAILED: 400,
    ErrorCode.MESSAGE_NOT_FOUND: 404,
    ErrorCode.CHANNEL_NOT_FOUND: 404,
    ErrorCode.IDEMPOTENCY_KEY_REUSE: 422,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PLATFORM_SEND_FAILED: 502,
    ErrorCode.PLATFORM_AUTH: 502,
    ErrorCode.SEND_FAILED: 502,
    ErrorCode.ATTACHMENT_TOO_LARGE_FOR_PLATFORM: 413,
    ErrorCode.INTERNAL_ERROR: 500,
}


def build_error_body(
    code: ErrorCode | str,
    message: str,
    details: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """Build the standard envelope dict.

    The ``details`` field is omitted entirely when ``None`` so it never
    serializes as ``"details": null``. When supplied, ``details`` is passed
    through verbatim (callers are responsible for ensuring it is JSON
    serializable).
    """
    code_str = code.value if isinstance(code, ErrorCode) else code
    inner: dict[str, Any] = {"code": code_str, "message": message}
    if details is not None:
        inner["details"] = details
    return {"error": inner}


class AMCError(Exception):
    """Canonical exception raised by route handlers and middleware.

    Parameters
    ----------
    code
        Stable error code (``ErrorCode`` member or raw string for forward
        compatibility).
    message
        Human-readable description surfaced in ``error.message``.
    status
        HTTP status to return. Defaults to the value in :data:`ERROR_STATUS`
        for the given code. Explicit override lets a route emit, for example,
        a ``503`` for ``PLATFORM_AUTH`` while still using the canonical code.
    details
        Optional structured detail. Omitted from the JSON when ``None``.
    headers
        Optional HTTP response headers (e.g. ``Retry-After`` on
        ``RATE_LIMITED``, ``WWW-Authenticate`` on ``UNAUTHORIZED``).
    """

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        status: int | None = None,
        details: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        # Accept raw strings so callers in other modules don't have to import
        # ErrorCode just to raise a code. Coerce to ErrorCode when possible
        # so the status lookup works.
        try:
            if isinstance(code, ErrorCode):
                self.code: ErrorCode | str = code
            else:
                self.code = ErrorCode(code)
        except ValueError:
            self.code = code  # unknown code, use as-is

        self.message = message
        self.details = details
        self.headers = headers

        if status is not None:
            self.status = status
        elif isinstance(self.code, ErrorCode):
            self.status = ERROR_STATUS[self.code]
        else:
            # Unknown raw-string code with no explicit status — fall back to 500.
            self.status = ERROR_STATUS[ErrorCode.INTERNAL_ERROR]

        super().__init__(f"{self.code}: {self.message}")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _amc_error_to_response(exc: AMCError) -> JSONResponse:
    body = build_error_body(exc.code, exc.message, exc.details)
    return JSONResponse(status_code=exc.status, content=body, headers=exc.headers)


async def _amc_error_handler(request: Request, exc: AMCError) -> JSONResponse:  # noqa: ARG001
    return _amc_error_to_response(exc)


async def _validation_error_handler(
    request: Request,  # noqa: ARG001
    exc: RequestValidationError,
) -> JSONResponse:
    """Translate Pydantic ``RequestValidationError`` to the standard envelope.

    Spec §7.4.12 reserves ``details`` for field-level validation; we surface
    Pydantic's structured ``errors()`` list there. Status is ``400`` (per the
    task spec); FastAPI's stock 422 is overridden so the contract stays
    uniform across all endpoints.
    """
    body = build_error_body(
        ErrorCode.VALIDATION_FAILED,
        "Request validation failed.",
        details=exc.errors(),
    )
    return JSONResponse(status_code=ERROR_STATUS[ErrorCode.VALIDATION_FAILED], content=body)


async def _unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """Catch-all handler: log full traceback, return a sanitized 500.

    Logs at ``error`` level with ``exc_info=exc`` so structlog's
    ``format_exc_info`` processor renders the full traceback into the
    ``exception`` field. The client receives only the canonical
    ``INTERNAL_ERROR`` envelope — no stack, no exception type, no message
    leakage from the underlying exception.
    """
    log = structlog.get_logger("amc.errors")
    log.error(
        "unhandled_exception",
        method=request.method,
        path=request.url.path,
        exc_type=type(exc).__name__,
        exc_info=exc,
    )
    body = build_error_body(
        ErrorCode.INTERNAL_ERROR,
        "Internal server error.",
    )
    return JSONResponse(status_code=ERROR_STATUS[ErrorCode.INTERNAL_ERROR], content=body)


def register_exception_handlers(app: FastAPI) -> None:
    """Register the three standard exception handlers on ``app``.

    Order is irrelevant — FastAPI dispatches by exception type. Call once at
    startup, after the app is constructed but before any routes are served.
    """
    app.add_exception_handler(AMCError, _amc_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
