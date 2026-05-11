"""Thin wrappers around the ``launchctl`` binary.

This module is the **only** place in the CLI that shells out to
``launchctl``. Every public function builds an ``argv`` ``list[str]`` and
calls :func:`subprocess.run` with ``shell=False`` (spec §6.2 — no shell
substitution, no string interpolation into a shell pipeline). The argv
shapes are documented verbatim in spec §5.5 (lifecycle) and §5.6 (status).

Two result types are exposed:

* :class:`CompletedResult` — generic wrapper around ``subprocess.run``
  output. ``returncode``, ``stdout``, ``stderr`` are surfaced as fields so
  command bodies can ``raise typer.Exit(2)`` and print stderr without
  reaching back into ``subprocess`` types.
* :class:`PrintResult` — the parsed payload of ``launchctl print
  gui/<uid>/<label>``. Fields default to ``"-"`` (matching ``amc status``
  display semantics, spec §5.6 acceptance criteria) when launchctl returned
  non-zero or when the parser could not locate a field; individual fields
  fall back to ``"unknown"`` when the line exists but the value side is
  malformed. The parser **never raises** on bad input (spec §5.6 Edge
  Cases).

The :data:`DOMAIN` constant is computed once at import time from
``os.getuid()`` since the CLI is a single-user, single-process tool
(spec §6.3). Tests that need to override the uid can re-compute via
:func:`_build_service_target` directly.

Spec references: §5.5 Lifecycle, §5.6 Status, §6.2 Security, §7.1
Architecture Overview.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

__all__ = [
    "DOMAIN",
    "CompletedResult",
    "PrintResult",
    "bootout",
    "bootstrap",
    "disable",
    "enable",
    "kickstart",
    "kill_sigterm",
    "print_disabled_state",
    "print_service",
]

# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------

# The launchd domain target for per-user agents. ``gui/<uid>`` is the
# graphical-login session domain; every AMC service is a LaunchAgent so this
# is correct for all of them (spec §5.5).
#
# Computed at import time. The CLI is short-lived (one command per invocation)
# and never changes its uid mid-run, so caching is safe.
DOMAIN: str = f"gui/{os.getuid()}"


def _build_service_target(label: str) -> str:
    """Return the ``gui/<uid>/<label>`` target string.

    Kept as a function (rather than f-string at call site) so tests can
    patch :data:`DOMAIN` and re-derive the target for every helper without
    chasing each caller.
    """
    return f"{DOMAIN}/{label}"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompletedResult:
    """Generic result for one ``launchctl`` invocation.

    Fields:
        argv: The exact argv passed to :func:`subprocess.run`. Useful for
            error reporting and for tests that assert the spec-mandated
            command shape.
        returncode: Process exit code. ``0`` on success. Callers map
            non-zero to :class:`typer.Exit(2)` per spec §5.5 / §5.6.
        stdout: Captured standard output (text). May be empty.
        stderr: Captured standard error (text). May be empty. ``amc``
            command bodies print this verbatim on failure so the operator
            sees launchctl's own message.
        ok: ``True`` when the helper treats the invocation as successful.
            Usually equivalent to ``returncode == 0``, but :func:`bootout`
            overrides this for the "already unloaded" case (exit 3 / 113)
            so the caller does not need to special-case it.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    ok: bool


