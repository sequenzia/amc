"""Tests for :mod:`amg.cli.launchctl`.

Spec references:
* §5.5 Lifecycle — exact ``launchctl`` argv per verb (start/stop/restart/
  enable/disable, plus bootstrap/bootout used by install/uninstall).
* §5.6 Status — ``launchctl print`` parsing, ``launchctl print-disabled``
  parsing, and the defensive-parser contract (never raises; missing
  fields → ``"-"``; malformed → ``"unknown"``).
* §6.2 Security — every shell-out must pass argv as ``list[str]`` (no
  ``shell=True``); asserted by checking the captured argv shape.

Strategy:
* Patch ``amg.cli.launchctl._run`` to inject canned
  :class:`subprocess.CompletedProcess` results. This is the only function
  in the module that touches :mod:`subprocess`, and patching it gives the
  tests deterministic stdout/stderr/returncode triples without spinning up
  a real launchctl.
* Capture ``argv`` invocations through a closure recorder so we can assert
  the spec-mandated argv shape per helper (Functional AC).
* Build canned ``launchctl print`` output samples (loaded / unloaded /
  never-run / malformed) inline; the parser fixtures live near their
  consumers so the relationship between sample and assertion stays
  legible.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

import pytest

from amg.cli import launchctl

# Local alias avoids the Yoda-condition lint when asserting equality
# against the module's import-time constant in
# ``test_domain_is_gui_uid_at_module_import``.
_EXPECTED_UID = os.getuid()


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _expected_domain() -> str:
    """Compute the expected ``gui/<uid>`` domain at test time.

    Using the live ``os.getuid()`` keeps the assertion correct on whatever
    machine the suite runs (CI, dev, the operator's Mac). The module's
    :data:`launchctl.DOMAIN` is computed identically at import time.
    """
    return f"gui/{os.getuid()}"


def _expected_target(label: str) -> str:
    return f"{_expected_domain()}/{label}"


def _make_fake_run(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    captured_argv: list[list[str]] | None = None,
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Return a stand-in for :func:`launchctl._run`.

    The closure appends each invocation's argv to ``captured_argv`` (if
    provided) so individual tests can assert on the argv shape. The same
    canned ``returncode``/``stdout``/``stderr`` is returned for every call;
    tests that need varying responses per call should build a more
    sophisticated fake.
    """

    def _fake(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if captured_argv is not None:
            captured_argv.append(list(argv))
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )

    return _fake


# ---------------------------------------------------------------------------
# Domain constant
# ---------------------------------------------------------------------------


def test_domain_is_gui_uid_at_module_import() -> None:
    """The module computes ``gui/<uid>`` exactly once at import time.

    Spec §5.5/§5.6 — every launchctl invocation targets this domain.
    """
    expected = f"gui/{_EXPECTED_UID}"
    assert expected == launchctl.DOMAIN  # noqa: SIM300 — module attr, not a literal


# ---------------------------------------------------------------------------
# Lifecycle argv shape (spec §5.5 AC)
# ---------------------------------------------------------------------------


def test_kickstart_argv_no_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """``launchctl kickstart gui/<uid>/<label>`` (no -k)."""
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    result = launchctl.kickstart("com.user.amg-adapter")

    assert captured == [["launchctl", "kickstart", _expected_target("com.user.amg-adapter")]]
    assert result.ok is True
    assert result.returncode == 0
    assert result.argv == tuple(captured[0])


def test_kickstart_argv_with_restart_inserts_dash_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``launchctl kickstart -k gui/<uid>/<label>`` for restart."""
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    launchctl.kickstart("com.user.amg-adapter", restart=True)

    assert captured == [["launchctl", "kickstart", "-k", _expected_target("com.user.amg-adapter")]]


def test_bootstrap_uses_domain_plus_plist_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``launchctl bootstrap gui/<uid> <plist_path>`` — domain, not target.

    Note: ``bootstrap`` is the one helper that takes the *domain* (no
    label suffix) plus the plist file path.
    """
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    launchctl.bootstrap(
        "com.user.amg-adapter",
        "/Users/op/Library/LaunchAgents/com.user.amg-adapter.plist",
    )

    assert captured == [
        [
            "launchctl",
            "bootstrap",
            _expected_domain(),
            "/Users/op/Library/LaunchAgents/com.user.amg-adapter.plist",
        ]
    ]


def test_bootout_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``launchctl bootout gui/<uid>/<label>``."""
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    launchctl.bootout("com.user.amg-adapter")

    assert captured == [["launchctl", "bootout", _expected_target("com.user.amg-adapter")]]


def test_enable_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``launchctl enable gui/<uid>/<label>``."""
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    launchctl.enable("com.user.amg-adapter")

    assert captured == [["launchctl", "enable", _expected_target("com.user.amg-adapter")]]


def test_disable_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``launchctl disable gui/<uid>/<label>``."""
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    launchctl.disable("com.user.amg-adapter")

    assert captured == [["launchctl", "disable", _expected_target("com.user.amg-adapter")]]


def test_kill_sigterm_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``launchctl kill SIGTERM gui/<uid>/<label>``.

    Spec §5.5 names ``SIGTERM`` explicitly; the helper passes the string
    form rather than the numeric ``15``.
    """
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    launchctl.kill_sigterm("com.user.amg-adapter")

    assert captured == [["launchctl", "kill", "SIGTERM", _expected_target("com.user.amg-adapter")]]


# ---------------------------------------------------------------------------
# CompletedResult propagation
# ---------------------------------------------------------------------------


def test_completed_result_captures_returncode_and_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero exit surfaces stderr; ``ok`` is False for generic helpers.

    Spec §5.5 error handling — caller maps non-zero to exit 2 after
    printing stderr.
    """
    monkeypatch.setattr(
        launchctl,
        "_run",
        _make_fake_run(returncode=1, stderr="Load failed: 5: Input/output error\n"),
    )

    result = launchctl.kickstart("com.user.amg-adapter")

    assert result.ok is False
    assert result.returncode == 1
    assert "Input/output error" in result.stderr


def test_completed_result_argv_is_tuple_for_immutability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CompletedResult.argv`` is a tuple so callers cannot mutate it."""
    monkeypatch.setattr(launchctl, "_run", _make_fake_run())

    result = launchctl.enable("com.user.amg-adapter")

    assert isinstance(result.argv, tuple)


# ---------------------------------------------------------------------------
# bootout tolerance (spec §5.5 Edge Case)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rc", [3, 113])
def test_bootout_tolerates_not_bootstrapped_exit_codes(
    monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    """``bootout`` on an already-unloaded service returns ``ok=True``.

    macOS launchctl returns either ``3`` (ESRCH "No such process") or
    ``113`` ("Could not find service in domain") depending on the OS
    revision. Both are treated as no-op success.
    """
    monkeypatch.setattr(
        launchctl,
        "_run",
        _make_fake_run(returncode=rc, stderr="Boot-out failed: 3: No such process\n"),
    )

    result = launchctl.bootout("com.user.amg-adapter")

    assert result.ok is True
    # The underlying returncode is preserved for diagnostics — only the
    # ``ok`` flag is overridden.
    assert result.returncode == rc
    assert "No such process" in result.stderr


def test_bootout_with_real_failure_returns_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Other non-zero exits still propagate as ``ok=False``."""
    monkeypatch.setattr(
        launchctl,
        "_run",
        _make_fake_run(returncode=5, stderr="Boot-out failed: 5: I/O error\n"),
    )

    result = launchctl.bootout("com.user.amg-adapter")

    assert result.ok is False
    assert result.returncode == 5


def test_bootout_success_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero exit code → ``ok=True`` (the common path)."""
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(returncode=0))

    result = launchctl.bootout("com.user.amg-adapter")

    assert result.ok is True
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# launchctl print parsing (spec §5.6 AC + Edge Cases)
# ---------------------------------------------------------------------------


# Canned output approximating ``launchctl print gui/<uid>/<label>`` for a
# loaded, running service. Indentation matches what launchctl emits (one
# leading TAB).
_PRINT_OUT_RUNNING = """\
com.user.amg-adapter = {
\tactive count = 1
\tpath = /Users/op/Library/LaunchAgents/com.user.amg-adapter.plist
\ttype = LaunchAgent
\tstate = running
\tprogram = /Users/op/repo/ops/launchd/run-adapter.sh
\tdomain = gui/502 [100002]
\truns = 1
\tpid = 12345
\tlast exit code = 0
}
"""


# Canned output for a service that has been bootstrapped but has never
# launched. launchctl emits ``last exit code = (never exited)`` literally;
# ``pid`` is absent because no process is running.
_PRINT_OUT_NEVER_RUN = """\
com.user.amg-adapter = {
\tactive count = 0
\tpath = /Users/op/Library/LaunchAgents/com.user.amg-adapter.plist
\ttype = LaunchAgent
\tstate = not running
\truns = 0
\tlast exit code = (never exited)
}
"""


# launchctl's error output when the service is not in the domain.
_PRINT_STDERR_NOT_LOADED = (
    'Could not find service "com.user.amg-adapter" in domain for user gui: 502\n'
)


def test_print_service_running_extracts_pid_state_and_last_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three promoted fields parse cleanly for a running service."""
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(returncode=0, stdout=_PRINT_OUT_RUNNING))

    result = launchctl.print_service("com.user.amg-adapter")

    assert result.loaded is True
    assert result.returncode == 0
    assert result.pid == "12345"
    assert result.last_exit == "0"
    assert result.state == "running"
    # start_time has no analogue in current launchctl output and is left
    # at its default ``"-"`` sentinel.
    assert result.start_time == "-"
    # The raw fields dict is exposed for ``amg doctor`` and similar
    # introspection tools.
    assert result.fields["path"].endswith("com.user.amg-adapter.plist")


def test_print_service_never_run_maps_to_dash_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pid`` defaults to ``"-"`` when absent; ``(never exited)`` → ``"-"``."""
    monkeypatch.setattr(
        launchctl, "_run", _make_fake_run(returncode=0, stdout=_PRINT_OUT_NEVER_RUN)
    )

    result = launchctl.print_service("com.user.amg-adapter")

    assert result.loaded is True
    assert result.pid == "-"
    assert result.last_exit == "-"
    assert result.state == "not running"


def test_print_service_not_loaded_returns_loaded_false_and_dash_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero launchctl exit → ``loaded=False``; all fields are ``"-"``.

    Spec §5.6 Edge Case: ``launchctl print`` returns non-zero → row shows
    ``loaded=no``, all other fields ``-``.
    """
    monkeypatch.setattr(
        launchctl,
        "_run",
        _make_fake_run(returncode=113, stderr=_PRINT_STDERR_NOT_LOADED),
    )

    result = launchctl.print_service("com.user.amg-adapter")

    assert result.loaded is False
    assert result.returncode == 113
    assert result.pid == "-"
    assert result.last_exit == "-"
    assert result.state == "-"
    assert result.start_time == "-"
    # stderr is preserved for the caller (``amg status`` may surface it on
    # request, or ``amg doctor`` may include it in diagnostics).
    assert "Could not find service" in result.raw_stderr


def test_print_service_invokes_correct_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``launchctl print gui/<uid>/<label>`` exactly."""
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    launchctl.print_service("com.user.amg-adapter")

    assert captured == [["launchctl", "print", _expected_target("com.user.amg-adapter")]]


def test_print_service_malformed_pid_becomes_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pid value that isn't a positive integer → ``"unknown"``.

    Spec §5.6 Edge Case: parsing fails for an individual field → field is
    ``unknown``; CLI does not crash.
    """
    malformed = (
        "com.user.amg-adapter = {\n"
        "\tstate = running\n"
        "\tpid = not-a-number\n"
        "\tlast exit code = 0\n"
        "}\n"
    )
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(stdout=malformed))

    result = launchctl.print_service("com.user.amg-adapter")

    assert result.loaded is True
    assert result.pid == "unknown"
    # Sibling fields keep parsing — a single bad line doesn't poison the
    # rest of the result.
    assert result.last_exit == "0"
    assert result.state == "running"


def test_print_service_malformed_last_exit_becomes_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer ``last exit code`` value → ``"unknown"``."""
    malformed = (
        "com.user.amg-adapter = {\n\tstate = running\n\tpid = 12345\n\tlast exit code = banana\n}\n"
    )
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(stdout=malformed))

    result = launchctl.print_service("com.user.amg-adapter")

    assert result.last_exit == "unknown"


