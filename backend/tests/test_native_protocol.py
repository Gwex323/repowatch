import json
import os
import subprocess
from pathlib import Path

import pytest

import repowatch


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell native protocol")
def test_native_atomic_publication_can_replace_existing_state(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    script = tmp_path / "atomic.ps1"
    script.write_text(
        "param([string]$Commands,[string]$Directory)\n"
        "$ErrorActionPreference='Stop'\n"
        "$env:PSModulePath=Join-Path $PSHOME 'Modules'\n"
        ". $Commands\n"
        "$path=Join-Path $Directory 'state.json'\n"
        "Write-NativeAtomic $path @{phase=1}\n"
        "Write-NativeAtomic $path @{phase=2}\n"
        "Get-Content -LiteralPath $path -Raw\n",
        encoding="utf-8",
    )
    commands = Path(repowatch.__file__).parent / "_native/command.ps1"
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Commands",
            str(commands),
            "-Directory",
            str(directory),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout.decode("utf-8-sig")) == {"phase": 2}
    assert [entry.name for entry in directory.iterdir()] == ["state.json"]


@pytest.mark.skipif(os.name != "nt", reason="Windows junction and hardlink cleanup")
def test_native_task_cleanup_removes_links_without_changing_their_targets(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_bytes(b"must remain unchanged")
    task = tmp_path / "owned-task"
    task.mkdir()
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(task / "junction"), str(outside)],
        check=True,
        capture_output=True,
    )
    os.link(marker, task / "hardlink.txt")
    script = tmp_path / "cleanup.ps1"
    script.write_text(
        "param([string]$Commands,[string]$TaskDirectory)\n"
        "$ErrorActionPreference='Stop'\n"
        "$env:PSModulePath=Join-Path $PSHOME 'Modules'\n"
        ". $Commands\n"
        "Remove-NativeTaskTree $TaskDirectory\n",
        encoding="utf-8",
    )
    commands = Path(repowatch.__file__).parent / "_native/command.ps1"
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Commands",
            str(commands),
            "-TaskDirectory",
            str(task),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert not task.exists()
    assert marker.read_bytes() == b"must remain unchanged"
