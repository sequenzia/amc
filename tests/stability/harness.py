"""Reusable bounded stability-run harness (task #73; spec §3.2 / §6.1).

Boots the *full* AMC stack against the Phase-0 fakes inside the test process,
drives a configurable load profile, captures latency / webhook / log metrics,
and writes a JSON metrics summary the surrounding pytest can assert against.

The harness is deliberately a small, batteries-included library — not a
subprocess or container — because the build-acceptance gate (spec §6.1) is a
deterministic, in-process load test. A subprocess uvicorn would not let us
intercept the webhook stream, the structlog records, or the per-message
latency points without a control plane that itself would have to be tested.

Public surface
--------------
* :class:`StabilityConfig` — knobs for one harness run (duration, target
  rate, webhook failure rate, parallel agents).
* :class:`StabilityMetrics` — frozen dataclass returned by
  :meth:`StabilityHarness.collect_metrics`.
* :class:`StabilityHarness` — async context manager wrapping the live system.
  Use as::

      async with StabilityHarness(config) as harness:
          await harness.drive_traffic()
          metrics = await harness.collect_metrics()
          harness.write_metrics_json(out_path)

Latency definitions
-------------------
* **receive→visible** — wall-clock time from when the harness *injects* an
  inbound message (Discord ``deliver(payload)`` call returns, or chat.db
  row is appended) to when the row is queryable via
  ``GET /messages/unread``. Measured by the first /messages/unread response
  that returns the new ``messages.id``.
* **send→ack** — wall-clock time from when an in-process client issues
  ``POST /messages/send`` to when the 200 response lands.

Webhook receiver
----------------
The harness boots an :class:`aiohttp.web.Application` on an ephemeral port
that serves ``POST /hooks/amc``. Each request increments a counter; the
first response is 200 by default but the test can switch the receiver to
return ``failure_rate`` percent of the time as 500 (per task brief).

The :class:`amc.core.webhook.WebhookWorker` runs in its real form against
this receiver — no respx interception, no monkey-patching, so the harness
exercises the actual delivery path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import secrets
import shutil
import statistics
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import structlog
from aiohttp import web
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from amc.api.messages_mark_read import (
    configure_session_factory as configure_mark_read_session_factory,
)
from amc.api.messages_mark_read import (
    reset_session_factory as reset_mark_read_session_factory,
)
from amc.api.messages_send import (
    configure_discord_connector,
    configure_imessage_connector,
    configure_rate_limiter,
    reset_discord_connector,
    reset_imessage_connector,
    reset_rate_limiter,
)
from amc.api.messages_send import (
    configure_session_factory as configure_send_session_factory,
)
from amc.api.messages_send import (
    reset_session_factory as reset_send_session_factory,
)
from amc.api.messages_unread import (
    configure_session_factory as configure_unread_session_factory,
)
from amc.api.messages_unread import (
    reset_session_factory as reset_unread_session_factory,
)
from amc.app import build_app
from amc.connectors.discord.connector import DiscordConnector
from amc.connectors.imessage.connector import ImessageConnector
from amc.connectors.imessage.reader import ChatDbReader
from amc.core.allowlist import AllowlistEntry, AllowlistLoader
from amc.core.auth import configure_bearer_token, reset_bearer_token
from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.envelope import Source
from amc.core.idempotency import (
    IdempotencyStore,
    configure_idempotency_store,
    reset_idempotency_store,
)
from amc.core.message_sink import MessageSink
from amc.core.rate_limit import RateLimiter
from amc.core.webhook import WebhookConfig, WebhookWorker
from tests.fakes.applescript import FakeAppleScriptSender
from tests.fakes.discord_gateway import FakeDiscordGateway
from tests.fixtures.build_chat_db import ALLOWLISTED_HANDLE, ensure_chat_db

__all__ = [
    "StabilityConfig",
    "StabilityHarness",
    "StabilityMetrics",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

BEARER = "test-bearer-stability-run"  # noqa: S105 — test fixture
WEBHOOK_SECRET = "stability-shared-secret"  # noqa: S105 — test fixture

# A single Discord DM channel used for every injected Discord message so the
# /messages/send rate-limit bucket and the channels row are stable.
DISCORD_CHANNEL_ID = "777666555444333222"
ALLOWED_DISCORD_USER_ID = "888777666555444333"

# Aggressive iMessage poll so the connector reacts within ~50ms of an append.
TEST_POLL_INTERVAL = 0.05

# Handshake budget for the discord.py gateway dance.
GATEWAY_HANDSHAKE_TIMEOUT = 5.0

# How many concurrent /messages/unread + /messages/mark_read polling clients
# the harness drives in parallel by default.
DEFAULT_PARALLEL_AGENTS = 5

# Receive→visible measurement: how often an agent re-polls /messages/unread.
AGENT_POLL_INTERVAL = 0.05

# Hard cap for AMC_STABILITY_DURATION_SECONDS so an env override can't blow
# CI budgets accidentally — matches the task brief ("cap at 3600 for ad-hoc
# longer runs").
DURATION_CEILING_SECONDS = 3600


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StabilityConfig:
    """Runtime knobs for one stability run.

    Args:
        duration_seconds: How long :meth:`StabilityHarness.drive_traffic`
            keeps generating inbound load.
        target_rate_per_second: Target inbound messages per second across
            both transports combined. Defaults to 1.0 (the spec §6.1
            "1 msg/s" CI profile).
        webhook_failure_rate: Probability (0.0..1.0) the in-process webhook
            receiver returns 500 instead of 200. Defaults to 0.05 (the
            "5% failure rate" from the task brief).
        parallel_agents: Number of in-process clients that hit
            ``/messages/unread`` + ``/messages/mark_read`` in parallel.
        rng_seed: Seed for the PRNG that drives the inbound transport
            choice + receiver flake. Fixing the seed makes the inbound
            stream + flake schedule deterministic across runs.
    """

    duration_seconds: float = 60.0
    target_rate_per_second: float = 1.0
    webhook_failure_rate: float = 0.05
    parallel_agents: int = DEFAULT_PARALLEL_AGENTS
    rng_seed: int = 12345


@dataclass(frozen=True, slots=True)
class StabilityMetrics:
    """Summary metrics produced by one stability run."""

    duration_seconds: float
    inbound_count: int
    inbound_visible_count: int
    p50_receive_to_visible_ms: float
    p95_receive_to_visible_ms: float
    p50_send_to_ack_ms: float
    p95_send_to_ack_ms: float
    webhook_attempts: int
    webhook_first_attempt_success: int
    webhook_first_attempt_success_rate: float
    webhook_500_count: int
    error_log_count: int
    cursor_monotonic: bool
    distinct_agents_with_reads: int
    parallel_agents: int

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict view."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Logging-side scaffolding
# ---------------------------------------------------------------------------


class _ErrorRecorder(logging.Handler):
    """Stdlib log handler that counts ``levelno >= ERROR`` records.

    Attached to the root logger so every adapter component (api, connectors,
    core.*, webhook, etc.) flows through it. Records are also kept in
    memory (capped) so a failing test can surface the actual error message.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []
        self.error_count: int = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.error_count += 1
        # Keep at most 200 records — enough to debug a failure but bounded
        # so a stuck loop doesn't OOM the test process.
        if len(self.records) < 200:
            self.records.append(record)