def test_print_service_signed_last_exit_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative exit codes (signal kills) are preserved verbatim."""
    text = "com.user.amg-adapter = {\n\tstate = not running\n\tlast exit code = -9\n}\n"
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(stdout=text))

    result = launchctl.print_service("com.user.amg-adapter")

    assert result.last_exit == "-9"


def test_print_service_parser_never_raises_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Random non-launchctl output is parsed without raising.

    Spec §5.6 Edge Case: defensive parser — never raises on malformed
    output.
    """
    monkeypatch.setattr(
        launchctl,
        "_run",
        _make_fake_run(stdout="!!! totally not launchctl output ??? \x00 ", stderr=""),
    )

    # Must not raise.
    result = launchctl.print_service("com.user.amg-adapter")

    assert result.loaded is True
    # No recognizable fields → defaults preserved.
    assert result.pid == "-"
    assert result.last_exit == "-"
    assert result.state == "-"


def test_print_service_with_start_time_field_populates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a future launchctl emits ``start time = …``, we capture it.

    Spec §5.6 AC: ``uptime = human-readable (...) computed from process
    start if launchctl exposes it``. We probe the most plausible field
    names so the higher-level status command can derive uptime when
    available.
    """
    text = (
        "com.user.amg-adapter = {\n"
        "\tstate = running\n"
        "\tpid = 12345\n"
        "\tstart time = 2026-05-10 09:00:00\n"
        "\tlast exit code = 0\n"
        "}\n"
    )
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(stdout=text))

    result = launchctl.print_service("com.user.amg-adapter")

    assert result.start_time == "2026-05-10 09:00:00"


# ---------------------------------------------------------------------------
# print_disabled_state parsing (spec §5.6 AC)
# ---------------------------------------------------------------------------


_PRINT_DISABLED_OUT = """\
com.apple.xpc.launchd.user.domain.501.100002.gui = {
\tdisabled services = {
\t\t"com.user.amg-adapter" => enabled
\t\t"com.user.amg-webhook-receiver" => disabled
\t\t"com.apple.foo" => enabled
\t}
}
"""


def test_print_disabled_state_parses_disabled_services_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each quoted ``"label" => state`` line is parsed into ``{label: bool}``."""
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(stdout=_PRINT_DISABLED_OUT))

    state = launchctl.print_disabled_state()

    assert state == {
        "com.user.amg-adapter": True,
        "com.user.amg-webhook-receiver": False,
        "com.apple.foo": True,
    }


