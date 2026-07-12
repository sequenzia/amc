"""Tests for ``amg logs`` (spec §5.7).

Coverage:
* Path resolution: today's file wins; missing today → newest-by-mtime
  glob; nothing matches → ``None`` (Functional + Edge Case).
* ``read_last_n_lines`` returns the trailing N lines and tolerates short
  files (Functional).
* ``--no-follow`` reads-then-exits with code 0 (Functional).
* ``all`` mode prefixes interleaved output with ``[<service>] ``
  (Functional).
* ``--launchd`` mode tails launchd-level stdout/stderr (Functional).
* Tail-follow picks up appended lines (Integration).
* Missing logs directory → exit 1 with clear message (Error Handling).
* No matching file → exit 1 with canonical "No logs found" message
  (Edge Case).
* Unknown service → exit 1 (Error Handling).
"""

from __future__ import annotations

import io
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from amg.cli.app import app
from amg.cli.logs import (
    follow,
    read_last_n_lines,
    resolve_app_log,
    run_logs,
)
from amg.cli.services import REGISTRY

runner = CliRunner()


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _adapter():
    return REGISTRY[0]


def test_resolve_app_log_prefers_todays_file(tmp_path: Path) -> None:
    svc = _adapter()
    today = date(2026, 5, 10)
    today_file = tmp_path / f"{svc.name}-{today.isoformat()}.log"
    older = tmp_path / f"{svc.name}-2026-05-01.log"
    older.write_text("old\n")
    today_file.write_text("new\n")

    chosen = resolve_app_log(svc, log_dir=tmp_path, today=lambda: today)
    assert chosen == today_file


def test_resolve_app_log_falls_back_to_newest_when_today_missing(
    tmp_path: Path,
) -> None:
    svc = _adapter()
    today = date(2026, 5, 10)
    older = tmp_path / f"{svc.name}-2026-05-01.log"
    newer = tmp_path / f"{svc.name}-2026-05-09.log"
    older.write_text("old\n")
    newer.write_text("newer\n")
    # Force mtime ordering so the test isn't filesystem-clock-dependent.
    import os

    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    chosen = resolve_app_log(svc, log_dir=tmp_path, today=lambda: today)
    assert chosen == newer


def test_resolve_app_log_returns_none_when_no_match(tmp_path: Path) -> None:
    svc = _adapter()
    # Drop an unrelated file so the directory isn't empty.
    (tmp_path / "unrelated.log").write_text("nope\n")
    chosen = resolve_app_log(svc, log_dir=tmp_path, today=lambda: date(2026, 5, 10))
    assert chosen is None


# ---------------------------------------------------------------------------
# read_last_n_lines
# ---------------------------------------------------------------------------


def test_read_last_n_lines_short_file_returns_all(tmp_path: Path) -> None:
    f = tmp_path / "x.log"
    f.write_text("a\nb\nc\n")
    assert read_last_n_lines(f, 50) == ["a\n", "b\n", "c\n"]


def test_read_last_n_lines_returns_last_n(tmp_path: Path) -> None:
    f = tmp_path / "x.log"
    f.write_text("".join(f"line{i}\n" for i in range(100)))
    out = read_last_n_lines(f, 5)
    assert out == [f"line{i}\n" for i in range(95, 100)]


def test_read_last_n_lines_zero(tmp_path: Path) -> None:
    f = tmp_path / "x.log"
    f.write_text("a\nb\n")
    assert read_last_n_lines(f, 0) == []


# ---------------------------------------------------------------------------
# run_logs --no-follow behavior
# ---------------------------------------------------------------------------


def test_run_logs_no_follow_dumps_last_n_then_exits(tmp_path: Path) -> None:
    svc = _adapter()
    today = date(2026, 5, 10)
    f = tmp_path / f"{svc.name}-{today.isoformat()}.log"
    f.write_text("".join(f"line{i}\n" for i in range(10)))

    out = io.StringIO()
    err = io.StringIO()
    code = run_logs(
        "adapter",
        follow_mode=False,
        last_n=3,
        log_dir=tmp_path,
        stream=out,
        err_stream=err,
        today=lambda: today,
    )
    assert code == 0
    assert err.getvalue() == ""
    # Last three lines, no prefix (single-service mode).
    assert out.getvalue() == "line7\nline8\nline9\n"


def test_run_logs_no_follow_all_mode_prefixes_each_line(tmp_path: Path) -> None:
    today = date(2026, 5, 10)
    for svc in REGISTRY:
        (tmp_path / f"{svc.name}-{today.isoformat()}.log").write_text(
            f"{svc.name}-1\n{svc.name}-2\n"
        )

    out = io.StringIO()
    err = io.StringIO()
    code = run_logs(
        "all",
        follow_mode=False,
        last_n=50,
        log_dir=tmp_path,
        stream=out,
        err_stream=err,
        today=lambda: today,
    )
    assert code == 0
    # Order matches REGISTRY (adapter, receiver, backup) — the initial dump
    # iterates services in that order.
    expected = (
        "[adapter] adapter-1\n[adapter] adapter-2\n"
        "[receiver] receiver-1\n[receiver] receiver-2\n"
        "[backup] backup-1\n[backup] backup-2\n"
    )
    assert out.getvalue() == expected


