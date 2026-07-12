"""Idempotency-Key middleware and persistent store (spec §5.2 / §7.3.3 / §7.4.5).

The agent calls :http:post:`/messages/send` with an ``Idempotency-Key`` header.
Per spec §5.2 the adapter must:

1. Hash the normalized request body (sorted-key JSON, no whitespace) with SHA-256.
2. On *hit, same hash* — return the previously cached ``(status, body)`` and add
   ``Idempotency-Replayed: true`` so the agent knows it was a replay.
3. On *hit, different hash* — return ``422`` with
   ``code=IDEMPOTENCY_KEY_REUSE`` (collision treated as a client bug per OQ-5).
4. On *miss* — let the handler run and persist
   ``(key, request_hash, response_status, response_body, expires_at = now + 24h)``.

Concurrent agents racing on the same fresh key are handled by SQLite's
``INSERT ... ON CONFLICT(key) DO NOTHING``: exactly one writer wins; the loser
re-reads and returns the winning row's cached response. The semantics for the
loser are identical to a same-hash replay.

Public surface
--------------
* :class:`IdempotencyStore` — the persistence layer (lookup / persist / sweep).
* :class:`IdempotencyContext` — handed to a route by the dependency on a *miss*;
  the handler calls :meth:`IdempotencyContext.record` after producing its
  response so the cache is populated.
* :func:`idempotent` — FastAPI dependency factory. Used as
  ``ctx: IdempotencyContext | None = Depends(idempotent("send"))``. Returns
  ``None`` when the header is absent (idempotency is opt-in *per request*, even
  on routes that opt in *per scope*). On a hit, raises
  :class:`_IdempotencyReplay` (handled below) or
  :class:`amg.core.errors.AMGError`.
* :func:`register_idempotency_handlers` — wires the replay exception handler
  into a FastAPI app.
* :func:`normalize_body` / :func:`hash_body` — exposed for tests and for any
  caller that needs to compute the same hash outside a request flow.

Notes on routes that *don't* opt in
-----------------------------------
Per spec §7.4.4, ``POST /messages/mark_read`` is naturally idempotent (UPSERT
semantics) and does **not** wire this dependency. Anything that mutates state
non-deterministically (``send``) opts in by adding ``Depends(idempotent(...))``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import structlog
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from amg.core.errors import AMGError, ErrorCode
from amg.core.schema import idempotency_keys

__all__ = [
    "DEFAULT_TTL",
    "REPLAY_HEADER",
    "IdempotencyContext",
    "IdempotencyStore",
    "hash_body",
    "idempotent",
    "normalize_body",
    "register_idempotency_handlers",
]

# 24 h cache TTL per spec §5.2 / §7.3.3 idempotency_keys table.
DEFAULT_TTL: Final[timedelta] = timedelta(hours=24)

# Header set on every cached / race-loser response so the agent sees that the
# adapter short-circuited rather than re-running the handler.
REPLAY_HEADER: Final[str] = "Idempotency-Replayed"

# ISO 8601 with millisecond precision and trailing ``Z`` — matches the rest of
# the project (see ``amg/core/envelope.py`` field_serializer pattern). SQLite
# stores TEXT; tests compare on parsed datetimes so the exact format only
# matters for debuggability.
_ISO_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S.%fZ"


# ---------------------------------------------------------------------------
# Body hashing
# ---------------------------------------------------------------------------


def normalize_body(raw: bytes) -> bytes:
    """Return a canonical byte representation of the request body.

    JSON bodies are decoded, re-serialized with ``sort_keys=True`` and the
    most compact separators so that semantically-equivalent bodies hash to the
    same digest regardless of key ordering or whitespace.

    Non-JSON bodies (or malformed JSON) fall back to the raw bytes — the
    caller still gets *some* deterministic hash, and any subsequent mismatch
    is detected the same way.
    """

    if not raw:
        return b""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw
    return json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")


def hash_body(raw: bytes) -> str:
    """Return ``sha256(normalize_body(raw))`` as a lowercase hex string."""

    return hashlib.sha256(normalize_body(raw)).hexdigest()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CachedResponse:
    """In-memory representation of a row from ``idempotency_keys``."""

    request_hash: str
    response_status: int
    response_body: str  # JSON-encoded


class IdempotencyStore:
    """Async persistence helper for the ``idempotency_keys`` table.

    All time arithmetic is funneled through the injected ``time_provider`` so
    expiry and sweeper tests are deterministic. Default is
    ``datetime.now(tz=UTC)`` to match production wall-clock semantics.
    """

    __slots__ = ("_session_factory", "_now", "_ttl")

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        time_provider: Callable[[], datetime] | None = None,
        ttl: timedelta = DEFAULT_TTL,
    ) -> None:
        self._session_factory = session_factory
        self._now = time_provider if time_provider is not None else _utcnow
        self._ttl = ttl

    # -- queries -----------------------------------------------------------

    async def lookup(self, key: str) -> _CachedResponse | None:
        """Return the cached row for ``key`` if it exists and is unexpired.

        Expired rows are treated as misses (the sweeper deletes them lazily
        out-of-band; this read-path filter stops a stale row from short-
        circuiting a fresh call).
        """

        now = _format_ts(self._now())
        stmt = (
            select(
                idempotency_keys.c.request_hash,
                idempotency_keys.c.response_status,
                idempotency_keys.c.response_body,
                idempotency_keys.c.expires_at,
            )
            .where(idempotency_keys.c.key == key)
        )
        async with self._session_factory() as session:
            row = (await session.execute(stmt)).one_or_none()
        if row is None:
            return None
        if row.expires_at <= now:
            return None
        return _CachedResponse(
            request_hash=row.request_hash,
            response_status=row.response_status,
            response_body=row.response_body,
        )

    async def persist(
        self,
        *,
        key: str,
        request_hash: str,
        response_status: int,
        response_body: str,
    ) -> _CachedResponse:
        """Insert a row, or no-op if another writer beat us to it.

        Returns the *effective* cached row — either the one we just wrote or
        the one already there. Callers use the return value to detect (and
        defer to) a race winner so concurrent agents converge on identical
        responses.
        """

        expires_at = _format_ts(self._now() + self._ttl)
        # SQLite-flavored UPSERT-with-ignore. We deliberately do NOT clobber
        # an existing row even on a hash match — the first write is the
        # canonical response, and the row's ``expires_at`` should reflect
        # *that* call's timestamp, not a later replay's.
        upsert_sql = text(
            """
            INSERT INTO idempotency_keys
                (key, request_hash, response_status, response_body, expires_at)
            VALUES (:key, :request_hash, :response_status, :response_body, :expires_at)
            ON CONFLICT(key) DO NOTHING
            """
        )
        async with self._session_factory() as session:
            await session.execute(
                upsert_sql,
                {
                    "key": key,
                    "request_hash": request_hash,
                    "response_status": response_status,
                    "response_body": response_body,
                    "expires_at": expires_at,
                },
            )
            await session.commit()

            # Re-read so the caller always sees the canonical row, even when
            # the INSERT was ignored (race loser case).
            stmt = select(
                idempotency_keys.c.request_hash,
                idempotency_keys.c.response_status,
                idempotency_keys.c.response_body,
            ).where(idempotency_keys.c.key == key)
            row = (await session.execute(stmt)).one()

        return _CachedResponse(
            request_hash=row.request_hash,
            response_status=row.response_status,
            response_body=row.response_body,
        )

    async def sweep_expired(self) -> int:
        """Delete rows whose ``expires_at`` is <= now. Returns # rows deleted.

        Intended to be called from a periodic background task; safe to call
        from anywhere because the operation is a single ``DELETE``.
        """

        now = _format_ts(self._now())
        delete_sql = text("DELETE FROM idempotency_keys WHERE expires_at <= :now")
        async with self._session_factory() as session:
            result = await session.execute(delete_sql, {"now": now})
            await session.commit()
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# Per-request context handed to route handlers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IdempotencyContext:
    """Returned by the dependency on a cache *miss* — the handler must call
    :meth:`record` after producing its response.

    Fields:
        scope: Logical grouping (e.g. ``"send"``). Currently informational;
            kept on the context so future per-scope policies (different TTLs,
            metrics labels) have a hook.
        key: Raw header value the client supplied.
        request_hash: SHA-256 hex digest of the normalized body. Same value
            the store will compare on the next call.
        store: The ``IdempotencyStore`` to call from :meth:`record`.
        replayed: ``True`` if :meth:`record` lost the INSERT race and the
            handler should override its own response with ``replayed_body``.
        replayed_body: JSON string of the winning response (only set after a
            losing :meth:`record`).
        replayed_status: HTTP status of the winning response (only set after
            a losing :meth:`record`).
    """

    scope: str
    key: str
    request_hash: str
    store: IdempotencyStore
    replayed: bool = False
    replayed_body: str | None = None
    replayed_status: int | None = None

    async def record(
        self,
        *,
        status: int,
        body: dict[str, Any] | list[Any] | str,
    ) -> None:
        """Persist the response, or detect a race-lost write.

        ``body`` may be a JSON-serializable dict/list (the common case) or a
        pre-serialized JSON string (e.g. when the handler already produced
        bytes). After this returns, callers should check :attr:`replayed`:
        if true, return the response body / status from
        :attr:`replayed_body` / :attr:`replayed_status` instead of their
        own — another writer won the race and that's now the canonical
        response.
        """

        body_str = body if isinstance(body, str) else json.dumps(body, separators=(",", ":"))
        cached = await self.store.persist(
            key=self.key,
            request_hash=self.request_hash,
            response_status=status,
            response_body=body_str,
        )
        # Race-loser: the persisted row carries someone else's body. We
        # surface both flag and payload so the caller can swap its response.
        if cached.response_body != body_str or cached.response_status != status:
            self.replayed = True
            self.replayed_body = cached.response_body
            self.replayed_status = cached.response_status


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


class _IdempotencyReplay(Exception):
    """Internal short-circuit raised on a same-hash cache hit.

    Carries the cached body verbatim plus the status. Handled by
    :func:`_replay_handler`, which serves the response with
    ``Idempotency-Replayed: true``.
    """

    def __init__(self, *, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"idempotency replay status={status}")


# Module-level holder so the FastAPI dependency can look up the configured
# store without callers having to thread it through every Depends. Set via
# :func:`configure_idempotency_store` at app startup; reset for tests with
# :func:`reset_idempotency_store`.
_configured_store: IdempotencyStore | None = None


def configure_idempotency_store(store: IdempotencyStore) -> None:
    """Install the process-wide :class:`IdempotencyStore` for the dependency."""

    global _configured_store
    _configured_store = store


def get_configured_store() -> IdempotencyStore:
    """Return the configured store, raising if startup hasn't installed one."""

    if _configured_store is None:
        raise RuntimeError(
            "IdempotencyStore not configured; call configure_idempotency_store() at startup."
        )
    return _configured_store


