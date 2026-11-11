import os
import subprocess
from hashlib import sha256
from types import SimpleNamespace

import pytest

from repowatch.adapters.native_workspaces import NativeWorkspaces, native_source_path


@pytest.fixture
def native(tmp_path):
    class Host:
        workspace_root = tmp_path / "workspaces"
        runtime_id = "sha256:" + "a" * 64
        ready = True
        busy = False

        def require_ready(self):
            if not self.ready:
                raise PermissionError("Host is not admitted")

        def assert_workspace_idle(self, _identifier):
            if self.busy:
                raise PermissionError("Command is not verified stopped")

    contents = {"app.py": b"print(2 + 3)\n", "tests/test_app.py": b"assert 2 + 3 == 5\n"}
    entries = [
        {"path": path, "mode": "100644", "oid": sha256(data).hexdigest()}
        for path, data in contents.items()
    ]
    blobs = {entry["oid"]: contents[entry["path"]] for entry in entries}
    objects = SimpleNamespace(entries=lambda *_: entries, blob=lambda _repo, oid: blobs[oid])
    host = Host()
    backend = NativeWorkspaces(host, objects)
    return SimpleNamespace(host=host, backend=backend, entries=entries, blobs=blobs)


def create(native, mode="implement"):
    return native.backend.create(
        "repo", "base", task_id="task", mode=mode, image=native.host.runtime_id
    )


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "C:x",
        "x\\y",
        "CON",
        "con .txt",
        "aux.py",
        "COM1.log",
        "x.",
        "x ",
        "x:stream",
        "中文.py",
        "foo/*",
        "a//b",
    ],
)
def test_native_paths_fail_closed_for_windows_aliases_and_escape(path):
    with pytest.raises(PermissionError):
        native_source_path(path)


def test_native_materialization_and_snapshot_preserve_actual_bytes_and_task_scope(native):
    workspace = create(native)
    assert workspace["ready"] and workspace["kind"] == "native"
    assert native.backend.read_source(workspace, "task", "app.py") == b"print(2 + 3)\n"
    native.backend.write_source(workspace, "task", "nested/fix.py", b"value = 5\n")
    files = native.backend.snapshot(workspace, "task")
    assert files["nested/fix.py"] == ("100644", b"value = 5\n")
    with pytest.raises(PermissionError, match="归属"):
        native.backend.read_source(workspace, "other-task", "app.py")
    with pytest.raises(PermissionError, match="登记身份"):
        native.backend.snapshot({**workspace, "mode": "review"}, "task")
    native.backend.delete_source(workspace, "task", "nested/fix.py")
    assert "nested/fix.py" not in native.backend.snapshot(workspace, "task")


@pytest.mark.parametrize("mode", ["scan", "review", "check"])
def test_native_read_only_roles_cannot_write_or_delete(native, mode):
    workspace = create(native, mode)
    with pytest.raises(PermissionError, match="处理工作区"):
        native.backend.write_source(workspace, "task", "app.py", b"changed")
    with pytest.raises(PermissionError, match="处理工作区"):
        native.backend.delete_source(workspace, "task", "app.py")


def test_native_freeze_updates_role_metadata_and_revokes_old_write_references(native):
    workspace = create(native)
    native.backend.write_source(workspace, "task", "app.py", b"fixed candidate\n")
    frozen = native.backend.freeze_read_only(workspace)
    assert frozen["mode"] == "review"
    assert native.backend.snapshot(frozen, "task")["app.py"] == ("100644", b"fixed candidate\n")
    with pytest.raises(PermissionError, match="处理工作区"):
        native.backend.write_source(frozen, "task", "app.py", b"late write")
    with pytest.raises(PermissionError, match="登记身份"):
        native.backend.write_source(workspace, "task", "app.py", b"stale write")


def test_native_runtime_and_live_command_guards_are_not_configuration_booleans(native):
    native.host.ready = False
    with pytest.raises(PermissionError, match="not admitted"):
        create(native)
    assert not native.host.workspace_root.exists()
    native.host.ready = True
    with pytest.raises(PermissionError, match="运行时摘要"):
        native.backend.create("repo", "base", task_id="task", mode="scan", image="docker-image")
    workspace = create(native)
    native.host.busy = True
    for operation in (
        lambda: native.backend.snapshot(workspace, "task"),
        lambda: native.backend.write_source(workspace, "task", "app.py", b"changed"),
        lambda: native.backend.remove(workspace),
    ):
        with pytest.raises(PermissionError, match="verified stopped"):
            operation()


def test_native_external_objects_and_case_collisions_do_not_materialize(native):
    native.entries += [
        {"path": "App.py", "mode": "100644", "oid": native.entries[0]["oid"]},
        {"path": "symlink", "mode": "120000", "oid": "link"},
        {"path": "vendor/submodule", "mode": "160000", "oid": "module"},
    ]
    result = create(native)
    assert not result["ready"]
    assert {row["reason"] for row in result["gaps"]} == {
        "native_case_collision",
        "native_link_or_object_unsupported",
    }
    assert not native.host.workspace_root.exists()


def test_native_hardlink_is_never_read_or_used_for_cleanup(native, tmp_path):
    workspace = create(native)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"harmless marker")
    link = native.host.workspace_root / workspace["id"] / "source/linked.txt"
    os.link(outside, link)
    with pytest.raises(PermissionError, match="硬链接"):
        native.backend.read_source(workspace, "task", "linked.txt")
    with pytest.raises(PermissionError):
        native.backend.remove(workspace)
    assert outside.read_bytes() == b"harmless marker"
    link.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_native_junction_is_not_followed_by_snapshot_read_or_remove(native, tmp_path):
    workspace = create(native)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_bytes(b"harmless marker")
    junction = native.host.workspace_root / workspace["id"] / "source/junction"
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        for operation in (
            lambda: native.backend.read_source(workspace, "task", "junction/marker.txt"),
            lambda: native.backend.snapshot(workspace, "task"),
            lambda: native.backend.remove(workspace),
        ):
            with pytest.raises(PermissionError, match="重解析点"):
                operation()
        assert (outside / "marker.txt").read_bytes() == b"harmless marker"
    finally:
        # Remove only the known junction itself; never recurse into its target.
        os.rmdir(junction)


def test_native_cleanup_validates_registered_root_and_keeps_other_workspaces(native):
    first, second = create(native), create(native)
    with pytest.raises(PermissionError, match="身份无效"):
        native.backend.remove({**first, "id": "../other"})
    native.backend.remove(first)
    assert not (native.host.workspace_root / first["id"]).exists()
    assert native.backend.exists(second)
