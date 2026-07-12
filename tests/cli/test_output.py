"""Tests for ``amg.cli.output`` rendering helpers.

Spec references:
* §6.4 Accessibility — Rich auto-degrades to plain text off-TTY; ``--json``
  is the screen-reader / programmatic path.
* §7.1 Architecture Overview — ``amg/cli/output.py`` is the single point
  where format selection (Rich / plain / JSON) lives.

Coverage:
* Functional: TTY -> Rich table; piped -> plain text (no ANSI);
  ``json_mode=True`` -> parseable JSON array with column-id keys.
* Edge cases: empty rows; unicode pass-through.
* Error handling: column-count mismatch raises ``ValueError``.
* ``NO_COLOR`` env var disables color even on a TTY (spec §6.4 +
  https://no-color.org).
"""

from __future__ import annotations

import io
import json
import re

import pytest

from amg.cli.output import print_kv, print_status_line, render_table

# Matches any ANSI CSI sequence (color, style, cursor moves). Used to
# assert plain-mode output is escape-free.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _FakeTTY(io.StringIO):
    """StringIO that lies about being a TTY.

    Used to exercise the Rich-rendering path without an actual terminal.
    """

    def isatty(self) -> bool:  # pragma: no cover — trivial
        return True


COLUMNS = [
    {"id": "service", "header": "service"},
    {"id": "loaded", "header": "loaded"},
    {"id": "pid", "header": "pid"},
]


# ---------------------------------------------------------------------------
# Functional
# ---------------------------------------------------------------------------


def test_render_table_tty_emits_ansi_styling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A TTY stream (without NO_COLOR) should produce ANSI-styled output."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    # Force Rich to think the terminal supports color even in a headless
    # test environment.
    monkeypatch.setenv("FORCE_COLOR", "1")

    buf = _FakeTTY()
    rows = [
        {"service": "adapter", "loaded": "yes", "pid": "1234"},
        {"service": "receiver", "loaded": "no", "pid": "-"},
    ]
    render_table(rows, COLUMNS, stream=buf)

    output = buf.getvalue()
    # Header values still present (Rich keeps the text, just decorates it).
    assert "adapter" in output
    assert "receiver" in output
    # Rich on a TTY emits at least one ANSI escape (header bold, borders).
    assert ANSI_RE.search(output), f"expected ANSI codes in TTY output, got: {output!r}"


def test_render_table_piped_emits_plain_text() -> None:
    """A plain ``StringIO`` (no ``isatty``) should produce ANSI-free text."""
    buf = io.StringIO()
    rows = [{"service": "adapter", "loaded": "yes", "pid": "1234"}]

    render_table(rows, COLUMNS, stream=buf)

    output = buf.getvalue()
    assert "service" in output
    assert "adapter" in output
    assert "1234" in output
    assert not ANSI_RE.search(output), f"unexpected ANSI in plain output: {output!r}"


def test_render_table_json_mode_emits_parseable_array() -> None:
    """``json_mode=True`` must produce a JSON array with column-id keys."""
    buf = io.StringIO()
    rows = [
        {"service": "adapter", "loaded": "yes", "pid": 1234},
        {"service": "receiver", "loaded": "no", "pid": None},
    ]
    render_table(rows, COLUMNS, stream=buf, json_mode=True)

    payload = json.loads(buf.getvalue())
    assert payload == [
        {"service": "adapter", "loaded": "yes", "pid": 1234},
        {"service": "receiver", "loaded": "no", "pid": None},
    ]


def test_render_table_json_mode_ignores_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``json_mode`` wins over TTY auto-detection — always pure JSON."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")

    buf = _FakeTTY()
    render_table(
        [{"service": "adapter", "loaded": "yes", "pid": 1}],
        COLUMNS,
        stream=buf,
        json_mode=True,
    )
    payload = json.loads(buf.getvalue())
    assert payload == [{"service": "adapter", "loaded": "yes", "pid": 1}]
    # Pure JSON: no ANSI escapes.
    assert not ANSI_RE.search(buf.getvalue())


def test_render_table_accepts_positional_row_sequences() -> None:
    """Rows may also be sequences aligned to the columns list."""
    buf = io.StringIO()
    rows = [
        ("adapter", "yes", 1234),
        ("receiver", "no", None),
    ]
    render_table(rows, COLUMNS, stream=buf, json_mode=True)
    payload = json.loads(buf.getvalue())
    assert payload == [
        {"service": "adapter", "loaded": "yes", "pid": 1234},
        {"service": "receiver", "loaded": "no", "pid": None},
    ]


