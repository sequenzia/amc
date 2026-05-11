"""Tests for ``amc status`` (spec §5.6).

Coverage:
* Functional: ``amc status`` emits a table with all six spec columns
  (service, loaded, enabled, pid, last_exit, uptime).
* Functional: ``amc status adapter`` emits exactly one row.
* Functional: ``--json`` emits a JSON-parseable array with identical
  fields (column ids preserved as keys).
* Functional: non-TTY stdout renders plain text (no ANSI codes).
* Edge case: ``loaded=no`` service renders ``-`` for pid/last_exit/
  uptime.
* Edge case: malformed launchctl field surfaces as ``unknown`` and the
  CLI does not crash.
* Error handling: unknown service exits 1 with the spec message.
* Error handling: any service not loaded → aggregate exit code 2.
* Error handling: all-loaded → exit 0 even if some services are not
  running (informational status).
* Uptime helper: ``humanize_uptime`` covers the seconds / minutes /
  hours / days bands plus negative-delta safety.

Strategy: patch :mod:`amc.cli.status` and :mod:`amc.cli.launchctl`
seams (``print_service``, ``print_disabled_state``, ``_run_ps``) so the
tests never touch a real ``launchctl`` or ``ps`` invocation.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
from datetime import datetime

import pytest
from typer.testing import CliRunner

from amc.cli import status as status_mod
from amc.cli.app import app
from amc.cli.launchctl import PrintResult

runner = CliRunner()

# ANSI CSI sequence pattern — used to assert plain-mode output is
# escape-free (spec §6.4 Accessibility).
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ---------------------------------------------------------------------------
# Fixtures: canned launchctl results
# ---------------------------------------------------------------------------


def _loaded(
    label: str,
    *,
    pid: str = "1234",
    last_exit: str = "0",
    state: str = "running",
) -> PrintResult:
    """Build a ``loaded=True`` PrintResult for ``label``."""
    return PrintResult(
        label=label,
        loaded=True,
        returncode=0,
        pid=pid,
        last_exit=last_exit,
        state=state,
        start_time="-",
        raw_stderr="",
        fields={},
    )


def _unloaded(label: str) -> PrintResult:
    """Build a ``loaded=False`` PrintResult for ``label``."""
    return PrintResult(
        label=label,
        loaded=False,
        returncode=113,
        raw_stderr="Could not find service\n",
    )


def _install_print_service(
    monkeypatch: pytest.MonkeyPatch,
    results: dict[str, PrintResult],
) -> None:
    """Patch ``print_service`` to return canned ``PrintResult`` per label."""

    def fake(label: str) -> PrintResult:
        if label in results:
            return results[label]
        return _unloaded(label)

    monkeypatch.setattr(status_mod, "print_service", fake)


def _install_disabled_state(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, bool] | None = None,
) -> None:
    """Patch ``print_disabled_state`` to return the given map."""
    mapping = mapping or {}
    monkeypatch.setattr(status_mod, "print_disabled_state", lambda: dict(mapping))


def _disable_ps_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``probe_process_start`` to return None (uptime = "-")."""
    monkeypatch.setattr(status_mod, "probe_process_start", lambda pid: None)


# ---------------------------------------------------------------------------
# Functional: table contents
# ---------------------------------------------------------------------------


def test_status_all_emits_table_with_six_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_print_service(
        monkeypatch,
        {
            "com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1001"),
            "com.user.amc-webhook-receiver": _loaded("com.user.amc-webhook-receiver", pid="1002"),
            "com.user.amc-backup": _loaded("com.user.amc-backup", pid="-", last_exit="0"),
        },
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    # Headers
    for header in ("service", "loaded", "enabled", "pid", "last_exit", "uptime"):
        assert header in out, f"expected header {header!r} in output: {out!r}"
    # All three services on three rows
    assert "adapter" in out
    assert "receiver" in out
    assert "backup" in out


