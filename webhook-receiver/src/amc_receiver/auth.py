"""HMAC-SHA256 verification for inbound AMC webhooks.

The adapter signs each delivery as ``X-AMC-Signature: sha256=<hex>`` over
the **raw request body bytes** (see ``amc/core/webhook.py:200-210, 526-527``).
Verification therefore must run against the bytes pulled off the wire
*before* JSON parsing — re-serializing JSON is not stable.

Returns
-------
``verify_signature`` returns ``True`` on a valid signature and ``False`` on
any structural problem (missing/wrong scheme/wrong-length hex/mismatch).
The caller decides the HTTP status; a 4xx tells the adapter to dead-letter,
which is the right outcome for any signature failure (those are permanent,
not transient).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

__all__ = [
    "SIGNATURE_SCHEME",
    "compute_signature",
    "verify_signature",
]

SIGNATURE_SCHEME: Final[str] = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` value for ``X-AMC-Signature``."""
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError("body must be bytes; sign the raw request body, not a string")
    digest = hmac.new(secret.encode("utf-8"), bytes(body), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_SCHEME}{digest}"


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Constant-time check of ``X-AMC-Signature`` against ``body``.

    All failure modes (missing header, wrong prefix, wrong length, mismatch)
    return ``False``. Success returns ``True``. The check uses
    :func:`hmac.compare_digest` to avoid timing-side-channel leakage.
    """
    if not header_value:
        return False
    if not header_value.startswith(SIGNATURE_SCHEME):
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(header_value.encode("ascii"), expected.encode("ascii"))