def test_print_disabled_state_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """``launchctl print-disabled gui/<uid>`` exactly."""
    captured: list[list[str]] = []
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(captured_argv=captured))

    launchctl.print_disabled_state()

    assert captured == [["launchctl", "print-disabled", _expected_domain()]]


def test_print_disabled_state_accepts_single_quoted_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some launchctl versions emit single-quoted labels in this block."""
    text = (
        "disabled services = {\n"
        "\t'com.user.amg-adapter' => enabled\n"
        "\t'com.user.amg-webhook-receiver' => disabled\n"
        "}\n"
    )
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(stdout=text))

    state = launchctl.print_disabled_state()

    assert state == {
        "com.user.amg-adapter": True,
        "com.user.amg-webhook-receiver": False,
    }


def test_print_disabled_state_empty_on_no_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No disabled-services block → empty dict (never raises)."""
    monkeypatch.setattr(launchctl, "_run", _make_fake_run(stdout="not a real domain\n"))

    state = launchctl.print_disabled_state()

    assert state == {}


def test_print_disabled_state_returns_partial_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero launchctl exit with partial stdout still parses what it can.

    The function's contract is "never raise"; surfacing partial data is
    more useful than throwing away whatever launchctl did manage to emit
    before failing.
    """
    monkeypatch.setattr(
        launchctl,
        "_run",
        _make_fake_run(
            returncode=2,
            stdout=('disabled services = {\n\t"com.user.amg-adapter" => disabled\n}\n'),
            stderr="some launchd warning\n",
        ),
    )

    state = launchctl.print_disabled_state()

    assert state == {"com.user.amg-adapter": False}
