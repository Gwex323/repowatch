"""Storage/budget/gate integration with an explicitly simulated execution host."""

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_scans import scan_setup  # noqa: F401

from repowatch.adapters.native_client import PROTOCOL
from repowatch.adapters.workspace_backends import WorkspaceBackends
from repowatch.application.workspaces import WorkspaceService
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.operations.native import NativeConfiguration
from repowatch.worker.runner import Runner


@pytest.fixture
def native_execution(scan_setup, tmp_path, monkeypatch):  # noqa: F811
    env = scan_setup

    class Host:
        root = tmp_path / "native"
        workspace_root = root / "workspaces"
        runtime_id = "sha256:" + "a" * 64
        profile = {"fixed": "test-only simulated host"}
        calls = []
        started = False
        on_binding = None
        fail_submit = False

        def require_ready(self):
            return {"generation": "b" * 32}

        def assert_workspace_idle(self, _identifier):
            pass

        def submit(self, operation, payload, *, request_id, expected_generation):
            self.calls.append("submit")
            with env.store.read() as records:
                row = records.one("command_states", id=request_id)
                assert row["state"] == "intent"
                assert json.loads(row["identity_json"])["reservation_id"]
            if self.fail_submit:
                raise PermissionError("host generation changed")
            request = {
                "protocol": PROTOCOL,
                "request_id": request_id,
                "generation": expected_generation,
                "profile_sha256": content_digest(self.profile),
                "operation": operation,
                "payload": payload,
            }
            self.receipt = {
                "request_id": request_id,
                "generation": expected_generation,
                "request_sha256": content_digest(request),
            }
            return self.receipt

        def response(self, _receipt):
            if self.started:
                return {
                    **self.receipt,
                    "state": "stopped",
                    "identity_verified": True,
                    "active_processes": 0,
                    "exit_code": 0,
                }

        def binding(self, _receipt):
            self.calls.append("binding")
            if self.on_binding:
                self.on_binding()
            return {"pid": 42, "created": 123}

        def start(self, receipt):
            with env.store.read() as records:
                row = records.one("command_states", id=receipt["request_id"])
                identity = json.loads(row["identity_json"])
                assert row["state"] == "running" and identity["pid"] == 42
                assert identity["created"] == 123
                assert not records.one("global_control", singleton=1)["paused"]
            self.calls.append("start")
            self.started = True

        def log(self, _receipt, _result):
            return b"native fixture passed\n"

    host = Host()
    host.workspace_root.mkdir(parents=True)
    monkeypatch.setattr("repowatch.adapters.native_client.NativeHostClient", lambda *_: host)
    NativeConfiguration(env.store).configure(
        "bind-native", enabled=True, expected_revision=1, host_root=host.root
    )
    objects = SimpleNamespace(
        entries=lambda *_: [{"path": "app.py", "mode": "100644", "oid": "blob"}],
        blob=lambda *_: b"print(2 + 3)\n",
    )
    backend = WorkspaceBackends(env.store, SimpleNamespace(), objects)
    workspaces = WorkspaceService(env.store, backend)
    workspace = backend.create_for(
        {"execution_backend": "native"}, env.repository, "base", task_id=env.task, mode="scan"
    )
    with env.store.write() as records:
        records.add(
            "workspaces",
            {
                "id": workspace["id"],
                "task_id": env.task,
                "repository_id": env.repository,
                "snapshot_json": canonical_json(workspace),
                "state": "ready",
                "created_at": 3,
            },
        )
    services = SimpleNamespace(
        store=env.store, budgets=env.budgets, queue=env.queue, artifacts=env.artifacts
    )
    runner = Runner(services, workspaces, clock=lambda: 3)
    stops = []

    def stop(identity, **kwargs):
        stops.append((identity, kwargs))
        return {"stopped": True, "identity_verified": True, "active_processes": 0}

    monkeypatch.setattr("repowatch.worker.native_runner.native_command_status", stop)
    return SimpleNamespace(env=env, host=host, runner=runner, workspace=workspace, stops=stops)


def test_native_runner_persists_before_start_and_replays_without_new_execution(native_execution):
    case = native_execution
    first = case.runner.check(
        case.env.token, case.workspace["id"], ["python", "app.py"], operation_key="native-test"
    )
    assert (
        first["exit_code"] == 0
        and first["log_sha256"] == sha256(b"native fixture passed\n").hexdigest()
    )
    assert case.host.calls == ["submit", "binding", "start"]
    assert (
        case.runner.check(
            case.env.token, case.workspace["id"], ["python", "app.py"], operation_key="native-test"
        )
        == first
    )
    assert case.host.calls == ["submit", "binding", "start"]
    with pytest.raises(PermissionError, match="参数已变化"):
        case.runner.check(
            case.env.token,
            case.workspace["id"],
            ["python", "other.py"],
            operation_key="native-test",
        )
    with case.env.store.read() as records:
        commands = records.find("command_states")
        assert len(commands) == 1 and commands[0]["state"] == "stopped"
        assert len(records.find("runner_results")) == 1