def test_render_table_accepts_string_columns() -> None:
    """Column entries may be bare strings (id == header)."""
    buf = io.StringIO()
    render_table(
        [{"a": 1, "b": 2}],
        ["a", "b"],
        stream=buf,
        json_mode=True,
    )
    assert json.loads(buf.getvalue()) == [{"a": 1, "b": 2}]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_render_table_empty_rows_plain() -> None:
    """Empty rows render an empty (header-only) table off-TTY."""
    buf = io.StringIO()
    render_table([], COLUMNS, stream=buf)
    output = buf.getvalue()
    # Header present, no data rows beyond the separator line.
    assert "service" in output
    assert "loaded" in output
    assert "pid" in output
    # No ANSI escapes in plain mode.
    assert not ANSI_RE.search(output)


def test_render_table_empty_rows_json_emits_empty_array() -> None:
    buf = io.StringIO()
    render_table([], COLUMNS, stream=buf, json_mode=True)
    assert json.loads(buf.getvalue()) == []


def test_render_table_unicode_passes_through_plain() -> None:
    buf = io.StringIO()
    rows = [{"service": "iméssage", "loaded": "✓", "pid": "—"}]
    render_table(rows, COLUMNS, stream=buf)
    output = buf.getvalue()
    assert "iméssage" in output
    assert "✓" in output
    assert "—" in output


def test_render_table_unicode_passes_through_json() -> None:
    buf = io.StringIO()
    rows = [{"service": "iméssage", "loaded": "✓", "pid": "—"}]
    render_table(rows, COLUMNS, stream=buf, json_mode=True)
    payload = json.loads(buf.getvalue())
    assert payload == [{"service": "iméssage", "loaded": "✓", "pid": "—"}]


def test_no_color_env_disables_ansi_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NO_COLOR`` forces plain output even when the stream is a TTY."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")  # would normally enable color

    buf = _FakeTTY()
    render_table(
        [{"service": "adapter", "loaded": "yes", "pid": 1234}],
        COLUMNS,
        stream=buf,
    )
    output = buf.getvalue()
    assert "adapter" in output
    assert not ANSI_RE.search(output), f"NO_COLOR must suppress ANSI codes, got: {output!r}"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_render_table_column_count_mismatch_raises_value_error() -> None:
    """Sequence rows with the wrong arity must raise a clear ValueError."""
    buf = io.StringIO()
    with pytest.raises(ValueError, match=r"2 value\(s\) but 3 column\(s\)"):
        render_table([("adapter", "yes")], COLUMNS, stream=buf, json_mode=True)
    # Critically: nothing landed on stdout before the failure.
    assert buf.getvalue() == ""


def test_render_table_mapping_row_missing_key_raises_value_error() -> None:
    buf = io.StringIO()
    with pytest.raises(ValueError, match=r"missing column id\(s\)"):
        render_table(
            [{"service": "adapter", "loaded": "yes"}],  # no 'pid'
            COLUMNS,
            stream=buf,
            json_mode=True,
        )
    assert buf.getvalue() == ""


def test_render_table_column_missing_id_raises_value_error() -> None:
    buf = io.StringIO()
    with pytest.raises(ValueError, match=r"missing required 'id' key"):
        render_table([], [{"header": "service"}], stream=buf, json_mode=True)


# ---------------------------------------------------------------------------
# print_kv / print_status_line
# ---------------------------------------------------------------------------


def test_print_kv_plain_off_tty() -> None:
    buf = io.StringIO()
    print_kv("loaded", "yes", stream=buf)
    assert buf.getvalue() == "loaded: yes\n"


def test_print_kv_none_renders_dash() -> None:
    buf = io.StringIO()
    print_kv("pid", None, stream=buf)
    assert buf.getvalue() == "pid: -\n"


def test_print_status_line_plain_off_tty() -> None:
    buf = io.StringIO()
    print_status_line("adapter", "started", ok=True, stream=buf)
    assert buf.getvalue() == "adapter: started\n"


def test_print_status_line_with_detail() -> None:
    buf = io.StringIO()
    print_status_line("adapter", "failed", detail="exit 1", ok=False, stream=buf)
    assert buf.getvalue() == "adapter: failed (exit 1)\n"


def test_print_status_line_tty_styles_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    buf = _FakeTTY()
    print_status_line("adapter", "started", ok=True, stream=buf)
    output = buf.getvalue()
    assert "adapter" in output
    assert "started" in output
    assert ANSI_RE.search(output), f"expected ANSI styling on TTY: {output!r}"


def test_print_status_line_no_color_disables_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    buf = _FakeTTY()
    print_status_line("adapter", "started", ok=True, stream=buf)
    output = buf.getvalue()
    assert "adapter: started" in output
    assert not ANSI_RE.search(output)
