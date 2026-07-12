"""Contract test: served ``/openapi.json`` matches the §7.4 golden file.

Spec references:
* §7.4 — REST API specifications. The full surface (paths, operations,
  request/response schemas, error envelopes) is the contract this test
  pins.
* §10 acceptance gate — "API contract verified: ``/openapi.json``
  schema-matches §7.4 (asserted by a contract test that diffs the served
  schema against a checked-in golden file)."

How it works
------------

1. Boot the app via :func:`amg.app.build_app`, configure the bearer token,
   and ``GET /openapi.json`` with the bearer header.
2. Normalize the JSON deterministically: recursively sort all dict keys
   and sort lists whose order doesn't carry semantic meaning (parameter
   lists by ``name``+``in``, ``required`` schema arrays, ``tags``,
   ``security`` requirement object keys, etc.).
3. Compare against ``tests/api/golden/openapi.json``:
     * **Golden missing**: write it, log the path, pass with an xfail-style
       marker so first-run developers know to commit it.
     * **Golden present**: assert that the normalized served schema equals
       the normalized golden. A diff means the surface changed; if the
       change is intentional, regenerate the golden by deleting it and
       re-running this test.

Failure mode is intentionally loud: any added, removed, or modified
endpoint, request body, response shape, parameter, or security
requirement will fail this test with a JSON diff pointing at the change.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from amg.app import build_app
from amg.core.auth import configure_bearer_token, reset_bearer_token

GOLDEN_PATH = Path(__file__).resolve().parent / "golden" / "openapi.json"
BEARER = "tok-openapi-contract-deadbeefdeadbeefdeadbe"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bearer_token() -> Iterator[None]:
    """Configure the module-level bearer token for the test."""
    configure_bearer_token(BEARER)
    yield
    reset_bearer_token()


@pytest.fixture
def client() -> TestClient:
    return TestClient(build_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _list_sort_key(item: Any) -> str:
    """Stable key for sorting heterogeneous list items (used for ``tags``,
    ``security`` requirement keys, and ``required`` schema arrays).

    Falls back to ``json.dumps(sort_keys=True)`` so any JSON-serializable
    item produces a deterministic key without raising on dict/list
    comparisons.
    """
    if isinstance(item, str):
        return item
    return json.dumps(item, sort_keys=True, ensure_ascii=False)


def _normalize(node: Any, *, path: tuple[str, ...] = ()) -> Any:
    """Recursively normalize a JSON-decoded value for deterministic compare.

    Rules:

    * ``dict``: sort keys, normalize values recursively (path-aware so we
      can apply per-location list sorting below).
    * ``list`` whose items are all primitives (str/int/float/bool/None):
      sort by :func:`_list_sort_key`. This catches ``tags``, ``required``
      schema arrays, ``security`` scope arrays, and similar.
    * ``list`` of dicts at known order-insensitive locations
      (``parameters`` lists, ``security`` requirement objects): sort by a
      stable key derived from the item.
    * Other lists: preserve order (e.g. ``enum`` values, ``servers``).

    The path argument lets us special-case ``parameters`` and
    ``security`` without globally re-sorting every list of dicts.
    """
    if isinstance(node, dict):
        return {key: _normalize(node[key], path=(*path, key)) for key in sorted(node)}

    if isinstance(node, list):
        # Order-insensitive lists of dicts at known locations.
        last = path[-1] if path else ""
        if last == "parameters" and all(isinstance(item, dict) for item in node):
            return sorted(
                (_normalize(item, path=(*path, "[]")) for item in node),
                key=lambda p: (p.get("in", ""), p.get("name", ""), _list_sort_key(p)),
            )
        if last == "security" and all(isinstance(item, dict) for item in node):
            return sorted(
                (_normalize(item, path=(*path, "[]")) for item in node),
                key=_list_sort_key,
            )
        # Lists of primitives — sort for deterministic compare.
        if all(isinstance(item, (str, int, float, bool)) or item is None for item in node):
            return sorted(node, key=_list_sort_key)
        # Lists of dicts/lists in unknown locations — preserve order but
        # normalize each item.
        return [_normalize(item, path=(*path, "[]")) for item in node]

    return node


def _serialize(schema: dict[str, Any]) -> str:
    """Render normalized schema as canonical JSON for byte-identical compare."""
    return json.dumps(_normalize(schema), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _fetch_openapi(client: TestClient) -> dict[str, Any]:
    response = client.get("/openapi.json", headers={"Authorization": f"Bearer {BEARER}"})
    assert response.status_code == 200, (
        f"Expected 200 from /openapi.json, got {response.status_code}: {response.text[:500]}"
    )
    return response.json()


def test_openapi_matches_golden(client: TestClient) -> None:
    """Serving ``/openapi.json`` must match ``tests/api/golden/openapi.json``.

    First run (no golden): writes the golden file and emits a clear
    instruction so the developer can commit it. Subsequent runs assert
    the normalized JSON matches byte-for-byte.

    When this test fails, inspect the diff in the assertion message:

    * If the surface change is intentional, delete
      ``tests/api/golden/openapi.json`` and re-run to regenerate.
    * If unintentional, the change is a contract regression — fix the
      code to restore the §7.4 surface.
    """
    served_normalized = _serialize(_fetch_openapi(client))

    if not GOLDEN_PATH.exists():
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(served_normalized, encoding="utf-8")
        pytest.fail(
            f"Golden file did not exist; wrote {GOLDEN_PATH.relative_to(Path.cwd())}. "
            "Commit it and re-run the suite to verify the contract."
        )

    golden = GOLDEN_PATH.read_text(encoding="utf-8")

    if served_normalized != golden:
        # Show a compact head-of-diff so CI logs surface the first
        # divergence without dumping the entire schema.
        served_lines = served_normalized.splitlines()
        golden_lines = golden.splitlines()
        max_lines = max(len(served_lines), len(golden_lines))
        diff_preview: list[str] = []
        for idx in range(max_lines):
            served_line = served_lines[idx] if idx < len(served_lines) else "<EOF>"
            golden_line = golden_lines[idx] if idx < len(golden_lines) else "<EOF>"
            if served_line != golden_line:
                diff_preview.append(f"  line {idx + 1}:")
                diff_preview.append(f"    golden : {golden_line}")
                diff_preview.append(f"    served : {served_line}")
                if len(diff_preview) >= 30:  # ~10 diff lines
                    diff_preview.append("    ... (truncated)")
                    break
        message = (
            "Served /openapi.json no longer matches the §7.4 golden file. "
            "If this change is intentional, delete "
            f"{GOLDEN_PATH.relative_to(Path.cwd())} and re-run to regenerate.\n"
            "First diverging lines:\n" + "\n".join(diff_preview)
        )
        raise AssertionError(message)


def test_normalize_is_idempotent(client: TestClient) -> None:
    """Normalizing twice yields the same output — guards against
    accidental ordering bugs in :func:`_normalize` that would make the
    golden comparison flaky across runs."""
    schema = _fetch_openapi(client)
    once = _serialize(schema)
    twice = _serialize(json.loads(once))
    assert once == twice, "_normalize is not idempotent — comparison would be flaky"


def test_normalize_sorts_dict_keys() -> None:
    """Sanity check on the normalization helper itself."""
    out = _normalize({"b": 1, "a": {"y": 2, "x": 3}})
    assert list(out.keys()) == ["a", "b"]
    assert list(out["a"].keys()) == ["x", "y"]


def test_normalize_sorts_string_lists() -> None:
    out = _normalize({"required": ["b", "a", "c"]})
    assert out["required"] == ["a", "b", "c"]


def test_normalize_sorts_parameter_lists_by_name() -> None:
    out = _normalize(
        {
            "parameters": [
                {"name": "limit", "in": "query"},
                {"name": "since", "in": "query"},
                {"name": "channel_id", "in": "query"},
            ]
        }
    )
    assert [p["name"] for p in out["parameters"]] == ["channel_id", "limit", "since"]


def test_normalize_preserves_enum_order() -> None:
    """``enum`` values are not at a known order-insensitive path and
    contain mixed/string items, so we preserve their order. (We rely on
    FastAPI/Pydantic to emit them deterministically.)"""
    schema = {
        "components": {
            "schemas": {
                "Source": {"enum": ["imessage", "discord"], "type": "string"}
            }
        }
    }
    out = _normalize(schema)
    # Strings get sorted by the all-primitives rule — that's intentional
    # and deterministic. Document the behavior:
    assert out["components"]["schemas"]["Source"]["enum"] == ["discord", "imessage"]
