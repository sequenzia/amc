"""Sender-allowlist loader and SIGHUP-driven reloader (spec §5.7, §7.3.3).

The allowlist is a TOML file (default ``~/.config/messaging-agent/allowlist.toml``,
override via ``$AMG_ALLOWLIST_PATH``) that declares which iMessage handles and
Discord user IDs are trusted, plus optional cross-platform identity grouping.

Public surface:

- :class:`AllowlistEntry` — frozen dataclass mirroring one ``[[entry]]`` block.
- :class:`AllowlistError` — refuse-to-start exception (``ValueError`` subclass).
- :class:`AllowlistLoader` — owns the in-memory map keyed by ``(source, id)``;
  exposes :meth:`load`, :meth:`reload`, and :meth:`resolve`.
- :func:`load_allowlist` / :func:`get_allowlist` / :func:`reset_allowlist` —
  module-level config cache mirroring the pattern used in
  :mod:`amg.core.auth`.
- :func:`rebuild_identity_links` — async helper that deletes all
  ``identity_links`` rows and re-materializes one row per
  ``(person_id, source, sender_id)`` grouping (spec §7.3.3 identity_links
  notes). Also upserts the corresponding ``senders`` rows so the FK target
  exists and ``allowlist_status='allowed'`` is the source of truth.
- :func:`setup_sighup_reload` — installs a ``SIGHUP`` handler that reloads
  the file in-place and dispatches an optional sync callback. Returns a
  teardown thunk that restores the previous handler.

Design notes:
- Resolution happens once per inbound message at INSERT time (spec §5.7);
  the loader's job is only to keep the in-memory map fresh and to maintain
  the ``identity_links`` materialization. In-flight messages capture the
  map version at message time by passing the resolved entry to the INSERT
  path — this module does not re-resolve already-stored messages.
- The signal-handler callback runs synchronously in the signal context; it
  only mutates the in-memory map. DB rebuilds on SIGHUP must be scheduled
  by the adapter on its event loop (e.g., via
  ``loop.add_signal_handler(SIGHUP, ...)``); this module does not assume
  an event loop is running because the loader is also useful in CLI tools.
"""

from __future__ import annotations

import os
import signal
import sys
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from amg.core.envelope import Source
from amg.core.schema import identity_links, senders

__all__ = [
    "DEFAULT_ALLOWLIST_PATH",
    "ENV_ALLOWLIST_PATH",
    "AllowlistEntry",
    "AllowlistError",
    "AllowlistLoader",
    "configure_allowlist",
    "get_allowlist",
    "load_allowlist",
    "rebuild_identity_links",
    "reset_allowlist",
    "setup_sighup_reload",
]

ENV_ALLOWLIST_PATH: Final[str] = "AMG_ALLOWLIST_PATH"
DEFAULT_ALLOWLIST_PATH: Final[Path] = Path("~/.config/messaging-agent/allowlist.toml").expanduser()

_VALID_SOURCES: Final[frozenset[str]] = frozenset(s.value for s in Source)


class AllowlistError(ValueError):
    """Raised when the allowlist file cannot be loaded.

    Subclasses :class:`ValueError` so call sites that already handle config
    errors (``ConfigError``-style) catch it. The adapter startup path treats
    this as fatal and prints the message to stderr before exiting.
    """


@dataclass(frozen=True, slots=True)
class AllowlistEntry:
    """One ``[[entry]]`` block from ``allowlist.toml``.

    Attributes:
        source: Originating platform (``imessage`` or ``discord``).
        id: Platform-native sender identifier (E.164 phone, email, or
            Discord user-id string). Treated as opaque by this module.
        display_name: Optional override for the platform-supplied display
            name; ``None`` means "use whatever the platform reports".
        person_id: Optional cross-platform identity tag. Multiple entries
            sharing the same ``person_id`` materialize one
            ``identity_links`` row each.
    """

    source: Source
    id: str
    display_name: str | None = None
    person_id: str | None = None


