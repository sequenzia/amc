"""HMAC signature verify tests."""

from __future__ import annotations

from amg_receiver.auth import compute_signature, verify_signature

SECRET = "super-secret-test-key"  # noqa: S105 — test fixture
BODY = b'{"hello":"world"}'


def test_compute_signature_returns_sha256_prefixed_hex() -> None:
    sig = compute_signature(SECRET, BODY)
    assert sig.startswith("sha256=")
    # 64 hex chars = 32 bytes of SHA-256 digest.
    assert len(sig) == len("sha256=") + 64


def test_compute_signature_is_deterministic() -> None:
    assert compute_signature(SECRET, BODY) == compute_signature(SECRET, BODY)


def test_verify_accepts_valid_signature() -> None:
    sig = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY, sig) is True


def test_verify_rejects_missing_header() -> None:
    assert verify_signature(SECRET, BODY, None) is False
    assert verify_signature(SECRET, BODY, "") is False


def test_verify_rejects_wrong_scheme() -> None:
    sig = compute_signature(SECRET, BODY)
    munged = sig.replace("sha256=", "md5=", 1)
    assert verify_signature(SECRET, BODY, munged) is False


def test_verify_rejects_truncated_signature() -> None:
    sig = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, BODY, sig[:-2]) is False


def test_verify_rejects_signature_for_different_body() -> None:
    sig = compute_signature(SECRET, BODY)
    assert verify_signature(SECRET, b'{"hello":"WORLD"}', sig) is False


def test_verify_rejects_signature_for_different_secret() -> None:
    sig = compute_signature(SECRET, BODY)
    assert verify_signature("other-secret", BODY, sig) is False
