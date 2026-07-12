"""Tests for amg.core.logging — JSON output + redaction + rotation + fallback."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import logging.handlers
import os
from pathlib import Path

import pytest
import structlog

from amg.core import logging as amg_logging
from amg.core.logging import (
    DEFAULT_LOG_DIR,
    LOG_FILE_PREFIX,
    REDACTED,
    RedactingProcessor,
    configure_logging,
    install_request_logging,
    resolve_log_dir,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_logging():
    """Snapshot + restore root logger / structlog config across tests."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_uvicorn_handlers = list(logging.getLogger("uvicorn.access").handlers)
    saved_uvicorn_propagate = logging.getLogger("uvicorn.access").propagate
    yield
    # Close any handlers we created so file descriptors are released on macOS.
    for h in list(root.handlers):
        with contextlib.suppress(Exception):
            h.close()
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)
    uv = logging.getLogger("uvicorn.access")
    for h in list(uv.handlers):
        uv.removeHandler(h)
    for h in saved_uvicorn_handlers:
        uv.addHandler(h)
    uv.propagate = saved_uvicorn_propagate
    structlog.reset_defaults()


def _read_log_lines(log_dir: Path) -> list[dict]:
    """Read every JSON line written into the log dir (today's adapter file)."""
    files = sorted(log_dir.glob(f"{LOG_FILE_PREFIX}-*.log"))
    assert files, f"no log files found in {log_dir}"
    lines: list[dict] = []
    for f in files:
        for raw in f.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            lines.append(json.loads(raw))
    return lines


# ---------------------------------------------------------------------------
# resolve_log_dir
# ---------------------------------------------------------------------------


def test_resolve_log_dir_uses_default_when_unset():
    result = resolve_log_dir(env={})
    assert result == Path(DEFAULT_LOG_DIR).expanduser()
    assert result.is_absolute()


def test_resolve_log_dir_honors_env_var(tmp_path):
    result = resolve_log_dir(env={"AMG_LOG_DIR": str(tmp_path)})
    assert result == tmp_path


def test_resolve_log_dir_expands_tilde():
    result = resolve_log_dir(env={"AMG_LOG_DIR": "~/somewhere"})
    assert "~" not in str(result)
    assert result.is_absolute()


# ---------------------------------------------------------------------------
# configure_logging — directory + file creation
# ---------------------------------------------------------------------------


def test_configure_logging_creates_log_directory(tmp_path):
    log_dir = tmp_path / "logs" / "nested"
    info = configure_logging(log_dir=log_dir)
    assert log_dir.exists() and log_dir.is_dir()
    assert info["fallback"] is False
    assert info["log_dir"] == log_dir


def test_configure_logging_writes_to_configured_directory(tmp_path):
    info = configure_logging(log_dir=tmp_path)
    log = structlog.get_logger("amg.test")
    log.info("hello", foo="bar")
    info["file_handler"].flush()

    files = list(tmp_path.glob(f"{LOG_FILE_PREFIX}-*.log"))
    assert len(files) == 1
    assert files[0].name.startswith(f"{LOG_FILE_PREFIX}-")
    assert files[0].name.endswith(".log")


# ---------------------------------------------------------------------------
# JSON shape
# ---------------------------------------------------------------------------


def test_every_log_line_is_valid_json_with_standard_shape(tmp_path):
    info = configure_logging(log_dir=tmp_path)
    log = structlog.get_logger("amg.test")
    log.info("startup", component="adapter")
    log.warning("close call", component="webhook", retries=3)
    info["file_handler"].flush()

    lines = _read_log_lines(tmp_path)
    assert len(lines) == 2
    for entry in lines:
        # Standard shape required keys.
        assert "ts" in entry
        assert "level" in entry
        assert "event" in entry
        # Each kwarg is preserved at the top level.
    assert lines[0]["event"] == "startup"
    assert lines[0]["component"] == "adapter"
    assert lines[0]["level"] == "info"
    assert lines[1]["component"] == "webhook"
    assert lines[1]["retries"] == 3


def test_exception_logging_uses_format_exc_info(tmp_path):
    info = configure_logging(log_dir=tmp_path)
    log = structlog.get_logger("amg.test")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.error("crashed", exc_info=True)
    info["file_handler"].flush()

    [entry] = _read_log_lines(tmp_path)
    assert entry["event"] == "crashed"
    assert entry["level"] == "error"
    assert "exception" in entry
    assert "RuntimeError: boom" in entry["exception"]


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "authorization",
        "X-AMG-Signature",
        "x-amg-signature",
        "password",
        "bearer",
        "AMG_BEARER_TOKEN",
        "discord_bot_token",
        "webhook_secret",
        "api_secret",
    ],
)
def test_redactor_scrubs_secret_keys_top_level(tmp_path, key):
    info = configure_logging(log_dir=tmp_path)
    log = structlog.get_logger("amg.test")
    log.info("auth", **{key: "supersecret-value"})
    info["file_handler"].flush()

    [entry] = _read_log_lines(tmp_path)
    assert entry[key] == REDACTED
    assert "supersecret-value" not in json.dumps(entry)