def reset_idempotency_store() -> None:
    """Drop any configured store. Used by tests for isolation."""

    global _configured_store
    _configured_store = None


def idempotent(
    scope: str,
) -> Callable[..., Awaitable[IdempotencyContext | None]]:
    """Build a FastAPI dependency for routes that opt into idempotency.

    Usage::

        @router.post("/messages/send")
        async def send(
            payload: SendMessageRequest,
            idem: IdempotencyContext | None = Depends(idempotent("send")),
        ):
            ...
            response = {"message_id": ..., "sent_at": ...}
            if idem is not None:
                await idem.record(status=200, body=response)
                if idem.replayed:
                    # Race loser: defer to the winner's response.
                    return JSONResponse(
                        status_code=idem.replayed_status,
                        content=json.loads(idem.replayed_body),
                    )
            return response

    The dependency returns ``None`` when the client omits ``Idempotency-Key``
    so the handler can detect "no idempotency requested" and skip the record
    call. On a cache hit it raises :class:`_IdempotencyReplay` (same hash) or
    :class:`AMGError(IDEMPOTENCY_KEY_REUSE)` (different hash); the caller
    sees neither because both unwind via FastAPI's exception handlers.
    """

    async def dependency(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> IdempotencyContext | None:
        # Spec note: missing header is allowed even on opt-in routes — the
        # client gets non-idempotent behavior for that single call.
        if idempotency_key is None or not idempotency_key.strip():
            return None
        key = idempotency_key.strip()

        body = await request.body()
        # Stash the body on request.state so downstream Pydantic parsing in
        # the route still sees it (Starlette caches request.body() internally
        # so this is safe).
        request_hash = hash_body(body)

        store = get_configured_store()
        cached = await store.lookup(key)
        log = structlog.get_logger("amg.idempotency")

        if cached is not None:
            if cached.request_hash == request_hash:
                log.info(
                    "idempotency_replay",
                    scope=scope,
                    key_prefix=key[:8],
                    status=cached.response_status,
                )
                raise _IdempotencyReplay(
                    status=cached.response_status,
                    body=cached.response_body,
                )
            log.warning(
                "idempotency_key_reuse",
                scope=scope,
                key_prefix=key[:8],
            )
            raise AMGError(
                code=ErrorCode.IDEMPOTENCY_KEY_REUSE,
                message=(
                    "Idempotency-Key has already been used with a different request body."
                ),
            )

        return IdempotencyContext(
            scope=scope,
            key=key,
            request_hash=request_hash,
            store=store,
        )

    return dependency


# ---------------------------------------------------------------------------
# Replay exception handler
# ---------------------------------------------------------------------------


async def _replay_handler(
    request: Request,  # noqa: ARG001 — FastAPI signature
    exc: _IdempotencyReplay,
) -> JSONResponse:
    # ``exc.body`` is already a JSON string. Send it through Starlette's
    # ``Response`` directly (rather than re-serializing) so byte-for-byte the
    # client sees the same payload it saw on the first call.
    return JSONResponse(
        status_code=exc.status,
        content=json.loads(exc.body) if exc.body else None,
        headers={REPLAY_HEADER: "true"},
    )


def register_idempotency_handlers(app: FastAPI) -> None:
    """Wire :class:`_IdempotencyReplay` to its JSON handler on ``app``."""

    app.add_exception_handler(_IdempotencyReplay, _replay_handler)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Default time provider — UTC wall clock, tz-aware."""

    return datetime.now(tz=UTC)


def _format_ts(ts: datetime) -> str:
    """Render a UTC datetime into the project's canonical ISO 8601 form.

    Naive datetimes are assumed to already be UTC (tests use both forms).
    """

    ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return ts.strftime(_ISO_FORMAT)
