"""Tests for ``amc.core.errors`` — standard error envelope + FastAPI handlers.

Covers spec §7.4.12 (envelope shape) and the codes referenced throughout §5
and §7.4. Uses a minimal in-test FastAPI app per task #27's note that no
top-level adapter app entry exists yet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from amc.core.errors import (
    ERROR_STATUS,
    AMCError,
    ErrorCode,
    build_error_body,
    register_exception_handlers,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_logging() -> Iterator[None]:
    """Snapshot/restore root logger + structlog defaults across tests."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    structlog.reset_defaults()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app with one route per error path.

    Each route deliberately raises a different exception so the test suite
    can hit every handler branch without coupling to real adapter routes.
    """
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raise/{code}")
    def raise_code(code: str) -> dict[str, str]:
        # Raise the AMCError for the given code with optional details/headers
        # selected by the code under test.
        if code == "RATE_LIMITED":
            raise AMCError(
                ErrorCode.RATE_LIMITED,
                "Per-channel rate limit exceeded.",
                headers={"Retry-After": "7"},
            )
        if code == "MESSAGE_NOT_FOUND":
            raise AMCError(
                ErrorCode.MESSAGE_NOT_FOUND,
                "No such message.",
                details={"message_id": "msg_xyz"},
            )
        return {"unreachable": code}

    @app.get("/raise-by-name/{name}")
    def raise_by_name(name: str) -> dict[str, str]:
        # Generic raiser that maps a path arg to an ErrorCode.
        ec = ErrorCode(name)
        raise AMCError(ec, f"failure for {ec.value}")

    @app.get("/raise-string-code")
    def raise_string_code() -> dict[str, str]:
        # Raising with a raw string code (callers in other modules may do
        # this without importing the enum).
        raise AMCError("UNAUTHORIZED", "Bearer missing.")

    @app.get("/raise-unknown-code")
    def raise_unknown_code() -> dict[str, str]:
        # Unknown code with no explicit status → falls back to 500.
        raise AMCError("MADE_UP_CODE", "unclassified failure")

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("secret traceback contents")

    class _Body(BaseModel):
        name: str
        count: int

    @app.post("/validate")
    def validate(body: _Body) -> dict[str, str]:
        return {"name": body.name}

    return app


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so the catch-all handler is exercised
    # rather than re-raising into Starlette's TestClient.
    return TestClient(_build_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Functional: code constants and status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        (ErrorCode.UNAUTHORIZED, 401),
        (ErrorCode.AGENT_ID_REQUIRED, 400),
        (ErrorCode.VALIDATION_FAILED, 400),
        (ErrorCode.MESSAGE_NOT_FOUND, 404),
        (ErrorCode.CHANNEL_NOT_FOUND, 404),
        (ErrorCode.IDEMPOTENCY_KEY_REUSE, 422),
        (ErrorCode.RATE_LIMITED, 429),
        (ErrorCode.PLATFORM_SEND_FAILED, 502),
        (ErrorCode.PLATFORM_AUTH, 502),
        (ErrorCode.SEND_FAILED, 502),
        (ErrorCode.ATTACHMENT_TOO_LARGE_FOR_PLATFORM, 413),
        (ErrorCode.INTERNAL_ERROR, 500),
    ],
)
def test_error_status_mapping(code: ErrorCode, expected_status: int) -> None:
    assert ERROR_STATUS[code] == expected_status


def test_every_error_code_has_status_mapping() -> None:
    """The mapping must be exhaustive — guards against new codes added without status."""
    for code in ErrorCode:
        assert code in ERROR_STATUS, f"missing ERROR_STATUS entry for {code}"


def test_error_code_values_match_string_names() -> None:
    """Each enum value's string equals its name (stable wire codes)."""
    for code in ErrorCode:
        assert code.value == code.name


# ---------------------------------------------------------------------------
# Functional: envelope shape + handler dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.UNAUTHORIZED,
        ErrorCode.AGENT_ID_REQUIRED,
        ErrorCode.MESSAGE_NOT_FOUND,
        ErrorCode.CHANNEL_NOT_FOUND,
        ErrorCode.IDEMPOTENCY_KEY_REUSE,
        ErrorCode.PLATFORM_SEND_FAILED,
        ErrorCode.PLATFORM_AUTH,
        ErrorCode.SEND_FAILED,
        ErrorCode.ATTACHMENT_TOO_LARGE_FOR_PLATFORM,
        ErrorCode.INTERNAL_ERROR,
    ],
)
def test_amc_error_produces_standard_envelope(client: TestClient, code: ErrorCode) -> None:
    response = client.get(f"/raise-by-name/{code.value}")
    assert response.status_code == ERROR_STATUS[code]

    body = response.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == code.value
    assert body["error"]["message"] == f"failure for {code.value}"
    # `details` must be omitted (not serialized as null) when not provided.
    assert "details" not in body["error"]


