"""Tests for ``amc.core.attachments`` — local re-host store (spec §5.8).

Coverage map (matches task #49 acceptance):

* Functional
    - File copied to ``<store>/<id>`` with original bytes intact.
    - ``AttachmentRow`` populated with ``id``, ``mime``, ``size_bytes``,
      ``bytes_path``, ``original_url_or_path``.
    - URL form is ``f"{bind_url}/attachments/{id}"`` where the connector
      will pull ``bind_url`` from config.
    - Caller-supplied mime wins; missing mime → :func:`mimetypes.guess_type`;
      unrecognized extension → :data:`FALLBACK_MIME`.
    - Env var ``AMC_ATTACHMENT_DIR`` overrides the default.

* Edge cases / error handling (per spec §5.8 edge-case rows)
    - Source missing → log WARN; ``rehost`` returns ``None``; no file
      created in the store dir.
    - Disk full (simulated as ``OSError(ENOSPC)``) → log WARN;
      ``rehost`` returns ``None``; partial file (if any) cleaned up.
    - Permission denied on store dir at construction → refuse-to-start
      with :class:`AttachmentStoreError` carrying a clear message.
    - Store dir auto-created on construction.

* ULID / id shape
    - Minted ids match the ``att_<26 Crockford base32>`` pattern shared
      with :mod:`amc.core.envelope`.
"""

from __future__ import annotations

import errno
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog

from amc.core.attachments import (
    DEFAULT_BIND_URL,
    ENV_ATTACHMENT_DIR,
    FALLBACK_MIME,
    AttachmentRow,
    AttachmentStore,
    AttachmentStoreError,
)
from amc.core.envelope import ATT_ID_PATTERN


