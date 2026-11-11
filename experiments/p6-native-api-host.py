"""Temporary real control API for native boundary qualification; no model or remote calls."""

import argparse
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate


def refuse(*_args, **_kwargs):
    raise AssertionError("Native API qualification has no credentials or remote access")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--session-seconds", type=int, default=1800)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if not 30 <= args.session_seconds <= 14400:
        raise ValueError("Temporary qualification API lifetime must be within 30..14400 seconds")
    if not 0 <= args.port <= 65535:
        raise ValueError("Invalid temporary qualification API port")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    settings = StorageSettings(root / "data")
    migrate(settings)
    services = HostServices(
        settings,
        vault=SimpleNamespace(read=refuse),
        transport=httpx.MockTransport(refuse),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", args.port))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(services, sessions=LocalSessions(f"http://127.0.0.1:{port}")),
            log_config=None,
            access_log=False,
        )
    )
    (root / "host.json").write_text(
        json.dumps(
            {"port": port, "control_url": f"http://127.0.0.1:{port}/api/v1/state"}
        ),
        encoding="utf-8",
    )

    def stop_when_requested():
        deadline = time.monotonic() + args.session_seconds
        while time.monotonic() < deadline and not (root / "stop").exists():
            time.sleep(0.1)
        server.should_exit = True

    threading.Thread(target=stop_when_requested, daemon=True).start()
    print(json.dumps({"native_api_port": port}), flush=True)
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
        services.close()


if __name__ == "__main__":
    main()
