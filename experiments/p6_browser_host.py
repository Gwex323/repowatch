"""Temporary P6 maintenance UI host; no model or remote GitHub requests."""

import argparse
import json
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.application.task_records import block
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = StorageSettings(args.data_dir.resolve())
    migrate(settings)
    services = HostServices(
        settings,
        vault=SimpleNamespace(read=lambda _: None),
        transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(AssertionError("no model"))
        ),
    )
    for name, state in (("active", "queued"), ("finished", "ended")):
        repository = services.tasks.register_repository(
            "p6-" + name, admission_mode="manual"
        )
        task = services.tasks.ensure_task(repository, name, now=0)
        path = settings.artifacts_dir / (name + ".bin")
        path.write_bytes(b"controlled P6 evidence")
        import os

        os.utime(path, (0, 0))
        with services.store.write() as records:
            records.change("tasks", {"id": task}, {"state": state})
            records.add(
                "runs",
                {
                    "id": name,
                    "task_id": task,
                    "budget_owner_task_id": task,
                    "generation": 1,
                    "snapshot_json": "{}",
                    "state": "ended",
                    "ended_at": time.time() - 90 * 86400,
                },
            )
            records.add(
                "artifacts",
                {
                    "id": name,
                    "run_id": name,
                    "relative_path": path.name,
                    "sha256": sha256(path.read_bytes()).hexdigest(),
                    "context_json": "{}",
                },
            )
    origin = f"http://127.0.0.1:{args.port}"
    with services.store.write() as records:
        block(records, "backup_restore_reconciliation", None, now=time.time())
    sessions = LocalSessions(origin)
    packaged = Path(__import__("repowatch").__file__).parent / "_frontend"
    static = packaged if packaged.is_dir() else Path("frontend/dist").resolve()
    app = create_app(services, sessions=sessions, static_dir=static)
    print(json.dumps({"bootstrap": sessions.bootstrap_token}), flush=True)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
