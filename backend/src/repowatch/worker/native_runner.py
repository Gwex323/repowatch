"""The native Runner uses the existing budget, execution token and evidence contracts."""

import json
import time
from hashlib import sha256
from uuid import uuid4

from repowatch.adapters.native_client import PROTOCOL, native_command_status
from repowatch.adapters.native_workspaces import NativeWorkspaces
from repowatch.application.budgets import BudgetedExecutor
from repowatch.application.repair_evidence import file_manifest
from repowatch.application.task_records import business_event, execution_record, required
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json, content_digest


def native_check(runner, token, workspace, argv, *, timeout, operation_key, preparation):
    if (
        not argv
        or not all(isinstance(arg, str) and "\0" not in arg for arg in argv)
        or not 0 < timeout <= 3600
    ):
        raise ValueError("检查需要明确命令及有限超时")
    services, clock = runner.services, runner.clock
    host = runner.backend.native_host()
    files = NativeWorkspaces(host, runner.backend.objects).snapshot(workspace, workspace["task_id"])
    identifier = uuid4().hex
    generation = host.require_ready()["generation"]
    payload = {
        "workspace_id": workspace["id"],
        "argv": list(argv),
        "timeout_ms": round(timeout * 1000),
        "deadline_unix_ms": round((time.time() + timeout) * 1000),
        "manifest_sha256": content_digest(file_manifest(files)),
    }
    request = {
        "protocol": PROTOCOL,
        "request_id": identifier,
        "generation": generation,
        "profile_sha256": content_digest(host.profile),
        "operation": "check",
        "payload": payload,
    }
    receipt = {
        "request_id": identifier,
        "generation": generation,
        "request_sha256": content_digest(request),
    }
    identity = {
        "kind": "native",
        "host_root": str(host.root),
        "profile_sha256": content_digest(host.profile),
        "request_id": identifier,
        "receipt": receipt,
        "job_name": "Local\\RepoWatchNative-" + identifier,
        "workspace_id": workspace["id"],
        "argv": list(argv),
        "timeout": timeout,
        "generation": token.generation,
        "pid": None,
        "created": None,
    }

    def guard():
        services.queue.guard(
            token, now=clock(), purpose="tool", reservation_id=identity["reservation_id"]
        )
        with services.store.read() as records:
            native = json.loads(
                required(records, "operations_configuration", singleton=1)["native_json"]
            )
            if (
                not native.get("enabled")
                or native.get("host_root") != str(host.root)
                or native.get("profile_sha256") != identity["profile_sha256"]
            ):
                raise PermissionError("原生宿主准入已撤销或变化")

    def stopped(proof):
        with services.store.write() as records:
            records.change(
                "command_states",
                {"id": identifier},
                {"state": "stopped", "evidence_json": canonical_json(proof)},
            )

    def execute():
        started = clock()
        with services.store.write() as records:
            execution_record(records, token, clock())
            reservation = required(
                records, "budget_reservations", run_id=token.run_id, operation_key=operation_key
            )
            identity["reservation_id"] = reservation["id"]
            records.add(
                "command_states",
                {
                    "id": identifier,
                    "run_id": token.run_id,
                    "generation": token.generation,
                    "state": "intent",
                    "identity_json": canonical_json(identity),
                },
            )
            business_event(
                records,
                "runner_intent",
                command_id=identifier,
                run_id=token.run_id,
                backend="native",
            )
        unavailable = bool(preparation["steps"] or preparation["gaps"])
        declarations_changed = any(
            item["path"] not in files
            or sha256(files[item["path"]][1]).hexdigest() != item["sha256"]
            for item in preparation["declarations"]
        )
        if unavailable or declarations_changed:
            report = {
                "reason": "native_dependency_preparation_unavailable"
                if unavailable
                else "declaration_changed",
                "steps": preparation["steps"],
                "gaps": preparation["gaps"],
            }
            content = ("REPOWATCH_ENVIRONMENT_UNAVAILABLE: " + canonical_json(report)).encode()
            proof = {
                "stopped": True,
                "identity_verified": True,
                "active_processes": 0,
                "exit_code": 125,
                "reason": "native_preparation_refused_before_dispatch",
            }
            stopped(proof)
        else:
            try:
                guard()
                if (
                    host.submit(
                        "check", payload, request_id=identifier, expected_generation=generation
                    )
                    != receipt
                ):
                    raise PermissionError("原生提交回执与持久意图不匹配")
                gate_open = False
                wall_deadline = time.monotonic() + timeout
                while True:
                    response = host.response(receipt)
                    if response is not None:
                        if (
                            response.get("state") != "stopped"
                            or response.get("identity_verified") is not True
                            or response.get("active_processes") != 0
                        ):
                            raise PermissionError("原生命令尚未核实停止")
                        proof = {**response, "stopped": True}
                        stopped(proof)
                        content = host.log(receipt, response)
                        break
                    guard()
                    if not gate_open:
                        bound = host.binding(receipt)
                        if bound is not None:
                            identity.update(bound)
                            with services.store.write() as records:
                                execution_record(records, token, clock())
                                records.change(
                                    "command_states",
                                    {"id": identifier},
                                    {"identity_json": canonical_json(identity), "state": "running"},
                                )
                                business_event(
                                    records,
                                    "command_bound",
                                    command_id=identifier,
                                    run_id=token.run_id,
                                )
                            guard()
                            host.start(receipt)
                            gate_open = True
                    if time.monotonic() >= wall_deadline:
                        proof = native_command_status(identity, stop=True)
                        if proof["stopped"]:
                            stopped(proof)
                        raise TimeoutError("原生 Runner 已到截止时间，停止证据保留供恢复核对")
                    time.sleep(0.05)
            except BaseException:
                proof = native_command_status(identity, stop=True)
                if proof["stopped"]:
                    stopped(proof)
                raise
        from repowatch.worker.runner import preparation_report_from_log

        artifact = services.artifacts.register(
            token,
            "runner-log:" + identifier,
            content,
            context={
                "producer": "native_runner",
                "workspace_id": workspace["id"],
                "command_id": identifier,
                "sha": workspace["sha"],
                "argv": list(argv),
            },
            now=clock(),
        )
        result = {
            "command_id": identifier,
            "workspace_id": workspace["id"],
            "argv": list(argv),
            "sha": workspace["sha"],
            "manifest_sha256": workspace["manifest_sha256"],
            "image": workspace["image"],
            "exit_code": proof.get("exit_code"),
            "timed_out": proof.get("timed_out", False),
            "control_stopped": proof.get("control_stopped", False),
            "timeout": timeout,
            "preparation": preparation,
            "preparation_report": preparation_report_from_log(content),
            "oom_killed": False,
            "log_artifact_id": artifact,
            "log_sha256": sha256(content).hexdigest(),
            "log_truncated": proof.get("log_truncated", False),
            "elapsed_ms": round((clock() - started) * 1000),
        }
        with services.store.write() as records:
            execution_record(records, token, clock())
            records.add(
                "runner_results",
                {
                    "command_id": identifier,
                    "run_id": token.run_id,
                    "workspace_id": workspace["id"],
                    "result_json": canonical_json(result),
                    "artifact_id": artifact,
                    "created_at": clock(),
                },
            )
        return result

    return BudgetedExecutor(services.budgets, services.queue, clock=clock).execute(
        token,
        operation_key,
        BudgetAmounts(active_ms=round(timeout * 1000)),
        execute,
        lambda _: {},
        purpose="tool",
    )
