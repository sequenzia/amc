"""Console-script entry point: ``amc-webhook-receiver``.

Mirrors the wiring the launchd run script uses (``uvicorn amc_receiver.app:app``)
so operators can run the service the same way out of the venv as launchd does.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("AMC_RECEIVER_BIND_HOST", "").strip() or "127.0.0.1"
    port = int(os.environ.get("AMC_RECEIVER_BIND_PORT", "").strip() or "8090")
    uvicorn.run("amc_receiver.app:app", host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
