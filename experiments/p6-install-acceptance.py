"""Execute with an installed wheel's interpreter, without editable source imports."""

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import repowatch
from repowatch.configuration import StorageSettings
from repowatch.operations.launcher import (
    frontend_directory,
    request_stop,
    start,
    status,
    write_json,
)
from repowatch.operations.maintenance import Maintenance, restore
from repowatch.operations.migrate import migrate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve() / uuid4().hex
    if (
        not Path(repowatch.__file__)
        .resolve()
        .is_relative_to(Path(sys.prefix).resolve())
    ):
        raise AssertionError("必须使用实际安装的 wheel，不得回到源码或其他环境")
    if "_frontend" not in frontend_directory().parts:
        raise AssertionError("安装包必须包含生产前端")
    settings = StorageSettings(root / "data")
    migrate(settings)
    from repowatch.api.host import HostServices
    from repowatch.operations.reconcile import RestoreReconciliation

    services = HostServices(settings)
    try:
        backup = Maintenance(services).backup("installed-backup")
    finally:
        services.close()
    restored = StorageSettings(root / "restored")
    result = restore(settings.data_dir / "backups" / backup["id"], restored)
    assert result["paused"] is True
    services = HostServices(restored)
    try:
        reconciliation = RestoreReconciliation(services)
        report = reconciliation.report("installed-report")
        reconciliation.acknowledge(
            "installed-ack",
            report["report_id"],
            report["basis_sha256"],
            explanation="reviewed empty clean installation",
        )
        with services.store.read() as records:
            assert records.one("global_control", singleton=1)["paused"] == 1
    finally:
        services.close()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        running = start(restored, port=port, browser=False)
        assert running["state"] == "running"
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", trust_env=False
        ) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/v1/state").status_code == 401
            assert client.get("/api/v1/health").json()["stage"] == "p6"
    finally:
        request_stop(restored)
        deadline = time.monotonic() + 50
        while status(restored).get("supervisor_alive") and time.monotonic() < deadline:
            time.sleep(0.1)
    assert status(restored)["clean_stop"] is True
    migrate(restored)
    output = {
        "passed": True,
        "checks": [
            "installed_import",
            "packaged_frontend",
            "explicit_migration",
            "consistent_backup",
            "restore_pause",
            "read_only_reconciliation",
            "explicit_acknowledgement",
            "api_worker_start",
            "static_http",
            "unauthenticated_control_refusal",
            "clean_stop",
            "post_stop_migration",
        ],
        "external_models": False,
        "github_writes": False,
    }
    write_json(root / "report.json", output)
    print(
        json.dumps(
            {"passed": True, "report": str(root / "report.json")}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
