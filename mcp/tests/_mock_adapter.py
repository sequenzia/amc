"""In-process mock AMC adapter for the wrapper e2e harness.

Boots a tiny stdlib HTTP server on an ephemeral loopback port, with
queueable canned responses per path. Used by the stdio e2e test that
spawns the real ``amc-mcp`` binary and points it at the mock URL.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass
class CannedResponse:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class RecordedCall:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes


class MockAdapter:
    """A minimal pluggable HTTP server.

    Use :meth:`enqueue` to seed canned responses keyed by path. Each call
    pops the next response off the queue for that path. Calls without a
    canned response receive a 500 with the spec §7.4.12 envelope so the
    test fails loudly.
    """

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = 0
        self.calls: list[RecordedCall] = []
        self._queues: dict[str, list[CannedResponse]] = defaultdict(list)
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def enqueue(self, path: str, response: CannedResponse) -> None:
        with self._lock:
            self._queues[path].append(response)

    def start(self) -> None:
        adapter = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                # Silence the default stderr log; we have explicit assertions.
                return

            def _serve(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length > 0 else b""
                parsed = urlsplit(self.path)
                query: dict[str, list[str]] = {
                    k: v for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
                }
                headers = {k.lower(): v for k, v in self.headers.items()}
                with adapter._lock:
                    adapter.calls.append(
                        RecordedCall(
                            method=method,
                            path=parsed.path,
                            query=query,
                            headers=headers,
                            body=body,
                        )
                    )
                    queue = adapter._queues.get(parsed.path) or []
                    canned = queue.pop(0) if queue else None

                if canned is None:
                    fallback = json.dumps(
                        {
                            "error": {
                                "code": "INTERNAL_ERROR",
                                "message": (
                                    f"mock adapter: no canned response for {method} {parsed.path}"
                                ),
                            }
                        }
                    ).encode("utf-8")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(fallback)))
                    self.end_headers()
                    self.wfile.write(fallback)
                    return

                self.send_response(canned.status)
                response_headers = dict(canned.headers)
                response_headers.setdefault("Content-Type", "application/json")
                response_headers["Content-Length"] = str(len(canned.body))
                for k, v in response_headers.items():
                    self.send_header(k, v)
                self.end_headers()
                if canned.body:
                    self.wfile.write(canned.body)

            def do_GET(self) -> None:  # noqa: N802
                self._serve("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._serve("POST")

        self._server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mock-adapter",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


def json_canned(body: Any, status: int = 200) -> CannedResponse:
    return CannedResponse(
        status=status,
        body=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
