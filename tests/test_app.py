"""Integration tests for the top-level adapter app (``amc.app``).

Spec references:
* §6.2 Authentication — every endpoint, including the schema and Swagger
  UI, requires the bearer token.
* §7.4 header table — ``GET /openapi.json`` and ``GET /docs`` rows
  explicitly list ``Bearer = required``.

Coverage:
* ``/openapi.json`` returns 401 without bearer (Functional).
* ``/openapi.json`` returns the spec with bearer (Functional).
* ``/docs`` Swagger UI loads with bearer and 401s without (Functional).
* ``/redoc`` is similarly protected.
* The generated OpenAPI document includes every Phase-1 endpoint
  (Edge Case).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from amc.app import APP_TITLE, APP_VERSION, build_app
from amc.core.auth import configure_bearer_token, reset_bearer_token

VALID_TOKEN = "tok-test-app-deadbeefdeadbeefdeadbeef"
BAD_TOKEN = "tok-test-app-not-the-real-token"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bearer_token() -> Iterator[None]:
    """Configure the module-level bearer token for the duration of each test."""
    configure_bearer_token(VALID_TOKEN)
    yield
    reset_bearer_token()


@pytest.fixture
def client() -> TestClient:
    """Build a fresh app + TestClient. ``raise_server_exceptions=False`` lets
    us assert on 4xx/5xx without TestClient re-raising the underlying
    exception (we want to inspect the standard error envelope)."""
    return TestClient(build_app(), raise_server_exceptions=False)


def _auth(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Functional — bearer enforcement on /openapi.json
# ---------------------------------------------------------------------------


def test_openapi_json_returns_401_without_bearer(client: TestClient) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 401
    body = response.json()
    # spec §7.4.12 envelope: {"error": {"code": ..., "message": ...}}.
    # FastAPI's HTTPException wraps the body under "detail".
    error = body.get("detail", {}).get("error", {})
    assert error.get("code") == "UNAUTHORIZED"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_openapi_json_returns_401_with_invalid_bearer(client: TestClient) -> None:
    response = client.get("/openapi.json", headers=_auth(BAD_TOKEN))
    assert response.status_code == 401


def test_openapi_json_returns_spec_with_bearer(client: TestClient) -> None:
    response = client.get("/openapi.json", headers=_auth())
    assert response.status_code == 200
    spec = response.json()
    assert spec["openapi"].startswith("3.")
    info = spec["info"]
    assert info["title"] == APP_TITLE
    assert info["version"] == APP_VERSION
    assert info.get("description")  # populated from spec §1


# ---------------------------------------------------------------------------
# Functional — bearer enforcement on /docs (Swagger UI)
# ---------------------------------------------------------------------------


def test_docs_returns_401_without_bearer(client: TestClient) -> None:
    response = client.get("/docs")
    assert response.status_code == 401


def test_docs_loads_with_bearer(client: TestClient) -> None:
    response = client.get("/docs", headers=_auth())
    assert response.status_code == 200
    # Swagger UI returns text/html that references the OpenAPI URL.
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "swagger" in body.lower()
    # The HTML embeds the openapi URL it should fetch.
    assert "/openapi.json" in body


# ---------------------------------------------------------------------------
# Functional — /redoc is similarly protected
# ---------------------------------------------------------------------------


def test_redoc_returns_401_without_bearer(client: TestClient) -> None:
    response = client.get("/redoc")
    assert response.status_code == 401


def test_redoc_loads_with_bearer(client: TestClient) -> None:
    response = client.get("/redoc", headers=_auth())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text.lower()
    assert "redoc" in body
    assert "/openapi.json" in response.text


# ---------------------------------------------------------------------------
# Edge Case — every Phase-1 endpoint appears in the OpenAPI document
# ---------------------------------------------------------------------------


# (path, method) pairs that must appear in /openapi.json. Matches the
# routers mounted by ``amc.app.build_app``. The schema/UI routes are
# intentionally absent because they're registered with
# ``include_in_schema=False``.
_EXPECTED_ENDPOINTS: list[tuple[str, str]] = [
    ("/messages/unread", "get"),
    ("/messages/{message_id}", "get"),
    ("/messages/context", "get"),
    ("/messages/mark_read", "post"),
    ("/messages/quarantine", "get"),
]


def test_openapi_spec_includes_every_phase1_endpoint(client: TestClient) -> None:
    response = client.get("/openapi.json", headers=_auth())
    assert response.status_code == 200
    spec = response.json()
    paths = spec.get("paths", {})

    missing: list[tuple[str, str]] = []
    for path, method in _EXPECTED_ENDPOINTS:
        if path not in paths or method not in paths[path]:
            missing.append((path, method))

    assert missing == [], (
        f"OpenAPI spec is missing expected endpoints: {missing}. "
        f"Present paths: {sorted(paths.keys())}"
    )


def test_openapi_spec_excludes_schema_and_ui_routes(client: TestClient) -> None:
    """The /openapi.json, /docs, /redoc routes themselves should not be
    documented (we registered them with ``include_in_schema=False`` to
    match FastAPI's stock behavior)."""
    response = client.get("/openapi.json", headers=_auth())
    spec = response.json()
    paths = spec.get("paths", {})
    assert "/openapi.json" not in paths
    assert "/docs" not in paths
    assert "/redoc" not in paths


# ---------------------------------------------------------------------------
# Sanity — a mounted endpoint also enforces bearer
# ---------------------------------------------------------------------------


def test_mounted_endpoint_requires_bearer(client: TestClient) -> None:
    """Smoke check that the routers were mounted with their own
    require_bearer dependency intact."""
    response = client.get("/messages/quarantine")
    assert response.status_code == 401