class AllowlistLoader:
    """Owns the in-memory ``(source, id) -> AllowlistEntry`` map.

    Construct directly only in tests. Production callers should use
    :func:`load_allowlist`, which sets up the module-level singleton that
    :func:`get_allowlist` returns.

    Thread/signal safety: the map is replaced atomically on :meth:`reload`
    by swapping the dict reference, so a concurrent :meth:`resolve` call
    sees either the old map or the new one but never a partial mutation.
    """

    __slots__ = ("path", "_entries")

    def __init__(self, path: Path | str) -> None:
        self.path: Path = Path(path).expanduser()
        self._entries: dict[tuple[Source, str], AllowlistEntry] = {}

    def load(self) -> dict[tuple[Source, str], AllowlistEntry]:
        """Parse ``self.path`` and replace the in-memory map.

        Returns the new map. Raises :class:`AllowlistError` (refuse-to-start)
        on missing file, malformed TOML, unknown ``source`` values, missing
        required fields, or duplicate ``(source, id)`` pairs.
        """
        new_map = _parse_file(self.path)
        self._entries = new_map
        return new_map

    def reload(self) -> dict[tuple[Source, str], AllowlistEntry]:
        """Re-read the file and atomically swap the in-memory map.

        Same semantics and error contract as :meth:`load`. Used by the
        SIGHUP handler.
        """
        return self.load()

    def resolve(self, source: Source | str, id: str) -> AllowlistEntry | None:
        """Return the entry for ``(source, id)`` or ``None`` if not allowlisted."""
        key_source = Source(source) if not isinstance(source, Source) else source
        return self._entries.get((key_source, id))

    def entries(self) -> Iterable[AllowlistEntry]:
        """Iterate over all entries currently loaded (snapshot order)."""
        return tuple(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, tuple) or len(key) != 2:
            return False
        source, id_ = key
        try:
            key_source = Source(source) if not isinstance(source, Source) else source
        except ValueError:
            return False
        return (key_source, id_) in self._entries


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


def _parse_file(path: Path) -> dict[tuple[Source, str], AllowlistEntry]:
    """Read and validate the allowlist TOML file at ``path``.

    The file MUST contain a top-level ``entry`` array (``[[entry]]`` blocks).
    Per-entry shape: ``source`` (required, ``imessage``|``discord``),
    ``id`` (required, non-empty string), ``display_name`` (optional string),
    ``person_id`` (optional string).
    """
    if not path.exists():
        raise AllowlistError(
            f"Allowlist file not found at {path}. Create it (see "
            f"specs/agent-messaging-gateway-SPEC.md §5.7 for the schema) "
            f"or set ${ENV_ALLOWLIST_PATH} to point at a different location."
        )

    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise AllowlistError(f"Allowlist file at {path} is not valid TOML: {exc}") from exc

    raw_entries = data.get("entry", [])
    if not isinstance(raw_entries, list):
        raise AllowlistError(
            f"Allowlist file at {path}: top-level 'entry' must be an array of tables "
            f"(use [[entry]] blocks); got {type(raw_entries).__name__}."
        )

    parsed: dict[tuple[Source, str], AllowlistEntry] = {}
    for index, raw in enumerate(raw_entries):
        entry = _parse_entry(raw, path=path, index=index)
        key = (entry.source, entry.id)
        if key in parsed:
            existing = parsed[key]
            raise AllowlistError(
                f"Allowlist file at {path}: duplicate entry for "
                f"(source={entry.source.value!r}, id={entry.id!r}). "
                f"First seen as {existing!r}; refused to load."
            )
        parsed[key] = entry

    return parsed


