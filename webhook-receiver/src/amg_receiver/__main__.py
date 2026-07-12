"""Console-script entry point: ``amg-webhook-receiver``.

Mirrors the wiring the launchd run script uses (``uvicorn amg_receiver.app:app``)
so operators can run the service the same way out of the venv as launchd does.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("AMG_RECEIVER_BIND_HOST", "").strip() or "127.0.0.1"
    port = int(os.environ.get("AMG_RECEIVER_BIND_PORT", "").strip() or "8090")
    uvicorn.run("amg_receiver.app:app", host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
