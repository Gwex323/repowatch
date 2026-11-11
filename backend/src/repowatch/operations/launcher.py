"""Windows supervisor: one API/Worker pair, identity-bound stop and durable status."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

from repowatch.adapters.windows_jobs import WindowsJob, current_process, process_identity
from repowatch.api.host import HostServices
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import block
from repowatch.operations.locks import FileLock
from repowatch.security import install_log_redaction


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def runtime_dir(settings):
    return settings.data_dir / "runtime"


def status(settings):
    path = runtime_dir(settings) / "service.json"
    if not path.is_file():
        return {"state": "stopped", "clean_stop": None}
    value = json.loads(path.read_text("utf-8"))
    alive = identity_alive(value.get("supervisor", {}))
    return {**value, "state": value["state"] if alive else "stopped", "supervisor_alive": alive}


def identity_alive(identity):
    if os.name != "nt" or identity.get("platform") != "windows" or not identity.get("created"):
        return False
    try:
        return process_identity(identity["pid"]) == identity["created"]
    except OSError:
        return False


def request_stop(settings):
    current = status(settings)
    if not current.get("supervisor_alive"):
        return current
    write_json(runtime_dir(settings) / "stop.json", {"generation": current["generation"]})
    return {"state": "stop_requested", "generation": current["generation"]}


def request_open(settings):
    current = status(settings)
    if not current.get("supervisor_alive") or current["state"] != "running":
        raise ConflictError("服务未运行，请先启动")
    write_json(
        runtime_dir(settings) / "open.json",
        {"generation": current["generation"], "request": uuid4().hex},
    )
    return {"state": "open_requested"}


def frontend_directory():
    packaged = Path(__file__).resolve().parents[1] / "_frontend"
    directory = (
        packaged if packaged.is_dir() else Path(__file__).resolve().parents[4] / "frontend/dist"
    )
    if not (directory / "index.html").is_file():
        raise ValueError("生产前端未安装；请先构建并安装完整发行包")
    return directory


def start(settings, *, port=8765, browser=True):
    if os.name != "nt":
        raise ValueError("正式启动器要求 Windows 宿主，Linux 执行代码通过 Docker 完成")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("端口无效")
    frontend_directory()
    from repowatch.adapters.storage.sqlite import SQLiteBusinessStore

    store = SQLiteBusinessStore(settings)
    store.close()
    if status(settings).get("supervisor_alive"):
        raise ConflictError("服务已经启动，不能重复创建 API／Worker")
    generation = uuid4().hex
    command = [
        sys.executable,
        "-m",
        "repowatch.operations",
        "serve",
        "--data-dir",
        str(settings.data_dir),
        "--port",
        str(port),
        "--generation",
        generation,
    ]
    if not browser:
        command.append("--no-browser")
    directory = runtime_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"supervisor-{generation}.log").open("ab") as log:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = status(settings)
        if current.get("generation") == generation and current["state"] == "running":
            return current
        if child.poll() is not None:
            raise ConflictError("启动失败，请查看脱敏启动日志及运行状态")
        time.sleep(0.1)
    raise ConflictError("启动未在期限内确认，请检查状态；不会重复启动")


def _pause_and_stop_commands(services):
    with services.store.read() as records:
        control = records.one("global_control", singleton=1)
    if not control["paused"]:
        services.control.pause(
            "service-stop:" + uuid4().hex,
            expected_revision=control["revision"],
            now=services.clock(),
        )
    with services.store.read() as records:
        commands = [
            row["id"]
            for row in records.find("command_states")
            if row["state"] not in {"stopped", "isolated"}
        ]
    for identifier in commands:
        services.commands.stop(identifier)


def supervise(settings, *, port=8765, browser=True, generation=None):
    install_log_redaction()
    directory = runtime_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(directory / "supervisor.lock", exclusive=True, timeout=0):
        previous = status(settings)
        generation = generation or uuid4().hex
        if len(generation) != 32 or any(
            character not in "0123456789abcdef" for character in generation
        ):
            raise ValueError("无效的启动代次")
        stop_api = directory / f"api-{generation}.stop"
        stop_worker = directory / f"worker-{generation}.stop"
        state = {
            "state": "starting",
            "generation": generation,
            "supervisor": current_process(),
            "port": port,
            "clean_stop": False,
            "logs": {name: f"{name}-{generation}.log" for name in ("supervisor", "api", "worker")},
        }
        write_json(directory / "service.json", state)
        children, streams = [], []
        job = WindowsJob(f"Local\\RepoWatch-Service-{generation}", create=True)
        services = None
        normal = False
        try:
            services = HostServices(settings)
            if previous.get("generation") and previous.get("clean_stop") is False:
                with services.store.write() as records:
                    block(records, "service_exit_unconfirmed", None, now=services.clock())
            services.reconcile_startup()
            for name, module, stop_file in (
                ("api", "repowatch.api", stop_api),
                ("worker", "repowatch.worker", stop_worker),
            ):
                command = [
                    sys.executable,
                    "-m",
                    module,
                    "--data-dir",
                    str(settings.data_dir),
                    "--stop-file",
                    str(stop_file),
                ]
                if name == "api":
                    command += ["--port", str(port)]
                    if not browser:
                        command.append("--no-browser")
                stream = (directory / state["logs"][name]).open("ab")
                streams.append(stream)
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import runpy,sys; gate=sys.stdin.buffer.readline(); "
                        "sys.argv=sys.argv[1:]; "
                        "runpy.run_module(sys.argv[0],run_name='__main__') "
                        "if gate==b'run\\n' else sys.exit(125)",
                        module,
                        *command[3:],
                    ],
                    stdin=subprocess.PIPE,
                    stdout=stream,
                    stderr=stream,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                children.append(child)
                try:
                    job.assign(child)
                    child.stdin.write(b"run\n")
                    child.stdin.close()
                except BaseException:
                    child.terminate()
                    raise
                state[name] = {
                    "platform": "windows",
                    "pid": child.pid,
                    "created": process_identity(child.pid),
                }
                if name == "api":
                    ready_deadline = time.monotonic() + 15
                    while not stop_api.with_suffix(".ready").is_file():
                        if child.poll() is not None or time.monotonic() >= ready_deadline:
                            raise ConflictError("API 未就绪，未启动 Worker")
                        time.sleep(0.05)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if any(child.poll() is not None for child in children):
                    raise ConflictError("子服务启动失败")
                try:
                    response = httpx.get(
                        f"http://127.0.0.1:{port}/api/v1/health", timeout=0.5, trust_env=False
                    )
                    if response.status_code == 200:
                        state["state"] = "running"
                        write_json(directory / "service.json", state)
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise ConflictError("回环服务未就绪")
            while all(child.poll() is None for child in children):
                request = directory / "stop.json"
                if request.is_file():
                    wanted = json.loads(request.read_text("utf-8"))
                    if wanted.get("generation") == generation:
                        normal = True
                        break
                time.sleep(0.2)
        finally:
            state["state"] = "stopping"
            write_json(directory / "service.json", state)
            if services is not None:
                _pause_and_stop_commands(services)
            stop_worker.touch()
            for child in children[1:]:
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    normal = False
            stop_api.touch()
            for child in children[:1]:
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    normal = False
            if any(child.poll() is None for child in children):
                job.stop()
            for child in children:
                child.wait(timeout=10)
            if services is not None:
                with services.store.read() as records:
                    normal = (
                        normal
                        and not records.find("runs", state="running")
                        and all(
                            row["state"] in {"stopped", "isolated"}
                            for row in records.find("command_states")
                        )
                    )
                services.recovery.scan_startup(now=services.clock())
                if not normal:
                    with services.store.write() as records:
                        block(records, "service_exit_unconfirmed", None, now=services.clock())
                services.close()
            job.close()
            for stream in streams:
                stream.close()
            write_json(
                directory / "service.json", {**state, "state": "stopped", "clean_stop": normal}
            )
