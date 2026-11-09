import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from test_formal_runtime import fixture_wire
from test_memory_runtime import prepare
from test_repository_memory import CONTENT
from test_roles import kernel as kernel

from repowatch.adapters.memory_index import MemoryIndex
from repowatch.adapters.providers import ProviderAdapter
from repowatch.application.memory import RepositoryMemory
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import TaskRecords
from repowatch.domain.task_records import canonical_json
from repowatch.runtime.engine import FormalRuntime
from repowatch.worker.memory import MemoryWorker
from repowatch.worker.repairs import RepairCoordinator
from repowatch.worker.service import Worker


def exclude(services, task):
    with services.store.write() as records:
        records.change("tasks", {"id": task}, {"user_excluded": 1})


def adapter(services, *, exclude_after_request=None):
    respond, calls = fixture_wire()

    def wire(request):
        data = respond(request).json()
        function = data["choices"][0]["message"]["tool_calls"][0]["function"]
        if function["name"] == "read_source":
            function.update(name="read_evidence", arguments="{}")
            if exclude_after_request:
                exclude(services, exclude_after_request)
        else:
            import json

            arguments = json.loads(function["arguments"])
            arguments["summary"] = canonical_json(CONTENT)
            function["arguments"] = json.dumps(arguments)
        return httpx.Response(200, json=data)

    services.adapter = ProviderAdapter(services.providers, transport=httpx.MockTransport(wire))
    return calls


@pytest.mark.parametrize("timing", ["before_schedule", "queued", "claimed", "between_requests"])
def test_excluded_source_cannot_start_or_continue_memory_calls(kernel, timing):
    services = kernel
    task, token = prepare(services)
    services.queue.complete(token, now=0, state="ended")
    worker = MemoryWorker(services, FormalRuntime(services))
    calls = adapter(services, exclude_after_request=task if timing == "between_requests" else None)
    if timing == "before_schedule":
        exclude(services, task)
        worker.schedule()
        with services.store.read() as records:
            assert not records.find("tasks", mode="distill")
    else:
        worker.schedule()
        if timing == "queued":
            exclude(services, task)
            assert services.queue.claim(now=0) is None
        else:
            current = services.queue.claim(now=0)
            if timing == "claimed":
                exclude(services, task)
            assert worker.run(current)["reason"] == "memory_source_unavailable"
            with services.store.write() as records:
                records.change("tasks", {"id": task}, {"user_excluded": 0})
            worker.schedule()
            with services.store.read() as records:
                assert len(records.find("tasks", mode="distill")) == 1
                assert records.find("tasks", mode="distill")[0]["state"] == "queued"
    assert len(calls) == (1 if timing == "between_requests" else 0)
    budget = services.budgets.view(task)
    assert budget["used"]["model_calls"] == len(calls)
    assert budget["reserved"]["model_calls"] == 0
    with services.store.read() as records:
        assert not records.find("repository_memories")
        assert records.one("distillation_intents", id="memory-intent")["state"] == "pending"
        assert not records.find("block_reasons", resolved=0)


@pytest.mark.parametrize(
    "problem", [sqlite3.OperationalError("locked"), OSError("disk"), RuntimeError("derived index")]
)
def test_index_failure_does_not_fail_completed_memory_fact(kernel, monkeypatch, problem):
    services = kernel
    task, token = prepare(services)
    services.queue.complete(token, now=0, state="ended")
    calls = adapter(services)
    services.memory_index = MemoryIndex(services.settings, services.memories)
    rebuild = services.memory_index.rebuild

    def fail_index():
        raise problem

    monkeypatch.setattr(services.memory_index, "rebuild", fail_index)
    result = Worker(services).run_once()
    assert result["state"] == "ended"
    assert result["index"]["state"] == "deferred"
    with services.store.read() as records:
        assert len(records.find("repository_memories")) == 1
        assert records.one("distillation_intents", id="memory-intent")["state"] == "consumed"
        assert records.find("memory_index_events", indexed=0)
        assert records.find("tasks", mode="distill")[0]["state"] == "ended"
        assert not records.find("block_reasons", resolved=0)
        assert any(
            row["kind"] == "memory_index_deferred" for row in records.find("business_events")
        )
    assert len(calls) == 2
    monkeypatch.setattr(services.memory_index, "rebuild", rebuild)
    assert services.memory_index.rebuild()["count"] == 1
    with services.store.read() as records:
        assert not records.find("memory_index_events", indexed=0)


def test_actual_business_storage_failure_is_not_swallowed(kernel, monkeypatch):
    services = kernel
    _, token = prepare(services)
    services.queue.complete(token, now=0, state="ended")
    adapter(services)

    def fail_store():
        raise StorageError("business store unavailable")

    services.memory_index = SimpleNamespace(rebuild=fail_store)
    assert Worker(services).run_once()["state"] == "reconciliation_required"