@pytest.fixture
def structlog_to_stdlib() -> Iterator[None]:
    """Bridge structlog into stdlib so ``caplog`` captures structured records."""
    structlog.configure(
        processors=[structlog.stdlib.add_log_level, structlog.stdlib.render_to_log_kwargs],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    try:
        yield
    finally:
        structlog.reset_defaults()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    """A fresh store directory inside the test's tmp_path."""
    return tmp_path / "attachments"


@pytest.fixture
def store(store_dir: Path) -> AttachmentStore:
    """An :class:`AttachmentStore` rooted at ``store_dir``."""
    return AttachmentStore(store_dir)


@pytest.fixture
def png_bytes() -> bytes:
    """Tiny but valid-looking PNG header bytes for size/identity assertions."""
    # 8-byte PNG signature followed by an IHDR chunk header. Not a real PNG
    # but enough to make sure the bytes are non-trivial and round-trip.
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32


@pytest.fixture
def source_file(tmp_path: Path, png_bytes: bytes) -> Path:
    """A real on-disk source file the store can copy from."""
    src = tmp_path / "incoming.png"
    src.write_bytes(png_bytes)
    return src


# ---------------------------------------------------------------------------
# Construction / startup behavior
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_explicit_dir_wins_over_env_and_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Explicit ``store_dir=`` argument beats the env var."""
        env_dir = tmp_path / "from-env"
        explicit_dir = tmp_path / "explicit"
        monkeypatch.setenv(ENV_ATTACHMENT_DIR, str(env_dir))

        store = AttachmentStore(explicit_dir)

        assert store.store_dir == explicit_dir
        assert explicit_dir.is_dir()
        # The env-named dir was never touched.
        assert not env_dir.exists()

    def test_falls_back_to_env_when_no_arg(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        env_dir = tmp_path / "from-env"
        monkeypatch.setenv(ENV_ATTACHMENT_DIR, str(env_dir))

        store = AttachmentStore()

        assert store.store_dir == env_dir
        assert env_dir.is_dir()

    def test_creates_dir_on_startup_including_parents(
        self,
        tmp_path: Path,
    ) -> None:
        nested = tmp_path / "a" / "b" / "c" / "attachments"
        assert not nested.exists()

        AttachmentStore(nested)

        assert nested.is_dir()

    def test_existing_dir_is_accepted(self, tmp_path: Path) -> None:
        existing = tmp_path / "already-here"
        existing.mkdir()

        store = AttachmentStore(existing)

        assert store.store_dir == existing

    def test_expanduser_on_tilde_path(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``~/sub/path`` is expanded against ``$HOME``."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # On macOS the os.environ HOME is what Path.expanduser uses.
        store = AttachmentStore("~/store")

        assert store.store_dir == tmp_path / "store"

    def test_permission_denied_on_mkdir_refuses_to_start(
        self,
        tmp_path: Path,
    ) -> None:
        """Constructor raises :class:`AttachmentStoreError` with a clear msg."""

        # Make the parent unwritable so mkdir fails with PermissionError.
        # We instead patch Path.mkdir directly so the test does not depend
        # on the test runner's umask / filesystem capabilities.
        target = tmp_path / "blocked" / "attachments"

        with (
            patch.object(
                Path,
                "mkdir",
                side_effect=PermissionError("not allowed"),
            ),
            pytest.raises(AttachmentStoreError) as exc_info,
        ):
            AttachmentStore(target)

        msg = str(exc_info.value)
        # Clear error: names the path, names the env var, mentions perms.
        assert str(target) in msg
        assert ENV_ATTACHMENT_DIR in msg
        assert "permission denied" in msg.lower()

    def test_existing_dir_without_write_access_refuses_to_start(
        self,
        tmp_path: Path,
    ) -> None:
        """Pre-existing dir that the process cannot write to fails fast."""
        existing = tmp_path / "readonly"
        existing.mkdir()

        # Simulate an unwritable directory without messing with real perms
        # (which can vary across CI environments). os.access is what the
        # implementation uses to decide.
        def _no_write(path: object, mode: int) -> bool:
            return False

        with (
            patch("amc.core.attachments.os.access", side_effect=_no_write),
            pytest.raises(AttachmentStoreError) as exc_info,
        ):
            AttachmentStore(existing)

        msg = str(exc_info.value)
        assert str(existing) in msg
        assert ENV_ATTACHMENT_DIR in msg
        assert "writable" in msg.lower()

    def test_oserror_on_mkdir_raises_attachment_store_error(
        self,
        tmp_path: Path,
    ) -> None:
        """Non-permission OSError on mkdir still surfaces as the typed error."""
        target = tmp_path / "bad" / "attachments"

        with (
            patch.object(
                Path,
                "mkdir",
                side_effect=OSError("disk on fire"),
            ),
            pytest.raises(AttachmentStoreError) as exc_info,
        ):
            AttachmentStore(target)

        assert str(target) in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bind URL / URL formation
# ---------------------------------------------------------------------------


class TestBindUrl:
    def test_default_bind_url_matches_spec(self, store: AttachmentStore) -> None:
        assert store.bind_url == "http://127.0.0.1:8080"
        # And matches the constant exposed for connectors.
        assert store.bind_url == DEFAULT_BIND_URL

    def test_url_for_appends_attachments_path(self, store: AttachmentStore) -> None:
        url = store.url_for("att_01HABCDEFGHJKMNPQRSTVWXYZ0")
        assert url == "http://127.0.0.1:8080/attachments/att_01HABCDEFGHJKMNPQRSTVWXYZ0"

    def test_trailing_slash_on_bind_url_is_stripped(self, store_dir: Path) -> None:
        """Two slashes between host and ``/attachments/`` would break clients."""
        store = AttachmentStore(store_dir, bind_url="http://localhost:9999/")

        assert store.bind_url == "http://localhost:9999"
        assert store.url_for("att_X") == "http://localhost:9999/attachments/att_X"

    def test_custom_bind_url_propagates(self, store_dir: Path) -> None:
        store = AttachmentStore(store_dir, bind_url="https://amc.example.com")

        assert store.url_for("att_id123") == "https://amc.example.com/attachments/att_id123"


# ---------------------------------------------------------------------------
# rehost: success path
# ---------------------------------------------------------------------------


class TestRehostSuccess:
    async def test_copies_file_bytes_intact(
        self,
        store: AttachmentStore,
        source_file: Path,
        png_bytes: bytes,
    ) -> None:
        row = await store.rehost(str(source_file), mime="image/png")

        assert row is not None
        copied = Path(row.bytes_path).read_bytes()
        assert copied == png_bytes

    async def test_returns_attachment_row_with_all_fields(
        self,
        store: AttachmentStore,
        source_file: Path,
        png_bytes: bytes,
    ) -> None:
        row = await store.rehost(str(source_file), mime="image/png")

        assert row is not None
        assert isinstance(row, AttachmentRow)
        assert row.id.startswith("att_")
        assert ATT_ID_PATTERN.match(row.id) is not None
        assert row.mime == "image/png"
        assert row.size_bytes == len(png_bytes)
        # bytes_path is the on-disk location; lives inside the store dir.
        assert Path(row.bytes_path).parent == store.store_dir
        assert Path(row.bytes_path).name == row.id
        assert row.original_url_or_path == str(source_file)

    async def test_destination_filename_equals_attachment_id(
        self,
        store: AttachmentStore,
        source_file: Path,
    ) -> None:
        row = await store.rehost(str(source_file), mime=None)

        assert row is not None
        # Spec §5.8 acceptance: copy lives at <store>/<id>.
        assert Path(row.bytes_path) == store.store_dir / row.id

    async def test_url_for_row_uses_attachments_id_form(
        self,
        store: AttachmentStore,
        source_file: Path,
    ) -> None:
        row = await store.rehost(str(source_file), mime=None)

        assert row is not None
        url = store.url_for(row.id)
        assert url == f"http://127.0.0.1:8080/attachments/{row.id}"
        # The acceptance criterion focuses on the path component shape:
        assert url.endswith(f"/attachments/{row.id}")

    async def test_each_call_generates_unique_id_and_destination(
        self,
        store: AttachmentStore,
        source_file: Path,
    ) -> None:
        a = await store.rehost(str(source_file), mime=None)
        b = await store.rehost(str(source_file), mime=None)

        assert a is not None and b is not None
        assert a.id != b.id
        assert a.bytes_path != b.bytes_path
        # Both files exist independently.
        assert Path(a.bytes_path).exists()
        assert Path(b.bytes_path).exists()

    async def test_accepts_pathlike_source(
        self,
        store: AttachmentStore,
        source_file: Path,
        png_bytes: bytes,
    ) -> None:
        """``rehost`` accepts a ``Path`` directly, not just ``str``."""
        row = await store.rehost(source_file, mime="image/png")

        assert row is not None
        assert row.original_url_or_path == str(source_file)
        assert Path(row.bytes_path).read_bytes() == png_bytes


# ---------------------------------------------------------------------------
# rehost: MIME resolution
# ---------------------------------------------------------------------------


class TestMimeResolution:
    async def test_caller_supplied_mime_wins(
        self,
        store: AttachmentStore,
        source_file: Path,
    ) -> None:
        # Source extension says image/png; caller asserts something else.
        row = await store.rehost(str(source_file), mime="application/x-custom")

        assert row is not None
        assert row.mime == "application/x-custom"

    async def test_mime_guessed_from_extension_when_none(
        self,
        store: AttachmentStore,
        source_file: Path,
    ) -> None:
        row = await store.rehost(str(source_file), mime=None)

        assert row is not None
        assert row.mime == "image/png"

    async def test_unknown_extension_falls_back_to_octet_stream(
        self,
        store: AttachmentStore,
        tmp_path: Path,
    ) -> None:
        oddball = tmp_path / "no-extension-here"
        oddball.write_bytes(b"some bytes")

        row = await store.rehost(str(oddball), mime=None)

        assert row is not None
        assert row.mime == FALLBACK_MIME

    async def test_empty_string_mime_falls_back_to_guess(
        self,
        store: AttachmentStore,
        source_file: Path,
    ) -> None:
        """Empty string from the connector is treated as 'no mime supplied'."""
        row = await store.rehost(str(source_file), mime="")

        assert row is not None
        # .png → image/png from mimetypes.
        assert row.mime == "image/png"


# ---------------------------------------------------------------------------
# rehost: error paths
# ---------------------------------------------------------------------------


class TestRehostErrors:
    async def test_missing_source_returns_none_and_logs_warning(
        self,
        store: AttachmentStore,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        structlog_to_stdlib: None,
    ) -> None:
        missing = tmp_path / "does-not-exist.png"

        with caplog.at_level(logging.WARNING, logger="amc.attachments"):
            row = await store.rehost(str(missing), mime="image/png")

        assert row is None
        # No row created → no file under the store dir.
        assert list(store.store_dir.iterdir()) == []
        # WARN logged with the source path so the operator can find it.
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records, "expected at least one WARNING log record"
        joined = " ".join(r.getMessage() for r in warning_records)
        assert "attachment_source_missing" in joined or str(missing) in joined

    async def test_disk_full_returns_none_and_cleans_partial(
        self,
        store: AttachmentStore,
        source_file: Path,
        caplog: pytest.LogCaptureFixture,
        structlog_to_stdlib: None,
    ) -> None:
        """ENOSPC during copy → WARN, no row, partial file removed."""

        # Fake the copy step to raise ENOSPC as if mid-write.
        def _enospc(*_args: object, **_kwargs: object) -> int:
            err = OSError(errno.ENOSPC, "No space left on device")
            err.errno = errno.ENOSPC
            raise err

        with (
            patch("amc.core.attachments._copy_file", side_effect=_enospc),
            caplog.at_level(logging.WARNING, logger="amc.attachments"),
        ):
            row = await store.rehost(str(source_file), mime="image/png")

        assert row is None
        # No partial bytes lingering (the copy stub never created one,
        # but cleanup must also tolerate that case).
        assert list(store.store_dir.iterdir()) == []
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records
        joined = " ".join(r.getMessage() for r in warning_records)
        assert "attachment_copy_failed" in joined or "ENOSPC" in joined

    async def test_disk_full_partial_file_is_unlinked(
        self,
        store: AttachmentStore,
        source_file: Path,
    ) -> None:
        """If the failed copy left a partial file, cleanup removes it."""

        # Stub that writes a partial file then raises — exactly the pattern
        # shutil.copyfile would produce on a mid-stream ENOSPC.
        def _partial_then_fail(source: str, dest: Path) -> int:
            dest.write_bytes(b"partial bytes")
            err = OSError(errno.ENOSPC, "No space left on device")
            err.errno = errno.ENOSPC
            raise err

        with patch("amc.core.attachments._copy_file", side_effect=_partial_then_fail):
            row = await store.rehost(str(source_file), mime="image/png")

        assert row is None
        # Cleanup ran — the partial file is gone.
        assert list(store.store_dir.iterdir()) == []

    async def test_permission_denied_on_source_returns_none(
        self,
        store: AttachmentStore,
        source_file: Path,
        caplog: pytest.LogCaptureFixture,
        structlog_to_stdlib: None,
    ) -> None:
        with (
            patch(
                "amc.core.attachments._copy_file",
                side_effect=PermissionError("can't read source"),
            ),
            caplog.at_level(logging.WARNING, logger="amc.attachments"),
        ):
            row = await store.rehost(str(source_file), mime="image/png")

        assert row is None
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records
        joined = " ".join(r.getMessage() for r in warning_records)
        assert "attachment_source_unreadable" in joined or "permission" in joined.lower()

    async def test_subsequent_rehost_after_failure_still_works(
        self,
        store: AttachmentStore,
        source_file: Path,
        tmp_path: Path,
    ) -> None:
        """A failed rehost does not put the store into a bad state."""
        missing = tmp_path / "ghost.png"

        first = await store.rehost(str(missing), mime=None)
        second = await store.rehost(str(source_file), mime="image/png")

        assert first is None
        assert second is not None
        assert Path(second.bytes_path).exists()


# ---------------------------------------------------------------------------
# Concurrency: rehost calls do not block the event loop / share state
# ---------------------------------------------------------------------------


class TestConcurrency:
    async def test_parallel_rehost_calls_each_get_unique_destination(
        self,
        store: AttachmentStore,
        source_file: Path,
    ) -> None:
        """Two concurrent rehost calls produce two distinct ids and files."""
        import asyncio

        results = await asyncio.gather(
            store.rehost(str(source_file), mime="image/png"),
            store.rehost(str(source_file), mime="image/png"),
            store.rehost(str(source_file), mime="image/png"),
        )

        assert all(r is not None for r in results)
        ids = {r.id for r in results if r is not None}
        paths = {r.bytes_path for r in results if r is not None}
        assert len(ids) == 3
        assert len(paths) == 3
        # All three files exist on disk.
        for r in results:
            assert r is not None
            assert Path(r.bytes_path).exists()


# ---------------------------------------------------------------------------
# AttachmentRow shape
# ---------------------------------------------------------------------------


class TestAttachmentRow:
    def test_is_frozen(self) -> None:
        """Row is immutable so connectors cannot mutate it after creation."""
        row = AttachmentRow(
            id="att_01HABCDEFGHJKMNPQRSTVWXYZ0",
            mime="image/png",
            size_bytes=100,
            bytes_path="/tmp/att_x",
            original_url_or_path="/source/file.png",
        )

        with pytest.raises((AttributeError, TypeError)):
            row.size_bytes = 200  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        """Row uses ``__slots__`` (no ``__dict__``) for memory efficiency."""
        row = AttachmentRow(
            id="att_01HABCDEFGHJKMNPQRSTVWXYZ0",
            mime="image/png",
            size_bytes=100,
            bytes_path="/tmp/att_x",
            original_url_or_path="/source/file.png",
        )

        with pytest.raises(AttributeError):
            row.__dict__  # noqa: B018 — intentional attribute access for assert