def test_string_code_round_trips_through_handler(client: TestClient) -> None:
    response = client.get("/raise-string-code")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert body["error"]["message"] == "Bearer missing."


def test_unknown_code_falls_back_to_500(client: TestClient) -> None:
    response = client.get("/raise-unknown-code")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "MADE_UP_CODE"
    assert body["error"]["message"] == "unclassified failure"


# ---------------------------------------------------------------------------
# Functional: Retry-After header on RATE_LIMITED
# ---------------------------------------------------------------------------


def test_rate_limited_response_sets_retry_after_header(client: TestClient) -> None:
    response = client.get("/raise/RATE_LIMITED")
    assert response.status_code == 429
    assert response.headers.get("Retry-After") == "7"

    body = response.json()
    assert body["error"]["code"] == "RATE_LIMITED"
    assert body["error"]["message"] == "Per-channel rate limit exceeded."
    assert "details" not in body["error"]


# ---------------------------------------------------------------------------
# Functional: Pydantic validation failures use the standard envelope
# ---------------------------------------------------------------------------


def test_pydantic_validation_failure_returns_standard_envelope(client: TestClient) -> None:
    # Missing `count` and bad `name` type → triggers RequestValidationError.
    response = client.post("/validate", json={"name": 123})
    assert response.status_code == 400

    body = response.json()
    assert set(body.keys()) == {"error"}
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["message"] == "Request validation failed."
    # Pydantic errors must land in `details` and be a non-empty list.
    assert "details" in body["error"]
    assert isinstance(body["error"]["details"], list)
    assert len(body["error"]["details"]) >= 1
    # Each detail item is a dict with a `loc` (per Pydantic v2 .errors()).
    for item in body["error"]["details"]:
        assert isinstance(item, dict)
        assert "loc" in item


# ---------------------------------------------------------------------------
# Edge cases: details serialization
# ---------------------------------------------------------------------------


def test_details_omitted_when_none() -> None:
    body = build_error_body(ErrorCode.INTERNAL_ERROR, "boom")
    assert body == {"error": {"code": "INTERNAL_ERROR", "message": "boom"}}
    assert "details" not in body["error"]


def test_details_included_when_supplied() -> None:
    body = build_error_body(
        ErrorCode.MESSAGE_NOT_FOUND,
        "missing",
        details={"message_id": "msg_xyz"},
    )
    assert body["error"]["details"] == {"message_id": "msg_xyz"}


def test_details_appears_when_amc_error_passes_them(client: TestClient) -> None:
    response = client.get("/raise/MESSAGE_NOT_FOUND")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["details"] == {"message_id": "msg_xyz"}


def test_string_code_accepted_by_build_error_body() -> None:
    body = build_error_body("UNAUTHORIZED", "bad token")
    assert body["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Error handling: unhandled exception → 500 with no leaked stack
# ---------------------------------------------------------------------------


def test_unhandled_exception_returns_sanitized_500(
    client: TestClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "Internal server error.",
        }
    }
    # No leaked stack / underlying message anywhere in the response body.
    raw = response.text
    assert "secret traceback contents" not in raw
    assert "RuntimeError" not in raw
    assert "Traceback" not in raw

    # Traceback + structured event must have been logged. Without
    # configure_logging() running, structlog's default writer goes to
    # stderr/stdout — pytest captures both via capsys.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "unhandled_exception" in combined, (
        f"expected structlog event in output; got out={captured.out!r} err={captured.err!r}"
    )
    assert "Traceback" in combined, "expected traceback in logged output"
    assert "RuntimeError: secret traceback contents" in combined, (
        "expected the original exception text in the logged traceback"
    )


def test_amc_error_constructor_status_override() -> None:
    err = AMCError(ErrorCode.PLATFORM_AUTH, "down", status=503)
    assert err.status == 503
    assert err.code is ErrorCode.PLATFORM_AUTH


def test_amc_error_default_status_uses_mapping() -> None:
    err = AMCError(ErrorCode.RATE_LIMITED, "slow")
    assert err.status == 429


def test_amc_error_string_code_resolved_to_enum() -> None:
    err = AMCError("CHANNEL_NOT_FOUND", "no chan")
    assert err.code is ErrorCode.CHANNEL_NOT_FOUND
    assert err.status == 404


def test_amc_error_unknown_string_code_status_falls_back() -> None:
    err = AMCError("MYSTERY", "hmm")
    assert err.status == 500
    assert err.code == "MYSTERY"
