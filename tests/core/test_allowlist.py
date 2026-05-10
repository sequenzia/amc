"""Tests for ``amc.core.allowlist`` (spec §5.7 / §7.3.3 identity_links).

Coverage:

Functional
- Loader parses a valid file and returns the expected map.
- Duplicate ``(source, id)`` pairs raise refuse-to-start with both entries
  named.
- Missing file raises refuse-to-start with the configured path printed.
- ``identity_links`` table reflects every ``person_id`` grouping after
  ``rebuild_identity_links``.
- SIGHUP handler reloads the file and the new map is visible immediately.

Edge cases
- Entries without ``person_id`` resolve correctly but produce no
  ``identity_links`` row.
- A ``person_id`` shared by 3+ entries materializes one ``identity_links``
  row per entry.
- Reload preserves resolution of an already-captured snapshot (per-message
  resolution semantics).

Error handling
- Malformed TOML raises refuse-to-start with parse-error excerpt.
- Unknown ``source`` value raises refuse-to-start with offending entry.

Test setup
- ``fresh_db`` mirrors the ``tests/core/test_schema.py`` fixture: tmp_path
  SQLite + alembic ``upgrade head`` so identity_links + senders tables
  exist with the real CHECK/FK constraints.
- We then bind an :class:`AsyncEngine` to that same path via
  :func:`create_engine_from_env` to exercise the production write path.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from amc.core.allowlist import (
    ENV_ALLOWLIST_PATH,
    AllowlistEntry,
    AllowlistError,
    AllowlistLoader,
    configure_allowlist,
    get_allowlist,
    load_allowlist,
    rebuild_identity_links,
    reset_allowlist,
    setup_sighup_reload,
)
from amc.core.db import create_engine_from_env, create_session_factory
from amc.core.envelope import Source
from amc.core.schema import identity_links, senders

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Apply ``alembic upgrade head`` to a fresh tmp_path SQLite file."""
    db_path = tmp_path / "allowlist.db"
    monkeypatch.setenv("AMC_DB_PATH", str(db_path))
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    yield db_path


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Ensure every test starts with a clean module-level cache."""
    reset_allowlist()
    yield
    reset_allowlist()


def _write_allowlist(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


_VALID_BODY = """
[[entry]]
source = "imessage"
id = "+15551234567"
display_name = "Alice"
person_id = "alice"

[[entry]]
source = "discord"
id = "discord:user:99887766554433"
display_name = "Alice (DC)"
person_id = "alice"

