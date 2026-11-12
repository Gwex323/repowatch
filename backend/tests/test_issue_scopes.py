import json
from types import SimpleNamespace

import pytest
from test_control_api import api as api
from test_control_api import mutation
from test_roles import kernel as kernel

from repowatch.application.acceptance import acceptance_view
from repowatch.application.candidate_issues import CandidateIssues
from repowatch.application.issue_scopes import decide_scope, scope_children, scope_view
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.domain.triage import TriageAnalysis

GOALS = [
    {"objective": "修复空游标", "criteria": ["末页返回成功"]},
    {"objective": "修复重复重试", "criteria": ["一次超时只重试一次"]},
]


def seed_scope(services, suffix="scope"):
    repository = services.tasks.register_repository(suffix, admission_mode="all_bugs")
    task = services.tasks.ensure_task(repository, "issue:51", issue_id="51", now=0)
    with services.store.write() as records:
        records.change(
            "tasks",
            {"id": task},
            {
                "state": "waiting",
                "wait_reason": "approval",
                "input_json": canonical_json({"number": 51, "title": "两个独立缺陷"}),
            },
        )
        records.add(
            "triage_decisions",
            {
                "id": "scope-" + suffix,
                "task_id": task,
                "input_revision": 1,
                "feedback_revision": 1,
                "admission_revision": 1,
                "result": "needs_approval",
                "reason_code": "scope_confirmation",
                "evidence_json": canonical_json(
                    {
                        "analysis": {
                            "independent_goals": [item["objective"] for item in GOALS],
                        }
                    }
                ),
            },
        )
    with services.store.read() as records:
        scope = scope_view(records, records.one("tasks", id=task))
    return repository, task, scope


def decide(services, task, scope, kind="split", operation="split"):
    return decide_scope(
        services.store,
        operation,
        task,
        expected_revision=1,
        context_digest=scope["context_digest"],
        kind=kind,
        goals=GOALS if kind == "split" else GOALS[:1],
        explanation="已核对触发条件和交付边界",
        now=0,
    )


def test_scope_api_is_explicit_idempotent_and_preserves_pause_and_budget(api):
    client, headers, services, _ = api
    _, task, scope = seed_scope(services)
    before = services.budgets.view(task)
    path = f"/api/v1/tasks/{task}/scope/decide"
    body = mutation(
        "split",
        1,
        context_digest=scope["context_digest"],
        kind="split",
        goals=GOALS,
        explanation="各自独立验收",
    )
    assert client.post(path, json=body).status_code == 403
    result = client.post(path, json=body, headers=headers)
    assert result.status_code == 200, result.text
    assert len(result.json()["children"]) == 2
    assert client.post(path, json=body, headers=headers).json() == result.json()
    assert services.budgets.view(task) == before
    parent = client.get(f"/api/v1/tasks/{task}").json()
    assert parent["wait_reason"] == "scope_children"
    assert len(parent["scope_children"]) == 2
    state = client.get("/api/v1/state").json()
    assert state["control"]["paused"] == 1
    with services.store.read() as records:
        assert len(records.find("budget_ledgers")) == 1
        assert records.find("runs") == []
        assert records.find("publication_intents") == []
        assert all(row["budget_owner_task_id"] == task for row in records.find("tasks"))
    with pytest.raises(ConflictError, match="原跟踪项"):
        services.tasks.operate(
            "restart", task, expected_revision=parent["revision"], action="select", now=0
        )


@pytest.mark.parametrize(
    "change",
    ["input_revision", "feedback_revision", "admission_revision", "target_revision", "bound_pr"],
)
def test_scope_rejects_stale_evidence_or_existing_delivery(api, change):
    _, _, services, _ = api
    repo, task, scope = seed_scope(services)
    if change == "bound_pr":
        services.tasks.bind_managed_pr(task, "repo", "pull")
    else:
        with services.store.write() as records:
            table, identifier = (
                ("tasks", task)
                if change.endswith("put_revision") or change == "feedback_revision"
                else ("repositories", repo)
            )
            records.change(table, {"id": identifier}, {change: 2})
    with pytest.raises(ConflictError):
        decide(services, task, scope)
    with services.store.read() as records:
        assert len(records.find("tasks")) == 1
        assert records.find("user_operations") == []


def test_converge_preserves_original_issue_and_approves_only_one_goal(api):
    _, _, services, _ = api
    _, task, scope = seed_scope(services)
    result = decide(services, task, scope, kind="converge")
    assert result["children"] == []
    with services.store.read() as records:
        current = records.one("tasks", id=task)
        spec = acceptance_view(records, current)
        assert spec["approved"] and spec["content"] == GOALS[0]
        assert current["issue_id"] == "51" and current["state"] == "queued"
        assert json.loads(current["input_json"])["title"] == "两个独立缺陷"
        assert records.find("publication_intents") == []


