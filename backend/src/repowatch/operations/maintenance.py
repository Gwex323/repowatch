"""Consistent local backups and conservative reference-aware retention."""

import hashlib
import json
import re
import shutil
import sqlite3
import time
from contextlib import closing
from importlib.metadata import version
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from repowatch.adapters.storage.sqlite import SCHEMA_REVISION
from repowatch.application.budgets import remember_operation, replay_operation
from repowatch.application.safety import assert_quiescent
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import block, business_event
from repowatch.configuration import StorageSettings
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.operations.launcher import status, write_json
from repowatch.operations.locks import data_lock


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    minimum_free_bytes: int = Field(default=67108864, ge=1048576, le=1099511627776)
    artifact_days: int = Field(default=30, ge=1, le=36500)
    log_days: int = Field(default=14, ge=1, le=36500)
    workspace_days: int = Field(default=7, ge=1, le=36500)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_file(root, relative):
    path = Path(relative)
    if (
        "\\" in relative
        or ":" in relative
        or path.is_absolute()
        or not path.parts
        or any(part in {"..", "."} for part in path.parts)
    ):
        raise ValueError("非受管相对路径")
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()):
        raise PermissionError("路径超出受管目录")
    for parent in (target, *target.parents):
        if parent == root:
            break
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise PermissionError("受管文件不能经过链接或目录联接")
    return target


def dependency_status():
    import sys

    expected = {
        "fastapi": "0.162.0",
        "deepagents": "0.7.32",
        "langgraph": "1.2.19",
        "langchain-openai": "1.7.3",
        "langgraph-checkpoint-sqlite": "3.1.3",
    }
    installed = {name: version(name) for name in expected}
    if sys.version_info[:2] != (3, 12) or installed != expected:
        raise ValueError("Python／关键 SDK 版本不兼容，请从锁文件重新安装")
    return {"python": "3.12", "packages": installed}


def verify_backup(root):
    root = root.resolve()
    manifest = json.loads(safe_file(root, "manifest.json").read_text("utf-8"))
    if manifest.get("format") != 1 or manifest.get("schema") != SCHEMA_REVISION:
        raise ValueError("备份格式或 schema 不兼容；请用对应版本恢复后显式升级")
    files = manifest.get("files", {})
    if not {"business.sqlite3", "checkpoints.sqlite3"} <= files.keys():
        raise ValueError("备份缺少必要数据库")
    allowed = {"business.sqlite3", "checkpoints.sqlite3", "memory-index.sqlite3"}
    for relative, fingerprint in files.items():
        if relative not in allowed and not relative.startswith(("artifacts/", "git-objects/")):
            raise ValueError("备份包含未授权路径")
        path = safe_file(root, relative)
        if not path.is_file() or digest(path) != fingerprint:
            raise ValueError("备份文件缺失或摘要不符")
    for name in ("business.sqlite3", "checkpoints.sqlite3"):
        with closing(
            sqlite3.connect(safe_file(root, name).as_uri() + "?mode=ro", uri=True)
        ) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("备份数据库完整性核验失败")
    return manifest