def test_status_single_service_emits_one_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_print_service(
        monkeypatch,
        {"com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="2222")},
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status", "adapter"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "adapter" in out
    # Receiver / backup should not appear as data rows
    assert "receiver" not in out
    assert "backup" not in out


# ---------------------------------------------------------------------------
# Functional: --json output
# ---------------------------------------------------------------------------


def test_status_json_emits_parseable_array(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_print_service(
        monkeypatch,
        {
            "com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1001"),
            "com.user.amc-webhook-receiver": _loaded(
                "com.user.amc-webhook-receiver", pid="-", last_exit="-"
            ),
            "com.user.amc-backup": _loaded("com.user.amc-backup", pid="-", last_exit="-"),
        },
    )
    _install_disabled_state(monkeypatch, {"com.user.amc-webhook-receiver": False})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 3

    expected_keys = {"service", "loaded", "enabled", "pid", "last_exit", "uptime"}
    for row in payload:
        assert set(row.keys()) == expected_keys

    # Order matches registry: adapter, receiver, backup.
    assert payload[0]["service"] == "adapter"
    assert payload[0]["loaded"] == "yes"
    assert payload[0]["pid"] == "1001"
    assert payload[1]["service"] == "receiver"
    # Disabled in the disabled map → enabled=no
    assert payload[1]["enabled"] == "no"
    assert payload[2]["service"] == "backup"


def test_status_json_single_service(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_print_service(
        monkeypatch,
        {"com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1234")},
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status", "adapter", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    assert payload[0]["service"] == "adapter"
    assert payload[0]["loaded"] == "yes"
    assert payload[0]["pid"] == "1234"


# ---------------------------------------------------------------------------
# Functional: non-TTY -> plain text (no ANSI)
# ---------------------------------------------------------------------------


def test_status_non_tty_emits_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_print_service(
        monkeypatch,
        {"com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1234")},
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status", "adapter"])
    assert result.exit_code == 0
    # Typer's CliRunner uses a non-TTY buffer, so output must be plain.
    assert not ANSI_RE.search(result.stdout), (
        f"expected ANSI-free output off-TTY, got: {result.stdout!r}"
    )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_status_unloaded_row_uses_dashes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_print_service(
        monkeypatch,
        {
            # adapter loaded, receiver unloaded, backup loaded
            "com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1001"),
            "com.user.amc-webhook-receiver": _unloaded("com.user.amc-webhook-receiver"),
            "com.user.amc-backup": _loaded("com.user.amc-backup", pid="1003"),
        },
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status", "--json"])
    # Aggregate exit 2 because at least one service is not loaded.
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    receiver = next(r for r in payload if r["service"] == "receiver")
    assert receiver["loaded"] == "no"
    assert receiver["pid"] == "-"
    assert receiver["last_exit"] == "-"
    assert receiver["uptime"] == "-"


def test_status_malformed_field_renders_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §5.6 Edge Cases: parse failure → ``unknown``; CLI must not crash."""
    bad = PrintResult(
        label="com.user.amc-adapter",
        loaded=True,
        returncode=0,
        pid="unknown",
        last_exit="unknown",
        state="unknown",
        start_time="-",
        raw_stderr="",
        fields={},
    )
    _install_print_service(monkeypatch, {"com.user.amc-adapter": bad})
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status", "adapter", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload[0]["pid"] == "unknown"
    assert payload[0]["last_exit"] == "unknown"
    # uptime collapses to "-" because pid is "unknown".
    assert payload[0]["uptime"] == "-"


# ---------------------------------------------------------------------------
# Error handling: unknown service
# ---------------------------------------------------------------------------


def test_status_unknown_service_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_print_service(monkeypatch, {})
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status", "bogus"])
    assert result.exit_code == 1
    # Spec §5.1 canonical message.
    assert "Unknown service: bogus" in result.stderr
    assert "Known: adapter, receiver, backup, all" in result.stderr


# ---------------------------------------------------------------------------
# Exit code matrix
# ---------------------------------------------------------------------------


def test_status_all_loaded_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_print_service(
        monkeypatch,
        {
            "com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1"),
            "com.user.amc-webhook-receiver": _loaded("com.user.amc-webhook-receiver", pid="2"),
            "com.user.amc-backup": _loaded("com.user.amc-backup", pid="3"),
        },
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0


def test_status_loaded_but_not_running_still_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §5.6 AC: non-running but loaded is informational (exit 0)."""
    _install_print_service(
        monkeypatch,
        {
            "com.user.amc-adapter": _loaded(
                "com.user.amc-adapter", pid="-", last_exit="0", state="not running"
            ),
            "com.user.amc-webhook-receiver": _loaded(
                "com.user.amc-webhook-receiver", pid="-", last_exit="-"
            ),
            "com.user.amc-backup": _loaded("com.user.amc-backup", pid="-", last_exit="-"),
        },
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0


def test_status_one_missing_service_exits_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_print_service(
        monkeypatch,
        {
            "com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1"),
            "com.user.amc-backup": _loaded("com.user.amc-backup", pid="3"),
            # receiver intentionally absent -> _unloaded() fallback
        },
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Uptime helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (1, "1s"),
        (59, "59s"),
        (60, "1m 0s"),
        (192, "3m 12s"),
        (3599, "59m 59s"),
        (3600, "1h 0m"),
        (7680, "2h 8m"),
        (86_399, "23h 59m"),
        (86_400, "1d"),
        (86_400 * 4, "4d"),
        (-1, "-"),
    ],
)
def test_humanize_uptime_bands(seconds: int, expected: str) -> None:
    assert status_mod.humanize_uptime(seconds) == expected


def test_probe_process_start_parses_lstart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_ps(pid: str) -> subprocess.CompletedProcess[str]:
        assert pid == "4242"
        return subprocess.CompletedProcess(
            args=["ps"], returncode=0, stdout="Sun May 10 12:00:00 2026\n", stderr=""
        )

    monkeypatch.setattr(status_mod, "_run_ps", fake_ps)
    got = status_mod.probe_process_start("4242")
    assert got == datetime(2026, 5, 10, 12, 0, 0)


def test_probe_process_start_returns_none_on_non_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``_run_ps`` should never be called; assert by patching it to raise.
    def boom(pid: str) -> subprocess.CompletedProcess[str]:
        raise AssertionError("should not call ps for non-numeric pid")

    monkeypatch.setattr(status_mod, "_run_ps", boom)
    assert status_mod.probe_process_start("-") is None
    assert status_mod.probe_process_start("unknown") is None


def test_probe_process_start_returns_none_on_ps_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_ps(pid: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["ps"], returncode=1, stdout="", stderr="No such process\n"
        )

    monkeypatch.setattr(status_mod, "_run_ps", fake_ps)
    assert status_mod.probe_process_start("99999") is None


def test_probe_process_start_returns_none_on_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_ps(pid: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["ps"], returncode=0, stdout="garbage\n", stderr="")

    monkeypatch.setattr(status_mod, "_run_ps", fake_ps)
    assert status_mod.probe_process_start("1234") is None


def test_status_uptime_computed_from_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_print_service(
        monkeypatch,
        {"com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="4242")},
    )
    _install_disabled_state(monkeypatch, {})

    started = datetime(2026, 5, 10, 12, 0, 0)
    now = datetime(2026, 5, 10, 14, 8, 0)  # 2h 8m later
    monkeypatch.setattr(status_mod, "probe_process_start", lambda pid: started)

    # Patch run_status via a direct call so we can inject ``now``.
    buf = io.StringIO()
    code = status_mod.run_status("adapter", json_mode=True, stream=buf, now=lambda: now)
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload[0]["uptime"] == "2h 8m"


# ---------------------------------------------------------------------------
# run_status direct calls
# ---------------------------------------------------------------------------


def test_run_status_direct_call_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the testable core directly with injected streams."""
    _install_print_service(
        monkeypatch,
        {"com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1234")},
    )
    _install_disabled_state(monkeypatch, {})
    _disable_ps_probe(monkeypatch)

    buf = io.StringIO()
    err = io.StringIO()
    code = status_mod.run_status("adapter", json_mode=True, stream=buf, err_stream=err)
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload[0]["service"] == "adapter"


def test_run_status_enabled_defaults_to_yes_when_label_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A label missing from print-disabled is enabled by launchd default."""
    _install_print_service(
        monkeypatch,
        {"com.user.amc-adapter": _loaded("com.user.amc-adapter", pid="1234")},
    )
    _install_disabled_state(monkeypatch, {})  # empty
    _disable_ps_probe(monkeypatch)

    buf = io.StringIO()
    code = status_mod.run_status("adapter", json_mode=True, stream=buf)
    assert code == 0
    payload = json.loads(buf.getvalue())
    assert payload[0]["enabled"] == "yes"