def _parse_entry(raw: object, *, path: Path, index: int) -> AllowlistEntry:
    if not isinstance(raw, dict):
        raise AllowlistError(
            f"Allowlist file at {path}: entry #{index} must be a table; got {type(raw).__name__}."
        )

    source_raw = raw.get("source")
    if not isinstance(source_raw, str) or not source_raw:
        raise AllowlistError(
            f"Allowlist file at {path}: entry #{index} is missing a non-empty 'source' field."
        )
    if source_raw not in _VALID_SOURCES:
        raise AllowlistError(
            f"Allowlist file at {path}: entry #{index} has unknown source {source_raw!r}; "
            f"valid sources are {sorted(_VALID_SOURCES)}. Offending entry: {raw!r}."
        )
    source = Source(source_raw)

    id_raw = raw.get("id")
    if not isinstance(id_raw, str) or not id_raw:
        raise AllowlistError(
            f"Allowlist file at {path}: entry #{index} is missing a non-empty 'id' field."
        )

    display_name = raw.get("display_name")
    if display_name is not None and not isinstance(display_name, str):
        raise AllowlistError(
            f"Allowlist file at {path}: entry #{index} 'display_name' must be a string "
            f"if present; got {type(display_name).__name__}."
        )

    person_id = raw.get("person_id")
    if person_id is not None and not isinstance(person_id, str):
        raise AllowlistError(
            f"Allowlist file at {path}: entry #{index} 'person_id' must be a string "
            f"if present; got {type(person_id).__name__}."
        )

    return AllowlistEntry(
        source=source,
        id=id_raw,
        display_name=display_name,
        person_id=person_id,
    )


# ---------------------------------------------------------------------------
# Module-level config cache (parallels amg.core.auth)
# ---------------------------------------------------------------------------

_configured_loader: AllowlistLoader | None = None


def load_allowlist(path: Path | str | None = None) -> AllowlistLoader:
    """Load the allowlist from disk and cache the loader.

    Resolution order for the path:

    1. ``path`` argument (used by tests).
    2. ``$AMG_ALLOWLIST_PATH`` environment variable.
    3. :data:`DEFAULT_ALLOWLIST_PATH`.

    Side effect: caches the loader for :func:`get_allowlist`. Calling this
    a second time replaces the cache (and reloads the file).
    """
    if path is not None:
        resolved = Path(path).expanduser()
    else:
        env_value = os.environ.get(ENV_ALLOWLIST_PATH, "").strip()
        resolved = Path(env_value).expanduser() if env_value else DEFAULT_ALLOWLIST_PATH

    loader = AllowlistLoader(resolved)
    loader.load()
    configure_allowlist(loader)
    return loader


def configure_allowlist(loader: AllowlistLoader) -> None:
    """Install ``loader`` as the module-level singleton (test helper)."""
    global _configured_loader
    _configured_loader = loader


def get_allowlist() -> AllowlistLoader:
    """Return the cached loader; raise if :func:`load_allowlist` has not run."""
    if _configured_loader is None:
        raise RuntimeError(
            "Allowlist has not been loaded. Call load_allowlist() at startup "
            "or configure_allowlist() in tests."
        )
    return _configured_loader


def reset_allowlist() -> None:
    """Clear the cached loader. Intended for test isolation."""
    global _configured_loader
    _configured_loader = None


# ---------------------------------------------------------------------------
# identity_links + senders rebuild
# ---------------------------------------------------------------------------


