"""Explicit admission for host execution; user configuration is never isolation proof."""

import json
from pathlib import Path

from repowatch.application.budgets import remember_operation, replay_operation
from repowatch.application.storage import ConflictError
from repowatch.domain.task_records import canonical_json
from repowatch.operations.locks import guarded


def require_backend(records, configuration):
    selected = configuration.get("execution_backend", "docker")
    if selected == "docker":
        return
    if selected != "native":
        raise ValueError("未知执行后端")
    row = records.one("operations_configuration", singleton=1)
    native = json.loads(row["native_json"])
    if not native.get("enabled"):
        raise PermissionError("原生执行未显式启用")
    if not native.get("host_root"):
        raise PermissionError("当前宿主尚无通过隔离身份、文件及控制接口边界验收的原生适配")
    from repowatch.adapters.native_client import NativeHostClient

    host = NativeHostClient(Path(native["host_root"]))
    from repowatch.domain.task_records import content_digest

    if content_digest(host.profile) != native.get("profile_sha256"):
        raise PermissionError("原生宿主绑定内容已变化，须重新显式准入")
    return host


class NativeConfiguration:
    def __init__(self, store):
        self.store = store

    @guarded
    def configure(self, operation_id, *, enabled, expected_revision, host_root=None):
        if type(enabled) is not bool:
            raise ValueError("原生开关必须为布尔值")
        request = {
            "kind": "native_configuration",
            "enabled": enabled,
            "revision": expected_revision,
            "host_root": str(host_root) if host_root is not None else None,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            row = records.one("operations_configuration", singleton=1)
            if row["revision"] != expected_revision:
                raise ConflictError("原生配置已变化")
            native = json.loads(row["native_json"])
            if host_root is not None:
                from repowatch.adapters.native_client import NativeHostClient
                from repowatch.domain.task_records import content_digest

                host = NativeHostClient(Path(host_root))
                native.update(host_root=str(host.root), profile_sha256=content_digest(host.profile))
            native["enabled"] = enabled
            result = {
                "revision": row["revision"] + 1,
                "enabled": enabled,
                "admitted": bool(enabled and host_root is not None),
            }
            records.change(
                "operations_configuration",
                {"singleton": 1},
                {
                    "revision": result["revision"],
                    "native_json": canonical_json(native),
                },
            )
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            return result
