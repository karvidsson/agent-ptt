"""Container service entrypoint and dependency-free health probe."""

from __future__ import annotations

import os
import sys
import urllib.request


def serve():
    from sqlalchemy.engine import make_url

    from agent_ptt.workspace_auth import public_origin

    if os.environ.get("AGENT_PTT_HOSTED") != "1":
        raise ValueError("The production runtime requires AGENT_PTT_HOSTED=1")
    if not os.environ.get("AGENT_PTT_PUBLIC_ORIGIN"):
        raise ValueError("AGENT_PTT_PUBLIC_ORIGIN is required")
    public_origin()
    database = os.environ.get("DATABASE_URL", "")
    if not database or make_url(database).drivername != "postgresql+psycopg":
        raise ValueError("The production runtime requires a postgresql+psycopg DATABASE_URL")
    port = int(os.environ.get("PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    os.environ.setdefault("AGENT_PTT_SERVER_SPEECH", "1")
    # One process: rate limits, speech admission, and local queues are in-process.
    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "agent_ptt.server:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--workers",
            "1",
            "--timeout-graceful-shutdown",
            "30",
            "--no-proxy-headers",
        ],
    )


def probe():
    port = int(os.environ.get("PORT", "8080"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/health/ready", timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("Service is not ready")


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    elif sys.argv[1:] == ["probe"]:
        probe()
    else:
        raise SystemExit("Usage: python -m agent_ptt.runtime serve|probe")
