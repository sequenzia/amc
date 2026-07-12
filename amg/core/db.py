"""Async SQLAlchemy engine + session factory for the AMG SQLite store.

Spec references: §7.3 (storage), §11.2 (``AMG_DB_PATH``).

Public surface:

- :data:`DEFAULT_DB_PATH` — fallback location when ``AMG_DB_PATH`` is unset.
- :func:`build_database_url` — turns a filesystem path into an ``aiosqlite`` URL.
- :func:`create_engine_from_env` — convenience factory that reads
  ``AMG_DB_PATH`` from the environment, applies WAL + ``foreign_keys = ON`` at
  every connection, and returns an :class:`AsyncEngine`.
- :func:`create_session_factory` — returns an
  :class:`async_sessionmaker[AsyncSession]` bound to the given engine.

Design notes:
- WAL mode is set per connection via SQLAlchemy's ``connect`` event hook (per
  the task spec: "WAL mode pragma applied at adapter startup, not in
  migration"). The pragma is idempotent so re-applying on each connection is
  cheap.
- ``PRAGMA foreign_keys = ON`` is also set per connection because SQLite
  defaults to OFF and the schema relies on FK enforcement.
- The default DB path is
  ``~/Library/Application Support/messaging-agent/state.db``. The filename is
  deliberately brand-free, so the AMC→AMG rename left the operator's database
  where it was.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

__all__ = [
    "DEFAULT_DB_PATH",
    "DB_PATH_ENV_VAR",
    "build_database_url",
    "create_engine_from_env",
    "create_session_factory",
    "ensure_parent_dir",
    "register_sqlite_pragmas",
]

DB_PATH_ENV_VAR: Final[str] = "AMG_DB_PATH"
DEFAULT_DB_PATH: Final[Path] = Path(
    "~/Library/Application Support/messaging-agent/state.db"
).expanduser()


def build_database_url(db_path: Path | str) -> str:
    """Return an ``aiosqlite`` URL for ``db_path``.

    Accepts either an absolute filesystem path or the literal string
    ``":memory:"`` for in-process tests.
    """
    if str(db_path) == ":memory:":
        return "sqlite+aiosqlite:///:memory:"
    return f"sqlite+aiosqlite:///{Path(db_path).expanduser()}"


def ensure_parent_dir(db_path: Path) -> None:
    """Create the parent directory of ``db_path`` if it does not exist."""
    parent = db_path.expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)


def register_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Hook the engine so every new connection has WAL + FK enforcement on.

    SQLite's ``journal_mode`` is a per-database setting (persists in the file
    header), so the WAL pragma only does work the first time. ``foreign_keys``
    is per-connection and must be set every time.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute("PRAGMA foreign_keys = ON;")
        finally:
            cursor.close()


def create_engine_from_env(
    *,
    echo: bool = False,
    db_path_override: Path | str | None = None,
) -> AsyncEngine:
    """Build an :class:`AsyncEngine` from the environment.

    Resolution order for the DB path:

    1. ``db_path_override`` argument (used by tests).
    2. ``$AMG_DB_PATH`` environment variable.
    3. :data:`DEFAULT_DB_PATH`.

    Side effects: ensures the parent directory exists, and registers a
    ``connect`` event hook that applies WAL mode + ``foreign_keys = ON``.
    """
    if db_path_override is not None:
        db_path: Path = (
            Path(db_path_override).expanduser()
            if str(db_path_override) != ":memory:"
            else Path(":memory:")
        )
    else:
        env_value = os.environ.get(DB_PATH_ENV_VAR, "").strip()
        db_path = Path(env_value).expanduser() if env_value else DEFAULT_DB_PATH

    if str(db_path) != ":memory:":
        ensure_parent_dir(db_path)

    url = build_database_url(db_path)
    engine = create_async_engine(url, echo=echo, future=True)
    register_sqlite_pragmas(engine)
    return engine


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to ``engine``.

    The factory yields :class:`AsyncSession` instances with autoflush off and
    ``expire_on_commit=False`` so loaded objects remain usable after commit
    (we don't use the ORM identity map but the setting is still required for
    Core-mode session reuse).
    """
    return async_sessionmaker(
        engine,
        class_=AsyncSession,
        autoflush=False,
        expire_on_commit=False,
    )