[[entry]]
source = "discord"
id = "discord:user:11223344556677"
display_name = "Bob"
"""


# ---------------------------------------------------------------------------
# Functional: parsing + map shape
# ---------------------------------------------------------------------------


def test_load_returns_keyed_map(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    entries = loader.load()

    assert (Source.IMESSAGE, "+15551234567") in entries
    assert (Source.DISCORD, "discord:user:99887766554433") in entries
    assert (Source.DISCORD, "discord:user:11223344556677") in entries
    assert len(entries) == 3


def test_resolve_returns_entry_for_known_pair(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    entry = loader.resolve(Source.IMESSAGE, "+15551234567")
    assert entry is not None
    assert entry.display_name == "Alice"
    assert entry.person_id == "alice"


def test_resolve_accepts_string_source(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    entry = loader.resolve("imessage", "+15551234567")
    assert entry is not None
    assert entry.id == "+15551234567"


def test_resolve_returns_none_for_unknown(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    assert loader.resolve(Source.IMESSAGE, "+19999999999") is None
    assert loader.resolve(Source.DISCORD, "nope") is None


def test_entry_without_person_id_resolves(tmp_path: Path) -> None:
    """Edge case: missing ``person_id`` is allowed; entry resolves normally."""
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    bob = loader.resolve(Source.DISCORD, "discord:user:11223344556677")
    assert bob is not None
    assert bob.person_id is None
    assert bob.display_name == "Bob"


def test_entry_without_display_name_is_optional(tmp_path: Path) -> None:
    body = """
    [[entry]]
    source = "imessage"
    id = "+15550000000"
    """
    path = _write_allowlist(tmp_path / "min.toml", body)
    loader = AllowlistLoader(path)
    loader.load()

    entry = loader.resolve(Source.IMESSAGE, "+15550000000")
    assert entry is not None
    assert entry.display_name is None
    assert entry.person_id is None


def test_empty_file_loads_to_empty_map(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "empty.toml", "")
    loader = AllowlistLoader(path)
    entries = loader.load()
    assert entries == {}


def test_contains_dunder(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    assert (Source.IMESSAGE, "+15551234567") in loader
    assert (Source.IMESSAGE, "+19999999999") not in loader
    assert "not-a-tuple" not in loader


# ---------------------------------------------------------------------------
# Functional: refuse-to-start contracts
# ---------------------------------------------------------------------------


def test_missing_file_refuses_to_start_with_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    loader = AllowlistLoader(missing)

    with pytest.raises(AllowlistError) as excinfo:
        loader.load()

    assert str(missing) in str(excinfo.value)


def test_duplicate_entry_refuses_to_start_with_offending_pair(
    tmp_path: Path,
) -> None:
    body = """
    [[entry]]
    source = "imessage"
    id = "+15550001111"
    display_name = "First"

    [[entry]]
    source = "imessage"
    id = "+15550001111"
    display_name = "Second"
    """
    path = _write_allowlist(tmp_path / "dup.toml", body)
    loader = AllowlistLoader(path)

    with pytest.raises(AllowlistError) as excinfo:
        loader.load()

    msg = str(excinfo.value)
    assert "duplicate" in msg.lower()
    assert "imessage" in msg
    assert "+15550001111" in msg


def test_duplicate_across_platforms_is_allowed(tmp_path: Path) -> None:
    """Same id on different platforms is fine; the key is (source, id)."""
    body = """
    [[entry]]
    source = "imessage"
    id = "shared-id"

    [[entry]]
    source = "discord"
    id = "shared-id"
    """
    path = _write_allowlist(tmp_path / "cross.toml", body)
    loader = AllowlistLoader(path)
    entries = loader.load()
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_malformed_toml_refuses_to_start_with_parse_error(
    tmp_path: Path,
) -> None:
    path = _write_allowlist(tmp_path / "bad.toml", "this is = not [valid toml\n")
    loader = AllowlistLoader(path)

    with pytest.raises(AllowlistError) as excinfo:
        loader.load()

    msg = str(excinfo.value).lower()
    assert "toml" in msg
    assert str(path) in str(excinfo.value)


def test_unknown_source_refuses_to_start(tmp_path: Path) -> None:
    body = """
    [[entry]]
    source = "telegram"
    id = "@bob"
    """
    path = _write_allowlist(tmp_path / "unk.toml", body)
    loader = AllowlistLoader(path)

    with pytest.raises(AllowlistError) as excinfo:
        loader.load()

    msg = str(excinfo.value)
    assert "telegram" in msg
    assert "unknown source" in msg.lower() or "valid sources" in msg.lower()


def test_missing_source_field_refuses_to_start(tmp_path: Path) -> None:
    body = """
    [[entry]]
    id = "+15550000000"
    """
    path = _write_allowlist(tmp_path / "nosrc.toml", body)
    loader = AllowlistLoader(path)

    with pytest.raises(AllowlistError) as excinfo:
        loader.load()
    assert "source" in str(excinfo.value).lower()


def test_missing_id_field_refuses_to_start(tmp_path: Path) -> None:
    body = """
    [[entry]]
    source = "imessage"
    """
    path = _write_allowlist(tmp_path / "noid.toml", body)
    loader = AllowlistLoader(path)

    with pytest.raises(AllowlistError) as excinfo:
        loader.load()
    assert "'id'" in str(excinfo.value)


def test_non_string_display_name_refuses_to_start(tmp_path: Path) -> None:
    body = """
    [[entry]]
    source = "imessage"
    id = "+15550000000"
    display_name = 12345
    """
    path = _write_allowlist(tmp_path / "bad-name.toml", body)
    loader = AllowlistLoader(path)

    with pytest.raises(AllowlistError) as excinfo:
        loader.load()
    assert "display_name" in str(excinfo.value)


def test_top_level_entry_must_be_array(tmp_path: Path) -> None:
    body = """
    [entry]
    source = "imessage"
    id = "+15550000000"
    """
    path = _write_allowlist(tmp_path / "wrong-shape.toml", body)
    loader = AllowlistLoader(path)

    with pytest.raises(AllowlistError) as excinfo:
        loader.load()
    msg = str(excinfo.value).lower()
    assert "entry" in msg


# ---------------------------------------------------------------------------
# Module-level config cache
# ---------------------------------------------------------------------------


def test_load_allowlist_uses_argument_path(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "explicit.toml", _VALID_BODY)
    loader = load_allowlist(path)
    assert loader.path == path
    assert get_allowlist() is loader


def test_load_allowlist_uses_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_allowlist(tmp_path / "env.toml", _VALID_BODY)
    monkeypatch.setenv(ENV_ALLOWLIST_PATH, str(path))

    loader = load_allowlist()
    assert loader.path == path


def test_load_allowlist_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When neither argument nor env is set, the default path is used."""
    monkeypatch.delenv(ENV_ALLOWLIST_PATH, raising=False)
    # Point the module-level default at a guaranteed-missing path so the
    # test is hermetic on dev machines that already have AMC configured
    # (where ~/.config/messaging-agent/allowlist.toml may exist).
    missing = tmp_path / "no-such-allowlist.toml"
    monkeypatch.setattr("amc.core.allowlist.DEFAULT_ALLOWLIST_PATH", missing)
    with pytest.raises(AllowlistError) as excinfo:
        load_allowlist()
    assert str(missing) in str(excinfo.value)