async def rebuild_identity_links(
    session: AsyncSession,
    entries: Iterable[AllowlistEntry],
) -> int:
    """Rebuild ``identity_links`` from ``entries``; return the row count inserted.

    Behavior (spec §5.7 + §7.3.3 identity_links notes):

    1. Upsert ``senders`` rows for every entry so the composite FK target
       ``(source, sender_id)`` exists. The ``allowlist_status`` is set to
       ``'allowed'`` and ``person_id`` is updated to the entry's value.
       ``display_name`` is populated from the entry if provided, else from
       a stable fallback (the entry's ``id``) — the connector overwrites
       this on first inbound message if the entry has no override.
    2. Delete every existing ``identity_links`` row.
    3. Insert one ``identity_links`` row per entry that has a ``person_id``.

    Entries without ``person_id`` are still upserted as ``senders`` (so they
    are recognized as allowlisted at INSERT time) but produce no
    ``identity_links`` row.

    The caller is responsible for the surrounding transaction
    (``async with session.begin(): ...``) — this function flushes but does
    not commit, so failures roll back cleanly.
    """
    entries_list = list(entries)

    # Step 1: upsert senders. SQLite supports INSERT ... ON CONFLICT DO
    # UPDATE since 3.24, but SQLAlchemy's portable Core path is to try
    # update-then-insert via two passes. We keep it simple: for each entry,
    # try update first; if zero rows affected, insert.
    for entry in entries_list:
        result = await session.execute(
            update(senders)
            .where(
                senders.c.source == entry.source.value,
                senders.c.sender_id == entry.id,
            )
            .values(
                allowlist_status="allowed",
                display_name=entry.display_name or entry.id,
                person_id=entry.person_id,
            )
        )
        if result.rowcount == 0:
            await session.execute(
                insert(senders).values(
                    source=entry.source.value,
                    sender_id=entry.id,
                    display_name=entry.display_name or entry.id,
                    allowlist_status="allowed",
                    person_id=entry.person_id,
                )
            )

    # Step 2: wipe identity_links so we can re-materialize from the
    # current allowlist version. The spec says "rebuild" not "merge".
    await session.execute(delete(identity_links))

    # Step 3: one row per (person_id, source, sender_id) grouping. Entries
    # without a person_id contribute nothing here.
    inserted = 0
    for entry in entries_list:
        if entry.person_id is None:
            continue
        await session.execute(
            insert(identity_links).values(
                person_id=entry.person_id,
                source=entry.source.value,
                sender_id=entry.id,
            )
        )
        inserted += 1

    await session.flush()
    return inserted


async def count_identity_links(session: AsyncSession) -> int:
    """Convenience: ``SELECT count(*) FROM identity_links`` (test helper)."""
    result = await session.execute(select(identity_links))
    return len(result.all())


# ---------------------------------------------------------------------------
# SIGHUP wiring
# ---------------------------------------------------------------------------


def setup_sighup_reload(
    loader: AllowlistLoader,
    *,
    on_reload: Callable[[AllowlistLoader], None] | None = None,
    on_error: Callable[[AllowlistError], None] | None = None,
) -> Callable[[], None]:
    """Install a ``SIGHUP`` handler that calls ``loader.reload()``.

    Returns a teardown thunk that restores the previously-installed handler.
    On platforms without ``SIGHUP`` (Windows), this is a no-op and the
    teardown is a no-op too.

    The signal handler runs in signal context (async-signal-unsafe operations
    must be avoided), so we only:

    1. Re-parse the file (sync, no allocation beyond a dict).
    2. Atomically replace the loader's in-memory map.
    3. Optionally invoke ``on_reload(loader)`` for sync side effects.

    DB rebuilds and other async work must be scheduled by the caller via
    ``asyncio.get_event_loop().add_signal_handler`` instead. This API exists
    for the simple file-only reload path and for test wiring.

    If reload raises :class:`AllowlistError`, the previous in-memory map is
    retained (the loader's :meth:`load` only swaps the map after a successful
    parse). The ``on_error`` callback is invoked with the exception so the
    adapter can log it; without a callback the error is written to stderr.
    """
    if not hasattr(signal, "SIGHUP"):
        return lambda: None

    sighup = signal.SIGHUP  # type: ignore[attr-defined]
    previous = signal.getsignal(sighup)

    def _handler(_signum: int, _frame: object) -> None:
        try:
            loader.reload()
        except AllowlistError as exc:
            if on_error is not None:
                on_error(exc)
            else:
                print(f"allowlist reload failed: {exc}", file=sys.stderr)
            return
        if on_reload is not None:
            on_reload(loader)

    signal.signal(sighup, _handler)

    def _teardown() -> None:
        # ``getsignal`` may have returned the default handler enum or a
        # callable. ``signal.signal`` accepts both.
        signal.signal(sighup, previous)  # type: ignore[arg-type]

    return _teardown
