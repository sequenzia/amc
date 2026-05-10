"""Shared regex constants for message ids.

Copied (not imported) from ``amc/core/envelope.py`` so the wrapper has zero
runtime dependency on the adapter package and stays free of platform code.
"""

from __future__ import annotations

from typing import Final

# ULID body: 26 chars from Crockford base32 (no I, L, O, U).
ULID_BODY: Final[str] = r"^[0-9A-HJKMNP-TV-Z]{26}$"

# Full message id pattern used across the spec: ``msg_<ULID>``.
MESSAGE_ID: Final[str] = r"^msg_[0-9A-HJKMNP-TV-Z]{26}$"

__all__ = ["MESSAGE_ID", "ULID_BODY"]
