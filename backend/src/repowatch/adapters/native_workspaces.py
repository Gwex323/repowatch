"""Native source snapshots; execution is delegated to a separately admitted host port."""

import json
import os
import re
import shutil
import stat
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from repowatch.adapters.docker_workspaces import WorkspaceError, source_path
from repowatch.domain.task_records import canonical_json, content_digest

IDENTIFIER = re.compile(r"repowatch-native-[a-f0-9]{32}\Z")
RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_FILE = 16 * 1024 * 1024
MAX_SNAPSHOT = 64 * 1024 * 1024


class NativeHost(Protocol):
    workspace_root: Path
    runtime_id: str

    def require_ready(self) -> None: ...

    def assert_workspace_idle(self, identifier: str) -> None: ...


def native_source_path(value: str) -> str:
    value = source_path(value)
    for part in value.split("/"):
        if (
            not part.isascii()
            or part[-1] in {".", " "}
            or any(ord(char) < 32 or char in '<>"|?*' for char in part)
            or part.split(".", 1)[0].rstrip(" ").upper() in RESERVED
        ):
            raise PermissionError("当前原生适配不支持该 Windows 文件名")
    return value


def plain_path(path: Path, *, directory: bool | None = None, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return
        raise PermissionError("原生工作区路径缺失") from None
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise PermissionError("原生工作区不允许符号链接或重解析点")
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise PermissionError("原生工作区目录类型不符")
    if directory is False and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise PermissionError("原生工作区不允许特殊文件或硬链接")


class NativeWorkspaces:
    def __init__(self, host: NativeHost, objects):
        self.host, self.objects = host, objects
        self.root = Path(host.workspace_root)
        if not self.root.is_absolute():
            raise ValueError("原生工作区根目录必须是绝对路径")
        plain_path(self.root, directory=True, missing=True)
        self.root = self.root.resolve()
        self.default_image = host.runtime_id

    def _directory(self, identifier):
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise PermissionError("原生工作区身份无效")
        directory = self.root / identifier
        plain_path(self.root, directory=True)
        plain_path(directory, directory=True)
        return directory

    def _metadata(self, workspace, task_id=None):
        self.host.require_ready()
        self.host.assert_workspace_idle(workspace["id"])
        directory = self._directory(workspace["id"])
        metadata_path = directory / "workspace.json"
        plain_path(metadata_path, directory=False)
        metadata = json.loads(metadata_path.read_text("utf-8"))
        if metadata.get("ready") is not True or metadata.get("mode") not in {
            "scan",
            "review",
            "implement",
            "check",
        }:
            raise PermissionError("原生工作区状态或模式无效")
        if content_digest(metadata["manifest"]) != metadata["manifest_sha256"]:
            raise PermissionError("原生工作区清单摘要不匹配")
        for key in (
            "kind",
            "id",
            "task_id",
            "repository_id",
            "sha",
            "mode",
            "image",
            "manifest_sha256",
        ):
            if workspace.get(key) != metadata.get(key):
                raise PermissionError("原生工作区材料与登记身份不匹配")
        if metadata["kind"] != "native" or metadata["image"] != self.host.runtime_id:
            raise PermissionError("原生工作区运行时绑定已变化")
        if task_id is not None and metadata["task_id"] != task_id:
            raise PermissionError("工作区归属不符")
        plain_path(directory / "source", directory=True)
        return directory, metadata

    def _source(self, directory, value, *, missing=False):
        value = native_source_path(value)
        path = directory / "source"
        plain_path(path, directory=True)
        parts = value.split("/")
        for part in parts[:-1]:
            path = path / part
            plain_path(path, directory=True, missing=missing)
        path = path / parts[-1]
        plain_path(path, directory=False, missing=missing)
        return path

    def create(self, repository_id, sha, *, task_id, mode, image=None):
        self.host.require_ready()
        if mode not in {"scan", "review", "implement", "check"} or not task_id:
            raise ValueError("原生工作区需要任务归属和有效模式")
        if image is not None and image != self.host.runtime_id:
            raise PermissionError("原生运行时摘要不匹配")
        manifest, gaps, contents, seen = [], [], {}, set()
        total = 0
        for entry in self.objects.entries(repository_id, sha):
            record = dict(entry)
            path = record["path"]
            try:
                native_source_path(path)
            except PermissionError:
                gaps.append({"path": path, "reason": "native_path_unsupported"})
                manifest.append(record)
                continue
            folded = path.casefold()
            if folded in seen:
                gaps.append({"path": path, "reason": "native_case_collision"})
            seen.add(folded)
            if record["mode"] not in {"100644", "100755"}:
                gaps.append({"path": path, "reason": "native_link_or_object_unsupported"})
                manifest.append(record)
                continue
            content = self.objects.blob(repository_id, record["oid"])
            total += len(content)
            record.update(size=len(content), sha256=sha256(content).hexdigest())
            manifest.append(record)
            if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                gaps.append({"path": path, "reason": "lfs_entity_missing"})
            if len(content) > MAX_FILE or total > MAX_SNAPSHOT:
                gaps.append({"path": path, "reason": "native_snapshot_size_limit"})
            contents[path] = content
        if gaps:
            return {"ready": False, "gaps": gaps, "manifest": manifest}
        identifier = "repowatch-native-" + uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / identifier
        directory.mkdir()
        (directory / "source").mkdir()
        for path, content in contents.items():
            target = self._source(directory, path, missing=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        metadata = {
            "ready": True,
            "kind": "native",
            "id": identifier,
            "repository_id": repository_id,
            "task_id": task_id,
            "sha": sha,
            "mode": mode,
            "image": self.host.runtime_id,
            "image_reference": self.host.runtime_id,
            "manifest": manifest,
            "manifest_sha256": content_digest(manifest),
            "gaps": [],
        }
        (directory / "workspace.json").write_text(canonical_json(metadata), encoding="utf-8")
        return metadata

    def read_source(self, workspace, task_id, path):
        directory, _ = self._metadata(workspace, task_id)
        source = self._source(directory, path)
        if source.stat().st_size > MAX_FILE:
            raise WorkspaceError("原生源码超过读取上限")
        return source.read_bytes()

    def write_source(self, workspace, task_id, path, content):
        directory, metadata = self._metadata(workspace, task_id)
        if metadata["mode"] != "implement":
            raise PermissionError("只有处理工作区可以修改源码")
        if len(content) > MAX_FILE:
            raise WorkspaceError("原生源码超过写入上限")
        source = self._source(directory, path, missing=True)
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)

    def delete_source(self, workspace, task_id, path):
        directory, metadata = self._metadata(workspace, task_id)
        if metadata["mode"] != "implement":
            raise PermissionError("只有处理工作区可以删除源码")
        self._source(directory, path).unlink()

    def freeze_read_only(self, workspace):
        directory, metadata = self._metadata(workspace, workspace["task_id"])
        if metadata["mode"] != "implement":
            raise PermissionError("只有处理工作区可以冻结为只读候选")
        metadata["mode"] = "review"
        target = directory / "workspace.json"
        temporary = directory / ("workspace-" + uuid4().hex + ".tmp")
        temporary.write_text(canonical_json(metadata), encoding="utf-8")
        temporary.replace(target)
        return {**workspace, "mode": "review"}

    def snapshot(self, workspace, task_id):
        directory, metadata = self._metadata(workspace, task_id)
        original_modes = {entry["path"]: entry["mode"] for entry in metadata["manifest"]}
        files, seen, total = {}, set(), 0
        for parent, directories, names in os.walk(directory / "source", followlinks=False):
            for name in directories:
                plain_path(Path(parent) / name, directory=True)
            for name in names:
                source = Path(parent) / name
                path = native_source_path(source.relative_to(directory / "source").as_posix())
                plain_path(source, directory=False)
                folded = path.casefold()
                if folded in seen:
                    raise WorkspaceError("原生候选包含大小写碰撞")
                seen.add(folded)
                if source.stat().st_size > MAX_FILE:
                    raise WorkspaceError("原生候选包含过大文件")
                content = source.read_bytes()
                total += len(content)
                if total > MAX_SNAPSHOT or content.startswith(
                    b"version https://git-lfs.github.com/spec/v1\n"
                ):
                    raise WorkspaceError("原生候选快照过大或 LFS 实体缺失")
                files[path] = (original_modes.get(path, "100644"), content)
        return files

    def exists(self, workspace):
        try:
            self._metadata(workspace)
        except (FileNotFoundError, PermissionError):
            return False
        return True

    def remove(self, workspace):
        directory, _ = self._metadata(workspace)
        self.snapshot(workspace, workspace["task_id"])
        # The identifier is validated and every member is checked before recursive removal.
        if not directory.resolve().is_relative_to(self.root):
            raise PermissionError("原生清理目标越界")
        shutil.rmtree(directory)
