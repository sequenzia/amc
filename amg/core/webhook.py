"""Outbound webhook delivery worker (spec §5.5, §6.4, §7.4.11, OQ-6).

This module implements the async background task that drains the
``webhook_deliveries`` queue, signs each payload with HMAC-SHA256, POSTs the
normalized envelope to the configured receiver, and retries on failure with
the spec-mandated exponential backoff.

Public surface
--------------
* :class:`WebhookConfig` — frozen value object holding ``url`` + ``secret``.
* :class:`WebhookConfigError` — raised by :func:`load_webhook_config_from_env`
  and :func:`validate_webhook_config_or_exit` when the env vars are
  inconsistent (URL set, secret missing — per OQ-6 the adapter refuses to
  start in that case).
* :func:`load_webhook_config_from_env` — read ``AMG_WEBHOOK_URL`` and
  ``AMG_WEBHOOK_SECRET``; returns ``None`` when the URL is unset (worker
  disabled).
* :func:`validate_webhook_config_or_exit` — wrapper called from the adapter
  startup hook; logs and re-raises ``WebhookConfigError`` so launchd / the
  shell sees the failure.
* :func:`compute_signature` — HMAC-SHA256 over raw bytes, returns the
  ``sha256=<hex>`` string used in ``X-AMG-Signature``.
* :func:`enqueue_webhook` — INSERT a ``status='pending'`` row for a freshly
  persisted message id; returns the new delivery id (or ``None`` if the
  worker is disabled).
* :class:`WebhookWorker` — the asyncio task. ``await worker.start()`` from
  the adapter startup; ``await worker.stop()`` on shutdown.

Design notes
------------
* The worker is deliberately stateless beyond an in-memory
  ``delivery_id -> url`` snapshot map. The map is populated by
  :func:`enqueue_webhook` so an in-flight retry uses the URL captured at
  queue time rather than the live config (spec §5.5 edge-case row "Webhook
  URL changes at runtime"). Rows that were enqueued in a previous adapter
  process are not in the map; they fall back to the worker's
  startup-snapshot URL — which matches the operator intent of "switch to
  the new URL for everything once we restart".
* Backoff schedule is the literal sequence from spec §5.5 / §6.4:
  ``[1s, 5s, 30s, 2m, 10m]``. After the 5th failure (``attempt == 5`` post
  increment), the row is dead-lettered.
* ``time_provider`` is injectable so tests fast-forward through the
  10-minute backoff without sleeping.
* HTTP client is :class:`httpx.AsyncClient` with a 10-second connect /
  read timeout (per the task spec). Network errors and timeouts are
  treated like 5xx — they retry.
* The worker survives restarts because all state is in the DB. On
  ``start()`` it begins polling and naturally re-picks up any rows where
  ``status='pending' AND next_retry_at <= now``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ulid import ULID

from amg.core.envelope import Envelope

__all__ = [
    "BACKOFF_SECONDS",
    "DEFAULT_HTTP_TIMEOUT",
    "ENV_WEBHOOK_SECRET",
    "ENV_WEBHOOK_URL",
    "MAX_ATTEMPTS",
    "WebhookConfig",
    "WebhookConfigError",
    "WebhookWorker",
    "compute_signature",
    "enqueue_webhook",
    "load_webhook_config_from_env",
    "validate_webhook_config_or_exit",
]

# ---------------------------------------------------------------------------
# Constants (spec §5.5, §6.4)
# ---------------------------------------------------------------------------

ENV_WEBHOOK_URL: Final[str] = "AMG_WEBHOOK_URL"
ENV_WEBHOOK_SECRET: Final[str] = "AMG_WEBHOOK_SECRET"

# Backoff schedule per spec §5.5 / §6.4: 1s, 5s, 30s, 2m, 10m. Index ``n`` is
# the wait BEFORE attempt ``n+2`` (after attempt ``n+1`` failed). After the
# 5th attempt fails, the row is marked ``dead`` — there is no 6th attempt.
BACKOFF_SECONDS: Final[tuple[int, ...]] = (1, 5, 30, 120, 600)

# Total attempts allowed before dead-lettering (per spec §5.5).
MAX_ATTEMPTS: Final[int] = 5

# Per-request connect/read timeout (per task spec).
DEFAULT_HTTP_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(
    connect=10.0, read=10.0, write=10.0, pool=10.0
)

# Default poll interval when there's no work in the queue.
_IDLE_POLL_SECONDS: Final[float] = 1.0


_log = structlog.get_logger("amg.webhook")


def _to_iso_z(dt: datetime) -> str:
    """Serialize ``dt`` to ISO 8601 UTC with explicit microseconds + ``Z``.

    Microsecond width is fixed at 6 so the resulting strings sort
    lexicographically in chronological order — the SQLite ``next_retry_at``
    column is compared as text, and a mix of "...01Z" and "...01.001000Z"
    would sort wrong.
    """
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return aware.isoformat(timespec="microseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class WebhookConfigError(ValueError):
    """Raised when ``AMG_WEBHOOK_URL`` is set but ``AMG_WEBHOOK_SECRET`` is missing.

    Per OQ-6, the adapter must refuse to start in this case. The message
    surfaces in the launchd / shell error path so an operator can fix the env
    file.
    """


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    """URL + shared secret for outbound webhook delivery."""

    url: str
    secret: str


def load_webhook_config_from_env(env: dict[str, str] | None = None) -> WebhookConfig | None:
    """Resolve webhook config from ``AMG_WEBHOOK_URL`` / ``AMG_WEBHOOK_SECRET``.

    Returns ``None`` when the URL is unset (or empty) — the worker is then
    disabled and :func:`enqueue_webhook` is a no-op.

    Raises :class:`WebhookConfigError` when the URL is set but the secret is
    missing or empty (OQ-6: refuse to start without HMAC).
    """
    source = env if env is not None else os.environ
    url = (source.get(ENV_WEBHOOK_URL) or "").strip()
    secret = (source.get(ENV_WEBHOOK_SECRET) or "").strip()

    if not url:
        return None
    if not secret:
        raise WebhookConfigError(
            f"{ENV_WEBHOOK_URL} is set but {ENV_WEBHOOK_SECRET} is missing or empty. "
            "The AMG adapter refuses to start without an HMAC secret (OQ-6). "
            f"Set {ENV_WEBHOOK_SECRET} in ~/.config/messaging-agent/.env or unset "
            f"{ENV_WEBHOOK_URL} to disable webhook delivery."
        )
    return WebhookConfig(url=url, secret=secret)


def validate_webhook_config_or_exit(env: dict[str, str] | None = None) -> WebhookConfig | None:
    """Load + validate webhook config at adapter startup.

    Wrapper around :func:`load_webhook_config_from_env` that logs the
    validation outcome at INFO level. Re-raises :class:`WebhookConfigError`
    on misconfiguration so the adapter process exits non-zero (per OQ-6).

    Adapter wiring (task #41) calls this from the FastAPI startup hook.
    """
    try:
        config = load_webhook_config_from_env(env)
    except WebhookConfigError as exc:
        _log.error("webhook_config_invalid", reason=str(exc))
        raise
    if config is None:
        _log.info("webhook_disabled", reason="AMG_WEBHOOK_URL unset")
    else:
        _log.info("webhook_enabled", url=config.url)
    return config


# ---------------------------------------------------------------------------
# HMAC
# ---------------------------------------------------------------------------


def compute_signature(secret: str, body: bytes) -> str:
    """Compute the ``sha256=<hex>`` value for ``X-AMG-Signature``.

    The HMAC is computed over the **exact bytes** of ``body`` so the receiver
    can recompute it without re-parsing JSON. Use the bytes that will go on
    the wire — never re-serialize before signing.
    """
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes; sign the raw request body, not a string")
    digest = hmac.new(secret.encode("utf-8"), bytes(body), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue_webhook(
    session: AsyncSession,
    message_id: str,
    *,
    config: WebhookConfig | None,
    url_snapshot: dict[str, str] | None = None,
    time_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str | None:
    """Insert a fresh ``status='pending'`` row for ``message_id``.

    Returns the new delivery id (a ``str(ULID())``). Returns ``None`` when
    ``config`` is ``None`` (webhook disabled — no row is created, satisfying
    spec §5.5 "if unset, no `webhook_deliveries` rows are created").

    The URL from ``config`` is captured into ``url_snapshot`` so an in-flight
    retry uses this URL even if the live config later changes (spec §5.5
    edge-case row "Webhook URL changes at runtime"). The snapshot is the
    worker's in-memory map — pass ``WebhookWorker._url_snapshot`` here, or a
    fresh dict in tests.

    Caller is responsible for ``await session.commit()``.
    """
    if config is None:
        return None

    delivery_id = str(ULID())
    now_iso = _to_iso_z(time_provider())

    # status='pending', next_retry_at=now (process immediately on next tick).
    await session.execute(
        text(
            "INSERT INTO webhook_deliveries "
            "(id, message_id, attempt, next_retry_at, status, created_at, updated_at) "
            "VALUES (:id, :message_id, 0, :next_retry_at, 'pending', :now, :now)"
        ),
        {
            "id": delivery_id,
            "message_id": message_id,
            "next_retry_at": now_iso,
            "now": now_iso,
        },
    )

    if url_snapshot is not None:
        url_snapshot[delivery_id] = config.url

    return delivery_id


# ---------------------------------------------------------------------------
# Envelope reload
# ---------------------------------------------------------------------------


async def _load_envelope_for_delivery(
    session: AsyncSession,
    message_id: str,
) -> Envelope | None:
    """Reconstruct the :class:`Envelope` for ``message_id`` from persisted rows.

    Joins ``messages`` to ``senders`` to resolve sender display name +
    person_id, and uses the message row's ``attachments_json`` to populate
    the envelope's ``attachments`` array. ``raw_json`` is decoded into the
    envelope's ``raw`` field.

    Returns ``None`` if the message was deleted between enqueue and delivery
    (caller treats this as a permanent skip).
    """
    row = (
        (
            await session.execute(
                text(
                    "SELECT m.id, m.source, m.channel_id, m.channel_type, "
                    "       m.sender_id, m.text, m.reply_to, m.direction, "
                    "       m.message_ts, m.attachments_json, m.raw_json, "
                    "       s.display_name AS sender_display_name, "
                    "       s.person_id AS sender_person_id "
                    "FROM messages m "
                    "LEFT JOIN senders s "
                    "  ON s.source = m.source AND s.sender_id = m.sender_id "
                    "WHERE m.id = :id"
                ),
                {"id": message_id},
            )
        )
        .mappings()
        .first()
    )

    if row is None:
        return None

    attachments_json = row.get("attachments_json")
    attachments: list[dict[str, Any]]
    if attachments_json:
        try:
            decoded = json.loads(attachments_json)
        except json.JSONDecodeError:
            decoded = []
        attachments = decoded if isinstance(decoded, list) else []
    else:
        attachments = []

    raw_json = row.get("raw_json")
    raw: dict[str, Any]
    if raw_json:
        try:
            decoded_raw = json.loads(raw_json)
        except json.JSONDecodeError:
            decoded_raw = {}
        raw = decoded_raw if isinstance(decoded_raw, dict) else {}
    else:
        raw = {}

    envelope_dict = {
        "id": row["id"],
        "source": row["source"],
        "channel_id": row["channel_id"],
        "channel_type": row["channel_type"],
        "sender": {
            "id": row["sender_id"],
            "display_name": row["sender_display_name"] or row["sender_id"],
            "person_id": row["sender_person_id"],
        },
        "text": row["text"],
        "attachments": attachments,
        "reply_to": row["reply_to"],
        "timestamp": row["message_ts"],
        "direction": row["direction"],
        "raw": raw,
    }
    return Envelope.model_validate(envelope_dict)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class WebhookWorker:
    """Async background task that drains the ``webhook_deliveries`` queue.

    Construct with the session factory + webhook config, then ``await
    worker.start()`` from the adapter startup hook. ``await worker.stop()``
    cancels the task and closes the HTTP client.

    Tests typically construct the worker, then call :meth:`tick` directly
    instead of running the loop. ``tick()`` does one pass over the queue and
    returns the number of rows it processed.
    """

    __slots__ = (
        "_config",
        "_session_factory",
        "_time_provider",
        "_http_client",
        "_owns_http_client",
        "_url_snapshot",
        "_task",
        "_stop_event",
        "_idle_poll_seconds",
    )

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: WebhookConfig | None,
        time_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
        http_client: httpx.AsyncClient | None = None,
        idle_poll_seconds: float = _IDLE_POLL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._time_provider = time_provider
        self._idle_poll_seconds = idle_poll_seconds

        if http_client is not None:
            self._http_client = http_client
            self._owns_http_client = False
        else:
            self._http_client = httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT)
            self._owns_http_client = True

        # Per-delivery URL captured at enqueue time. Survives within this
        # process; rows enqueued before this worker existed (post-restart)
        # fall back to ``_config.url``.
        self._url_snapshot: dict[str, str] = {}

        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    @property
    def url_snapshot(self) -> dict[str, str]:
        """Mutable map of ``delivery_id -> url`` captured at enqueue time.

        Pass to :func:`enqueue_webhook` so each delivery's URL is pinned for
        the lifetime of this worker process.
        """
        return self._url_snapshot

    @property
    def enabled(self) -> bool:
        """``True`` if the worker has a config and will process rows."""
        return self._config is not None

    async def start(self) -> None:
        """Start the background polling loop.

        No-op when the worker is disabled (``AMG_WEBHOOK_URL`` unset).
        """
        if not self.enabled:
            _log.info("webhook_worker_disabled")
            return
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="amg-webhook-worker")
        _log.info("webhook_worker_started")

    async def stop(self) -> None:
        """Stop the loop and release the HTTP client."""
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        if self._owns_http_client:
            await self._http_client.aclose()
        _log.info("webhook_worker_stopped")

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        """Polling loop. Exits when ``_stop_event`` is set."""
        while not self._stop_event.is_set():
            try:
                processed = await self.tick()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                _log.error("webhook_worker_tick_error", exc_info=exc)
                processed = 0
            if processed == 0:
                # Sleep, but wake early if stop() is called.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._idle_poll_seconds,
                    )

    async def tick(self) -> int:
        """Run one pass over the queue.

        Selects every row where ``status='pending' AND next_retry_at <= now``
        in ``next_retry_at ASC`` order, attempts delivery, and updates the
        row in-place. Returns the count of rows processed.
        """
        if not self.enabled:
            return 0

        now_iso = _to_iso_z(self._time_provider())

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, message_id, attempt FROM webhook_deliveries "
                        "WHERE status = 'pending' AND next_retry_at <= :now "
                        "ORDER BY next_retry_at ASC"
                    ),
                    {"now": now_iso},
                )
            ).all()

        processed = 0
        for row in rows:
            delivery_id, message_id, attempt = row
            await self._deliver_one(delivery_id, message_id, attempt)
            processed += 1
        return processed

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def _deliver_one(self, delivery_id: str, message_id: str, attempt: int) -> None:
        """Deliver a single queue row; update DB based on the outcome."""
        assert self._config is not None  # noqa: S101 - tick gates on enabled

        async with self._session_factory() as session:
            envelope = await _load_envelope_for_delivery(session, message_id)
        if envelope is None:
            # Message was deleted; treat as dead so the row is closed out.
            _log.warning(
                "webhook_message_missing",
                delivery_id=delivery_id,
                message_id=message_id,
            )
            await self._mark_dead(delivery_id, code=None, error="message_missing")
            return

        # Serialize once so the body bytes used for HMAC == bytes on wire.
        body_bytes = envelope.model_dump_json(by_alias=False).encode("utf-8")
        signature = compute_signature(self._config.secret, body_bytes)

        # URL captured at enqueue time wins; fall back to live config when
        # the snapshot is empty (e.g. row enqueued in a previous process).
        target_url = self._url_snapshot.get(delivery_id, self._config.url)

        next_attempt = attempt + 1
        headers = {
            "Content-Type": "application/json",
            "X-AMG-Signature": signature,
            "X-AMG-Delivery-Id": delivery_id,
            "X-AMG-Message-Id": envelope.id,
            "X-AMG-Attempt": str(next_attempt),
        }

        status_code: int | None = None
        error_excerpt: str | None = None

        try:
            response = await self._http_client.post(target_url, content=body_bytes, headers=headers)
            status_code = response.status_code
        except httpx.TimeoutException as exc:
            error_excerpt = f"timeout: {exc!r}"[:500]
        except httpx.HTTPError as exc:
            error_excerpt = f"network_error: {exc!r}"[:500]

        outcome = self._classify_outcome(status_code, error_excerpt is not None)

        if outcome == "delivered":
            await self._mark_delivered(delivery_id, attempt=next_attempt, code=status_code)
            self._url_snapshot.pop(delivery_id, None)
            _log.info(
                "webhook_delivered",
                delivery_id=delivery_id,
                message_id=envelope.id,
                attempt=next_attempt,
                status=status_code,
            )
            return

        if outcome == "dead_4xx":
            await self._mark_dead(
                delivery_id,
                code=status_code,
                error=error_excerpt or "4xx_permanent",
                attempt=next_attempt,
            )
            self._url_snapshot.pop(delivery_id, None)
            _log.warning(
                "webhook_dead_4xx",
                delivery_id=delivery_id,
                message_id=envelope.id,
                attempt=next_attempt,
                status=status_code,
            )
            return

        # outcome == "retry": 5xx, network error, or timeout.
        if next_attempt >= MAX_ATTEMPTS:
            await self._mark_dead(
                delivery_id,
                code=status_code,
                error=error_excerpt or f"http_{status_code}",
                attempt=next_attempt,
            )
            self._url_snapshot.pop(delivery_id, None)
            _log.warning(
                "webhook_dead_max_attempts",
                delivery_id=delivery_id,
                message_id=envelope.id,
                attempt=next_attempt,
                status=status_code,
            )
            return

        # Schedule the next retry. ``BACKOFF_SECONDS[next_attempt - 1]`` is
        # the wait BEFORE attempt ``next_attempt + 1`` (i.e. after the
        # ``next_attempt``-th attempt just failed).
        backoff = BACKOFF_SECONDS[next_attempt - 1]
        next_retry_iso = _to_iso_z(self._time_provider() + timedelta(seconds=backoff))

        await self._update_for_retry(
            delivery_id,
            attempt=next_attempt,
            next_retry_at=next_retry_iso,
            code=status_code,
            error=error_excerpt or (f"http_{status_code}" if status_code else "unknown"),
        )
        _log.info(
            "webhook_retry_scheduled",
            delivery_id=delivery_id,
            message_id=envelope.id,
            attempt=next_attempt,
            next_retry_at=next_retry_iso,
            status=status_code,
        )

    @staticmethod
    def _classify_outcome(status_code: int | None, network_failure: bool) -> str:
        """Return ``"delivered"`` | ``"dead_4xx"`` | ``"retry"``."""
        if network_failure or status_code is None:
            return "retry"
        if 200 <= status_code < 300:
            return "delivered"
        if 400 <= status_code < 500:
            return "dead_4xx"
        # 1xx, 3xx, 5xx all retry. 1xx is degenerate; 3xx is treated as
        # non-success since httpx follows redirects by default = false.
        return "retry"

    # ------------------------------------------------------------------
    # DB updates
    # ------------------------------------------------------------------

    async def _mark_delivered(self, delivery_id: str, *, attempt: int, code: int | None) -> None:
        now_iso = _to_iso_z(self._time_provider())
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "UPDATE webhook_deliveries SET "
                    "  status = 'delivered', "
                    "  attempt = :attempt, "
                    "  next_retry_at = NULL, "
                    "  last_response_code = :code, "
                    "  last_error = NULL, "
                    "  updated_at = :now "
                    "WHERE id = :id"
                ),
                {"id": delivery_id, "attempt": attempt, "code": code, "now": now_iso},
            )
            await session.commit()

    async def _mark_dead(
        self,
        delivery_id: str,
        *,
        code: int | None,
        error: str | None,
        attempt: int | None = None,
    ) -> None:
        """Mark a delivery row dead and clear the retry pointer.

        ``attempt`` is updated when supplied (post-final-failure path so the
        ``attempt`` column reflects the number of POSTs we issued). When
        ``None`` (e.g. message-missing path), the existing value is retained.
        """
        now_iso = _to_iso_z(self._time_provider())
        if attempt is None:
            sql = (
                "UPDATE webhook_deliveries SET "
                "  status = 'dead', "
                "  next_retry_at = NULL, "
                "  last_response_code = :code, "
                "  last_error = :error, "
                "  updated_at = :now "
                "WHERE id = :id"
            )
            params: dict[str, object] = {
                "id": delivery_id,
                "code": code,
                "error": error[:500] if error else None,
                "now": now_iso,
            }
        else:
            sql = (
                "UPDATE webhook_deliveries SET "
                "  status = 'dead', "
                "  attempt = :attempt, "
                "  next_retry_at = NULL, "
                "  last_response_code = :code, "
                "  last_error = :error, "
                "  updated_at = :now "
                "WHERE id = :id"
            )
            params = {
                "id": delivery_id,
                "attempt": attempt,
                "code": code,
                "error": error[:500] if error else None,
                "now": now_iso,
            }
        async with self._session_factory() as session:
            await session.execute(text(sql), params)
            await session.commit()

    async def _update_for_retry(
        self,
        delivery_id: str,
        *,
        attempt: int,
        next_retry_at: str,
        code: int | None,
        error: str | None,
    ) -> None:
        now_iso = _to_iso_z(self._time_provider())
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "UPDATE webhook_deliveries SET "
                    "  attempt = :attempt, "
                    "  next_retry_at = :next_retry_at, "
                    "  last_response_code = :code, "
                    "  last_error = :error, "
                    "  updated_at = :now "
                    "WHERE id = :id AND status = 'pending'"
                ),
                {
                    "id": delivery_id,
                    "attempt": attempt,
                    "next_retry_at": next_retry_at,
                    "code": code,
                    "error": error[:500] if error else None,
                    "now": now_iso,
                },
            )
            await session.commit()
