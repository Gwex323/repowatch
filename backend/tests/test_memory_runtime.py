import json
from dataclasses import replace

import httpx
import pytest
from langchain_core.tools import tool
from test_formal_runtime import fixture_wire, plan, start
from test_repository_memory import CONTENT, source
from test_roles import bind, diagnose
from test_roles import kernel as kernel

from repowatch.adapters.providers import ProviderAdapter
from repowatch.application.memory import RepositoryMemory
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.runtime.engine import FormalRuntime, ToolBinding
from repowatch.worker.memory import MemoryWorker
from repowatch.worker.model_calls import BudgetedChatModel


def prepare(kernel):
    services = kernel
    bind(services)
    diagnose(services, "processor")
    task, token = start(services)
    services.clock = lambda: 0
    fixture = (
        services.settings,
        services.store,
        services.queue,
        services.budgets,
        services.artifacts,
        token,
    )
    source(fixture)
    services.memories = RepositoryMemory(services.store, services.artifacts)
    return task, token


def test_sdk_distillation_is_read_only_and_charged_to_original_owner(kernel):
    services = kernel
    task, token = prepare(services)
    services.queue.complete(token, now=0, state="ended")
    original = services.budgets.view(task)["used"]["model_calls"]
    worker = MemoryWorker(services, FormalRuntime(services))
    worker.schedule()
    worker.schedule()
    with services.store.read() as records:
        pending = records.find("tasks", mode="distill")
        assert len(pending) == 1
        assert pending[0]["budget_owner_task_id"] == task
    respond, calls = fixture_wire()

    def wire(request):
        response = respond(request)
        data = response.json()
        function = data["choices"][0]["message"]["tool_calls"][0]["function"]
        if function["name"] == "read_source":
            function.update(name="read_evidence", arguments="{}")
        else:
            arguments = json.loads(function["arguments"])
            arguments["summary"] = canonical_json(CONTENT)
            function["arguments"] = json.dumps(arguments)
        return httpx.Response(200, json=data)

    services.adapter = ProviderAdapter(services.providers, transport=httpx.MockTransport(wire))
    maintenance = services.queue.claim(now=0)
    assert worker.run(maintenance)["state"] == "ended"
    assert services.budgets.view(task)["used"]["model_calls"] == original + 2
    with services.store.read() as records:
        assert records.one("distillation_intents", id="memory-intent")["state"] == "consumed"
        memory = records.find("repository_memories")[0]
        assert memory["state"] == "success"
        assert not records.find("memory_shares")
    for body in calls:
        names = {item["function"]["name"] for item in body["tools"]}
        assert not names & {"read_source", "write_source", "request_check"}
    worker.schedule()
    with services.store.read() as records:
        assert len(records.find("tasks", mode="distill")) == 1


def test_deletion_during_sdk_session_rebuilds_without_old_summary(kernel):
    services = kernel
    _, token = prepare(services)
    identifier = services.memories.record_verified("memory-intent", CONTENT, now=0)
    seen = []

    @tool
    def read_source(path: str) -> str:
        """读取当前受控源码，不回传任何历史记忆。"""
        if not seen:
            item = services.memories.get(identifier)
            services.memories.operate(
                "delete-during-run",
                identifier,
                expected_revision=item["revision"],
                action="delete",
                data={},
            )
        seen.append(path)
        return "current source only"

    respond, calls = fixture_wire()
    services.adapter = ProviderAdapter(services.providers, transport=httpx.MockTransport(respond))
    stage = replace(
        plan(seen),
        objective="乘法",
        tools=(ToolBinding(read_source, BudgetAmounts(active_ms=1000)),),
    )
    result = FormalRuntime(services).run(token, stage)
    assert result.status == "completed"
    assert len(seen) == 2
    assert CONTENT["method"] in json.dumps(calls[0], ensure_ascii=False)
    for body in calls[1:]:
        assert CONTENT["method"] not in json.dumps(body, ensure_ascii=False)
    with services.store.read() as records:
        injections = records.find("memory_injections", run_id=token.run_id)
        assert len(injections) == 2
        assert any(not json.loads(item["dependencies_json"]) for item in injections)


def test_paused_worker_does_not_consume_distillation_intent(kernel):
    services = kernel
    task, token = prepare(services)
    services.queue.complete(token, now=0, state="waiting", wait_reason="pr_review")
    services.queue.set_pause(True, now=0)
    worker = MemoryWorker(services)
    worker.schedule()
    assert services.queue.claim(now=0) is None
    assert services.budgets.view(task)["used"]["model_calls"] == 0
    with services.store.read() as records:
        assert records.one("distillation_intents", id="memory-intent")["state"] == "pending"


