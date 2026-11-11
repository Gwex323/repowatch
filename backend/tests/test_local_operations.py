import json
import os
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from repowatch.api.host import HostServices
from repowatch.application.storage import ConflictError, StorageError
from repowatch.configuration import StorageSettings
from repowatch.operations.locks import FileLock, data_lock
from repowatch.operations.maintenance import Maintenance, restore, verify_backup
from repowatch.operations.migrate import migrate


@pytest.fixture
def services(tmp_path):
    settings = StorageSettings(tmp_path / "data")
    migrate(settings)
    vault = SimpleNamespace(read=lambda _: None)
    host = HostServices(settings, vault=vault, clock=lambda: time.time())
    try:
        yield host
    finally:
        host.close()


def test_migration_refuses_open_service_and_preserves_data(services):
    with pytest.raises(ConflictError):
        migrate(services.settings)
    with services.store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_cross_process_barrier_and_crash_release(tmp_path):
    path = tmp_path / "barrier.lock"
    source = """
import sys, time
from pathlib import Path
from repowatch.operations.locks import FileLock
with FileLock(Path(sys.argv[1]), exclusive=True):
    print('locked', flush=True)
    time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", source, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with pytest.raises(ConflictError):
            with FileLock(path, timeout=0):
                pass
    finally:
        child.terminate()
        child.wait(timeout=5)
    with FileLock(path, exclusive=True, timeout=0):
        pass


def test_barrier_prevents_sync_and_cleanup_transactions(services):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor() as pool:
        with data_lock(services.settings, exclusive=True):

            def write():
                with services.store.write() as records:
                    records.change("global_control", {"singleton": 1}, {"revision": 9})
                return True

            pending = pool.submit(write)
            time.sleep(0.1)
            assert not pending.done()
        assert pending.result(timeout=3)


def test_backup_wal_snapshot_and_restored_pause(services, tmp_path):
    repository = services.tasks.register_repository("backup", admission_mode="manual")
    maintenance = Maintenance(services)
    backup = maintenance.backup("consistent-1")
    assert maintenance.backup("consistent-1") == backup
    source = services.settings.data_dir / "backups" / backup["id"]
    manifest = verify_backup(source)
    assert "business.sqlite3" in manifest["files"]
    destination = StorageSettings(tmp_path / "restored")
    result = restore(source, destination)
    assert result["paused"]
    with sqlite3.connect(destination.business_database) as connection:
        assert connection.execute("SELECT id FROM repositories").fetchone() == (repository,)
        assert connection.execute("SELECT paused FROM global_control").fetchone() == (1,)
        assert connection.execute("SELECT code FROM block_reasons WHERE resolved=0").fetchone() == (
            "backup_restore_reconciliation",
        )
    with pytest.raises(ConflictError):
        restore(source, destination)


def test_corrupt_backup_does_not_initialize_restore_target(services, tmp_path):
    maintenance = Maintenance(services)
    backup = maintenance.backup("corruption")
    source = services.settings.data_dir / "backups" / backup["id"]
    with (source / "business.sqlite3").open("ab") as output:
        output.write(b"changed")
    target = StorageSettings(tmp_path / "not-restored")
    with pytest.raises(ValueError, match="摘要"):
        restore(source, target)
    assert not target.business_database.exists()


def test_backup_preserves_pause_revision(services):
    with services.store.read() as records:
        before = dict(records.one("global_control", singleton=1))
    Maintenance(services).backup("pause")
    with services.store.read() as records:
        assert dict(records.one("global_control", singleton=1)) == before


def test_backup_rejects_missing_available_artifact(services):
    repository = services.tasks.register_repository("missing", admission_mode="manual")
    task = services.tasks.ensure_task(repository, "source", now=0)
    with services.store.write() as records:
        records.add(
            "runs",
            {
                "id": "run",
                "task_id": task,
                "budget_owner_task_id": task,
                "generation": 1,
                "snapshot_json": "{}",
                "state": "ended",
            },
        )
        records.add(
            "artifacts",
            {
                "id": "missing",
                "run_id": "run",
                "relative_path": "missing.bin",
                "sha256": "a" * 64,
                "context_json": "{}",
            },
        )
    with pytest.raises(ValueError, match="证据缺失"):
        Maintenance(services).backup("missing")
    assert Maintenance(services).backups() == []


def test_low_disk_stops_execution_without_discarding_control(services, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(StorageError, match="磁盘"):
        services.store.assert_execution_available()
    with services.store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_policy_operations_are_versioned_and_idempotent(services):
    maintenance = Maintenance(services)
    policy = maintenance.configuration()["policy"]
    policy["artifact_days"] = 45
    result = maintenance.configure("policy", policy, expected_revision=1)
    assert maintenance.configure("policy", policy, expected_revision=1) == result
    with pytest.raises(ConflictError):
        maintenance.configure("stale", policy, expected_revision=1)
    with pytest.raises(ConflictError):
        maintenance.configure("policy", {**policy, "artifact_days": 1}, expected_revision=1)


def test_cleanup_protects_active_tasks_and_invalidates_finished_evidence(services):
    import hashlib

    repository = services.tasks.register_repository("retention", admission_mode="manual")
    active = services.tasks.ensure_task(repository, "active", now=0)
    finished_repo = services.tasks.register_repository("finished", admission_mode="manual")
    ended = services.tasks.ensure_task(finished_repo, "ended", now=0)
    with services.store.write() as records:
        records.change("tasks", {"id": ended}, {"state": "ended"})
        for name, task in (("active", active), ("ended", ended)):
            records.add(
                "runs",
                {
                    "id": name,
                    "task_id": task,
                    "budget_owner_task_id": task,
                    "generation": 1,
                    "snapshot_json": "{}",
                    "state": "ended",
                    "ended_at": 0,
                },
            )
            path = services.settings.artifacts_dir / (name + ".bin")
            path.write_bytes(b"evidence")
            os.utime(path, (0, 0))
            records.add(
                "artifacts",
                {
                    "id": name,
                    "run_id": name,
                    "relative_path": path.name,
                    "sha256": hashlib.sha256(b"evidence").hexdigest(),
                    "context_json": "{}",
                },
            )
    preview = Maintenance(services).cleanup()
    assert [row["id"] for row in preview["candidates"]] == ["ended"]
    assert (services.settings.artifacts_dir / "ended.bin").exists()
    Maintenance(services).cleanup(dry_run=False)
    assert (services.settings.artifacts_dir / "active.bin").is_file()
    assert not (services.settings.artifacts_dir / "ended.bin").exists()
    with services.store.read() as records:
        assert records.one("artifacts", id="ended")["available"] == 0
        assert records.one("artifacts", id="active")["available"] == 1


def test_malicious_manifest_path_is_rejected(services):
    backup = Maintenance(services).backup("path")
    root = services.settings.data_dir / "backups" / backup["id"]
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest["files"]["artifacts/../../outside"] = "a" * 64
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        verify_backup(root)


def test_unfinished_restore_cannot_be_started_or_migrated(services):
    from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
    from repowatch.application.storage import SchemaMismatch

    (services.settings.data_dir / ".restore-incomplete").touch()
    with pytest.raises(SchemaMismatch, match="恢复尚未完成"):
        SQLiteBusinessStore(services.settings)
    with pytest.raises(SchemaMismatch, match="恢复尚未完成"):
        migrate(services.settings)


def test_sdk_mismatch_refuses_migration_before_touching_data(tmp_path, monkeypatch):
    from repowatch.operations import maintenance

    actual = maintenance.version
    monkeypatch.setattr(
        maintenance, "version", lambda name: "0" if name == "langgraph" else actual(name)
    )
    settings = StorageSettings(tmp_path / "untouched")
    with pytest.raises(ValueError, match="SDK"):
        migrate(settings)
    assert not settings.data_dir.exists()


def test_two_migrators_serialize_without_destroying_data(tmp_path):
    settings = StorageSettings(tmp_path / "dual-migration")
    code = (
        "from pathlib import Path; from repowatch.configuration import StorageSettings; "
        "from repowatch.operations.migrate import migrate; import sys; "
        "migrate(StorageSettings(Path(sys.argv[1])))"
    )
    children = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(settings.data_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    for child in children:
        _, error = child.communicate(timeout=30)
        assert child.returncode == 0, error.decode("utf-8", errors="replace")
    with sqlite3.connect(settings.business_database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM global_control").fetchone() == (1,)


def test_uvicorn_access_formatter_preserves_structure_and_redacts_secrets():
    import logging

    from uvicorn.logging import AccessFormatter

    from repowatch.application.credentials import SecretValue
    from repowatch.security import RedactingLogFilter, Redactor

    secret = SecretValue("test-p6-controlled-key")
    redactor = Redactor()
    redactor.register(secret)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/?key=" + secret.reveal(), "1.1", 200),
        None,
    )
    RedactingLogFilter(redactor).filter(record)
    output = AccessFormatter("%(client_addr)s %(request_line)s %(status_code)s").format(record)
    assert secret.reveal() not in output
    assert "200" in output and "REDACTED" in output


def test_native_selection_never_falls_back_to_host_shell(services):
    from repowatch.operations.native import NativeConfiguration, require_backend

    with services.store.read() as records:
        require_backend(records, {})
        with pytest.raises(PermissionError, match="未显式启用"):
            require_backend(records, {"execution_backend": "native"})
    NativeConfiguration(services.store).configure("native-on", enabled=True, expected_revision=1)
    with services.store.read() as records:
        with pytest.raises(PermissionError, match="隔离身份"):
            require_backend(records, {"execution_backend": "native"})
        with pytest.raises(ValueError):
            require_backend(records, {"execution_backend": "powershell"})


@pytest.mark.skipif(os.name != "nt", reason="Windows 正式启停")
def test_real_windows_launcher_duplicate_start_and_clean_stop(tmp_path):

    import socket

    import httpx

    from repowatch.operations.launcher import request_stop, start, status

    settings = StorageSettings(tmp_path / "service-data")
    migrate(settings)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        result = start(settings, port=port, browser=False)
        assert result["state"] == "running"
        response = httpx.get(f"http://127.0.0.1:{port}/api/v1/health", trust_env=False)
        assert response.status_code == 200
        with pytest.raises(ConflictError):
            start(settings, port=port, browser=False)
        with pytest.raises(ConflictError):
            migrate(settings)
    finally:
        request_stop(settings)
        deadline = time.monotonic() + 50
        while status(settings).get("supervisor_alive") and time.monotonic() < deadline:
            time.sleep(0.1)
    assert status(settings)["clean_stop"] is True
    assert status(settings)["state"] == "stopped"
    migrate(settings)


@pytest.mark.skipif(os.name != "nt", reason="Windows 作业树异常退出")
def test_supervisor_death_stops_children_and_restart_requires_reconciliation(tmp_path):
    import ctypes
    import socket
    from ctypes import wintypes

    from repowatch.operations.launcher import identity_alive, request_stop, start, status

    settings = StorageSettings(tmp_path / "crash-data")
    migrate(settings)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        original = start(settings, port=port, browser=False)
        assert identity_alive(original["supervisor"])
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(1, False, original["supervisor"]["pid"])
        assert handle
        try:
            assert kernel.TerminateProcess(handle, 125)
        finally:
            kernel.CloseHandle(handle)
        deadline = time.monotonic() + 10
        while identity_alive(original["api"]) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not identity_alive(original["api"])
        assert not identity_alive(original["worker"])
        restarted = start(settings, port=port, browser=False)
        assert restarted["generation"] != original["generation"]
        from repowatch.adapters.storage.sqlite import SQLiteBusinessStore

        store = SQLiteBusinessStore(settings)
        try:
            with store.read() as records:
                assert records.one("global_control", singleton=1)["paused"] == 1
                assert records.find("block_reasons", code="service_exit_unconfirmed", resolved=0)
        finally:
            store.close()
    finally:
        request_stop(settings)
        deadline = time.monotonic() + 50
        while status(settings).get("supervisor_alive") and time.monotonic() < deadline:
            time.sleep(0.1)