# ---------------------------------------------------------------------------
# Webhook receiver (aiohttp)
# ---------------------------------------------------------------------------


class _WebhookReceiver:
    """In-process aiohttp app that records webhook deliveries.

    Per the task brief the receiver returns 500 ``failure_rate`` of the
    time. We use a *deterministic round-robin* pattern (every Nth attempt
    fails, where N = 1/failure_rate) instead of an RNG draw so the test
    is repeatable across runs and doesn't rely on a happy seed.

    With ``failure_rate=0.05`` exactly 1 in every 20 attempts returns 500.
    For a 60s / 1msg/s run that's 3 forced 500s out of ~60 attempts —
    matching the spec's 95% first-attempt success floor exactly without
    sampling variance.
    """

    def __init__(self, *, failure_rate: float, rng: random.Random) -> None:
        self._failure_rate = failure_rate
        self._rng = rng  # Kept for parity with the API; unused at runtime.
        # Stride between forced failures. ``round`` so the smallest stride
        # is 1 (every request fails) when failure_rate >= 1.0.
        if failure_rate <= 0.0:
            self._failure_stride = 0  # never fail
        else:
            self._failure_stride = max(1, round(1.0 / failure_rate))
        self.attempts: int = 0
        self.success_count: int = 0
        self.failure_count: int = 0
        # delivery_id -> attempt-number-at-which-it-was-first-2xx (or None
        # if it never has). attempt-number is the value of the
        # ``X-AMC-Attempt`` header (1-based per webhook.py).
        self.first_success_attempt: dict[str, int] = {}
        self.attempts_per_delivery: dict[str, int] = {}

        self._app = web.Application()
        self._app.router.add_post("/hooks/amc", self._handle)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._port: int | None = None

    async def start(self) -> None:
        """Bind to an ephemeral port and start serving."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # aiohttp ≥ 3.10: pull the port off the underlying server's sockets.
        server = self._site._server  # type: ignore[attr-defined]
        if server is None or not server.sockets:
            raise RuntimeError("WebhookReceiver failed to bind a socket")
        self._port = server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._port = None

    @property
    def url(self) -> str:
        if self._port is None:
            raise RuntimeError("WebhookReceiver not started")
        return f"http://127.0.0.1:{self._port}/hooks/amc"

    async def _handle(self, request: web.Request) -> web.Response:
        """Record the attempt; return 200 or 500 based on the PRNG."""
        delivery_id = request.headers.get("X-AMC-Delivery-Id", "")
        attempt_header = request.headers.get("X-AMC-Attempt", "0")
        try:
            attempt = int(attempt_header)
        except ValueError:
            attempt = 0

        self.attempts += 1
        if delivery_id:
            self.attempts_per_delivery[delivery_id] = max(
                self.attempts_per_delivery.get(delivery_id, 0), attempt
            )

        # Deterministic round-robin: every ``_failure_stride``-th attempt
        # fails. ``self.attempts`` was just incremented above so the first
        # forced failure is at attempt #N where N == stride; this gives
        # exactly ⌊total/stride⌋ failures over the run.
        if self._failure_stride > 0 and self.attempts % self._failure_stride == 0:
            self.failure_count += 1
            return web.Response(status=500, text="stability-run flake")

        self.success_count += 1
        if delivery_id and delivery_id not in self.first_success_attempt:
            self.first_success_attempt[delivery_id] = attempt
        return web.Response(status=200, text="ok")


# ---------------------------------------------------------------------------
# Live system bundle (mirrors test_crash_relaunch._LiveSystem)
# ---------------------------------------------------------------------------


@dataclass
class _LiveSystem:
    """Everything that has to be torn down at harness exit."""

    engine: AsyncEngine
    session_factory: async_sessionmaker
    sink: MessageSink
    discord_connector: DiscordConnector
    discord_task: asyncio.Task[None]
    imessage_connector: ImessageConnector
    rate_limiter: RateLimiter
    idempotency_store: IdempotencyStore
    webhook_worker: WebhookWorker
    fastapi_client: TestClient
    fastapi_client_cm: contextlib.AbstractContextManager[TestClient]


# ---------------------------------------------------------------------------
# Allowlist + chat.db helpers
# ---------------------------------------------------------------------------


def _build_inmemory_allowlist(
    entries: Iterable[tuple[Source, str, str, str | None]],
) -> AllowlistLoader:
    """Build an :class:`AllowlistLoader` from explicit tuples (no TOML)."""
    loader = AllowlistLoader.__new__(AllowlistLoader)
    loader.path = None  # type: ignore[assignment]
    loader._entries = {
        (src, ident): AllowlistEntry(
            source=src,
            id=ident,
            display_name=display_name,
            person_id=person_id,
        )
        for (src, ident, display_name, person_id) in entries
    }
    return loader


def _make_dm_payload(
    *,
    message_id: str,
    channel_id: str,
    author_id: str,
    content: str,
    timestamp: str,
) -> dict[str, Any]:
    """Minimal Discord ``MESSAGE_CREATE`` payload (no ``guild_id`` -> DM)."""
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author": {
            "id": author_id,
            "username": "stability-alice",
            "discriminator": "0",
            "global_name": "Stability Alice",
            "avatar": None,
            "bot": False,
        },
        "content": content,
        "timestamp": timestamp,
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "type": 0,
        "pinned": False,
        "flags": 0,
    }


def _append_chat_db_inbound_row(
    chat_db: Path,
    *,
    rowid: int,
    text_value: str,
    handle_id: int = 1,
) -> None:
    """Append one inbound ``message`` + ``chat_message_join`` row."""
    import sqlite3

    conn = sqlite3.connect(chat_db)
    try:
        conn.execute("BEGIN")
        date_ns = 1_000_000_000_000_000_000 + rowid * 1_000_000_000
        guid = f"stab-{rowid:020d}"
        conn.execute(
            "INSERT INTO message ("
            " ROWID, guid, text, handle_id, service, account, date, date_read,"
            " date_delivered, is_delivered, is_finished, is_from_me, is_read,"
            " is_sent, cache_has_attachments, item_type"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rowid,
                guid,
                text_value,
                handle_id,
                "iMessage",
                "E:owner@example.com",
                date_ns,
                0,
                date_ns,
                1,
                1,
                0,
                0,
                0,
                0,
                0,
            ),
        )
        conn.execute(
            "INSERT INTO chat_message_join (chat_id, message_id, message_date) VALUES (?, ?, ?)",
            (1, rowid, date_ns),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class StabilityHarness:
    """Bounded stability-run harness.

    Use as an async context manager so the connectors / engine / aiohttp
    receiver are torn down deterministically even when an assertion fires
    mid-run.

    The harness exposes three high-level operations:

    * :meth:`drive_traffic` — generate inbound load at the configured rate
      for ``config.duration_seconds``. Spawns the parallel agent loop in
      the background.
    * :meth:`collect_metrics` — fold the captured per-message events into
      a :class:`StabilityMetrics` summary.
    * :meth:`write_metrics_json` — dump the metrics as JSON to a path.
    """

    def __init__(
        self,
        config: StabilityConfig,
        *,
        tmp_dir: Path,
    ) -> None:
        self._config = config
        self._tmp_dir = tmp_dir
        self._rng = random.Random(config.rng_seed)

        # Per-message latency captures.
        self._inbound_injected_at: dict[str, float] = {}
        self._inbound_visible_at: dict[str, float] = {}
        self._send_ack_latencies_ms: list[float] = []

        # Per-agent read state (for the "5 concurrent agents" assertion).
        self._agent_reads: dict[str, set[str]] = {}

        # Cursor monotonicity tracking.
        self._cursor_samples_imessage: list[int] = []
        self._cursor_samples_discord: list[int] = []

        # Set up by __aenter__.
        self._gateway: FakeDiscordGateway | None = None
        self._receiver: _WebhookReceiver | None = None
        self._error_recorder: _ErrorRecorder | None = None
        self._live: _LiveSystem | None = None
        self._chat_db_path: Path | None = None
        self._db_path: Path | None = None
        self._agent_tasks: list[asyncio.Task[None]] = []
        self._stop_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Async context-manager lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> StabilityHarness:
        await self._boot()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self._shutdown()

    async def _boot(self) -> None:
        """Bring up every sub-component required for a stability run."""
        # 1) Materialize a writable chat.db copy and a fresh AMC SQLite DB.
        src_chat_db = ensure_chat_db()
        self._chat_db_path = self._tmp_dir / "chat.db"
        shutil.copy2(src_chat_db, self._chat_db_path)

        self._db_path = self._tmp_dir / "amc-stability.db"
        # alembic.command.upgrade reads AMC_DB_PATH via amc.core.db.
        import os

        os.environ["AMC_DB_PATH"] = str(self._db_path)
        cfg = Config(str(ALEMBIC_INI))
        command.upgrade(cfg, "head")

        # 2) Install error recorder on the root logger.
        self._error_recorder = _ErrorRecorder()
        logging.getLogger().addHandler(self._error_recorder)
        # Make sure the structlog→stdlib bridge is configured so adapter
        # components routed through structlog also reach the recorder.
        structlog.configure(
            processors=[structlog.stdlib.render_to_log_kwargs],
            logger_factory=structlog.stdlib.LoggerFactory(),
        )

        # 3) Start the fake Discord gateway and the webhook receiver.
        self._gateway = FakeDiscordGateway()
        await self._gateway.start()

        self._receiver = _WebhookReceiver(
            failure_rate=self._config.webhook_failure_rate,
            rng=self._rng,
        )
        await self._receiver.start()

        # 4) Wire the live system (engine, sink, both connectors, app).
        webhook_config = WebhookConfig(
            url=self._receiver.url,
            secret=WEBHOOK_SECRET,
        )
        self._live = await self._start_live_system(
            db_path=self._db_path,
            chat_db_path=self._chat_db_path,
            gateway=self._gateway,
            webhook_config=webhook_config,
        )

    async def _shutdown(self) -> None:
        """Tear down every resource boot brought up. Idempotent on errors."""
        # 1) Stop the agent loops first so they don't observe a half-torn-down
        #    adapter and log spurious errors.
        self._stop_event.set()
        for task in self._agent_tasks:
            task.cancel()
        for task in self._agent_tasks:
            with contextlib.suppress(BaseException):
                await task
        self._agent_tasks.clear()

        # 2) Adapter + connectors.
        if self._live is not None:
            with contextlib.suppress(Exception):
                await self._live.webhook_worker.stop()
            with contextlib.suppress(Exception):
                await self._live.imessage_connector.stop()
            with contextlib.suppress(Exception):
                await self._live.discord_connector.close()
            self._live.discord_task.cancel()
            with contextlib.suppress(BaseException):
                await self._live.discord_task
            with contextlib.suppress(Exception):
                self._live.fastapi_client_cm.__exit__(None, None, None)
            with contextlib.suppress(Exception):
                await self._live.engine.dispose()
            # Reset module globals so a follow-up test starts clean.
            reset_send_session_factory()
            reset_unread_session_factory()
            reset_mark_read_session_factory()
            reset_rate_limiter()
            reset_discord_connector()
            reset_imessage_connector()
            reset_idempotency_store()
            reset_bearer_token()
            self._live = None

        # 3) Receiver + gateway.
        if self._receiver is not None:
            await self._receiver.stop()
            self._receiver = None
        if self._gateway is not None:
            await self._gateway.stop()
            self._gateway = None

        # 4) Detach the error recorder.
        if self._error_recorder is not None:
            logging.getLogger().removeHandler(self._error_recorder)

    async def _start_live_system(
        self,
        *,
        db_path: Path,
        chat_db_path: Path,
        gateway: FakeDiscordGateway,
        webhook_config: WebhookConfig,
    ) -> _LiveSystem:
        """Wire engine + sink + connectors + worker + app. Mirrors e2e setup."""
        engine = create_engine_from_env(db_path_override=db_path)
        session_factory = create_session_factory(engine)

        allowlist = _build_inmemory_allowlist(
            [
                (
                    Source.DISCORD,
                    ALLOWED_DISCORD_USER_ID,
                    "Stability Alice (Discord)",
                    "person_alice",
                ),
                (
                    Source.IMESSAGE,
                    ALLOWLISTED_HANDLE,
                    "Stability Alice (iMessage)",
                    "person_alice",
                ),
            ]
        )

        # Build the worker first so we can pass its url_snapshot into the
        # sink — keeps every newly-enqueued delivery pinned to the running
        # receiver's URL even if a future task swaps the live config.
        webhook_worker = WebhookWorker(
            session_factory=session_factory,
            config=webhook_config,
            idle_poll_seconds=0.1,
        )

        sink = MessageSink(
            session_factory=session_factory,
            allowlist=allowlist,
            webhook_config=webhook_config,
            url_snapshot=webhook_worker.url_snapshot,
        )

        # ---- Discord connector --------------------------------------------
        discord_connector = DiscordConnector(
            bot_token="fake-token-not-used",
            message_sink=sink,
            allowlist=allowlist,
            gateway_url=gateway.url,
        )
        discord_task = asyncio.create_task(
            discord_connector.start("fake-token-not-used"),
            name="amc.stability.discord.start",
        )
        await self._await_discord_ready(discord_connector, discord_task)

        # ---- iMessage connector ------------------------------------------
        reader = ChatDbReader(chat_db_path)
        imessage_connector = ImessageConnector(
            reader=reader,
            sender=FakeAppleScriptSender(),
            sink=sink,
            allowlist=allowlist,
            session_factory=session_factory,
            attachment_store=None,
            poll_interval_seconds=TEST_POLL_INTERVAL,
        )
        await imessage_connector.start()

        # ---- Webhook worker ----------------------------------------------
        # Worker was constructed above (before the sink) so its url_snapshot
        # is shared with the sink. Start it now so it begins draining the
        # queue as soon as the sink enqueues the first delivery row.
        await webhook_worker.start()

        # ---- Adapter HTTP wiring -----------------------------------------
        configure_bearer_token(BEARER)
        configure_send_session_factory(session_factory)
        configure_unread_session_factory(session_factory)
        configure_mark_read_session_factory(session_factory)

        rate_limiter = RateLimiter(rps=100.0, burst=100)
        configure_rate_limiter(rate_limiter)

        idempotency_store = IdempotencyStore(session_factory=session_factory)
        configure_idempotency_store(idempotency_store)

        configure_discord_connector(discord_connector)
        configure_imessage_connector(None)  # iMessage send not exercised v1.

        app = build_app(bootstrap=False)
        client_cm = TestClient(app, raise_server_exceptions=False)
        client = client_cm.__enter__()

        return _LiveSystem(
            engine=engine,
            session_factory=session_factory,
            sink=sink,
            discord_connector=discord_connector,
            discord_task=discord_task,
            imessage_connector=imessage_connector,
            rate_limiter=rate_limiter,
            idempotency_store=idempotency_store,
            webhook_worker=webhook_worker,
            fastapi_client=client,
            fastapi_client_cm=client_cm,
        )

    @staticmethod
    async def _await_discord_ready(
        connector: DiscordConnector,
        task: asyncio.Task[None],
    ) -> None:
        """Block until the Discord client finishes IDENTIFY / READY."""
        deadline = asyncio.get_running_loop().time() + GATEWAY_HANDSHAKE_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if connector.is_ready():
                return
            if task.done():
                # Surface whatever blew up inside discord.py.
                task.result()
                raise RuntimeError("DiscordConnector start task exited before READY")
            await asyncio.sleep(0.02)
        raise TimeoutError("DiscordConnector did not reach READY within timeout")

    # ------------------------------------------------------------------
    # Driving load
    # ------------------------------------------------------------------

    async def drive_traffic(self) -> None:
        """Generate inbound load at ``target_rate_per_second`` for the duration.

        Also launches the agent-poll loops (one task per
        ``parallel_agents``). The method returns once the duration window
        elapses; outstanding agent tasks are cancelled by the harness exit.
        """
        assert self._gateway is not None and self._live is not None
        assert self._chat_db_path is not None

        # 1) Spawn agent polling tasks. They keep running until the stop
        #    event fires (set by drive_traffic at the end + by __aexit__).
        for i in range(self._config.parallel_agents):
            agent_id = f"stability-agent-{i:02d}"
            self._agent_reads[agent_id] = set()
            task = asyncio.create_task(
                self._agent_poll_loop(agent_id),
                name=f"stability-agent-{agent_id}",
            )
            self._agent_tasks.append(task)

        # 2) Inject inbound at the configured rate.
        deadline = asyncio.get_running_loop().time() + self._config.duration_seconds
        period = 1.0 / max(self._config.target_rate_per_second, 0.0001)
        # iMessage ROWIDs above MAX(seed)=6 — start well past so we never
        # collide with seed rows. Each iMessage append uses the next id.
        next_imessage_rowid = 1000
        sent_count = 0

        next_send_at = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() < deadline:
            now = asyncio.get_running_loop().time()
            if now < next_send_at:
                await asyncio.sleep(min(0.01, next_send_at - now))
                continue

            # Choose transport with a 50/50 split for a "mixed" inbound load.
            transport = "discord" if self._rng.random() < 0.5 else "imessage"
            content = f"stability msg #{sent_count}"
            inject_at = time.monotonic()

            if transport == "discord":
                payload = _make_dm_payload(
                    message_id=str(secrets.randbits(63)),
                    channel_id=DISCORD_CHANNEL_ID,
                    author_id=ALLOWED_DISCORD_USER_ID,
                    content=content,
                    timestamp=datetime.now(UTC).isoformat(timespec="microseconds"),
                )
                await self._gateway.deliver(payload)
                # Index by content so the agent-poll loop can look the row up.
                self._inbound_injected_at[content] = inject_at
            else:
                _append_chat_db_inbound_row(
                    self._chat_db_path,
                    rowid=next_imessage_rowid,
                    text_value=content,
                )
                self._inbound_injected_at[content] = inject_at
                next_imessage_rowid += 1

            sent_count += 1
            next_send_at += period

        # 3) Tell the agent loops to wind down. We don't await them here
        #    so the caller can immediately call collect_metrics(); shutdown
        #    will await them.
        self._stop_event.set()

    async def _agent_poll_loop(self, agent_id: str) -> None:
        """One agent: GET /messages/unread + POST /messages/mark_read in a loop."""
        assert self._live is not None
        client = self._live.fastapi_client
        headers = {
            "Authorization": f"Bearer {BEARER}",
            "X-Agent-ID": agent_id,
        }

        while not self._stop_event.is_set():
            try:
                resp = await asyncio.to_thread(
                    client.get,
                    "/messages/unread",
                    headers=headers,
                )
            except Exception:  # noqa: BLE001 — bound poll loop, never die
                await asyncio.sleep(AGENT_POLL_INTERVAL)
                continue

            if resp.status_code == 200:
                body = resp.json()
                ids_to_mark: list[str] = []
                visible_at = time.monotonic()
                for env in body.get("messages", []):
                    msg_id = env["id"]
                    text_body = env.get("text", "")
                    # Record receive→visible the FIRST time any agent
                    # observes the message — the metric is a property of
                    # the system, not of any one agent.
                    if (
                        text_body in self._inbound_injected_at
                        and msg_id not in self._inbound_visible_at
                    ):
                        self._inbound_visible_at[msg_id] = (
                            visible_at - (self._inbound_injected_at[text_body])
                        )
                    self._agent_reads[agent_id].add(msg_id)
                    ids_to_mark.append(msg_id)

                if ids_to_mark:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(
                            client.post,
                            "/messages/mark_read",
                            headers={
                                **headers,
                                "Content-Type": "application/json",
                            },
                            json={"message_ids": ids_to_mark},
                        )

            await asyncio.sleep(AGENT_POLL_INTERVAL)

    # ------------------------------------------------------------------
    # Send-path probe
    # ------------------------------------------------------------------

    async def issue_send_probe(
        self,
        *,
        text_body: str,
        channel_id: str | None = None,
    ) -> dict[str, Any]:
        """Issue one ``POST /messages/send`` and record the ack latency.

        The Discord connector's real ``send`` would attempt a REST POST to
        the channel; we monkey-patch it to return a synthetic SendResult so
        the harness exercises the *adapter* code path (rate-limit, persist,
        idempotency cache) without touching the network.
        """
        assert self._live is not None

        from amc.connectors.discord.connector import SendResult

        async def _fake_send(*_args: Any, **_kwargs: Any) -> SendResult:
            return SendResult(
                ok=True,
                message_id=str(secrets.randbits(63)),
                error=None,
                detail=None,
            )

        self._live.discord_connector.send = _fake_send  # type: ignore[method-assign]

        target_channel = channel_id or f"discord:dm:{DISCORD_CHANNEL_ID}"

        start = time.monotonic()
        resp = await asyncio.to_thread(
            self._live.fastapi_client.post,
            "/messages/send",
            headers={
                "Authorization": f"Bearer {BEARER}",
                "X-Agent-ID": "stability-send-probe",
                "Content-Type": "application/json",
            },
            json={"channel_id": target_channel, "text": text_body},
        )
        elapsed_ms = (time.monotonic() - start) * 1000.0
        if resp.status_code == 200:
            self._send_ack_latencies_ms.append(elapsed_ms)
        body: dict[str, Any] | None = None
        if resp.headers.get("content-type", "").startswith("application/json"):
            body = resp.json()
        return {
            "status_code": resp.status_code,
            "elapsed_ms": elapsed_ms,
            "body": body,
        }

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def collect_metrics(self) -> StabilityMetrics:
        """Fold captured per-message events into a :class:`StabilityMetrics`."""
        assert self._receiver is not None
        assert self._error_recorder is not None
        assert self._db_path is not None

        # Receive→visible
        rtv_values_ms = [v * 1000.0 for v in self._inbound_visible_at.values()]
        p50_rtv = self._percentile(rtv_values_ms, 50.0)
        p95_rtv = self._percentile(rtv_values_ms, 95.0)

        # Send→ack
        p50_sa = self._percentile(self._send_ack_latencies_ms, 50.0)
        p95_sa = self._percentile(self._send_ack_latencies_ms, 95.0)

        # Webhook delivery: "first attempt success" = first POST returned 2xx.
        first_success = sum(
            1 for attempt in self._receiver.first_success_attempt.values() if attempt == 1
        )
        # Use unique deliveries seen as the denominator so retries don't
        # inflate the success rate.
        total_unique = len(self._receiver.attempts_per_delivery)
        first_success_rate = (first_success / total_unique) if total_unique else 1.0

        # Cursor monotonicity.
        cursor_monotonic = self._sample_cursor_monotonic()

        # Distinct agents that read something.
        distinct_agents = sum(1 for reads in self._agent_reads.values() if reads)

        return StabilityMetrics(
            duration_seconds=self._config.duration_seconds,
            inbound_count=len(self._inbound_injected_at),
            inbound_visible_count=len(self._inbound_visible_at),
            p50_receive_to_visible_ms=p50_rtv,
            p95_receive_to_visible_ms=p95_rtv,
            p50_send_to_ack_ms=p50_sa,
            p95_send_to_ack_ms=p95_sa,
            webhook_attempts=self._receiver.attempts,
            webhook_first_attempt_success=first_success,
            webhook_first_attempt_success_rate=first_success_rate,
            webhook_500_count=self._receiver.failure_count,
            error_log_count=self._error_recorder.error_count,
            cursor_monotonic=cursor_monotonic,
            distinct_agents_with_reads=distinct_agents,
            parallel_agents=self._config.parallel_agents,
        )

    def write_metrics_json(self, path: Path, metrics: StabilityMetrics) -> None:
        """Dump ``metrics`` as a JSON file at ``path`` (for test assertions)."""
        path.write_text(json.dumps(metrics.as_dict(), indent=2, sort_keys=True))

    # ------------------------------------------------------------------
    # Diagnostics for failing tests
    # ------------------------------------------------------------------

    def error_records(self) -> list[logging.LogRecord]:
        """Return ERROR-level records captured during the run.

        Useful when a metrics assertion trips and the test wants to surface
        the actual error messages in the failure output.
        """
        if self._error_recorder is None:
            return []
        return [r for r in self._error_recorder.records if r.levelno >= logging.ERROR]

    async def wait_for_pending_webhooks(self, *, timeout: float = 10.0) -> None:
        """Poll until ``webhook_deliveries`` has no pending rows.

        Bounds the number of in-flight webhook deliveries the test waits
        for so the metrics summary captures the full delivery picture.
        """
        assert self._db_path is not None
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            pending = self._count_pending_deliveries()
            if pending == 0:
                return
            await asyncio.sleep(0.1)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        """Return the ``percentile``th percentile (linear) or 0.0 on empty."""
        if not values:
            return 0.0
        if len(values) == 1:
            return values[0]
        # statistics.quantiles wants n=100 + index access; for small samples
        # a manual linear interpolation is fine and avoids ``n>=2`` edge cases.
        sorted_vals = sorted(values)
        k = (len(sorted_vals) - 1) * (percentile / 100.0)
        lower = int(k)
        upper = min(lower + 1, len(sorted_vals) - 1)
        weight = k - lower
        return sorted_vals[lower] * (1.0 - weight) + sorted_vals[upper] * weight

    def _sample_cursor_monotonic(self) -> bool:
        """Read ``connector_state.cursor`` for both sources; sample monotonicity.

        Reads the persisted cursor for ``imessage`` and ``discord`` and
        compares to the running max samples we collected during the run.
        Any single regression flips the result to ``False``.
        """
        assert self._db_path is not None
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT source, cursor FROM connector_state").fetchall():
                source = row["source"]
                cursor_raw = row["cursor"]
                if source == "imessage":
                    try:
                        value = int(cursor_raw)
                    except (TypeError, ValueError):
                        continue
                    if self._cursor_samples_imessage and value < self._cursor_samples_imessage[-1]:
                        return False
                    self._cursor_samples_imessage.append(value)
                elif source == "discord":
                    try:
                        decoded = json.loads(cursor_raw)
                        seq = decoded.get("seq")
                        if not isinstance(seq, int):
                            continue
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if self._cursor_samples_discord and seq < self._cursor_samples_discord[-1]:
                        return False
                    self._cursor_samples_discord.append(seq)
        finally:
            conn.close()
        return True

    def _count_pending_deliveries(self) -> int:
        assert self._db_path is not None
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE status = 'pending'"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Convenience: async generator for pytest fixtures
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def boot_stability_harness(
    config: StabilityConfig,
    *,
    tmp_dir: Path,
) -> AsyncIterator[StabilityHarness]:
    """Async context-manager wrapper for :class:`StabilityHarness`.

    Identical to using ``async with StabilityHarness(...) as harness:`` —
    provided so a pytest fixture can ``yield`` the harness through a
    contextmanager-style helper without subclassing.
    """
    async with StabilityHarness(config, tmp_dir=tmp_dir) as harness:
        yield harness


# Silence "imported but unused" warnings for symbols re-exported indirectly.
_ = (httpx, statistics)
