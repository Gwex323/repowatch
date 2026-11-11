"""Controller-account kernel/copy/capture regression. Never admits a product host."""

import json
import os
import shutil
import subprocess
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from repowatch.adapters.native_client import PROTOCOL, windows_process_owner
from repowatch.adapters.native_workspaces import NativeWorkspaces
from repowatch.application.repair_evidence import file_manifest
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.operations.native_prepare import runtime_sources


def main():
    root = Path(__file__).resolve().parents[1] / ".cache" / ("native-command-" + uuid4().hex)
    for name in ("responses/logs", "responses/commands", "requests", "runtime"):
        (root / name).mkdir(parents=True, exist_ok=True)
    runtime = {}
    for relative, source in runtime_sources(Path("C:/Python314")).items():
        target = root / "runtime" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        runtime[relative] = sha256(target.read_bytes()).hexdigest()
    host = SimpleNamespace(workspace_root=root / "workspaces", runtime_id="sha256:" + content_digest(runtime),
                           require_ready=lambda: None, assert_workspace_idle=lambda _: None)
    program = b'''import pathlib, sys, json
pathlib.Path('sandbox-write.txt').write_text('allowed')
for value in sys.argv[1:]:
    try:
        pathlib.Path(value).read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('Outside read unexpectedly allowed')
print('native-command-pass')
'''
    objects = SimpleNamespace(entries=lambda *_: [{"mode": "100644", "path": "probe.py", "oid": "blob"}],
                              blob=lambda *_: program)
    backend = NativeWorkspaces(host, objects)
    workspace = backend.create("repo", "base", task_id="task", mode="check")
    marker = root / "outside.txt"
    marker.write_text("unchanged marker", encoding="utf-8")
    owner = windows_process_owner(os.getpid())["sid"]
    profile = {"controller_sid": owner, "runtime_id": host.runtime_id, "runtime_files": runtime,
               "tools": {"python": "python/python.exe"}}
    identifier, generation = uuid4().hex, uuid4().hex
    request = {"protocol": PROTOCOL, "request_id": identifier, "generation": generation,
               "payload": {"workspace_id": workspace["id"], "argv": ["python", "probe.py", str(marker),
                            str(host.workspace_root / workspace["id"] / "source/probe.py")],
                           "timeout_ms": 60000, "deadline_unix_ms": round((time.time() + 60) * 1000),
                           "manifest_sha256": content_digest(file_manifest(backend.snapshot(workspace, "task")))}}
    (root / "profile.json").write_text(canonical_json(profile), encoding="utf-8")
    (root / "probe-request.json").write_text(canonical_json(request), encoding="utf-8")
    kernel = Path(__file__).resolve().parents[1] / "backend/src/repowatch/_native"
    process = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                str(Path(__file__).with_suffix(".ps1")), "-Root", str(root), "-KernelRoot", str(kernel)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    bound_path = root / "responses/commands" / (identifier + ".bound.json")
    deadline = time.monotonic() + 90
    while process.poll() is None and time.monotonic() < deadline:
        if bound_path.exists():
            bound = json.loads(bound_path.read_text("utf-8-sig"))
            actual = windows_process_owner(bound["pid"])
            assert actual["created"] == bound["created"] and actual["appcontainer"] and not actual["elevated"]
            (root / "requests" / (identifier + ".start")).touch()
            break
        time.sleep(0.02)
    stdout, stderr = process.communicate(timeout=90)
    if process.returncode:
        raise RuntimeError(stderr.decode("utf-8", "replace"))
    result = json.loads(stdout.decode("utf-8-sig"))
    log = (root / "responses/logs" / result["log_file"]).read_bytes()
    assert result["exit_code"] == 0 and result["active_processes"] == 0, (result, log)
    assert b"native-command-pass" in log and not result["cleanup_pending"]
    assert marker.read_text("utf-8") == "unchanged marker"
    assert backend.read_source(workspace, "task", "probe.py") == program
    report = {"passed": True, "native_admitted": False, "scope": "controller-account command kernel regression",
              "checks": ["pinned_stdlib_runtime", "actual_appcontainer_token", "parent_verified_gate",
                         "isolated_copy_writable", "outside_and_source_denied", "original_source_unchanged",
                         "stdout_captured", "all_job_members_stopped", "temporary_tree_removed"],
              "result": result}
    (root / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "native_admitted": False, "report": str(root / "report.json")}))


if __name__ == "__main__":
    main()