@dataclass(frozen=True, slots=True)
class PrintResult:
    """Parsed payload of ``launchctl print gui/<uid>/<label>``.

    Field semantics mirror spec §5.6 acceptance criteria. All string fields
    default to ``"-"`` when launchctl returned non-zero (service not
    loaded). When launchctl returned zero but a specific field could not be
    parsed, that field becomes ``"unknown"`` while the rest of the parsed
    data is preserved.

    Fields:
        label: The launchd label that was queried.
        loaded: ``True`` if launchctl returned zero (service exists in the
            domain). ``False`` if launchctl returned non-zero — every other
            field is ``"-"`` in that case.
        returncode: launchctl's exit code. ``0`` for loaded services; the
            most common non-zero values are ``113`` ("Could not find
            service") and ``3`` ("No such process").
        pid: Process PID as a string, or ``"-"`` when not running, or
            ``"unknown"`` if the line was present but unparseable.
        last_exit: Last exit code as a string (e.g. ``"0"``, ``"15"``), or
            ``"-"`` when the field is the literal ``(never exited)``, or
            ``"unknown"`` if the line was present but malformed.
        state: ``state = …`` value from the print output (``running``,
            ``not running``, etc.). ``"-"`` when not loaded; ``"unknown"``
            when malformed.
        start_time: Best-effort start timestamp string. launchctl on
            current macOS does not expose a start time line — this field
            is ``"-"`` in practice. The field is retained so a future
            macOS revision (or a richer parser using ``ps``) can populate
            it without changing the public type.
        raw_stderr: launchctl's stderr verbatim. Useful when the caller
            wants to surface "Could not find service …" to the operator.
        fields: All parsed ``key = value`` pairs from the print output,
            keyed by the **trimmed key** as it appears in the launchctl
            output. Exposed for debugging and for ``amc doctor`` which may
            inspect additional fields not promoted to first-class
            attributes.
    """

    label: str
    loaded: bool
    returncode: int
    pid: str = "-"
    last_exit: str = "-"
    state: str = "-"
    start_time: str = "-"
    raw_stderr: str = ""
    fields: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` via :func:`subprocess.run` with text capture.

    Centralized so the test suite has a single attribute to patch
    (``amc.cli.launchctl._run``) when it needs to inject canned output. We
    always pass ``shell=False`` (the default when argv is a list) per spec
    §6.2 and never raise on non-zero exit — that is the caller's policy.
    """
    return subprocess.run(  # noqa: S603 — argv is a fixed list, no shell
        argv,
        capture_output=True,
        text=True,
        check=False,
    )


def _completed(
    argv: list[str],
    proc: subprocess.CompletedProcess[str],
    *,
    ok: bool | None = None,
) -> CompletedResult:
    """Wrap a :class:`subprocess.CompletedProcess` in :class:`CompletedResult`.

    ``ok`` defaults to ``proc.returncode == 0``; helpers that want a
    different "success" policy (e.g. :func:`bootout`) override it.
    """
    return CompletedResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        ok=(proc.returncode == 0) if ok is None else ok,
    )


# ---------------------------------------------------------------------------
# Lifecycle helpers (spec §5.5)
# ---------------------------------------------------------------------------


def kickstart(label: str, *, restart: bool = False) -> CompletedResult:
    """Run ``launchctl kickstart [-k] gui/<uid>/<label>``.

    Used by ``amc service start`` (``restart=False``) and ``amc service
    restart`` (``restart=True``). ``kickstart`` loads the service if it is
    bootstrapped but not running; with ``-k`` it terminates the running
    instance first (spec §5.5).
    """
    target = _build_service_target(label)
    argv = ["launchctl", "kickstart"]
    if restart:
        # ``-k`` must precede the target. launchctl's argument parser is
        # positional-ish; placing the flag after the target works on current
        # macOS but breaks on some older releases. Stick to the documented
        # order (spec §5.5 AC).
        argv.append("-k")
    argv.append(target)
    return _completed(argv, _run(argv))


def bootstrap(label: str, plist_path: str) -> CompletedResult:
    """Run ``launchctl bootstrap gui/<uid> <plist_path>``.

    Note that ``bootstrap`` takes the **domain** (not the service target)
    plus the plist path. The label argument is accepted for symmetry with
    the other helpers and for diagnostics; it is not part of the argv.
    """
    # ``label`` is intentionally unused in the argv. We accept it so the
    # caller can pass the same identifier to every helper without branching;
    # downstream logging can also tag the operation with the label.
    del label
    argv = ["launchctl", "bootstrap", DOMAIN, plist_path]
    return _completed(argv, _run(argv))


# Exit codes that ``launchctl bootout`` returns when the service is not
# loaded. We treat both as success — the caller's intent ("ensure unloaded")
# is satisfied. Documented inline because the launchctl(1) man page does not
# list these codes explicitly; we discovered them empirically.
#
# * 113 — "Could not find service in domain" (the launchd-domain-level
#   message; emitted by some macOS versions when the service was never
#   bootstrapped in this session).
# * 3   — "No such process" (POSIX ESRCH; emitted on more recent macOS
#   versions when the service exists in the registry but is not loaded).
_BOOTOUT_NOOP_EXITS: frozenset[int] = frozenset({3, 113})


def bootout(label: str) -> CompletedResult:
    """Run ``launchctl bootout gui/<uid>/<label>``.

    Tolerates the "not bootstrapped" case: when launchctl returns one of
    :data:`_BOOTOUT_NOOP_EXITS`, the result is reported as ``ok=True``
    (treat as no-op success) so callers do not have to special-case the
    common "already uninstalled" path. The original ``returncode`` and
    ``stderr`` are preserved for diagnostics.
    """
    target = _build_service_target(label)
    argv = ["launchctl", "bootout", target]
    proc = _run(argv)
    ok = proc.returncode == 0 or proc.returncode in _BOOTOUT_NOOP_EXITS
    return _completed(argv, proc, ok=ok)


def enable(label: str) -> CompletedResult:
    """Run ``launchctl enable gui/<uid>/<label>``."""
    target = _build_service_target(label)
    argv = ["launchctl", "enable", target]
    return _completed(argv, _run(argv))


def disable(label: str) -> CompletedResult:
    """Run ``launchctl disable gui/<uid>/<label>``."""
    target = _build_service_target(label)
    argv = ["launchctl", "disable", target]
    return _completed(argv, _run(argv))


def kill_sigterm(label: str) -> CompletedResult:
    """Run ``launchctl kill SIGTERM gui/<uid>/<label>``.

    Used by ``amc service stop``. ``SIGTERM`` is the spec-mandated signal
    (§5.5 AC). The numeric form (``15``) is also accepted by launchctl on
    modern macOS, but the spec names the signal so we pass the string.
    """
    target = _build_service_target(label)
    argv = ["launchctl", "kill", "SIGTERM", target]
    return _completed(argv, _run(argv))


# ---------------------------------------------------------------------------
# Status helpers (spec §5.6)
# ---------------------------------------------------------------------------


# Match a launchctl ``print`` field line: any amount of leading whitespace,
# then ``key = value`` where the key may include spaces (e.g. ``last exit
# code``) but stops at the ``=``. Trailing whitespace on the value is
# stripped. Anchored to start/end of line in re.MULTILINE mode at call site.
_PRINT_FIELD_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9 _]*?)\s*=\s*(.*?)\s*$")


def _parse_print_fields(stdout: str) -> dict[str, str]:
    """Parse ``key = value`` lines from ``launchctl print`` output.

    Only top-level scalar fields are useful for status. We deliberately
    accept *every* matching line at any indentation: this loses nested
    structure but is safe because the keys we promote (``pid``, ``last
    exit code``, ``state``) are not duplicated as nested keys in real
    launchctl output. Malformed or non-scalar lines are silently skipped.

    Never raises (spec §5.6 Edge Cases).
    """
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        # Skip block lines like ``foo = {`` — the value side is the literal
        # ``{`` which isn't useful for us, and including them would shadow
        # real scalar fields. Same for closing ``}`` lines.
        stripped = line.strip()
        if not stripped or stripped in {"{", "}"} or stripped.endswith("{"):
            continue
        m = _PRINT_FIELD_RE.match(line)
        if m is None:
            continue
        key = m.group(1).strip()
        value = m.group(2).strip()
        # Do not overwrite a previously-seen top-level key with a nested
        # duplicate of the same name. The first occurrence wins.
        if key and key not in fields:
            fields[key] = value
    return fields


def _coerce_pid(value: str | None) -> str:
    """Coerce a parsed ``pid`` field value to the display string.

    ``"-"`` when missing (service not running), the numeric string when
    well-formed, ``"unknown"`` when the line existed but the value side is
    not a positive integer.
    """
    if value is None:
        return "-"
    if value.isdigit():
        return value
    return "unknown"


def _coerce_last_exit(value: str | None) -> str:
    """Coerce a parsed ``last exit code`` field value to the display string.

    launchctl emits the literal ``(never exited)`` when the service has
    never run. We map that to ``"-"`` (matching spec §5.6 AC). Numeric
    values pass through; negative codes (e.g. ``-9``) are preserved.
    """
    if value is None:
        return "-"
    if value == "(never exited)":
        return "-"
    # Accept signed integers; reject everything else as "unknown".
    if value.lstrip("-").isdigit():
        return value
    return "unknown"


def _coerce_state(value: str | None) -> str:
    """Coerce a parsed ``state`` field value to the display string."""
    if value is None:
        return "-"
    if not value:
        return "unknown"
    return value


# Candidate keys for a start-time field. launchctl on current macOS does not
# emit a start time at all, but Apple has changed this surface before and
# the spec lists ``start_time/uptime`` as a parsed field. We probe the most
# plausible names so a future macOS revision is captured automatically.
_START_TIME_KEYS: tuple[str, ...] = (
    "start time",
    "started at",
    "spawn time",
    "spawned at",
)


def _extract_start_time(fields: dict[str, str]) -> str:
    """Return the first available start-time-ish field, or ``"-"``."""
    for key in _START_TIME_KEYS:
        if key in fields and fields[key]:
            return fields[key]
    return "-"


def print_service(label: str) -> PrintResult:
    """Run ``launchctl print gui/<uid>/<label>`` and parse the result.

    A successful (returncode 0) invocation parses every ``key = value``
    line from stdout. Per-field defaults handle:

    * service not loaded (returncode != 0): every field is ``"-"``;
      :attr:`PrintResult.loaded` is ``False`` and :attr:`raw_stderr` is
      preserved.
    * service loaded but field absent (e.g. ``pid`` while not running):
      that specific field is ``"-"``.
    * service loaded and field present but malformed: that specific field
      is ``"unknown"``.

    Never raises — even when the parser sees output it does not
    understand (spec §5.6 Edge Cases).
    """
    target = _build_service_target(label)
    argv = ["launchctl", "print", target]
    proc = _run(argv)

    if proc.returncode != 0:
        # Service not loaded (or some other launchctl failure). Defaults
        # of ``"-"`` from the dataclass declaration are correct; we only
        # need to surface stderr for the caller.
        return PrintResult(
            label=label,
            loaded=False,
            returncode=proc.returncode,
            raw_stderr=proc.stderr or "",
        )

    fields = _parse_print_fields(proc.stdout or "")

    return PrintResult(
        label=label,
        loaded=True,
        returncode=0,
        pid=_coerce_pid(fields.get("pid")),
        last_exit=_coerce_last_exit(fields.get("last exit code")),
        state=_coerce_state(fields.get("state")),
        start_time=_extract_start_time(fields),
        raw_stderr=proc.stderr or "",
        fields=fields,
    )


# Match one disabled-services entry. The block emitted by ``launchctl
# print-disabled gui/<uid>`` looks like:
#
#     disabled services = {
#         "com.user.amc-adapter" => enabled
#         "com.apple.foo" => disabled
#     }
#
# Labels are quoted with either single or double quotes depending on
# launchctl version; both forms appear in the wild.
_DISABLED_LINE_RE = re.compile(r'^\s*["\']([^"\']+)["\']\s*=>\s*(enabled|disabled)\s*$')


def print_disabled_state() -> dict[str, bool]:
    """Run ``launchctl print-disabled gui/<uid>`` and return ``{label: enabled}``.

    Returns a mapping of every label found in the ``disabled services = {
    … }`` block to ``True`` (enabled) or ``False`` (disabled). Labels that
    never appear in the block are simply absent from the dict — callers
    should treat "absent" as "enabled by default" (launchd's own
    semantics).

    Never raises: on parse failure or launchctl non-zero exit, returns the
    partial dict accumulated so far (which may be empty). The spec only
    requires that the function not crash; surfacing partial data is more
    useful than nothing for status displays.
    """
    argv = ["launchctl", "print-disabled", DOMAIN]
    proc = _run(argv)
    # We do not bail on non-zero: launchctl-print-disabled has been known
    # to emit partial output and a non-zero exit when the user has odd
    # session domains. Try to parse whatever we got.

    result: dict[str, bool] = {}
    for line in (proc.stdout or "").splitlines():
        m = _DISABLED_LINE_RE.match(line)
        if m is None:
            continue
        label, state = m.group(1), m.group(2)
        result[label] = state == "enabled"
    return result
