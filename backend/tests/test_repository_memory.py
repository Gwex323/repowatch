import json

import pytest
from test_artifacts import kernel as kernel

from repowatch.application.memory import RepositoryMemory
from repowatch.application.storage import ConflictError
from repowatch.domain.task_records import canonical_json, content_digest

CONTENT = {
    "symptoms": "乘法返回加法结果",
    "applicability": "整数算术函数",
    "method": "改为乘法并保持零值行为",
    "verification": "相同探针修改前失败、修改后通过，独审通过",
    "limitations": "根因未单独证实；当前仓库范围",
}


def source(kernel, *, verified=True):
    _, store, _, _, artifacts, token = kernel
    artifact = artifacts.register(token, "memory-proof", b"probe output", context={}, now=0)
    with store.write() as records:
        run = dict(records.one("runs", id=token.run_id))
        task = dict(records.one("tasks", id=run["task_id"]))
        records.add(
            "runs",
            {
                **run,
                "id": "review-run",
                "state": "ended",
                "parent_run_id": run["id"],
                "snapshot_json": '{"mode":"review"}',
            },
        )
        records.add(
            "workspaces",
            {
                "id": "memory-workspace",
                "task_id": task["id"],
                "repository_id": task["repository_id"],
                "snapshot_json": "{}",
                "state": "ready",
                "created_at": 0,
            },
        )
        frozen = {
            "base_sha": "a" * 40,
            "sha": "b" * 40,
            "tree_sha": "c" * 40,
            "manifest_sha256": "d" * 64,
            "feedback_revision": task["feedback_revision"],
        }
        evidence = {
            "passed": True,
            "review_passed": True,
            "review_run_id": "review-run",
            "artifact_ids": [artifact],
        }
        records.add(
            "repair_sessions",
            {
                "id": "memory-session",
                "task_id": task["id"],
                "run_id": run["id"],
                "workspace_id": "memory-workspace",
                "base_sha": "a" * 40,
                "state": "verified" if verified else "blocked",
                "snapshot_json": "{}",
                "frozen_json": canonical_json(frozen),
                "evidence_json": canonical_json(evidence),
                "created_at": 0,
            },
        )
        records.add(
            "distillation_intents",
            {
                "id": "memory-intent",
                "task_id": task["id"],
                "session_id": "memory-session",
                "feedback_revision": task["feedback_revision"],
                "source_sha256": content_digest({"frozen": frozen, "evidence": evidence}),
            },
        )
    return task, artifact


def test_manual_cannot_claim_verified_state_and_is_repository_scoped(kernel):
    _, store, _, _, artifacts, _ = kernel
    task, _ = source(kernel)
    service = RepositoryMemory(store, artifacts)
    identifier = service.record_manual(task["repository_id"], "hypothesis", CONTENT, now=0)
    assert service.record_manual(task["repository_id"], "hypothesis", CONTENT, now=1) == identifier
    item = service.list(task["repository_id"])[0]
    assert item["state"] == "manual_unverified"
    assert item["visibility"] == "repository"
    assert item["delivery_state"] == "unknown"
    with pytest.raises(ConflictError):
        service.record_manual(
            task["repository_id"], "hypothesis", {**CONTENT, "method": "不同"}, now=0
        )


def test_verified_record_keeps_code_and_actual_artifact_references(kernel):
    _, store, _, _, artifacts, _ = kernel
    task, artifact = source(kernel)
    service = RepositoryMemory(store, artifacts)
    identifier = service.record_verified("memory-intent", CONTENT, now=0)
    item = service.list(task["repository_id"])[0]
    assert item["state"] == "success"
    assert item["source"]["candidate_sha"] == "b" * 40
    assert item["source"]["artifacts"][0]["id"] == artifact
    assert service.record_verified("memory-intent", CONTENT, now=1) == identifier
    reopened = type(store)(store.settings)
    try:
        assert RepositoryMemory(reopened, artifacts).list(task["repository_id"])[0] == item
    finally:
        reopened.close()


def test_new_feedback_before_commit_retains_pending_state(kernel):
    _, store, _, _, artifacts, _ = kernel
    task, _ = source(kernel)
    with store.write() as records:
        records.change(
            "tasks", {"id": task["id"]}, {"feedback_revision": task["feedback_revision"] + 1}
        )
    service = RepositoryMemory(store, artifacts)
    service.record_verified("memory-intent", CONTENT, now=0)
    assert service.list(task["repository_id"])[0]["state"] == "pending_review"


def test_failure_does_not_become_success_and_requires_evidence(kernel):
    _, store, _, _, artifacts, _ = kernel
    task, artifact = source(kernel, verified=False)
    service = RepositoryMemory(store, artifacts)
    with pytest.raises(PermissionError):
        service.record_verified("memory-intent", CONTENT, now=0)
    with pytest.raises(ValueError):
        service.record_attempt("memory-session", "attempt-1", CONTENT, [], now=0)
    service.record_attempt("memory-session", "attempt-1", CONTENT, [artifact], now=0)
    assert service.list(task["repository_id"])[0]["state"] == "failure"


def test_missing_or_changed_artifact_cannot_become_memory(kernel):
    settings, store, _, _, artifacts, _ = kernel
    task, artifact = source(kernel)
    path = settings.artifacts_dir / artifacts.require(artifact, now=0)["relative_path"]
    path.write_bytes(b"tampered")
    service = RepositoryMemory(store, artifacts)
    with pytest.raises(PermissionError):
        service.record_verified("memory-intent", CONTENT, now=0)
    assert service.list(task["repository_id"]) == []


def test_source_history_cannot_be_overwritten_or_deleted(kernel):
    _, store, _, _, artifacts, _ = kernel
    task, _ = source(kernel)
    service = RepositoryMemory(store, artifacts)
    identifier = service.record_verified("memory-intent", CONTENT, now=0)
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.change("repository_memories", {"id": identifier}, {"source_json": "{}"})
    with pytest.raises(ConflictError):
        with store.write() as records:
            records.remove("repository_memories", id=identifier)
    with store.read() as records:
        assert (
            len(
                [
                    item
                    for item in records.find("business_events")
                    if item["kind"] == "memory_recorded"
                ]
            )
            == 1
        )
        assert (
            json.loads(records.one("repository_memories", id=identifier)["content_json"]) == CONTENT
        )