def test_get_allowlist_raises_before_load() -> None:
    reset_allowlist()
    with pytest.raises(RuntimeError, match="not been loaded"):
        get_allowlist()


def test_configure_allowlist_for_tests(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    configure_allowlist(loader)
    assert get_allowlist() is loader


# ---------------------------------------------------------------------------
# identity_links rebuild
# ---------------------------------------------------------------------------


async def _select_identity_link_rows(
    session_factory: object,
) -> list[tuple[str, str, str]]:
    async with session_factory() as session:  # type: ignore[misc]
        result = await session.execute(
            select(identity_links.c.person_id, identity_links.c.source, identity_links.c.sender_id)
        )
        rows = result.all()
    return sorted((r[0], r[1], r[2]) for r in rows)


async def _select_sender_rows(
    session_factory: object,
) -> list[tuple[str, str, str, str | None]]:
    async with session_factory() as session:  # type: ignore[misc]
        result = await session.execute(
            select(
                senders.c.source,
                senders.c.sender_id,
                senders.c.allowlist_status,
                senders.c.person_id,
            )
        )
        rows = result.all()
    return sorted((r[0], r[1], r[2], r[3]) for r in rows)


def test_rebuild_identity_links_materializes_groupings(fresh_db: Path) -> None:
    """Each entry with a ``person_id`` produces exactly one identity_links row."""
    engine = create_engine_from_env()
    session_factory = create_session_factory(engine)

    entries = [
        AllowlistEntry(Source.IMESSAGE, "+15551234567", "Alice", "alice"),
        AllowlistEntry(Source.DISCORD, "discord:user:1", "Alice (DC)", "alice"),
        AllowlistEntry(Source.DISCORD, "discord:user:2", "Bob"),  # no person_id
    ]

    async def _run() -> int:
        async with session_factory() as session, session.begin():
            return await rebuild_identity_links(session, entries)

    inserted = asyncio.run(_run())
    rows = asyncio.run(_select_identity_link_rows(session_factory))
    asyncio.run(engine.dispose())

    assert inserted == 2
    assert rows == sorted(
        [
            ("alice", "imessage", "+15551234567"),
            ("alice", "discord", "discord:user:1"),
        ]
    )


def test_rebuild_identity_links_three_aliases_one_row_each(
    fresh_db: Path,
) -> None:
    """Edge case: a person_id shared by 3+ entries -> 3+ identity_links rows."""
    engine = create_engine_from_env()
    session_factory = create_session_factory(engine)

    entries = [
        AllowlistEntry(Source.IMESSAGE, "+15550000001", "A1", "alice"),
        AllowlistEntry(Source.DISCORD, "discord:user:a", "A2", "alice"),
        AllowlistEntry(Source.DISCORD, "discord:user:b", "A3", "alice"),
        AllowlistEntry(Source.DISCORD, "discord:user:c", "A4", "alice"),
    ]

    async def _run() -> int:
        async with session_factory() as session, session.begin():
            return await rebuild_identity_links(session, entries)

    inserted = asyncio.run(_run())
    rows = asyncio.run(_select_identity_link_rows(session_factory))
    asyncio.run(engine.dispose())

    assert inserted == 4
    assert len(rows) == 4
    assert {r[0] for r in rows} == {"alice"}


def test_rebuild_identity_links_skips_entries_without_person_id(
    fresh_db: Path,
) -> None:
    """Edge case: entries without person_id resolve but produce no identity_links row."""
    engine = create_engine_from_env()
    session_factory = create_session_factory(engine)

    entries = [
        AllowlistEntry(Source.DISCORD, "discord:user:lonely", "Lonely"),
    ]

    async def _run() -> int:
        async with session_factory() as session, session.begin():
            return await rebuild_identity_links(session, entries)

    inserted = asyncio.run(_run())
    rows = asyncio.run(_select_identity_link_rows(session_factory))
    sender_rows = asyncio.run(_select_sender_rows(session_factory))
    asyncio.run(engine.dispose())

    assert inserted == 0
    assert rows == []
    # Sender row still exists so inbound resolution at INSERT time can find it.
    assert sender_rows == [("discord", "discord:user:lonely", "allowed", None)]


def test_rebuild_identity_links_replaces_existing_rows(fresh_db: Path) -> None:
    """Acceptance: subsequent rebuilds wipe and re-materialize."""
    engine = create_engine_from_env()
    session_factory = create_session_factory(engine)

    first = [
        AllowlistEntry(Source.IMESSAGE, "+15550000001", "Old", "oldperson"),
    ]
    second = [
        AllowlistEntry(Source.IMESSAGE, "+15550000002", "New", "newperson"),
        AllowlistEntry(Source.DISCORD, "discord:user:n", "New (DC)", "newperson"),
    ]

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            await rebuild_identity_links(session, first)
        async with session_factory() as session, session.begin():
            await rebuild_identity_links(session, second)

    asyncio.run(_run())
    rows = asyncio.run(_select_identity_link_rows(session_factory))
    asyncio.run(engine.dispose())

    assert rows == sorted(
        [
            ("newperson", "imessage", "+15550000002"),
            ("newperson", "discord", "discord:user:n"),
        ]
    )


def test_rebuild_identity_links_upserts_senders(fresh_db: Path) -> None:
    """The senders.allowlist_status is set to 'allowed' for every entry."""
    engine = create_engine_from_env()
    session_factory = create_session_factory(engine)

    entries = [
        AllowlistEntry(Source.IMESSAGE, "+15550000001", "Alice", "alice"),
        AllowlistEntry(Source.DISCORD, "discord:user:bob", None, None),
    ]

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            await rebuild_identity_links(session, entries)

    asyncio.run(_run())
    sender_rows = asyncio.run(_select_sender_rows(session_factory))
    asyncio.run(engine.dispose())

    assert sender_rows == sorted(
        [
            ("imessage", "+15550000001", "allowed", "alice"),
            ("discord", "discord:user:bob", "allowed", None),
        ]
    )


def test_rebuild_identity_links_updates_existing_sender(fresh_db: Path) -> None:
    """If a senders row exists from prior inbound, allowlist load updates it."""
    engine = create_engine_from_env()
    session_factory = create_session_factory(engine)

    # Pre-seed an unknown sender (as would happen on a quarantined inbound).
    async def _seed() -> None:
        async with session_factory() as session, session.begin():
            await session.execute(
                senders.insert().values(
                    source="imessage",
                    sender_id="+15559999999",
                    display_name="Unknown",
                    allowlist_status="unknown",
                    person_id=None,
                )
            )

    asyncio.run(_seed())

    entries = [
        AllowlistEntry(Source.IMESSAGE, "+15559999999", "Now Known", "kp"),
    ]

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            await rebuild_identity_links(session, entries)

    asyncio.run(_run())
    rows = asyncio.run(_select_sender_rows(session_factory))
    asyncio.run(engine.dispose())

    assert rows == [("imessage", "+15559999999", "allowed", "kp")]


# ---------------------------------------------------------------------------
# SIGHUP reload
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP not supported")
def test_sighup_handler_reloads_file(tmp_path: Path) -> None:
    """Sending SIGHUP triggers loader.reload() and the new map is visible."""
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    # Subsequent file edit: add a new entry.
    new_body = _VALID_BODY + (
        '\n[[entry]]\nsource = "imessage"\nid = "+15558675309"\ndisplay_name = "Carol"\n'
    )
    path.write_text(new_body, encoding="utf-8")

    teardown = setup_sighup_reload(loader)
    try:
        assert loader.resolve(Source.IMESSAGE, "+15558675309") is None
        os.kill(os.getpid(), signal.SIGHUP)  # type: ignore[attr-defined]
        # Signal delivery is synchronous on POSIX in main thread; one trip
        # through the interpreter is enough.
        # Resolve to confirm the new entry is now visible.
        carol = loader.resolve(Source.IMESSAGE, "+15558675309")
        assert carol is not None
        assert carol.display_name == "Carol"
    finally:
        teardown()


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP not supported")
def test_sighup_handler_invokes_on_reload_callback(tmp_path: Path) -> None:
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    calls: list[int] = []

    def _on_reload(_loader: AllowlistLoader) -> None:
        calls.append(len(_loader))

    teardown = setup_sighup_reload(loader, on_reload=_on_reload)
    try:
        os.kill(os.getpid(), signal.SIGHUP)  # type: ignore[attr-defined]
        assert calls == [3]
    finally:
        teardown()


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP not supported")
def test_sighup_failed_reload_preserves_old_map(tmp_path: Path) -> None:
    """If the new file is invalid, the old in-memory map is retained."""
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()
    original_size = len(loader)

    # Break the file.
    path.write_text("garbage = [unterminated\n", encoding="utf-8")

    errors: list[AllowlistError] = []

    def _on_error(exc: AllowlistError) -> None:
        errors.append(exc)

    teardown = setup_sighup_reload(loader, on_error=_on_error)
    try:
        os.kill(os.getpid(), signal.SIGHUP)  # type: ignore[attr-defined]
        # Map unchanged, error captured.
        assert len(loader) == original_size
        assert len(errors) == 1
        assert isinstance(errors[0], AllowlistError)
    finally:
        teardown()


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP not supported")
def test_sighup_teardown_restores_previous_handler(tmp_path: Path) -> None:
    """Teardown must restore whatever handler was installed before us."""
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    sighup = signal.SIGHUP  # type: ignore[attr-defined]
    sentinel_calls: list[int] = []

    def _sentinel(_signum: int, _frame: object) -> None:
        sentinel_calls.append(_signum)

    previous = signal.signal(sighup, _sentinel)
    try:
        teardown = setup_sighup_reload(loader)
        teardown()
        # Now SIGHUP should hit the sentinel again.
        os.kill(os.getpid(), sighup)
        assert sentinel_calls == [int(sighup)]
    finally:
        signal.signal(sighup, previous)


# ---------------------------------------------------------------------------
# Snapshot semantics: per-message capture is preserved across reload
# ---------------------------------------------------------------------------


def test_resolution_snapshot_survives_reload(tmp_path: Path) -> None:
    """Edge case: a captured AllowlistEntry is immutable; reload doesn't mutate it."""
    path = _write_allowlist(tmp_path / "ok.toml", _VALID_BODY)
    loader = AllowlistLoader(path)
    loader.load()

    snapshot = loader.resolve(Source.IMESSAGE, "+15551234567")
    assert snapshot is not None
    assert snapshot.display_name == "Alice"

    # Edit the file: rename Alice and bump person_id.
    new_body = """
    [[entry]]
    source = "imessage"
    id = "+15551234567"
    display_name = "Renamed"
    person_id = "different"
    """
    path.write_text(new_body, encoding="utf-8")
    loader.reload()

    # The previously captured entry is unchanged (frozen dataclass + atomic
    # map swap means the old object is still the same identity).
    assert snapshot.display_name == "Alice"
    assert snapshot.person_id == "alice"

    # Fresh resolution sees the new map.
    fresh = loader.resolve(Source.IMESSAGE, "+15551234567")
    assert fresh is not None
    assert fresh.display_name == "Renamed"
    assert fresh.person_id == "different"


def test_allowlist_entry_is_frozen() -> None:
    import dataclasses

    entry = AllowlistEntry(Source.IMESSAGE, "+1", "X", "p1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.display_name = "Y"  # type: ignore[misc]
