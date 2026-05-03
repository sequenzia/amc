"""Golden test for the ``attributedBody`` typedstream decoder.

Pins the production decoder
(``amc.connectors.imessage.reader.decode_attributed_body``) against a
text golden file so any future change to the decoder produces a clear,
reviewable diff in the test output.

Workflow:

1. Read the seeded ``attributedBody``-only row (``MSG_GUID_ATTRIB``)
   from ``tests/fixtures/chat.db`` whose body is a real
   NSAttributedString typedstream archive (built by
   ``tests.fixtures.build_chat_db.make_attributed_body``).
2. Decode the blob with the production decoder.
3. Compare the decoded text against
   ``tests/connectors/imessage/golden/attributed_body_text.txt``. If the
   golden does not exist yet, write it (so the first CI run on a fresh
   checkout self-bootstraps); otherwise assert byte-for-byte identity.

Edge cases covered alongside the golden assertion:

* **Empty NSString** — a typedstream archive whose embedded NSString
  carries zero UTF-8 bytes. The decoder must not crash; the current
  implementation returns ``None`` for that input (no decodable text)
  and we pin that behavior so later changes are deliberate.
* **Non-Latin Unicode** — multi-byte CJK / Cyrillic / emoji bodies
  round-trip through encode → decode.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from amc.connectors.imessage.reader import decode_attributed_body
from tests.fixtures.build_chat_db import (
    MSG_GUID_ATTRIB,
    ensure_chat_db,
    make_attributed_body,
)

# ---------------------------------------------------------------------------
# Golden file location
# ---------------------------------------------------------------------------

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
_GOLDEN_FILE = _GOLDEN_DIR / "attributed_body_text.txt"


def _read_fixture_attributed_body_blob() -> bytes:
    """Return the raw ``attributedBody`` blob from the seeded fixture row.

    We open the fixture chat.db read-only and look up the row by its
    stable GUID (``MSG_GUID_ATTRIB``) rather than by ROWID so the test
    is robust against fixture re-ordering.
    """
    db_path = ensure_chat_db()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute(
            "SELECT attributedBody FROM message WHERE guid = ?",
            (MSG_GUID_ATTRIB,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, (
        f"Fixture chat.db is missing the seeded attributedBody row "
        f"(guid={MSG_GUID_ATTRIB})."
    )
    blob = row[0]
    assert isinstance(blob, bytes) and len(blob) > 0, (
        "Seeded attributedBody column should be a non-empty BLOB."
    )
    return blob


# ---------------------------------------------------------------------------
# Golden assertion
# ---------------------------------------------------------------------------


def test_attributed_body_decode_matches_golden() -> None:
    """Decoded fixture text matches (or self-bootstraps) the golden file.

    On a fresh checkout where the golden file does not yet exist, this
    test writes it from the current decoder output. Subsequent runs
    assert byte-for-byte identity, so any decoder change surfaces as a
    clear diff in the failure message.
    """
    blob = _read_fixture_attributed_body_blob()
    decoded = decode_attributed_body(blob)

    assert decoded is not None, (
        "Production decoder returned None for the seeded fixture "
        "attributedBody blob; expected the embedded message text."
    )

    if not _GOLDEN_FILE.exists():
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        # Bytes-mode write so the golden file's on-disk encoding matches
        # what the decoder produces (UTF-8) without platform newline
        # translation.
        _GOLDEN_FILE.write_bytes(decoded.encode("utf-8"))

    expected = _GOLDEN_FILE.read_bytes().decode("utf-8")
    actual = decoded
    assert actual == expected, (
        "Decoded attributedBody text drifted from the golden file.\n"
        f"  golden file: {_GOLDEN_FILE}\n"
        f"  expected: {expected!r}\n"
        f"  actual:   {actual!r}\n"
        "If the decoder change is intentional, delete the golden file "
        "and re-run this test to regenerate it."
    )


# ---------------------------------------------------------------------------
# Edge case: empty NSString (zero-length text)
# ---------------------------------------------------------------------------


def test_attributed_body_decode_handles_empty_nsstring() -> None:
    """An archive whose embedded NSString is zero-length must not crash.

    ``make_attributed_body("")`` produces a well-formed typedstream
    archive with a single-byte length prefix of ``0x00``. The current
    production decoder treats that as "no decodable text" and returns
    ``None`` (the single-byte length-form branch requires
    ``0 < length < 0x81``). This test pins that behavior so any future
    decoder change for empty-string handling is deliberate, and proves
    the decoder does not crash on the input.
    """
    blob = make_attributed_body("")
    # Sanity check: the magic prefix is present so we're exercising the
    # main decode path, not the early-return on a non-typedstream blob.
    assert blob.startswith(b"\x04\x0bstreamtyped")
    result = decode_attributed_body(blob)
    assert result is None, (
        f"Empty-NSString archive should decode to None (no decodable "
        f"text); got {result!r}."
    )


# ---------------------------------------------------------------------------
# Edge case: non-Latin Unicode
# ---------------------------------------------------------------------------


def test_attributed_body_decode_handles_non_latin_unicode() -> None:
    """Multi-byte UTF-8 bodies round-trip through encode -> decode.

    Covers CJK (Japanese), Cyrillic, and an emoji body. Each input is
    short enough to fit the fixture builder's single-byte length form
    (<= 127 UTF-8 bytes).
    """
    cases = [
        "こんにちは",  # Japanese (15 UTF-8 bytes)
        "Привет, мир",  # Cyrillic + ASCII (20 UTF-8 bytes)
        "hello 🎉 world",  # ASCII + emoji (16 UTF-8 bytes)
        "café au lait",  # Latin-1 supplement
    ]
    for text in cases:
        blob = make_attributed_body(text)
        decoded = decode_attributed_body(blob)
        assert decoded == text, (
            f"Non-Latin Unicode round-trip failed for {text!r}: "
            f"got {decoded!r}."
        )
