"""Alembic environment for the AMC SQLite store.

The DB URL is resolved at runtime from ``$AMC_DB_PATH`` (default
``~/Library/Application Support/messaging-agent/state.db``) so the same
``alembic.ini`` works in dev, CI, and prod without edits.

We use a synchronous ``sqlite://`` driver here because Alembic's online runner
expects sync ``connect()`` semantics. The application itself uses an async
``aiosqlite`` engine via :mod:`amc.core.db`; the schema is identical.
"""

from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event, pool

# Make ``amc`` importable when invoked as ``alembic upgrade head`` from the
# repo root. ``prepend_sys_path = .`` in alembic.ini already does this in
# practice; this is belt-and-braces for cases where alembic is invoked via
# ``-c <abs path to ini>`` from outside the repo.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from amc.core.schema import metadata as target_metadata  # noqa: E402

DB_PATH_ENV_VAR = "AMC_DB_PATH"
DEFAULT_DB_PATH = Path("~/Library/Application Support/messaging-agent/state.db").expanduser()


def _resolve_database_url() -> str:
    """Resolve the sync SQLite URL from ``$AMC_DB_PATH``.

    Precedence:
    1. ``-x url=...`` argument passed on the alembic CLI (overrides everything;
       used by tests to point at a tmp_path DB).
    2. ``$AMC_DB_PATH`` environment variable.
    3. :data:`DEFAULT_DB_PATH`.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "url" in x_args and x_args["url"]:
        return x_args["url"]

    env_value = os.environ.get(DB_PATH_ENV_VAR, "").strip()
    db_path = Path(env_value).expanduser() if env_value else DEFAULT_DB_PATH

    if str(db_path) == ":memory:":
        return "sqlite:///:memory:"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def run_migrations_offline() -> None:
    """Render migrations as SQL without a live DB connection."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Reuse the same constraint naming convention as the schema so SQL
        # render output matches what create_all would produce.
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live SQLite DB."""
    url = _resolve_database_url()
    connectable = create_engine(url, poolclass=pool.NullPool, future=True)

    # Enable FK enforcement at the DBAPI level so any FK target mismatch fails
    # fast during migration (acceptance criterion: "Migration fails fast with
    # a clear error if FK targets don't exist when applied"). We do this via a
    # connect event so the PRAGMA fires on a fresh DBAPI connection without
    # polluting SQLAlchemy's transactional state — issuing PRAGMAs through the
    # ORM connection autobegins a transaction and breaks alembic's own
    # commit-after-stamp dance for SQLite.
    @event.listens_for(connectable, "connect")
    def _enable_fk(dbapi_conn: object, _record: object) -> None:
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        try:
            cur.execute("PRAGMA foreign_keys = ON;")
        finally:
            cur.close()

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