# ---------------------------------------------------------------------------
# Error / edge cases
# ---------------------------------------------------------------------------


def test_run_logs_missing_directory_returns_1(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    out = io.StringIO()
    err = io.StringIO()
    code = run_logs(
        "adapter",
        follow_mode=False,
        log_dir=missing,
        stream=out,
        err_stream=err,
        today=lambda: date(2026, 5, 10),
    )
    assert code == 1
    assert "Logs directory not found" in err.getvalue()


def test_run_logs_no_matching_file_returns_1(tmp_path: Path) -> None:
    out = io.StringIO()
    err = io.StringIO()
    code = run_logs(
        "adapter",
        follow_mode=False,
        log_dir=tmp_path,
        stream=out,
        err_stream=err,
        today=lambda: date(2026, 5, 10),
    )
    assert code == 1
    assert err.getvalue().strip() == "No logs found for adapter. Has it run yet?"


def test_run_logs_unknown_service_returns_1(tmp_path: Path) -> None:
    out = io.StringIO()
    err = io.StringIO()
    code = run_logs(
        "not-a-service",
        follow_mode=False,
        log_dir=tmp_path,
        stream=out,
        err_stream=err,
        today=lambda: date(2026, 5, 10),
    )
    assert code == 1
    assert "Unknown service" in err.getvalue()


# ---------------------------------------------------------------------------
# --launchd mode
# ---------------------------------------------------------------------------


def test_run_logs_launchd_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    svc = _adapter()
    # Materialize the launchd-level files in a tmp location and point the
    # registry entry's paths at them via monkeypatch.
    stdout_path = tmp_path / "launchd-stdout.log"
    stderr_path = tmp_path / "launchd-stderr.log"
    stdout_path.write_text("out-1\nout-2\n")
    stderr_path.write_text("err-1\n")

    import dataclasses

    patched = dataclasses.replace(svc, launchd_stdout=stdout_path, launchd_stderr=stderr_path)
    new_registry = tuple(patched if s.name == "adapter" else s for s in REGISTRY)
    # ``resolve_targets`` (in services.py) consults the module-level
    # ``_BY_NAME`` index built at import time, so patching just
    # ``amg.cli.logs.REGISTRY`` is not enough — we must patch the indices
    # the resolver actually reads.
    monkeypatch.setattr("amg.cli.logs.REGISTRY", new_registry)
    monkeypatch.setattr("amg.cli.services._BY_NAME", {s.name: s for s in new_registry})
    monkeypatch.setattr("amg.cli.services._BY_LABEL", {s.label: s for s in new_registry})
    monkeypatch.setattr("amg.cli.services.REGISTRY", new_registry)

    out = io.StringIO()
    err = io.StringIO()
    code = run_logs(
        "adapter",
        launchd=True,
        follow_mode=False,
        last_n=50,
        log_dir=tmp_path,
        stream=out,
        err_stream=err,
        today=lambda: date(2026, 5, 10),
    )
    assert code == 0
    # Both files dumped in order (stdout first, then stderr).
    assert out.getvalue() == "out-1\nout-2\nerr-1\n"


# ---------------------------------------------------------------------------
# Tail-follow integration
# ---------------------------------------------------------------------------


def test_follow_picks_up_appended_lines(tmp_path: Path) -> None:
    f = tmp_path / "x.log"
    f.write_text("initial\n")

    out = io.StringIO()
    stop_event = threading.Event()

    def writer() -> None:
        # Give the follower time to seek to EOF and start polling.
        time.sleep(0.1)
        with f.open("a", encoding="utf-8") as fp:
            fp.write("appended-1\n")
            fp.write("appended-2\n")
            fp.flush()
        # Let the follower's poll catch up before signalling stop.
        time.sleep(0.3)
        stop_event.set()

    t = threading.Thread(target=writer)
    t.start()
    follow(
        [f],
        stream=out,
        interval=0.05,
        stop=stop_event.is_set,
    )
    t.join(timeout=5)
    captured = out.getvalue()
    assert "appended-1" in captured
    assert "appended-2" in captured
    # Pre-existing content should NOT appear — follow seeks to EOF.
    assert "initial" not in captured


# ---------------------------------------------------------------------------
# Typer end-to-end (no-follow path)
# ---------------------------------------------------------------------------


def test_cli_logs_no_follow_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    today = date.today()
    svc = _adapter()
    f = tmp_path / f"{svc.name}-{today.isoformat()}.log"
    f.write_text("hello\n")
    monkeypatch.setattr("amg.cli.logs.LOG_DIR", tmp_path)

    result = runner.invoke(app, ["logs", "--no-follow", "-n", "10", "adapter"])
    assert result.exit_code == 0, result.output
    assert "hello" in result.output


def test_cli_logs_unknown_service_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Need a real directory so we get past the "logs dir missing" gate.
    monkeypatch.setattr("amg.cli.logs.LOG_DIR", tmp_path)

    result = runner.invoke(app, ["logs", "--no-follow", "not-a-service"])
    assert result.exit_code == 1
    # Typer's CliRunner merges stderr into output by default.
    assert "Unknown service" in (result.output or "") or "Unknown service" in (
        result.stderr if hasattr(result, "stderr") else ""
    )
