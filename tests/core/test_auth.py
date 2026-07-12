"""Tests for ``amg.core.auth`` — bearer auth + ``X-Agent-ID`` dependencies.

Covers spec §6.2 (Authentication) and §7.4 (header table). Uses a minimal in-test
FastAPI app so that no top-level adapter entry point is required yet.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from amg.core.auth import (
    DEFAULT_ENV_PATH,
    _parse_bearer,
    configure_bearer_token,
    get_configured_bearer_token,
    load_bearer_token,
    require_agent_id,
    require_bearer,
    reset_bearer_token,
)

VALID_TOKEN = "tok-deadbeefdeadbeefdeadbeefdeadbeef"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_token() -> Iterator[None]:
    """Ensure each test starts with no cached token."""
    reset_bearer_token()
    yield
    reset_bearer_token()


def _build_app() -> FastAPI:
    """Construct a minimal FastAPI app exercising both dependencies.

    - ``/agent-only`` requires the bearer (mounted on a router with the global dep).
    - ``/agent-scoped`` additionally requires ``X-Agent-ID``.
    """
    app = FastAPI()

    @app.get("/agent-only", dependencies=[Depends(require_bearer)])
    def agent_only() -> dict[str, bool]:
        return {"ok": True}

    @app.get(
        "/agent-scoped",
        dependencies=[Depends(require_bearer)],
    )
    def agent_scoped(agent_id: str = Depends(require_agent_id)) -> dict[str, str]:
        return {"agent_id": agent_id}

    return app


@pytest.fixture
def client() -> TestClient:
    configure_bearer_token(VALID_TOKEN)
    return TestClient(_build_app())


# ---------------------------------------------------------------------------
# Functional: bearer enforcement
# ---------------------------------------------------------------------------


def test_request_with_valid_bearer_returns_200(client: TestClient) -> None:
    response = client.get("/agent-only", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_request_without_authorization_returns_401(client: TestClient) -> None:
    response = client.get("/agent-only")
    assert response.status_code == 401
    body = response.json()
    assert body["detail"]["error"]["code"] == "UNAUTHORIZED"
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_request_with_wrong_token_returns_401(client: TestClient) -> None:
    response = client.get("/agent-only", headers={"Authorization": "Bearer not-the-right-token"})
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "UNAUTHORIZED"


def test_agent_scoped_route_also_requires_bearer(client: TestClient) -> None:
    """X-Agent-ID alone is not enough; bearer is checked first."""
    response = client.get("/agent-scoped", headers={"X-Agent-ID": "claude-code"})
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Functional: X-Agent-ID enforcement
# ---------------------------------------------------------------------------


def test_agent_scoped_route_returns_400_when_agent_id_missing(client: TestClient) -> None:
    response = client.get("/agent-scoped", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert response.status_code == 400
    body = response.json()
    assert body["detail"]["error"]["code"] == "AGENT_ID_REQUIRED"


def test_agent_scoped_route_returns_400_when_agent_id_blank(client: TestClient) -> None:
    response = client.get(
        "/agent-scoped",
        headers={"Authorization": f"Bearer {VALID_TOKEN}", "X-Agent-ID": "   "},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"]["code"] == "AGENT_ID_REQUIRED"


def test_agent_scoped_route_passes_agent_id_through(client: TestClient) -> None:
    response = client.get(
        "/agent-scoped",
        headers={"Authorization": f"Bearer {VALID_TOKEN}", "X-Agent-ID": "claude-code"},
    )
    assert response.status_code == 200
    assert response.json() == {"agent_id": "claude-code"}


def test_agent_scoped_route_trims_agent_id(client: TestClient) -> None:
    response = client.get(
        "/agent-scoped",
        headers={
            "Authorization": f"Bearer {VALID_TOKEN}",
            "X-Agent-ID": "  claude-code  ",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"agent_id": "claude-code"}


def test_routes_without_bearer_dep_are_unprotected_only_if_explicitly_excluded() -> None:
    """The implementation does not allowlist anonymous endpoints; protection comes
    from applying ``Depends(require_bearer)`` (per spec §6.2). This test documents
    that requirement by asserting the dependency is mountable globally and rejects
    anonymous traffic on a route declared *with* it."""
    configure_bearer_token(VALID_TOKEN)
    app = FastAPI()

    @app.get("/anything", dependencies=[Depends(require_bearer)])
    def _h() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/anything").status_code == 401


# ---------------------------------------------------------------------------
# Edge cases: header parsing
# ---------------------------------------------------------------------------


def test_empty_authorization_header_treated_as_missing(client: TestClient) -> None:
    response = client.get("/agent-only", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.json()["detail"]["error"]["code"] == "UNAUTHORIZED"


def test_whitespace_only_authorization_header_rejected(client: TestClient) -> None:
    response = client.get("/agent-only", headers={"Authorization": "    "})
    assert response.status_code == 401


def test_authorization_header_without_scheme_rejected(client: TestClient) -> None:
    response = client.get("/agent-only", headers={"Authorization": VALID_TOKEN})
    assert response.status_code == 401


def test_bearer_scheme_is_case_insensitive(client: TestClient) -> None:
    for scheme in ("bearer", "BEARER", "BeArEr"):
        response = client.get("/agent-only", headers={"Authorization": f"{scheme} {VALID_TOKEN}"})
        assert response.status_code == 200, f"scheme={scheme!r} should be accepted"


def test_surrounding_whitespace_tolerated(client: TestClient) -> None:
    response = client.get("/agent-only", headers={"Authorization": f"   Bearer {VALID_TOKEN}   "})
    assert response.status_code == 200


def test_bearer_with_empty_token_rejected(client: TestClient) -> None:
    response = client.get("/agent-only", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_non_bearer_scheme_rejected(client: TestClient) -> None:
    response = client.get("/agent-only", headers={"Authorization": f"Basic {VALID_TOKEN}"})
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Functional: constant-time comparison
# ---------------------------------------------------------------------------


def test_token_comparison_uses_constant_time_compare(client: TestClient) -> None:
    """Verify ``secrets.compare_digest`` is invoked on the comparison path so the
    behaviour is provably constant-time. Direct timing measurement is unreliable in
    a unit test; spying on ``compare_digest`` proves the implementation choice."""
    with patch("amg.core.auth.secrets.compare_digest", wraps=secrets.compare_digest) as spy:
        # Mismatched lengths must still go through compare_digest, not == /short-circuit.
        response = client.get("/agent-only", headers={"Authorization": "Bearer x"})
        assert response.status_code == 401
        assert spy.call_count >= 1
        # Confirm both args were bytes (compare_digest requires same type).
        call_args = spy.call_args_list[-1].args
        assert isinstance(call_args[0], bytes)
        assert isinstance(call_args[1], bytes)


def test_compare_digest_runs_even_when_header_missing(client: TestClient) -> None:
    """Avoid early-return timing leak: even a missing header should hit compare_digest
    so the two failure paths take similar time."""
    with patch("amg.core.auth.secrets.compare_digest", wraps=secrets.compare_digest) as spy:
        response = client.get("/agent-only")
        assert response.status_code == 401
        assert spy.call_count >= 1


# ---------------------------------------------------------------------------
# Error handling: startup token loading
# ---------------------------------------------------------------------------


def test_load_bearer_token_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f'AMG_BEARER_TOKEN="{VALID_TOKEN}"\n')

    token = load_bearer_token(env_path=env_file)

    assert token == VALID_TOKEN
    assert get_configured_bearer_token() == VALID_TOKEN


def test_load_bearer_token_falls_back_to_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.env"
    monkeypatch.setenv("AMG_BEARER_TOKEN", VALID_TOKEN)

    token = load_bearer_token(env_path=missing)

    assert token == VALID_TOKEN


def test_load_bearer_token_raises_when_missing_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.env"
    monkeypatch.delenv("AMG_BEARER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="AMG_BEARER_TOKEN is not set"):
        load_bearer_token(env_path=missing)


def test_load_bearer_token_raises_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text('AMG_BEARER_TOKEN=""\n')
    monkeypatch.delenv("AMG_BEARER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="AMG_BEARER_TOKEN is not set"):
        load_bearer_token(env_path=env_file)


def test_default_env_path_points_to_user_config() -> None:
    """Sanity check: spec §6.2 requires ``~/.config/messaging-agent/.env``."""
    assert DEFAULT_ENV_PATH.parts[-3:] == (".config", "messaging-agent", ".env")
    assert DEFAULT_ENV_PATH.is_absolute()


def test_get_configured_bearer_token_raises_before_load() -> None:
    with pytest.raises(RuntimeError, match="not been loaded"):
        get_configured_bearer_token()


def test_configure_bearer_token_rejects_empty_string() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        configure_bearer_token("")


def test_require_bearer_dependency_raises_if_token_not_configured() -> None:
    """Without a configured token, the dependency itself surfaces the misconfiguration
    (rather than silently 401-ing every request)."""
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(require_bearer)])
    def _h() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/x", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    # FastAPI converts the unhandled RuntimeError into a 500.
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Direct unit tests of header parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        ("  Bearer   abc  ", "abc"),
        ("Bearer\tabc", "abc"),
    ],
)
def test_parse_bearer_accepts_valid(header: str, expected: str) -> None:
    assert _parse_bearer(header) == expected


@pytest.mark.parametrize(
    "header",
    [
        "",
        "   ",
        "Bearer",
        "Bearer    ",
        "Basic abc",
        "abc",
    ],
)
def test_parse_bearer_rejects_invalid(header: str) -> None:
    assert _parse_bearer(header) is None
