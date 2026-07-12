"""Tests for the CLI service registry.

Spec references:
* §5.1 Service Registry — three services (adapter/receiver/backup) with the
  exact launchd labels and ports; ``all`` keyword; short-name **and** label
  lookups; unknown name raises a typed error with the canonical message.

Coverage:
* Registry contains the three expected entries with spec-correct fields
  (Functional).
* ``resolve_targets(["all"])`` returns ``[adapter, receiver, backup]`` in
  that order (Functional).
* Empty input returns the same three services in the same order
  (Functional — convenience equivalence to ``["all"]``).
* Lookup by short name and by label both succeed (Functional).
* Mixed list (short name + label) resolves correctly (Edge Case).
* Duplicate inputs dedupe while preserving first-seen order, including
  duplicates that mix short name + label (Edge Case).
* Unknown name raises :class:`UnknownServiceError` with the canonical
  message (Error Handling).
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from amg.cli.services import REGISTRY, Service, UnknownServiceError, resolve_targets

# ---------------------------------------------------------------------------
# Registry contents
# ---------------------------------------------------------------------------


def test_registry_has_exactly_three_services() -> None:
    assert len(REGISTRY) == 3


def test_registry_order_is_adapter_receiver_backup() -> None:
    assert [s.name for s in REGISTRY] == ["adapter", "receiver", "backup"]


def test_adapter_entry_matches_spec() -> None:
    adapter = REGISTRY[0]
    assert isinstance(adapter, Service)
    assert adapter.name == "adapter"
    assert adapter.label == "com.user.amg-adapter"
    assert adapter.port == 8080
    assert adapter.plist_template == Path("ops/launchd/com.user.amg-adapter.plist")
    assert adapter.run_script == Path("ops/launchd/run-adapter.sh")
    assert adapter.app_log_glob == "adapter-*.log"
    # launchd stdout/stderr live under ~/Library/Logs/messaging-agent/ per
    # the committed plist file. We compare to ``expanduser`` of the spec
    # path so the test is HOME-agnostic.
    log_dir = Path("~/Library/Logs/messaging-agent").expanduser()
    assert adapter.launchd_stdout == log_dir / "launchd-stdout.log"
    assert adapter.launchd_stderr == log_dir / "launchd-stderr.log"


def test_receiver_entry_matches_spec() -> None:
    receiver = REGISTRY[1]
    assert receiver.name == "receiver"
    assert receiver.label == "com.user.amg-webhook-receiver"
    assert receiver.port == 8090
    assert receiver.plist_template == Path("ops/launchd/com.user.amg-webhook-receiver.plist")
    assert receiver.run_script == Path("ops/launchd/run-webhook-receiver.sh")
    assert receiver.app_log_glob == "receiver-*.log"
    log_dir = Path("~/Library/Logs/messaging-agent").expanduser()
    assert receiver.launchd_stdout == log_dir / "launchd-receiver-stdout.log"
    assert receiver.launchd_stderr == log_dir / "launchd-receiver-stderr.log"


def test_backup_entry_matches_spec() -> None:
    backup = REGISTRY[2]
    assert backup.name == "backup"
    assert backup.label == "com.user.amg-backup"
    # The backup service is a scheduled one-shot — it does not bind a port.
    assert backup.port is None
    assert backup.plist_template == Path("ops/launchd/com.user.amg-backup.plist")
    assert backup.app_log_glob == "backup-*.log"
    log_dir = Path("~/Library/Logs/messaging-agent").expanduser()
    assert backup.launchd_stdout == log_dir / "launchd-backup-stdout.log"
    assert backup.launchd_stderr == log_dir / "launchd-backup-stderr.log"


def test_service_is_frozen_dataclass() -> None:
    """Defense in depth: registry entries should not be mutable at runtime."""
    adapter = REGISTRY[0]
    with pytest.raises(FrozenInstanceError):
        adapter.port = 9999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# resolve_targets
# ---------------------------------------------------------------------------


def test_resolve_all_keyword_returns_every_service_in_order() -> None:
    resolved = resolve_targets(["all"])
    assert [s.name for s in resolved] == ["adapter", "receiver", "backup"]


def test_resolve_empty_list_returns_every_service_in_order() -> None:
    # Empty input is the convenience shorthand for ``all`` — used by every
    # ``amg <verb>`` invocation that omits the target argument.
    resolved = resolve_targets([])
    assert [s.name for s in resolved] == ["adapter", "receiver", "backup"]


def test_resolve_short_name_returns_single_service() -> None:
    resolved = resolve_targets(["adapter"])
    assert len(resolved) == 1
    assert resolved[0].name == "adapter"


def test_resolve_full_label_returns_single_service() -> None:
    resolved = resolve_targets(["com.user.amg-webhook-receiver"])
    assert len(resolved) == 1
    assert resolved[0].name == "receiver"


def test_resolve_mixed_names_and_labels() -> None:
    resolved = resolve_targets(["adapter", "com.user.amg-backup"])
    assert [s.name for s in resolved] == ["adapter", "backup"]


def test_resolve_dedupes_preserving_order() -> None:
    resolved = resolve_targets(["receiver", "adapter", "receiver"])
    assert [s.name for s in resolved] == ["receiver", "adapter"]


def test_resolve_dedupes_across_short_name_and_label_forms() -> None:
    # The same service referred to once by short name and once by label
    # should collapse to a single entry.
    resolved = resolve_targets(["adapter", "com.user.amg-adapter"])
    assert [s.name for s in resolved] == ["adapter"]


def test_resolve_unknown_name_raises_typed_error_with_canonical_message() -> None:
    with pytest.raises(UnknownServiceError) as excinfo:
        resolve_targets(["bogus"])
    # The spec dictates the exact suffix; verify it verbatim so the CLI
    # can surface this message unchanged.
    assert str(excinfo.value) == "Unknown service: bogus. Known: adapter, receiver, backup, all."


def test_resolve_unknown_name_in_mixed_list_still_raises() -> None:
    with pytest.raises(UnknownServiceError):
        resolve_targets(["adapter", "bogus"])


def test_unknown_service_error_is_value_error_subclass() -> None:
    # Subclassing ValueError keeps the error catchable by generic
    # ``except ValueError`` blocks while still being a distinguishable type.
    assert issubclass(UnknownServiceError, ValueError)