@pytest.mark.parametrize(
    "problem",
    [
        ConflictError("constraint"),
        PermissionError("artifact"),
        ValueError("content"),
        LookupError("source"),
    ],
)
def test_optional_failure_memory_conflict_is_isolated(kernel, monkeypatch, problem):
    services = kernel
    task, token = prepare(services)
    coordinator = RepairCoordinator.__new__(RepairCoordinator)
    coordinator.services = services

    def fail_memory(*args, **kwargs):
        raise problem

    monkeypatch.setattr(RepositoryMemory, "record_attempt", fail_memory)
    coordinator.record_failure_memory(token, "memory-session", "attempt", CONTENT, [])
    with services.store.read() as records:
        assert not records.find("block_reasons", resolved=0)
        assert records.one("tasks", id=task)["state"] == "running"
        assert any(
            row["kind"] == "failure_memory_skipped" for row in records.find("business_events")
        )


def test_failure_memory_storage_error_remains_fail_closed(kernel, monkeypatch):
    services = kernel
    _, token = prepare(services)
    coordinator = RepairCoordinator.__new__(RepairCoordinator)
    coordinator.services = services

    def fail_memory(*args, **kwargs):
        raise StorageError("business store unavailable")

    monkeypatch.setattr(RepositoryMemory, "record_attempt", fail_memory)
    with pytest.raises(StorageError):
        coordinator.record_failure_memory(token, "memory-session", "attempt", CONTENT, [])


def test_real_exclusion_does_not_implicitly_restore_revoked_budget(kernel):
    services = kernel
    task, token = prepare(services)
    services.queue.complete(token, now=0, state="ended")
    worker = MemoryWorker(services, FormalRuntime(services))
    worker.schedule()
    current = services.queue.claim(now=0)
    with services.store.read() as records:
        revision = records.one("tasks", id=task)["revision"]
    TaskRecords(services.store).operate(
        "actual-exclude", task, expected_revision=revision, action="exclude", now=0
    )
    result = worker.run(current)
    with services.store.read() as records:
        maintenance = records.find("tasks", mode="distill")[0]
        assert result["state"] == maintenance["state"] == "blocked"
        assert records.one("budget_ledgers", owner_task_id=task)["revoked"]
        records_snapshot = dict(maintenance)
    with services.store.write() as records:
        records.change("tasks", {"id": task}, {"user_excluded": 0})
    worker.schedule()
    assert services.queue.claim(now=0) is None
    with services.store.read() as records:
        assert dict(records.one("tasks", id=records_snapshot["id"]))["state"] == "blocked"


def test_late_failure_enters_review_and_never_restores_as_success(kernel):
    services = kernel
    task, token = prepare(services)
    with services.store.write() as records:
        evidence = records.one("repair_sessions", id="memory-session")["evidence_json"]
        import json

        artifact_ids = json.loads(evidence)["artifact_ids"]
        records.change(
            "repair_sessions", {"id": "memory-session"}, {"state": "blocked", "evidence_json": None}
        )
    TaskRecords(services.store).observe(
        task, "late-feedback", "1", {"body": "new feedback"}, kind="feedback", now=0
    )
    identifier = services.memories.record_attempt(
        "memory-session", "failed-attempt", CONTENT, artifact_ids, now=0
    )
    item = services.memories.get(identifier)
    assert item["state"] == "pending_review"
    assert item["prior_state"] == "failure"
    services.queue.complete(token, now=0, state="waiting", wait_reason="pr_review")
    MemoryWorker(services).schedule()
    with services.store.read() as records:
        assert any(
            '"kind":"feedback"' in row["input_json"]
            for row in records.find("tasks", mode="distill")
        )
    services.memories.review_feedback(
        identifier,
        expected_revision=item["revision"],
        feedback_revision=item["feedback_revision"],
        unrelated=False,
        source_sha256=item["source"]["source_sha256"],
    )
    TaskRecords(services.store).observe(
        task, "late-feedback", "2", {"body": "format only"}, kind="feedback", now=0
    )
    item = services.memories.get(identifier)
    services.memories.review_feedback(
        identifier,
        expected_revision=item["revision"],
        feedback_revision=item["feedback_revision"],
        unrelated=True,
        source_sha256=item["source"]["source_sha256"],
    )
    assert services.memories.get(identifier)["state"] == "failure"


@pytest.mark.parametrize("empty_file", [True, False])
def test_status_and_search_do_not_initialize_derived_database(kernel, empty_file):
    services = kernel
    task, _ = prepare(services)
    index = MemoryIndex(services.settings, services.memories)
    target = services.settings.index_database
    if empty_file:
        target.touch()
    else:
        with sqlite3.connect(target) as connection:
            connection.execute("CREATE TABLE sentinel(value TEXT)")
    content, modified = target.read_bytes(), target.stat().st_mtime_ns
    assert index.status()["degradation"] == "index_empty"
    with services.store.read() as records:
        repository = records.one("tasks", id=task)["repository_id"]
    assert index.search(repository, "乘法")["mode"] == "range"
    assert target.read_bytes() == content
    assert target.stat().st_mtime_ns == modified
    with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert not names & {"entries", "terms", "metadata"}
