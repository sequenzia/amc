"""Shared HTTP client for the four MCP tools.

Spec references:
  - §5.6 — wrapper config (AMC_BASE_URL / AMC_BEARER_TOKEN / AMC_AGENT_ID)
  - §7.4 header table — Bearer + X-Agent-ID on every call;
                        Idempotency-Key recommended on POST /messages/send
  - §7.4.12 — standard error envelope: ``{"error": {"code", "message", "details"?}}``

Design contract:
  * Uses ``httpx.AsyncClient`` only.
  * Caller-provided ``extra_headers`` win over defaults — this is how
    ``send_message`` injects its per-call ``Idempotency-Key``. The wrapper
    itself never auto-generates the key; callers do (so non-send tools
    don't accidentally get one and so tests can pin it).
  * ``get`` and ``post`` always return a discriminated ``HttpResult``.
    They never raise on HTTP errors or transport failures — the four tool
    handlers translate the result via :mod:`amc_mcp.errors`.
  * :func:`random_idempotency_key` is exported as the canonical key
    generator so ``send_message`` imports it from one place.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

import httpx

from amc_mcp.config import WrapperConfig, get_config

__all__ = [
    "NETWORK_ERROR_STATUS",
    "HttpClient",
    "HttpErr",
    "HttpOk",
    "HttpResult",
    "create_http_client",
    "random_idempotency_key",
]

NETWORK_ERROR_STATUS: Final[int] = 0


@dataclass(frozen=True)
class HttpOk:
    ok: bool = field(default=True, init=False)
    status: int = 0
    data: Any = None


@dataclass(frozen=True)
class HttpErr:
    ok: bool = field(default=False, init=False)
    status: int = 0
    code: str = ""
    message: str = ""
    body: Any = None


HttpResult = HttpOk | HttpErr


@runtime_checkable
class HttpClient(Protocol):
    async def get(self, path: str, query: dict[str, Any] | None = None) -> HttpResult: ...

    async def post(
        self,
        path: str,
        body: Any,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult: ...

    async def aclose(self) -> None: ...


def random_idempotency_key() -> str:
    """UUID v4 string suitable for an ``Idempotency-Key`` header (spec §7.4.7)."""
    return str(uuid.uuid4())


def _strip_trailing_slash(url: str) -> str:
    return url[:-1] if url.endswith("/") else url


def _build_query(query: dict[str, Any] | None) -> dict[str, list[str]]:
    """Drop None values; render booleans/numbers as strings; flatten lists."""
    out: dict[str, list[str]] = {}
    if not query:
        return out
    for key, raw in query.items():
        if raw is None:
            continue
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if item is None:
                    continue
                out.setdefault(key, []).append(str(item))
        else:
            out.setdefault(key, []).append(str(raw))
    return out


def _extract_error(body: Any, fallback_message: str) -> tuple[str, str]:
    """Pull ``code`` / ``message`` from a §7.4.12 envelope; else fall back."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message")
            return (
                code if isinstance(code, str) and code else "INTERNAL_ERROR",
                message
                if isinstance(message, str) and message
                else (fallback_message or "Request failed"),
            )
    return ("INTERNAL_ERROR", fallback_message or "Request failed")


def _try_parse_json(text: str) -> Any:
    if text == "":
        return None
    try:
        import json

        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _parse_response(response: httpx.Response) -> HttpResult:
    status = response.status_code

    if status == 204:
        return HttpOk(status=status, data=None)

    raw_text = response.text
    parsed = _try_parse_json(raw_text)

    if 200 <= status < 300:
        return HttpOk(status=status, data=parsed)

    code, message = _extract_error(parsed, response.reason_phrase or "")
    return HttpErr(status=status, code=code, message=message, body=parsed)


class _AsyncHttpClient:
    """Concrete implementation of :class:`HttpClient` backed by ``httpx``."""

    def __init__(
        self,
        config: WrapperConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._base_url = _strip_trailing_slash(config.base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=10.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.bearer_token}",
            "X-Agent-ID": self._config.agent_id,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        normalized = path if path.startswith("/") else f"/{path}"
        return self._base_url + normalized

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        headers = self._headers()
        if extra_headers:
            headers.update(extra_headers)

        params = _build_query(query)

        kwargs: dict[str, Any] = {"headers": headers}
        if params:
            kwargs["params"] = params
        if body is not None:
            kwargs["json"] = body

        try:
            response = await self._client.request(method, self._url(path), **kwargs)
        except httpx.RequestError as exc:
            return HttpErr(
                status=NETWORK_ERROR_STATUS,
                code="NETWORK_ERROR",
                message=str(exc) or exc.__class__.__name__,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return HttpErr(
                status=NETWORK_ERROR_STATUS,
                code="NETWORK_ERROR",
                message=str(exc) if str(exc) else exc.__class__.__name__,
            )

        return _parse_response(response)

    async def get(self, path: str, query: dict[str, Any] | None = None) -> HttpResult:
        return await self._request("GET", path, query=query)

    async def post(
        self,
        path: str,
        body: Any,
        extra_headers: dict[str, str] | None = None,
    ) -> HttpResult:
        return await self._request("POST", path, body=body, extra_headers=extra_headers)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def create_http_client(
    *,
    config: WrapperConfig | None = None,
    client: httpx.AsyncClient | None = None,
) -> HttpClient:
    """Construct a client bound to a config.

    The default factory pulls config from :func:`get_config` (which requires
    :func:`load_config` to have been called at startup). Tests can pass an
    explicit config and a stubbed ``httpx.AsyncClient``.
    """
    cfg = config if config is not None else get_config()
    return _AsyncHttpClient(cfg, client=client)