@pytest.mark.parametrize(
    "unrelated,change,expected",
    [
        (True, None, "success"),
        (False, None, "pending_review"),
        (True, "delete", "success"),
        (True, "edit", "manual_unverified"),
    ],
)
def test_worker_feedback_review_cannot_overwrite_user_change(kernel, unrelated, change, expected):
    services = kernel
    task, token = prepare(services)
    identifier = services.memories.record_verified("memory-intent", CONTENT, now=0)
    services.queue.complete(token, now=0, state="waiting", wait_reason="pr_review")
    with services.store.write() as records:
        records.change("distillation_intents", {"id": "memory-intent"}, {"state": "consumed"})
    TaskRecords(services.store).observe(
        task, "new-feedback", "1", {"body": "review"}, kind="feedback", now=0
    )
    with services.store.write() as records:
        records.change("tasks", {"id": task}, {"state": "waiting", "wait_reason": "pr_review"})
    worker = MemoryWorker(services, FormalRuntime(services))
    worker.schedule()
    respond, calls = fixture_wire()

    def wire(request):
        data = respond(request).json()
        function = data["choices"][0]["message"]["tool_calls"][0]["function"]
        if function["name"] == "read_source":
            function.update(name="read_evidence", arguments="{}")
        else:
            if change:
                item = services.memories.get(identifier)
                services.memories.operate(
                    "concurrent-change",
                    identifier,
                    expected_revision=item["revision"],
                    action=change,
                    data={"content": {**CONTENT, "method": "人工变化"}},
                )
            arguments = json.loads(function["arguments"])
            arguments["summary"] = canonical_json(
                {"unrelated": unrelated, "rationale": "受控反馈证据"}
            )
            function["arguments"] = json.dumps(arguments)
        return httpx.Response(200, json=data)

    services.adapter = ProviderAdapter(services.providers, transport=httpx.MockTransport(wire))
    current = services.queue.claim(now=0)
    assert worker.run(current)["state"] == "ended"
    item = services.memories.get(identifier)
    if change == "delete":
        assert item["deleted"] == 1
        assert item["state"] == "feedback_pending"
    else:
        assert item["state"] == expected
    assert len(calls) == 2
    with services.store.read() as records:
        assert not records.one("global_control", singleton=1)["paused"]


@pytest.mark.parametrize("before_child_request", [False, True])
def test_parent_inherits_child_memory_dependencies_and_cannot_resend_deleted_summary(
    kernel, monkeypatch, before_child_request
):
    services = kernel
    bind(services, "subtask.investigator", model="child-model")
    diagnose(services, "processor", key="child-memory-capability", model="child-model")
    _, token = prepare(services)
    identifier = services.memories.record_verified("memory-intent", CONTENT, now=0)
    original_complete = services.queue.complete
    deleted_at = []
    respond, calls = fixture_wire(child=True)

    def complete(current, **kwargs):
        result = original_complete(current, **kwargs)
        if current.authority_run_id and not deleted_at and not before_child_request:
            item = services.memories.get(identifier)
            services.memories.operate(
                "delete-child-memory",
                identifier,
                expected_revision=item["revision"],
                action="delete",
                data={},
            )
            deleted_at.append(len(calls))
        return result

    def wire(request):
        data = respond(request).json()
        body = json.loads(request.content)
        function = data["choices"][0]["message"]["tool_calls"][0]["function"]
        if body["model"] == "child-model" and function["name"] == "StructuredResult":
            arguments = json.loads(function["arguments"])
            arguments["summary"] = CONTENT["method"] if not deleted_at else "clean child result"
            function["arguments"] = json.dumps(arguments)
        return httpx.Response(200, json=data)

    monkeypatch.setattr(services.queue, "complete", complete)
    original_preallocate = BudgetedChatModel.preallocate

    def preallocate(model):
        result = original_preallocate(model)
        if model.token.authority_run_id and before_child_request and not deleted_at:
            item = services.memories.get(identifier)
            services.memories.operate(
                "delete-before-child-request",
                identifier,
                expected_revision=item["revision"],
                action="delete",
                data={},
            )
            deleted_at.append(len(calls))
        return result

    monkeypatch.setattr(BudgetedChatModel, "preallocate", preallocate)
    services.adapter = ProviderAdapter(services.providers, transport=httpx.MockTransport(wire))
    reads = []
    parent = plan(reads)
    child = replace(parent, key="child-read", objective="乘法", role_key="subtask.investigator")
    parent = replace(parent, children=(child,))
    result = FormalRuntime(services).run(token, parent)
    assert result.status == "completed"
    assert deleted_at
    assert len(calls) == (5 if before_child_request else 7)
    assert services.budgets.view(result.context.task_id)["reserved"]["model_calls"] == 0
    for body in calls[deleted_at[0] :]:
        assert CONTENT["method"] not in json.dumps(body, ensure_ascii=False)