@pytest.mark.parametrize("failure", ["pause_before_gate", "host_generation_change"])
def test_native_runner_cannot_start_after_revocation_and_retains_stop_proof(
    native_execution, failure
):
    case = native_execution
    if failure == "pause_before_gate":
        case.host.on_binding = lambda: case.env.queue.set_pause(True, now=3)
    else:
        case.host.fail_submit = True
    with pytest.raises(PermissionError):
        case.runner.check(
            case.env.token,
            case.workspace["id"],
            ["python", "app.py"],
            operation_key="native-revoked",
        )
    assert "start" not in case.host.calls and case.stops
    with case.env.store.read() as records:
        assert records.find("command_states")[0]["state"] == "stopped"
        assert not records.find("runner_results")


def test_native_dependencies_are_explicitly_unavailable_without_dispatch(native_execution):
    case = native_execution
    result = case.runner.check(
        case.env.token,
        case.workspace["id"],
        ["python", "app.py"],
        operation_key="native-unavailable",
        preparation={"steps": [["pip", "install", "example"]], "gaps": [], "declarations": []},
    )
    assert result["exit_code"] == 125
    assert result["preparation_report"]["reason"] == "native_dependency_preparation_unavailable"
    assert not case.host.calls


def test_native_runtime_cannot_change_between_frozen_workspaces(native_execution):
    case = native_execution
    with pytest.raises(PermissionError, match="冻结的工作区运行时"):
        case.runner.backend.create_for(
            {"execution_backend": "native"},
            case.env.repository,
            "candidate",
            task_id=case.env.task,
            mode="check",
            image="sha256:" + "f" * 64,
        )
    assert not case.host.calls


def test_native_probe_source_validation_can_take_over_one_second(native_execution, monkeypatch):
    from repowatch.application.budgets import BudgetedExecutor
    from repowatch.worker.repairs import RepairCoordinator

    case = native_execution
    instant = [3.0]
    case.runner.services.clock = lambda: instant[0]
    coordinator = object.__new__(RepairCoordinator)
    coordinator.services = case.runner.services
    coordinator.code = lambda _: {}
    monkeypatch.setattr(
        "repowatch.worker.repairs.PullRequestLifecycle",
        lambda _: SimpleNamespace(view=lambda _: {}),
    )

    def slow_read(*args):
        content = case.runner.backend.read_source(*args)
        instant[0] += 2.3
        return content

    coordinator.backend = SimpleNamespace(read_source=slow_read)
    probe = {
        "argv": ["python", "app.py"],
        "target_failure": "FAIL",
        "target_success": "OK",
        "purpose": "Native source step budget regression",
    }

    def run(token, plan):
        binding = plan.tools[0]
        content = BudgetedExecutor(
            case.env.budgets, case.env.queue, clock=lambda: instant[0]
        ).execute(
            token,
            "native-source-read",
            binding.upper,
            lambda: binding.tool.invoke({"path": "app.py"}),
            lambda _: {},
            purpose="tool",
        )
        assert "print(2 + 3)" in content
        plan.tools[1].tool.invoke({"plan": probe})

    coordinator.runtime = SimpleNamespace(run=run)
    result = coordinator.plan_probe(case.env.token, case.workspace, {})
    assert result.argv == probe["argv"]
    with case.env.store.read() as records:
        assert not records.find("block_reasons", resolved=0)
        reservation = records.one("budget_reservations", operation_key="native-source-read")
        assert json.loads(reservation["actual_json"])["active_ms"] == 2300


def test_verified_terminal_repair_does_not_require_new_facts_on_each_recheck(native_execution):
    from repowatch.worker.repairs import RepairCoordinator

    case = native_execution
    case.runner.services.clock = lambda: 3
    coordinator = object.__new__(RepairCoordinator)
    coordinator.services = case.runner.services
    for _ in range(4):
        coordinator.record_attempt(
            case.env.token,
            {"manifest_sha256": "a" * 64},
            "passed",
            [{"passed": True}],
            [],
            goal_completed=True,
        )
    with case.env.store.read() as records:
        assert len(records.find("progress_attempts", objective_key="repair")) == 4
        assert not records.find("block_reasons", resolved=0)
        assert not records.find("budget_progress")
