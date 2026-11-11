"""Create a new pinned host profile; activation stays in the dedicated user's console."""

import os
import re
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from repowatch.adapters.native_client import PROTOCOL, windows_process_owner
from repowatch.adapters.native_workspaces import native_source_path, plain_path
from repowatch.domain.task_records import canonical_json, content_digest

ASSETS = (
    "host.ps1",
    "command.ps1",
    "kernel.cs",
    "qualification.ps1",
    "security_probe.cs",
    "crash_owner.ps1",
    "capture_qualification.ps1",
)


def runtime_sources(python_root):
    """Select the stdlib toolchain, excluding installed applications and dependencies."""
    python_root = Path(python_root)
    plain_path(python_root, directory=True)
    excluded = {
        "site-packages",
        "__pycache__",
        "test",
        "tests",
        "idlelib",
        "turtledemo",
        "ensurepip",
    }
    selected = {}
    for name in ("python.exe", "python3.dll"):
        path = python_root / name
        plain_path(path, directory=False)
        selected["python/" + name] = path
    for path in python_root.glob("*.dll"):
        plain_path(path, directory=False)
        selected["python/" + path.name] = path
    for name in ("DLLs", "Lib"):
        start = python_root / name
        plain_path(start, directory=True)
        for directory, dirs, files in os.walk(start, followlinks=False):
            dirs[:] = [value for value in dirs if value.casefold() not in excluded]
            for value in dirs:
                plain_path(Path(directory) / value, directory=True)
            for value in files:
                source = Path(directory) / value
                plain_path(source, directory=False)
                if source.suffix.casefold() in {".pyc", ".pyo"}:
                    continue
                relative = native_source_path(
                    "python/" + source.relative_to(python_root).as_posix()
                )
                selected[relative] = source
    if not any(re.fullmatch(r"python/python3\d+\.dll", name) for name in selected):
        raise ValueError("需要完整的 CPython Windows 标准库工具链")
    return selected


def prepare(root, *, execution_sid, python_root, control_url):
    if os.name != "nt":
        raise PermissionError("首批原生宿主要求 Windows")
    root = Path(root)
    if not root.is_absolute() or str(root).startswith(("\\\\", "//")) or root.exists():
        raise ValueError("原生宿主必须使用全新的本地绝对目录")
    if not re.fullmatch(r"S-1-5-21-(?:\d+-){3}\d+", execution_sid):
        raise ValueError("需要专用本地账户的明确 SID")
    url = urlsplit(control_url)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or not url.port
        or url.path != "/api/v1/state"
        or url.query
        or url.fragment
        or url.username
    ):
        raise ValueError("准入须绑定实际本地控制 API 的 /api/v1/state")
    sources = runtime_sources(python_root)
    if (
        len(sources) > 10000
        or sum(path.stat().st_size for path in sources.values()) > 256 * 1024 * 1024
    ):
        raise ValueError("工具链超过首批原生宿主限制")
    assets_root = Path(__file__).resolve().parents[1] / "_native"
    for name in (*ASSETS, "prepare.ps1"):
        plain_path(assets_root / name, directory=False)
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(assets_root / "prepare.ps1"),
            "-Root",
            str(root),
            "-ExecutionSid",
            execution_sid,
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    runtime = {}
    for relative, source in sources.items():
        target = root / "runtime" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        runtime[relative] = sha256(target.read_bytes()).hexdigest()
    assets = {}
    for name in ASSETS:
        target = root / "host" / name
        shutil.copyfile(assets_root / name, target)
        assets[name] = sha256(target.read_bytes()).hexdigest()
    profile = {
        "protocol": PROTOCOL,
        "profile_id": uuid4().hex,
        "controller_sid": windows_process_owner(os.getpid())["sid"],
        "execution_sid": execution_sid,
        "control_url": control_url,
        "runtime_files": runtime,
        "runtime_id": "sha256:" + content_digest(runtime),
        "tools": {"python": "python/python.exe", "python3": "python/python.exe"},
        "assets": assets,
    }
    (root / "profile.json").write_text(canonical_json(profile), encoding="utf-8")
    (root / "native-host.cmd").write_text(
        '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass '
        '-File "%~dp0host\\host.ps1" -AllowWorkspaceExecution\r\n',
        encoding="ascii",
    )
    return {
        "prepared": True,
        "admitted": False,
        "host_root": str(root),
        "runtime_id": profile["runtime_id"],
        "runtime_files": len(runtime),
        "launcher": str(root / "native-host.cmd"),
    }
