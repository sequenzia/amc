"""Shared CLI rendering helpers (spec §6.4 / §7.1).

Every command that emits structured output (``amc status``, ``amc doctor``,
``amc service ...``) funnels through this module so format selection — Rich
table for an interactive TTY, plain text for piped output, JSON for
``--json`` — happens in one place.

Design notes:

* TTY detection uses ``sys.stdout.isatty()`` at call time so tests can swap
  ``sys.stdout`` for a buffer and reliably get plain output.
* Rich already honors the ``NO_COLOR`` environment variable when it builds
  its ``Console``; we deliberately do **not** override that with explicit
  ``no_color=`` or ``force_terminal=`` kwargs. Setting ``NO_COLOR=`` (any
  value, per https://no-color.org) yields ANSI-free output even on a TTY.
* The module is intentionally lazy-friendly: importing ``amc.cli.output``
  pulls Rich, but ``amc --help`` never imports this module, so the
  sub-200 ms cold-start budget (spec §6.1) is preserved.

Public surface:

* :func:`render_table` — emit a table (Rich / plain / JSON).
* :func:`print_kv` — one ``key: value`` line, optionally styled.
* :func:`print_status_line` — one-line per-service result (used by
  service lifecycle commands).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import IO, Any

from rich.console import Console
from rich.table import Table

__all__ = [
    "render_table",
    "print_kv",
    "print_status_line",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_tty(stream: IO[str] | None) -> bool:
    """Return True when *stream* is a real interactive terminal.

    ``isatty`` is missing on some test doubles (``io.StringIO``), so we
    treat that as "not a TTY" — which is the safe, plain-text default.
    """
    if stream is None:
        return False
    isatty = getattr(stream, "isatty", None)
    if isatty is None:
        return False
    try:
        return bool(isatty())
    except (ValueError, OSError):  # closed stream, etc.
        return False


def _no_color_set() -> bool:
    """Return True when the ``NO_COLOR`` env var is present (any value).

    Per https://no-color.org any non-empty value disables color; we also
    treat an empty-string presence as "set" for parity with Rich's own
    handling (Rich checks ``"NO_COLOR" in os.environ``).
    """
    return "NO_COLOR" in os.environ


def _coerce_row(
    row: Sequence[Any] | Mapping[str, Any],
    column_ids: Sequence[str],
) -> list[Any]:
    """Return *row* as a positional list aligned to *column_ids*.

    Accepts either a sequence (must match column count) or a mapping
    keyed by column id. Raises :class:`ValueError` on mismatch so callers
    get a precise error at the source, not a confused Rich render later.
    """
    if isinstance(row, Mapping):
        missing = [cid for cid in column_ids if cid not in row]
        if missing:
            raise ValueError(
                f"row is missing column id(s) {missing!r}; "
                f"expected keys for columns {list(column_ids)!r}"
            )
        return [row[cid] for cid in column_ids]

    values = list(row)
    if len(values) != len(column_ids):
        raise ValueError(
            f"row has {len(values)} value(s) but {len(column_ids)} column(s) "
            f"are defined ({list(column_ids)!r})"
        )
    return values


def _normalize_columns(
    columns: Sequence[Mapping[str, Any] | str],
) -> list[dict[str, Any]]:
    """Normalize *columns* into ``[{"id": ..., "header": ...}, ...]``.

    A column may be either:

    * a string — used as both ``id`` and ``header``; or
    * a mapping with at minimum ``id`` and optionally ``header``,
      ``justify``, ``style``.
    """
    normalized: list[dict[str, Any]] = []
    for col in columns:
        if isinstance(col, str):
            normalized.append({"id": col, "header": col})
            continue
        if not isinstance(col, Mapping):
            raise ValueError(f"column entries must be str or mapping, got {type(col).__name__}")
        if "id" not in col:
            raise ValueError(f"column entry missing required 'id' key: {col!r}")
        entry: dict[str, Any] = {"id": col["id"], "header": col.get("header", col["id"])}
        for opt in ("justify", "style", "no_wrap"):
            if opt in col:
                entry[opt] = col[opt]
        normalized.append(entry)
    return normalized


def _stringify(value: Any) -> str:
    """Return a safe string for table rendering.

    ``None`` becomes ``"-"`` (matches the spec §5.4 status convention).
    Everything else goes through ``str()`` which preserves unicode.
    """
    if value is None:
        return "-"
    return str(value)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_table(
    rows: Sequence[Sequence[Any] | Mapping[str, Any]],
    columns: Sequence[Mapping[str, Any] | str],
    *,
    json_mode: bool = False,
    stream: IO[str] | None = None,
    title: str | None = None,
) -> None:
    """Render *rows* using the most appropriate format for the stream.

    Format selection:

    * ``json_mode=True`` — emit a JSON array; each row is an object keyed
      by column id. Unconditional, regardless of TTY.
    * Otherwise, if *stream* (default ``sys.stdout``) is a TTY and
      ``NO_COLOR`` is not set — render a colored Rich table.
    * Otherwise — render a plain ASCII table with no ANSI codes.

    Raises
    ------
    ValueError
        If any row's column count (sequence) or required keys (mapping)
        do not match *columns*.
    """
    out = stream if stream is not None else sys.stdout
    norm_columns = _normalize_columns(columns)
    column_ids = [c["id"] for c in norm_columns]

    # Validate every row up front so partial output never lands on stdout.
    materialized: list[list[Any]] = [_coerce_row(row, column_ids) for row in rows]

    if json_mode:
        payload = [
            {cid: _json_value(value) for cid, value in zip(column_ids, row, strict=True)}
            for row in materialized
        ]
        json.dump(payload, out, ensure_ascii=False)
        out.write("\n")
        return

    if _is_tty(out) and not _no_color_set():
        _render_rich_table(materialized, norm_columns, out, title=title)
    else:
        _render_plain_table(materialized, norm_columns, out, title=title)


def print_kv(
    key: str,
    value: Any,
    *,
    stream: IO[str] | None = None,
    style: str | None = None,
) -> None:
    """Print a single ``key: value`` line.

    On a TTY the *key* is bolded (via Rich) unless ``NO_COLOR`` is set or
    a custom ``style`` of ``""`` is provided. Off-TTY it's plain.
    """
    out = stream if stream is not None else sys.stdout
    text_value = _stringify(value)

    if _is_tty(out) and not _no_color_set():
        console = Console(file=out, highlight=False)
        effective = style if style is not None else "bold"
        if effective:
            console.print(f"[{effective}]{key}[/]: {text_value}")
        else:
            console.print(f"{key}: {text_value}")
        return

    out.write(f"{key}: {text_value}\n")


def print_status_line(
    service: str,
    status: str,
    *,
    detail: str | None = None,
    ok: bool | None = None,
    stream: IO[str] | None = None,
) -> None:
    """Print a one-line per-service status result.

    Format: ``"<service>: <status>"`` with an optional ``" (<detail>)"``
    suffix. On a TTY *status* is colored green when ``ok=True``, red when
    ``ok=False``, and uncolored when ``ok`` is ``None`` (neutral).

    Used by ``amc service start/stop/restart/enable/disable`` to report
    one line per target without committing to a full table.
    """
    out = stream if stream is not None else sys.stdout
    suffix = f" ({detail})" if detail else ""

    if _is_tty(out) and not _no_color_set():
        console = Console(file=out, highlight=False)
        if ok is True:
            color = "green"
        elif ok is False:
            color = "red"
        else:
            color = None
        rendered_status = f"[{color}]{status}[/]" if color else status
        console.print(f"[bold]{service}[/]: {rendered_status}{suffix}")
        return

    out.write(f"{service}: {status}{suffix}\n")


# ---------------------------------------------------------------------------
# Rendering backends
# ---------------------------------------------------------------------------


def _render_rich_table(
    rows: list[list[Any]],
    columns: list[dict[str, Any]],
    stream: IO[str],
    *,
    title: str | None,
) -> None:
    # Rich's Console honors NO_COLOR natively; we still gate via the
    # ``_no_color_set()`` check at the caller so we never even instantiate
    # a Rich Table when the operator opted out.
    console = Console(file=stream, highlight=False)
    table = Table(title=title, show_header=True, header_style="bold")
    for col in columns:
        kwargs: dict[str, Any] = {}
        if "justify" in col:
            kwargs["justify"] = col["justify"]
        if "style" in col:
            kwargs["style"] = col["style"]
        if "no_wrap" in col:
            kwargs["no_wrap"] = col["no_wrap"]
        table.add_column(str(col["header"]), **kwargs)

    if rows:
        for row in rows:
            table.add_row(*[_stringify(v) for v in row])

    console.print(table)


def _render_plain_table(
    rows: list[list[Any]],
    columns: list[dict[str, Any]],
    stream: IO[str],
    *,
    title: str | None,
) -> None:
    """Render a fixed-width ASCII table without ANSI escapes."""
    headers = [str(c["header"]) for c in columns]
    string_rows = [[_stringify(v) for v in row] for row in rows]

    # Compute per-column widths (header + every row value).
    widths = [len(h) for h in headers]
    for row in string_rows:
        for i, cell in enumerate(row):
            if len(cell) > widths[i]:
                widths[i] = len(cell)

    if title:
        stream.write(f"{title}\n")

    if not headers:
        # No columns at all — still emit a recognizable empty marker so
        # downstream tooling can tell rendering ran.
        stream.write("(no columns)\n")
        return

    def _format_row(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[i]) for i, value in enumerate(values))

    stream.write(_format_row(headers).rstrip() + "\n")
    stream.write("  ".join("-" * w for w in widths).rstrip() + "\n")

    if not string_rows:
        # Empty table: header + separator only. Matches the spirit of the
        # "Empty rows list renders an empty table" acceptance criterion —
        # we still tell the operator what columns *would* have appeared.
        return

    for row in string_rows:
        stream.write(_format_row(row).rstrip() + "\n")


def _json_value(value: Any) -> Any:
    """Coerce *value* into a JSON-serializable form.

    Most cells will already be primitives. Non-primitives fall back to
    ``str(value)`` so we never blow up on a stray ``Path`` or ``datetime``.
    ``None`` is preserved (becomes JSON ``null``) rather than coerced to
    ``"-"`` — JSON consumers prefer typed nulls.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    return str(value)