def test_redactor_scrubs_authorization_and_signature_in_headers_dict(tmp_path):
    info = configure_logging(log_dir=tmp_path)
    log = structlog.get_logger("amg.test")
    headers = {
        "Authorization": "Bearer abc123",
        "X-AMG-Signature": "sha256=deadbeef",
        "User-Agent": "amg/0.1",
    }
    log.info("incoming", headers=headers)
    info["file_handler"].flush()

    [entry] = _read_log_lines(tmp_path)
    h = entry["headers"]
    assert h["Authorization"] == REDACTED
    assert h["X-AMG-Signature"] == REDACTED
    assert h["User-Agent"] == "amg/0.1"
    raw = json.dumps(entry)
    assert "Bearer abc123" not in raw
    assert "deadbeef" not in raw


def test_redactor_walks_nested_dicts_recursively(tmp_path):
    info = configure_logging(log_dir=tmp_path)
    log = structlog.get_logger("amg.test")
    payload = {
        "request": {
            "headers": {
                "Authorization": "Bearer abc123",
                "Content-Type": "application/json",
            },
            "body": {
                "user": "alice",
                "api_token": "tok-deep-1",
                "nested": {"webhook_secret": "shh", "ok": True},
            },
        }
    }
    log.info("forwarded", payload=payload)
    info["file_handler"].flush()

    [entry] = _read_log_lines(tmp_path)
    p = entry["payload"]
    assert p["request"]["headers"]["Authorization"] == REDACTED
    assert p["request"]["headers"]["Content-Type"] == "application/json"
    assert p["request"]["body"]["api_token"] == REDACTED
    assert p["request"]["body"]["nested"]["webhook_secret"] == REDACTED
    assert p["request"]["body"]["nested"]["ok"] is True
    assert p["request"]["body"]["user"] == "alice"
    raw = json.dumps(entry)
    assert "Bearer abc123" not in raw
    assert "tok-deep-1" not in raw
    assert "shh" not in raw


def test_redactor_scans_lists_of_header_dicts(tmp_path):
    info = configure_logging(log_dir=tmp_path)
    log = structlog.get_logger("amg.test")
    headers_list = [
        {"name": "Authorization", "value": "Bearer xyz"},
        {"name": "X-AMG-Signature", "value": "sha256=cafefood"},
        {"name": "Accept", "value": "*/*"},
        {"password": "p4ss"},
    ]
    log.info("seen", headers=headers_list)
    info["file_handler"].flush()

    [entry] = _read_log_lines(tmp_path)
    raw = json.dumps(entry)
    # `name`/`value` aren't secret keys; redaction must instead come from
    # secret keys themselves (`password`). The Authorization header VALUE
    # remains visible because it sits under a non-secret `value` key — the
    # spec says we redact by FIELD NAME. Documented behavior: lists of
    # dicts are walked, and any dict-key matching a secret pattern is
    # redacted. Verify both conditions:
    assert entry["headers"][3]["password"] == REDACTED
    assert "p4ss" not in raw
    # And the non-secret keys are preserved (list traversal works at all).
    assert entry["headers"][2]["value"] == "*/*"


def test_redactor_processor_unit_top_level_keys():
    proc = RedactingProcessor()
    out = proc(None, "info", {"event": "x", "Authorization": "B abc", "ok": 1})
    assert out["Authorization"] == REDACTED
    assert out["ok"] == 1
    assert out["event"] == "x"


def test_redactor_processor_unit_nested():
    proc = RedactingProcessor()
    out = proc(
        None,
        "info",
        {"event": "x", "ctx": {"discord_bot_token": "t", "user": "a"}},
    )
    assert out["ctx"]["discord_bot_token"] == REDACTED
    assert out["ctx"]["user"] == "a"


# ---------------------------------------------------------------------------
# Daily rotation
# ---------------------------------------------------------------------------


def test_daily_rotation_handler_configured_for_midnight(tmp_path):
    info = configure_logging(log_dir=tmp_path)
    h = info["file_handler"]
    assert isinstance(h, logging.handlers.TimedRotatingFileHandler)
    assert h.when == "MIDNIGHT"
    assert h.interval == 60 * 60 * 24
    assert h.backupCount == 14


def test_daily_rotation_produces_new_file(tmp_path):
    """Force a rotation by calling doRollover() — handler creates a fresh file."""
    info = configure_logging(log_dir=tmp_path)
    log = structlog.get_logger("amg.test")
    log.info("before-rotation")
    info["file_handler"].flush()
    before_files = set(tmp_path.glob(f"{LOG_FILE_PREFIX}-*"))
    assert len(before_files) == 1

    info["file_handler"].doRollover()

    log.info("after-rotation")
    info["file_handler"].flush()
    after_files = set(tmp_path.glob(f"{LOG_FILE_PREFIX}-*"))
    # Rotation produces an additional historical file (suffix-dated rename).
    assert len(after_files) >= 2