class Maintenance:
    def __init__(self, services):
        self.services, self.settings, self.store = services, services.settings, services.store

    def configuration(self):
        with self.store.read() as records:
            row = records.one("operations_configuration", singleton=1)
            return {
                "revision": row["revision"],
                "policy": json.loads(row["policy_json"]),
                "native": json.loads(row["native_json"]),
            }

    def view(self):
        configuration = self.configuration()
        with self.store.read() as records:
            restore_required = bool(
                records.find("block_reasons", code="backup_restore_reconciliation", resolved=0)
            )
            capability = {
                "supported": False,
                "reason": "原生执行未启用；默认使用受控 Docker 工作区",
            }
            if configuration["native"].get("enabled"):
                from repowatch.operations.native import require_backend

                try:
                    require_backend(records, {"execution_backend": "native"})
                except (ValueError, PermissionError, OSError):
                    capability["reason"] = (
                        "当前原生宿主未通过实时准入；请核对专用账户、宿主会话及绑定配置"
                    )
                else:
                    capability = {
                        "supported": True,
                        "reason": "当前原生宿主身份、隔离能力和固定工具链核验通过；执行前仍会复核",
                    }
        usage = shutil.disk_usage(self.settings.data_dir)
        return {
            **configuration,
            "service": status(self.settings),
            "schema": SCHEMA_REVISION,
            "restore_required": restore_required,
            "dependencies": dependency_status(),
            "disk": {"free": usage.free, "total": usage.total},
            "data_dir": str(self.settings.data_dir),
            "backups": self.backups(),
            "native_capability": capability,
        }

    def configure(self, operation_id, policy, *, expected_revision):
        policy = RetentionPolicy.model_validate(policy).model_dump()
        request = {"kind": "retention_policy", "policy": policy, "revision": expected_revision}
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            row = records.one("operations_configuration", singleton=1)
            if row["revision"] != expected_revision:
                raise ConflictError("保留策略已变化")
            result = {"revision": row["revision"] + 1, "policy": policy}
            records.change(
                "operations_configuration",
                {"singleton": 1},
                {"revision": result["revision"], "policy_json": canonical_json(policy)},
            )
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(records, "retention_policy_configured", operation_id=operation_id)
            return result

    def backups(self):
        root = self.settings.data_dir / "backups"
        if not root.is_dir():
            return []
        return [
            {"id": path.name, "created_at": path.stat().st_mtime}
            for path in sorted(root.glob("backup-*"))
            if (path / "manifest.json").is_file()
        ]

    def backup(self, operation_id):
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
            raise ValueError("备份需要稳定操作标识")
        identifier = "backup-" + hashlib.sha256(operation_id.encode()).hexdigest()[:32]
        destination = self.settings.data_dir / "backups" / identifier
        with data_lock(self.settings, exclusive=True):
            request = {"kind": "consistent_backup"}
            with self.store.read() as records:
                fingerprint, replay = replay_operation(records, operation_id, request)
            if destination.exists():
                verify_backup(destination)
                result = {"id": identifier, "verified": True}
                if replay is None:
                    with self.store.write() as records:
                        remember_operation(records, operation_id, fingerprint, 1, result)
                return result
            if replay is not None:
                raise ConflictError("已登记备份现已缺失，请核对或使用新操作标识")
            with self.store.read() as records:
                # Archive unknown remote operations; restoring still requires reconciliation.
                assert_quiescent(records, allow_unknown_remote=True)
                control = dict(records.one("global_control", singleton=1))
                available = [dict(row) for row in records.find("artifacts") if row["available"]]
            self.store.assert_execution_available()
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(".incomplete-" + identifier)
            if temporary.exists():
                raise ConflictError("存在未完成备份，请保留并使用新操作标识重试")
            temporary.mkdir()
            files = {}
            for path in (
                self.settings.business_database,
                self.settings.checkpoint_database,
                self.settings.index_database,
            ):
                if path.is_file():
                    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
                        with closing(sqlite3.connect(temporary / path.name)) as target:
                            source.backup(target)
                    files[path.name] = digest(temporary / path.name)
            for directory in ("artifacts", "git-objects"):
                source_root = self.settings.data_dir / directory
                if source_root.is_dir():
                    for path in source_root.rglob("*"):
                        if path.is_symlink() or (
                            hasattr(path, "is_junction") and path.is_junction()
                        ):
                            raise PermissionError("归档包含链接，不能建立一致备份")
                        if path.is_file():
                            relative = path.relative_to(self.settings.data_dir).as_posix()
                            safe_file(self.settings.data_dir, relative)
                            target = safe_file(temporary, relative)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(path, target)
                            files[relative] = digest(target)
            for artifact in available:
                if files.get("artifacts/" + artifact["relative_path"]) != artifact["sha256"]:
                    raise ValueError("业务库引用的可用证据缺失或变化，备份不能标记可恢复")
            manifest = {
                "format": 1,
                "schema": SCHEMA_REVISION,
                "created_at": time.time(),
                "control": control,
                "files": files,
                "dependencies": dependency_status(),
                "credential_namespace": hashlib.sha256(
                    str(self.settings.data_dir).casefold().encode()
                ).hexdigest()[:40],
                "restore_requires": [
                    "remote_publication",
                    "workspace_volumes",
                    "credential_references",
                ],
            }
            write_json(temporary / "manifest.json", manifest)
            verify_backup(temporary)
            temporary.rename(destination)
            with self.store.write() as records:
                remember_operation(
                    records, operation_id, fingerprint, 1, {"id": identifier, "verified": True}
                )
                business_event(records, "consistent_backup_created", backup_id=identifier)
            return {"id": identifier, "verified": True}

    def cleanup(self, *, dry_run=True, operation_id=None, plan_sha256=None):
        if type(dry_run) is not bool:
            raise ValueError("清理模式无效")
        with data_lock(self.settings, exclusive=True):
            request = {"kind": "retention_cleanup", "dry_run": dry_run, "plan_sha256": plan_sha256}
            if operation_id:
                with self.store.read() as records:
                    _, replay = replay_operation(records, operation_id, request)
                    if replay is not None:
                        return replay
            policy = self.configuration()["policy"]
            now = self.services.clock()
            with self.store.read() as records:
                assert_quiescent(records, allow_unknown_remote=True)
                protected_tasks = {
                    row["id"]
                    for row in records.find("tasks")
                    if row["state"] not in {"ended", "cancelled"}
                }
                # Source evidence remains auditable even after memory deletion.
                protected_tasks |= {
                    row["source_task_id"]
                    for row in records.find("memory_heads")
                    if row["source_task_id"]
                }
                runs = {row["id"]: dict(row) for row in records.find("runs")}
                task_rows = records.find("tasks")
                active_repositories = {
                    row["repository_id"] for row in task_rows if row["id"] in protected_tasks
                }
                protected_tasks |= {
                    row["id"] for row in task_rows if row["repository_id"] in active_repositories
                }
                ended_at = {}
                for run in runs.values():
                    if run["ended_at"] is not None:
                        ended_at[run["task_id"]] = max(
                            ended_at.get(run["task_id"], 0), run["ended_at"]
                        )
                artifacts = [dict(row) for row in records.find("artifacts") if row["available"]]
                commands = [dict(row) for row in records.find("command_states")]
                workspaces = [dict(row) for row in records.find("workspaces", state="ready")]
            candidates, protected = [], []
            for artifact in artifacts:
                run = runs[artifact["run_id"]]
                path = safe_file(self.settings.artifacts_dir, artifact["relative_path"])
                item = {"kind": "artifact", "id": artifact["id"], "path": str(path)}
                if (
                    run["task_id"] in protected_tasks
                    or run["state"] == "running"
                    or run["task_id"] not in ended_at
                ):
                    protected.append(item)
                elif (
                    path.is_file()
                    and max(path.stat().st_mtime, ended_at[run["task_id"]])
                    < now - policy["artifact_days"] * 86400
                ):
                    candidates.append(item)
            for command in commands:
                path = safe_file(self.settings.data_dir, "commands/" + command["id"] + ".log")
                task_id = runs[command["run_id"]]["task_id"]
                if path.is_file() and task_id not in protected_tasks and task_id in ended_at:
                    if (
                        max(path.stat().st_mtime, ended_at[task_id])
                        < now - policy["log_days"] * 86400
                    ):
                        candidates.append({"kind": "log", "id": command["id"], "path": str(path)})
            for workspace in workspaces:
                if (
                    workspace["task_id"] not in protected_tasks
                    and workspace["task_id"] in ended_at
                    and max(workspace["created_at"], ended_at[workspace["task_id"]])
                    < now - policy["workspace_days"] * 86400
                ):
                    candidates.append(
                        {
                            "kind": "workspace",
                            "id": workspace["id"],
                            "snapshot": json.loads(workspace["snapshot_json"]),
                        }
                    )
            service = status(self.settings)
            service_logs = self.settings.data_dir / "runtime"
            if service_logs.is_dir():
                for path in sorted(service_logs.glob("*.log")):
                    if not re.fullmatch(r"(?:supervisor|api|worker)-[0-9a-f]{32}\.log", path.name):
                        continue
                    safe_file(self.settings.data_dir, "runtime/" + path.name)
                    if service.get("supervisor_alive") and service.get("generation") in path.name:
                        continue
                    if path.stat().st_mtime < now - policy["log_days"] * 86400:
                        candidates.append(
                            {"kind": "service_log", "id": path.name, "path": str(path)}
                        )
            current_plan = content_digest(
                {
                    "configuration": self.configuration(),
                    "candidates": candidates,
                    "protected": protected,
                }
            )
            if not dry_run and plan_sha256 is not None and plan_sha256 != current_plan:
                raise ConflictError("清理候选或保留策略已经变化，请重新预览")
            result = {
                "dry_run": dry_run,
                "candidates": candidates,
                "protected": protected,
                "plan_sha256": current_plan,
            }
            if not dry_run:
                # Invalidate first: a crash may leave an unused file, never false proof.
                with self.store.write() as records:
                    for item in candidates:
                        if item["kind"] == "artifact":
                            records.change("artifacts", {"id": item["id"]}, {"available": 0})
                            if records.one("artifact_intents", id=item["id"]):
                                records.change(
                                    "artifact_intents", {"id": item["id"]}, {"state": "unavailable"}
                                )
                        elif item["kind"] == "workspace":
                            records.change(
                                "workspaces", {"id": item["id"]}, {"state": "unavailable"}
                            )
                    business_event(records, "retention_cleanup", count=len(candidates))
                for item in candidates:
                    if item["kind"] == "workspace":
                        if item["snapshot"].get("kind") == "native":
                            from repowatch.adapters.native_workspaces import NativeWorkspaces
                            from repowatch.operations.native import require_backend

                            with self.store.read() as records:
                                host = require_backend(records, {"execution_backend": "native"})
                            NativeWorkspaces(host, None).remove(item["snapshot"])
                        else:
                            from repowatch.adapters.docker_workspaces import DockerWorkspaces

                            DockerWorkspaces(self.settings.data_dir / "workspaces", None).remove(
                                item["snapshot"]
                            )
                    else:
                        Path(item["path"]).unlink(missing_ok=True)
            if operation_id:
                with self.store.write() as records:
                    fingerprint, replay = replay_operation(records, operation_id, request)
                    if replay is not None:
                        return replay
                    remember_operation(records, operation_id, fingerprint, 1, result)
            return result


