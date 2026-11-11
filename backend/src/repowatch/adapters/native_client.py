"""Fail-closed binding to an explicitly activated Windows native execution host."""

import ctypes
import json
import os
import re
import time
from ctypes import wintypes
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from repowatch.adapters.native_workspaces import IDENTIFIER, native_source_path, plain_path
from repowatch.adapters.windows_jobs import WindowsJob, process_identity
from repowatch.configuration import StorageSettings
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.operations.locks import data_lock

PROTOCOL = "repowatch-native-v1"
HEX_ID = re.compile(r"[a-f0-9]{32}\Z")
RUNTIME_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")
QUALIFICATIONS = frozenset(
    {
        "appcontainer_token",
        "workspace_scope",
        "source_read_only",
        "credential_canary",
        "environment_allowlist",
        "control_api_boundary",
        "owner_death_job_cleanup",
        "bounded_timeout",
    }
)


def windows_process_owner(pid):
    if os.name != "nt":
        raise PermissionError("原生执行宿主要求 Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        raise PermissionError("原生宿主进程无法核验")
    token = wintypes.HANDLE()
    sid = wintypes.LPWSTR()
    try:
        if not advapi.OpenProcessToken(process, 8, ctypes.byref(token)):
            raise PermissionError("原生宿主身份无法核验")
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value <= 4096:
            raise PermissionError("原生宿主身份材料无效")
        data = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise PermissionError("原生宿主身份无法读取")
        user_sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(user_sid, ctypes.byref(sid)):
            raise PermissionError("原生宿主身份无法解析")
        elevated = wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 20, ctypes.byref(elevated), 4, ctypes.byref(size)):
            raise PermissionError("原生宿主提升状态无法核验")
        container = wintypes.DWORD()
        if not advapi.GetTokenInformation(
            token, 29, ctypes.byref(container), 4, ctypes.byref(size)
        ):
            raise PermissionError("原生进程隔离令牌无法核验")
        return {
            "sid": sid.value,
            "elevated": bool(elevated.value),
            "appcontainer": bool(container.value),
            "created": process_identity(pid),
        }
    finally:
        if sid:
            kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def _read_json(path, limit=64 * 1024):
    plain_path(path, directory=False)
    if path.stat().st_size > limit:
        raise PermissionError("原生宿主材料超过读取上限")
    try:
        value = json.loads(path.read_text("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise PermissionError("原生宿主材料不可解析") from None
    if not isinstance(value, dict):
        raise PermissionError("原生宿主材料类型无效")
    return value


def windows_profile_guard(path, controller_sid, execution_sid, *, writable_by_execution=False):
    """Reject broad ACL grants on the host's code and control channel."""
    if os.name != "nt":
        raise PermissionError("原生目录权限核验要求 Windows")
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    api.GetNamedSecurityInfoW.restype = wintypes.DWORD
    api.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    api.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    allowed = {controller_sid, execution_sid, "S-1-5-18", "S-1-5-32-544"}

    def sid_text(pointer):
        value = wintypes.LPWSTR()
        if not api.ConvertSidToStringSidW(pointer, ctypes.byref(value)):
            raise PermissionError("原生目录权限身份无法解析")
        try:
            return value.value
        finally:
            kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))

    code = api.GetNamedSecurityInfoW(
        str(path),
        1,
        5,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if code:
        raise PermissionError("原生目录权限无法核验")
    try:
        owners = {controller_sid, execution_sid} if writable_by_execution else {controller_sid}
        if sid_text(owner) not in owners or not dacl:
            raise PermissionError("原生目录归属不符或无受限 DACL")
        header = ctypes.string_at(dacl, 8)
        count = int.from_bytes(header[4:6], "little")
        for index in range(count):
            ace = ctypes.c_void_p()
            if not api.GetAce(dacl, index, ctypes.byref(ace)):
                raise PermissionError("原生目录权限条目无法核验")
            entry = ctypes.string_at(ace, 8)
            if entry[0] == 1:  # A deny cannot widen access.
                continue
            if entry[0] != 0:
                raise PermissionError("原生目录含未支持的条件或对象权限")
            actor = sid_text(ctypes.c_void_p(ace.value + 8))
            mask = int.from_bytes(entry[4:8], "little")
            if mask and actor not in allowed:
                raise PermissionError("原生控制目录向未授权身份授予访问")
            write_mask = 0x500D0116  # Generic all/write, delete, DAC/owner and file writes.
            if not writable_by_execution and actor == execution_sid and mask & write_mask:
                raise PermissionError("执行账户不能改写原生控制代码或请求")
    finally:
        kernel.LocalFree(descriptor)


class NativeHostClient:
    def __init__(
        self,
        root: Path,
        *,
        assets=None,
        process_probe=windows_process_owner,
        access_guard=windows_profile_guard,
    ):
        self.root = Path(root)
        if not self.root.is_absolute() or str(self.root).startswith(("\\\\", "//")):
            raise PermissionError("原生宿主必须使用明确的本地目录")
        plain_path(self.root, directory=True)
        self.root = self.root.resolve()
        self.workspace_root = self.root / "workspaces"
        self.profile = _read_json(self.root / "profile.json", limit=2 * 1024 * 1024)
        if self.profile.get("protocol") != PROTOCOL or not RUNTIME_ID.fullmatch(
            self.profile.get("runtime_id", "")
        ):
            raise PermissionError("原生宿主版本或运行时摘要无效")
        self.runtime_id = self.profile["runtime_id"]
        self.process_probe = process_probe
        self.access_guard = access_guard
        self.controller = process_probe(os.getpid())
        if (
            self.profile.get("controller_sid") != self.controller["sid"]
            or self.profile.get("execution_sid") == self.controller["sid"]
        ):
            raise PermissionError("原生执行身份必须独立于当前控制身份")
        if assets is None:
            directory = Path(__file__).resolve().parents[1] / "_native"
            required = (
                "host.ps1",
                "command.ps1",
                "kernel.cs",
                "qualification.ps1",
                "security_probe.cs",
                "crash_owner.ps1",
                "capture_qualification.ps1",
            )
            if any(not (directory / name).is_file() for name in required):
                raise PermissionError("正式原生宿主资源未安装")
            assets = {
                name: sha256((directory / name).read_bytes()).hexdigest() for name in required
            }
        self.assets = dict(assets)
        self._runtime_fingerprints = {}
        self.require_ready()

    def require_ready(self):
        # A user configuration flag cannot replace installed code or live identity proof.
        if _read_json(self.root / "profile.json", limit=2 * 1024 * 1024) != self.profile:
            raise PermissionError("原生宿主配置在准入后发生变化")
        controller, execution = self.controller["sid"], self.profile["execution_sid"]
        for path in (
            self.root,
            self.root / "host",
            self.root / "requests",
            self.root / "workspaces",
            self.root / "profile.json",
        ):
            plain_path(path, directory=path.name != "profile.json")
            self.access_guard(path, controller, execution)
        for name, expected in self.assets.items():
            if Path(name).name != name:
                raise PermissionError("原生宿主资源名无效")
            path = self.root / "host" / name
            plain_path(path, directory=False)
            self.access_guard(path, controller, execution)
            if (
                path.stat().st_size > 1024 * 1024
                or sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise PermissionError("原生宿主代码摘要已变化")
        runtime = self.profile.get("runtime_files")
        if (
            not isinstance(runtime, dict)
            or not 1 <= len(runtime) <= 10000
            or content_digest(runtime) != self.runtime_id.removeprefix("sha256:")
        ):
            raise PermissionError("原生工具链缺少固定内容清单")
        runtime_root = self.root / "runtime"
        plain_path(runtime_root, directory=True)
        self.access_guard(runtime_root, controller, execution)
        for relative, expected in runtime.items():
            relative = native_source_path(relative)
            path = runtime_root
            for part in relative.split("/")[:-1]:
                path /= part
                plain_path(path, directory=True)
            path = runtime_root / relative
            plain_path(path, directory=False)
            info = path.stat()
            fingerprint = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if self._runtime_fingerprints.get(relative) != fingerprint:
                self.access_guard(path, controller, execution)
                if sha256(path.read_bytes()).hexdigest() != expected:
                    raise PermissionError("原生工具链文件摘要已变化")
                self._runtime_fingerprints[relative] = fingerprint
        tools = self.profile.get("tools")
        if (
            not isinstance(tools, dict)
            or not tools
            or any(value not in runtime for value in tools.values())
        ):
            raise PermissionError("原生命令必须绑定到已核验工具链")
        plain_path(self.root / "responses", directory=True)
        plain_path(self.root / "responses" / "ready.json", directory=False)
        self.access_guard(
            self.root / "responses", controller, execution, writable_by_execution=True
        )
        self.access_guard(
            self.root / "responses" / "ready.json",
            controller,
            execution,
            writable_by_execution=True,
        )
        ready = _read_json(self.root / "responses" / "ready.json")
        if (
            ready.get("protocol") != PROTOCOL
            or ready.get("profile_sha256") != content_digest(self.profile)
            or not HEX_ID.fullmatch(ready.get("generation", ""))
            or type(ready.get("pid")) is not int
            or ready["pid"] <= 0
            or type(ready.get("created")) is not int
            or ready["created"] <= 0
            or ready.get("stopped") is not False
            or not QUALIFICATIONS.issubset(ready.get("qualifications", []))
        ):
            raise PermissionError("原生宿主缺少当前代次的完整准入材料")
        process = self.process_probe(ready["pid"])
        if (
            process["sid"] != self.profile.get("execution_sid")
            or process["created"] != ready["created"]
            or process["elevated"]
            or process.get("appcontainer", False)
        ):
            raise PermissionError("原生宿主实际身份、提升状态或创建时间不匹配")
        for name in ("requests", "responses", "workspaces"):
            plain_path(self.root / name, directory=True)
        return ready

    def submit(self, operation, payload, *, request_id=None, expected_generation=None):
        if operation != "check" or not isinstance(payload, dict):
            raise PermissionError("原生宿主操作不受支持")
        if operation == "check":
            if (
                set(payload)
                != {"workspace_id", "argv", "timeout_ms", "deadline_unix_ms", "manifest_sha256"}
                or not isinstance(payload.get("workspace_id"), str)
                or not IDENTIFIER.fullmatch(payload["workspace_id"])
                or not isinstance(payload.get("argv"), list)
                or not 1 <= len(payload["argv"]) <= 256
                or any(
                    not isinstance(arg, str) or "\0" in arg or len(arg) > 16384
                    for arg in payload["argv"]
                )
                or not payload["argv"][0]
                or sum(map(len, payload["argv"])) > 65536
                or type(payload.get("timeout_ms")) is not int
                or not 1 <= payload["timeout_ms"] <= 3600000
                or type(payload.get("deadline_unix_ms")) is not int
                or payload["deadline_unix_ms"] <= 0
                or not isinstance(payload.get("manifest_sha256"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", payload["manifest_sha256"])
            ):
                raise PermissionError("原生检查请求范围、命令或有限超时无效")
        ready = self.require_ready()
        if expected_generation is not None and ready["generation"] != expected_generation:
            raise PermissionError("原生宿主代次在命令登记后变化")
        identifier = request_id or uuid4().hex
        if not HEX_ID.fullmatch(identifier):
            raise PermissionError("原生请求身份无效")
        request = {
            "protocol": PROTOCOL,
            "request_id": identifier,
            "generation": ready["generation"],
            "profile_sha256": content_digest(self.profile),
            "operation": operation,
            "payload": payload,
        }
        path = self.root / "requests" / (identifier + ".json")
        with data_lock(StorageSettings(self.root), name="native-requests", exclusive=True):
            if self.require_ready()["generation"] != ready["generation"]:
                raise PermissionError("原生宿主代次在提交期间变化")
            if path.exists():
                if _read_json(path) != request:
                    raise PermissionError("同一原生请求身份不能更换内容或代次")
            else:
                temporary = path.with_suffix("." + uuid4().hex + ".tmp")
                temporary.write_text(canonical_json(request), encoding="utf-8")
                temporary.replace(path)
        return {
            "request_id": identifier,
            "request_sha256": content_digest(request),
            "generation": ready["generation"],
        }

    def response(self, receipt):
        if not HEX_ID.fullmatch(receipt.get("request_id", "")):
            raise PermissionError("原生响应请求身份无效")
        ready = self.require_ready()
        if receipt["generation"] != ready["generation"]:
            raise PermissionError("原生宿主代次已变化，旧请求需要核对")
        path = self.root / "responses" / (receipt["request_id"] + ".json")
        if not path.exists():
            return None
        result = _read_json(path)
        if (
            result.get("request_id") != receipt["request_id"]
            or result.get("request_sha256") != receipt["request_sha256"]
            or result.get("generation") != receipt["generation"]
        ):
            raise PermissionError("原生响应归属或请求摘要不匹配")
        return result

    def wait(self, receipt, *, timeout):
        if not 0 < timeout <= 3600:
            raise ValueError("原生请求等待必须有界")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.response(receipt)
            if result is not None:
                return result
            time.sleep(0.02)
        raise TimeoutError("原生请求尚未取得唯一响应，不得重复执行")

    def assert_workspace_idle(self, identifier):
        # The execution host must publish a command identity before releasing its startup gate.
        self.require_ready()
        directory = self.root / "responses" / "commands"
        plain_path(directory, directory=True)
        for path in directory.glob("*.json"):
            command = _read_json(path)
            if command.get("workspace_id") == identifier and command.get("state") != "stopped":
                raise PermissionError("原生工作区仍有尚未核实停止的命令")
        for path in (self.root / "requests").glob("*.json"):
            request = _read_json(path)
            if (
                request.get("operation") != "check"
                or request.get("payload", {}).get("workspace_id") != identifier
            ):
                continue
            result_path = self.root / "responses" / path.name
            result = _read_json(result_path) if result_path.exists() else {}
            if (
                result.get("state") != "stopped"
                or result.get("request_sha256") != content_digest(request)
                or result.get("generation") != request.get("generation")
                or result.get("request_id") != request.get("request_id")
            ):
                raise PermissionError("原生工作区仍有待绑定或未核实停止的检查请求")

    def binding(self, receipt):
        self.require_ready()
        if not HEX_ID.fullmatch(receipt.get("request_id", "")):
            raise PermissionError("原生命令身份无效")
        path = self.root / "responses/commands" / (receipt["request_id"] + ".bound.json")
        if not path.exists():
            return None
        bound = _read_json(path)
        if (
            type(bound.get("pid")) is not int
            or bound["pid"] <= 0
            or type(bound.get("created")) is not int
            or bound["created"] <= 0
        ):
            raise PermissionError("原生子进程绑定材料无效")
        actual = self.process_probe(bound["pid"])
        if (
            actual["sid"] != self.profile["execution_sid"]
            or actual["elevated"]
            or actual["created"] != bound["created"]
            or not actual.get("appcontainer")
        ):
            raise PermissionError("原生子进程实际隔离令牌或身份不匹配")
        return bound

    def start(self, receipt):
        if self.response(receipt) is not None:
            raise PermissionError("已结束的原生请求不能重新启动")
        if self.binding(receipt) is None:
            raise PermissionError("原生子进程尚未持久绑定")
        path = self.root / "requests" / (receipt["request_id"] + ".start")
        if path.with_suffix(".cancel").exists():
            raise PermissionError("原生命令已取消")
        path.touch(exist_ok=False)

    def log(self, receipt, result):
        if self.response(receipt) != result or result.get("state") != "stopped":
            raise PermissionError("原生日志缺少唯一停止响应")
        name = receipt["request_id"] + ".log"
        if result.get("log_file") != name:
            raise PermissionError("原生日志归属无效")
        directory = self.root / "responses/logs"
        plain_path(directory, directory=True)
        path = directory / name
        plain_path(path, directory=False)
        with path.open("rb") as handle:
            content = handle.read(8 * 1024 * 1024)
        if sha256(content).hexdigest() != result.get("log_sha256"):
            raise PermissionError("原生日志摘要不匹配")
        return content


def native_command_status(identity, *, stop=False, timeout=10):
    """Reconcile a persisted startup-gated job even after the execution host has died."""
    identifier = identity.get("request_id", "")
    if (
        not HEX_ID.fullmatch(identifier)
        or identity.get("job_name") != "Local\\RepoWatchNative-" + identifier
        or not isinstance(identity.get("host_root"), str)
    ):
        raise PermissionError("原生命令恢复身份无效")
    root = Path(identity["host_root"])
    plain_path(root, directory=True)
    if not root.is_absolute() or root.is_symlink():
        raise PermissionError("原生命令宿主路径无效")
    profile = _read_json(root / "profile.json", limit=2 * 1024 * 1024)
    if content_digest(profile) != identity.get("profile_sha256"):
        raise PermissionError("原生命令宿主配置已变化")
    current = windows_process_owner(os.getpid())
    if current["sid"] != profile.get("controller_sid"):
        raise PermissionError("原生命令控制身份不匹配")
    for name in ("requests", "responses", "responses/commands"):
        plain_path(root / name, directory=True)
        windows_profile_guard(
            root / name,
            current["sid"],
            profile["execution_sid"],
            writable_by_execution=name.startswith("responses"),
        )
    if stop:
        (root / "requests" / (identifier + ".cancel")).touch(exist_ok=True)
    deadline = time.monotonic() + (timeout if stop else 0)
    while True:
        result_path = root / "responses" / (identifier + ".json")
        result = _read_json(result_path) if result_path.exists() else None
        if result is not None:
            receipt = identity["receipt"]
            if any(
                result.get(key) != receipt[key]
                for key in ("request_id", "request_sha256", "generation")
            ):
                raise PermissionError("原生命令响应归属不符")
            if (
                result.get("state") == "stopped"
                and result.get("identity_verified") is True
                and result.get("active_processes") == 0
            ):
                return {**result, "stopped": True}
        bound_path = root / "responses/commands" / (identifier + ".bound.json")
        bound = _read_json(bound_path) if bound_path.exists() else None
        try:
            job = WindowsJob(identity["job_name"], create=False)
        except FileNotFoundError:
            job = None
        if job is not None:
            try:
                if stop:
                    job.stop()
                active = job.active()
            finally:
                job.close()
            if active == 0:
                return {
                    "stopped": True,
                    "identity_verified": True,
                    "active_processes": 0,
                    "reason": "native_job_process_count",
                }
        elif bound is not None:
            pid, created = bound.get("pid"), bound.get("created")
            if type(pid) is not int or type(created) is not int or pid <= 0 or created <= 0:
                raise PermissionError("原生命令创建身份无效")
            if process_identity(pid) != created:
                return {
                    "stopped": True,
                    "identity_verified": True,
                    "active_processes": 0,
                    "reason": "native_job_absent_and_identity_checked",
                }
        else:
            # No repository code can run before a bound identity and a controller gate.
            ready = _read_json(root / "responses/ready.json")
            if (
                ready.get("generation") != identity["receipt"]["generation"]
                or ready.get("stopped") is True
                or process_identity(ready["pid"]) != ready["created"]
            ):
                return {
                    "stopped": True,
                    "identity_verified": True,
                    "active_processes": 0,
                    "reason": "native_host_ended_before_startup_gate",
                }
        if not stop or time.monotonic() >= deadline:
            return {
                "stopped": False,
                "identity_verified": True,
                "active_processes": None,
                "reason": "native_completion_pending",
            }
        time.sleep(0.02)