# ---------------------------------------------------------------------------
# Fallback when log dir is unwritable
# ---------------------------------------------------------------------------


def test_fallback_to_stdout_when_log_dir_unwritable(tmp_path, monkeypatch):
    """Make mkdir raise so the file handler can't be built."""
    bogus = tmp_path / "cannot-create"

    real_mkdir = Path.mkdir

    def _raise_mkdir(self, *args, **kwargs):
        if self == bogus or bogus in self.parents:
            raise PermissionError("denied for test")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _raise_mkdir)

    err = io.StringIO()
    out = io.StringIO()
    info = configure_logging(log_dir=bogus, stderr=err, stdout=out)
    log = structlog.get_logger("amg.test")
    log.info("post-fallback", phase="boot")

    assert info["fallback"] is True
    err_text = err.getvalue()
    assert "log directory unwritable" in err_text
    assert "stdout" in err_text.lower()

    # Output went to stdout as JSON.
    out_text = out.getvalue()
    assert out_text, "expected fallback stdout output"
    last = json.loads(out_text.strip().splitlines()[-1])
    assert last["event"] == "post-fallback"
    assert last["phase"] == "boot"
    assert last["level"] == "info"
    assert "ts" in last


# ---------------------------------------------------------------------------
# AMG_LOG_DIR env integration
# ---------------------------------------------------------------------------


def test_configure_logging_picks_up_amg_log_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AMG_LOG_DIR", str(tmp_path))
    info = configure_logging()
    assert info["log_dir"] == tmp_path


# ---------------------------------------------------------------------------
# install_request_logging — middleware & uvicorn muting
# ---------------------------------------------------------------------------


def test_install_request_logging_mutes_uvicorn_access_logger(tmp_path):
    configure_logging(log_dir=tmp_path)
    # Pre-populate uvicorn.access with a sentinel handler we expect to be cleared.
    sentinel = logging.NullHandler()
    logging.getLogger("uvicorn.access").addHandler(sentinel)
    logging.getLogger("uvicorn.access").propagate = True

    class _StubApp:
        def __init__(self):
            self.middlewares = []

        def middleware(self, kind):
            def deco(fn):
                self.middlewares.append((kind, fn))
                return fn

            return deco

    app = _StubApp()
    install_request_logging(app)
    assert ("http", app.middlewares[0][0]) == ("http", "http")
    uv = logging.getLogger("uvicorn.access")
    assert uv.handlers == []
    assert uv.propagate is False


async def test_request_middleware_emits_structured_log(tmp_path):
    info = configure_logging(log_dir=tmp_path)

    captured: list = []

    class _StubApp:
        def middleware(self, kind):
            def deco(fn):
                captured.append(fn)
                return fn

            return deco

    install_request_logging(_StubApp())
    middleware = captured[0]

    class _Req:
        method = "POST"

        class url:
            path = "/messages/send"

    class _Resp:
        status_code = 202

    async def call_next(_req):
        return _Resp()

    response = await middleware(_Req(), call_next)
    assert response.status_code == 202

    info["file_handler"].flush()
    [entry] = _read_log_lines(tmp_path)
    assert entry["event"] == "request"
    assert entry["method"] == "POST"
    assert entry["path"] == "/messages/send"
    assert entry["status"] == 202
    assert isinstance(entry["duration_ms"], int | float)
    assert entry["duration_ms"] >= 0


async def test_request_middleware_records_500_on_exception(tmp_path):
    info = configure_logging(log_dir=tmp_path)

    captured: list = []

    class _StubApp:
        def middleware(self, kind):
            def deco(fn):
                captured.append(fn)
                return fn

            return deco

    install_request_logging(_StubApp())
    middleware = captured[0]

    class _Req:
        method = "GET"

        class url:
            path = "/boom"

    async def call_next(_req):
        raise RuntimeError("crash")

    with pytest.raises(RuntimeError):
        await middleware(_Req(), call_next)

    info["file_handler"].flush()
    [entry] = _read_log_lines(tmp_path)
    assert entry["event"] == "request"
    assert entry["status"] == 500
    assert entry["path"] == "/boom"


# ---------------------------------------------------------------------------
# Sanity: module exposes expected names
# ---------------------------------------------------------------------------


def test_module_public_api():
    assert hasattr(amg_logging, "configure_logging")
    assert hasattr(amg_logging, "install_request_logging")
    assert hasattr(amg_logging, "RedactingProcessor")
    assert hasattr(amg_logging, "resolve_log_dir")
    assert amg_logging.DEFAULT_LOG_DIR == "~/Library/Logs/messaging-agent"
    assert amg_logging.REDACTED == "[REDACTED]"


def test_amg_log_dir_default_matches_spec():
    """Spec §11.3 — logs land at ~/Library/Logs/messaging-agent."""
    # Simulate fresh env without AMG_LOG_DIR.
    env = {k: v for k, v in os.environ.items() if k != "AMG_LOG_DIR"}
    result = resolve_log_dir(env=env)
    assert result == Path("~/Library/Logs/messaging-agent").expanduser()