def restore(backup: Path, settings: StorageSettings):
    """Restore only into a fresh directory; retained data is never replaced or removed."""
    with data_lock(settings, "services", exclusive=True):
        with data_lock(settings, exclusive=True):
            if settings.business_database.exists() or settings.checkpoint_database.exists():
                raise ConflictError("恢复目标已有数据；请使用新的本地数据目录并保留原目录")
            manifest = verify_backup(backup)
            if any((settings.data_dir / name).exists() for name in ("artifacts", "git-objects")):
                raise ConflictError("恢复目标包含旧产物，请使用新目录")
            marker = settings.data_dir / ".restore-incomplete"
            marker.write_text("恢复尚未完成，禁止启动或迁移", encoding="utf-8")
            for relative in manifest["files"]:
                source = safe_file(backup, relative)
                target = safe_file(settings.data_dir, relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            with closing(sqlite3.connect(settings.business_database)) as connection:
                with connection:
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("UPDATE global_control SET paused=1,revision=revision+1")
                    connection.execute(
                        "UPDATE workspaces SET state='unavailable' WHERE state='ready'"
                    )
            from sqlalchemy import MetaData

            from repowatch.adapters.storage.sqlite import SqlRecords, sqlite_engine

            engine = sqlite_engine(settings)
            try:
                with engine.begin() as connection:
                    metadata = MetaData()
                    metadata.reflect(connection)
                    records = SqlRecords(connection, metadata, writable=True)
                    block(
                        records,
                        "backup_restore_reconciliation",
                        None,
                        now=time.time(),
                        checks=manifest["restore_requires"],
                        credentials_exported=False,
                    )
                    business_event(
                        records, "backup_restored", requires=manifest["restore_requires"]
                    )
            finally:
                engine.dispose()
            marker.unlink()
            return {"restored": True, "paused": True, "requires": manifest["restore_requires"]}
