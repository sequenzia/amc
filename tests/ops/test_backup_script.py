"""Tests for ops/scripts/backup.sh — nightly SQLite backup with 7-day retention.

The shell script is exercised end-to-end via subprocess against a tmp_path
SQLite database. We assert:

- A date-stamped backup file is produced in the expected location.
- The backup file is internally consistent (`PRAGMA integrity_check`) and
  contains the same row data as the source — verifying WAL-safe semantics
  of the SQLite Online Backup API.
- A second invocation overwrites the same-day backup (idempotency).
- Files older than the 7-day retention window are deleted; files inside the
  window are preserved.
- Files that do NOT match the `amc-YYYY-MM-DD.db` naming pattern are left
  untouched.
- A missing source DB causes the script to log ERROR and exit non-zero.
- A backup-dir create failure (target path exists as a regular file) is
  surfaced via non-zero exit + ERROR log.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKUP_SCRIPT = REPO_ROOT / "ops" / "scripts" / "backup.sh"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_db(path: Path) -> None:
    """Create a small SQLite DB with one table and a couple of rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO messages (id, body) VALUES (?, ?)",
            [(1, "hello"), (2, "world")],
        )
        conn.commit()
    finally:
        conn.close()


def _run_backup(
    *,
    db_path: Path,
    log_dir: Path,
    backup_dir: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke ops/scripts/backup.sh with the given environment."""
    env = os.environ.copy()
    env["AMC_DB_PATH"] = str(db_path)
    env["AMC_LOG_DIR"] = str(log_dir)
    if backup_dir is not None:
        env["AMC_BACKUP_DIR"] = str(backup_dir)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["/bin/bash", str(BACKUP_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _today_stamp() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Sanity
# ---------------------------------------------------------------------------


def test_backup_script_exists_and_is_executable() -> None:
    assert BACKUP_SCRIPT.is_file(), f"missing backup script at {BACKUP_SCRIPT}"
    assert os.access(BACKUP_SCRIPT, os.X_OK), "backup.sh must be executable"


def test_sqlite3_cli_available() -> None:
    """The script depends on the `sqlite3` CLI being on PATH."""
    assert shutil.which("sqlite3") is not None, "sqlite3 CLI required for backup"


# ---------------------------------------------------------------------------
# Functional: produces a date-stamped backup
# ---------------------------------------------------------------------------


def test_produces_date_stamped_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "amc.db"
    _seed_db(db_path)
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"

    result = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=backup_dir)

    assert result.returncode == 0, (
        f"backup.sh exited {result.returncode}\nstdout:{result.stdout}\nstderr:{result.stderr}"
    )

    target = backup_dir / f"amc-{_today_stamp()}.db"
    assert target.is_file(), f"expected backup at {target}; backup_dir={list(backup_dir.iterdir())}"
    assert target.stat().st_size > 0, "backup file is empty"

    log_file = log_dir / "backup.log"
    assert log_file.is_file(), "backup.log not created"
    log_text = log_file.read_text()
    assert "INFO" in log_text
    assert "backup succeeded" in log_text


def test_backup_is_internally_consistent_and_has_source_rows(tmp_path: Path) -> None:
    """SQLite Online Backup API guarantees a consistent snapshot (WAL-safe).

    We verify by running `PRAGMA integrity_check` and checking the row data
    survives the round-trip.
    """
    db_path = tmp_path / "amc.db"
    _seed_db(db_path)
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"

    result = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=backup_dir)
    assert result.returncode == 0, result.stderr

    target = backup_dir / f"amc-{_today_stamp()}.db"
    assert target.is_file()

    conn = sqlite3.connect(str(target))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        assert integrity == ("ok",), f"integrity_check failed: {integrity}"

        rows = conn.execute("SELECT id, body FROM messages ORDER BY id").fetchall()
        assert rows == [(1, "hello"), (2, "world")]
    finally:
        conn.close()


def test_wal_mode_source_produces_consistent_backup(tmp_path: Path) -> None:
    """Same as above but with the source DB in WAL mode and an active write
    in the WAL (un-checkpointed). The Online Backup API must still produce a
    consistent snapshot containing the WAL-resident write.
    """
    db_path = tmp_path / "amc.db"
    _seed_db(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("INSERT INTO messages (id, body) VALUES (3, 'in-wal')")
        conn.commit()
        # Intentionally do NOT close + checkpoint: leave the row in the WAL.

        log_dir = tmp_path / "logs"
        backup_dir = tmp_path / "backups"
        result = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=backup_dir)
        assert result.returncode == 0, result.stderr

        target = backup_dir / f"amc-{_today_stamp()}.db"
        assert target.is_file()

        # Open backup in a fresh connection and confirm the WAL-resident row
        # was captured.
        backup_conn = sqlite3.connect(str(target))
        try:
            integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()
            assert integrity == ("ok",)
            ids = [r[0] for r in backup_conn.execute("SELECT id FROM messages ORDER BY id")]
            assert ids == [1, 2, 3]
        finally:
            backup_conn.close()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Idempotency: running twice in a day overwrites
# ---------------------------------------------------------------------------


def test_idempotent_same_day_overwrites(tmp_path: Path) -> None:
    db_path = tmp_path / "amc.db"
    _seed_db(db_path)
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"

    r1 = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=backup_dir)
    assert r1.returncode == 0, r1.stderr
    target = backup_dir / f"amc-{_today_stamp()}.db"
    assert target.is_file()

    # Confirm initial contents (no row 99 yet).
    conn = sqlite3.connect(str(target))
    try:
        ids_before = [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY id")]
    finally:
        conn.close()
    assert 99 not in ids_before

    # Mutate the source so the second backup must differ if it actually ran.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO messages (id, body) VALUES (?, ?)",
            (99, "added-between-backups"),
        )
        conn.commit()
    finally:
        conn.close()

    # Sleep a hair so mtime can advance even on coarse-grained filesystems.
    time.sleep(1.05)

    r2 = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=backup_dir)
    assert r2.returncode == 0, r2.stderr

    # Same path; should still be a single file in backup_dir.
    backups_today = list(backup_dir.glob(f"amc-{_today_stamp()}.db"))
    assert len(backups_today) == 1, f"expected exactly one same-day backup, got {backups_today}"

    # The same-day backup file must reflect the post-mutation source, proving
    # the script overwrote (not skipped) it.
    conn = sqlite3.connect(str(target))
    try:
        ids_after = [r[0] for r in conn.execute("SELECT id FROM messages ORDER BY id")]
    finally:
        conn.close()
    assert 99 in ids_after, f"second backup did not overwrite same-day file; ids={ids_after}"


# ---------------------------------------------------------------------------
# 7-day retention
# ---------------------------------------------------------------------------


def test_retention_deletes_files_older_than_7_days(tmp_path: Path) -> None:
    db_path = tmp_path / "amc.db"
    _seed_db(db_path)
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)

    now = time.time()
    day = 86400

    # Pre-populate backups: a "very old" (10 days), a "boundary" (8 days),
    # a "fresh" (3 days), and a non-matching file.
    very_old = backup_dir / "amc-2020-01-01.db"
    boundary = backup_dir / "amc-2020-02-02.db"
    fresh = backup_dir / "amc-2020-03-03.db"
    unrelated = backup_dir / "do-not-touch.txt"

    for f in (very_old, boundary, fresh):
        f.write_bytes(b"\x00" * 16)
    unrelated.write_text("operator notes")

    os.utime(very_old, (now - 10 * day, now - 10 * day))
    os.utime(boundary, (now - 8 * day, now - 8 * day))
    os.utime(fresh, (now - 3 * day, now - 3 * day))
    # Push the unrelated file's mtime way back so we'd notice if it were
    # incorrectly pruned.
    os.utime(unrelated, (now - 30 * day, now - 30 * day))

    result = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=backup_dir)
    assert result.returncode == 0, result.stderr

    assert not very_old.exists(), "files >7d should be pruned"
    assert not boundary.exists(), "files >7d (8 days old) should be pruned"
    assert fresh.exists(), "files within retention window must be preserved"
    assert unrelated.exists(), "non-matching files must not be touched"

    # Today's backup is also present.
    today = backup_dir / f"amc-{_today_stamp()}.db"
    assert today.is_file()


def test_retention_logs_pruned_count(tmp_path: Path) -> None:
    db_path = tmp_path / "amc.db"
    _seed_db(db_path)
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir(parents=True)

    now = time.time()
    day = 86400
    old = backup_dir / "amc-2020-01-01.db"
    old.write_bytes(b"\x00" * 16)
    os.utime(old, (now - 30 * day, now - 30 * day))

    result = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=backup_dir)
    assert result.returncode == 0

    log_text = (log_dir / "backup.log").read_text()
    assert "pruned old backup" in log_text
    assert "retention sweep complete" in log_text


# ---------------------------------------------------------------------------
# Edge cases: missing source / failure paths
# ---------------------------------------------------------------------------


def test_missing_source_db_logs_error_and_exits_nonzero(tmp_path: Path) -> None:
    db_path = tmp_path / "absent.db"  # never created
    log_dir = tmp_path / "logs"
    backup_dir = tmp_path / "backups"

    result = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=backup_dir)

    assert result.returncode != 0, "missing DB must cause non-zero exit"

    log_file = log_dir / "backup.log"
    assert log_file.is_file()
    log_text = log_file.read_text()
    assert "ERROR" in log_text
    assert "source DB missing" in log_text
    # No backup file should be produced.
    if backup_dir.exists():
        assert not list(backup_dir.glob("amc-*.db"))


def test_backup_dir_creation_failure_logs_error(tmp_path: Path) -> None:
    """If the backup-dir path exists as a regular file, mkdir -p fails;
    the script must surface this clearly.
    """
    db_path = tmp_path / "amc.db"
    _seed_db(db_path)
    log_dir = tmp_path / "logs"
    # Put a regular file where the backup dir should go.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a dir")

    result = _run_backup(db_path=db_path, log_dir=log_dir, backup_dir=blocked)
    assert result.returncode != 0

    log_text = (log_dir / "backup.log").read_text()
    assert "ERROR" in log_text
    assert "failed to create backup dir" in log_text