def test_split_history_survives_remote_observation_and_cannot_duplicate(api):
    _, _, services, _ = api
    _, task, scope = seed_scope(services)
    first = decide(services, task, scope)
    services.tasks.observe(
        task, "github:issue:51", "remote-v2", {"number": 51, "title": "远端更新"}, now=1
    )
    with services.store.read() as records:
        parent = records.one("tasks", id=task)
        assert scope_children(records, parent) == first["children"]
        revision = parent["revision"]
    with pytest.raises(ConflictError):
        services.tasks.operate("restart", task, expected_revision=revision, action="select", now=1)
    with services.store.read() as records:
        assert len(records.find("tasks")) == 3


def test_split_children_use_existing_lookup_and_receipt_reconciliation(kernel, monkeypatch):
    services = kernel
    services.tasks = TaskRecords(services.store)
    services.clock = lambda: 0
    services.vault = SimpleNamespace(read=lambda _: None)
    repo, parent, scope = seed_scope(services)
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repo},
            {
                "github_owner": "fixture",
                "github_name": "repo",
                "identity_json": canonical_json({"execution_login": "worker"}),
            },
        )
    services.budgets.authorize_initial(
        "budget",
        parent,
        BudgetAmounts(
            active_ms=2000000, model_calls=10, tokens=10000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    result = decide(services, parent, scope)
    child = result["children"][0]["task_id"]
    with services.store.write() as records:
        records.change("tasks", {"id": child}, {"manual_priority": 100})
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    with services.store.read() as records:
        assert records.one("runs", id=token.run_id)["task_id"] == child
    items = [{"id": 51, "number": 51, "title": "两个独立缺陷", "state": "open"}]
    writes = []

    class Port:
        def __init__(self, **_):
            pass

        def _path(self, *_):
            return "/repos/fixture/repo"

        def collection(self, *_, **__):
            return list(items)

        def issue(self, *args):
            return next(item for item in items if item["number"] == args[-1])

        def write(self, method, path, body):
            writes.append(body)
            item = {"id": 52, "number": 52, "state": "open", **body, "user": {"login": "worker"}}
            items.append(item)
            raise PermissionError("receipt lost")

    monkeypatch.setattr("repowatch.application.candidate_issues.GitHubPublisher", Port)
    resolver = CandidateIssues(services)
    analysis = TriageAnalysis(kind="bug", **GOALS[0], reproduction_steps=["运行固定探针"])
    lookup = resolver.read(token)
    with pytest.raises(ConflictError, match="原 Issue"):
        resolver.resolve(
            token, analysis, lookup, {"relation": "same", "issue_number": 51, "evidence": "same"}
        )
    assert writes == []
    with pytest.raises(PermissionError, match="receipt lost"):
        resolver.resolve(
            token, analysis, lookup, {"relation": "none", "evidence": "已核对全部 Issue"}
        )
    target = resolver.resolve(
        token, TriageAnalysis(kind="unknown", objective="核对回执"), resolver.read(token), {}
    )
    assert len(writes) == 1
    with services.store.read() as records:
        assert records.one("tasks", id=target)["issue_id"] == "52"
        assert records.one("tasks", id=target)["budget_owner_task_id"] == parent
        assert scope_children(records, records.one("tasks", id=parent))[0]["task_id"] == target
        assert len(records.find("tasks", issue_id="52")) == 1
        assert len(records.find("budget_ledgers")) == 1


def test_state_projection_uses_current_triage_and_never_requires_a_timestamp(api):
    client, _, services, _ = api
    repo, task, _ = seed_scope(services)
    with services.store.write() as records:
        records.change(
            "triage_decisions",
            {"id": "scope-scope"},
            {
                "reason_code": "issue_lookup_required",
                "evidence_json": canonical_json(
                    {
                        "analysis": {"issue_relation": {"issue_number": 42}},
                        "issues": [{"number": 42}],
                    }
                ),
            },
        )
    row = next(row for row in client.get("/api/v1/state").json()["tasks"] if row["id"] == task)
    assert row["similar_issue_number"] == 42
    with services.store.write() as records:
        records.change("repositories", {"id": repo}, {"admission_revision": 2})
    row = next(row for row in client.get("/api/v1/state").json()["tasks"] if row["id"] == task)
    assert "similar_issue_number" not in row
