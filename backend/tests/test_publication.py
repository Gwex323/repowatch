import json
from types import SimpleNamespace

import pytest
from test_roles import kernel as kernel

from repowatch.application.github_ports import GitHubReadError
from repowatch.application.publication import Publisher, pr_body
from repowatch.application.publication_paths import FORK_DISPATCHED
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.scans import readiness
from repowatch.domain.task_records import canonical_json


@pytest.mark.parametrize("lost", [False, True])
@pytest.mark.parametrize(
    "path,gate",
    [
        (path, gate)
        for path in ("direct", "fork")
        for gate in ("allowed", "occupied", "uncertain_pr", "merged_verified", "merged_unverified")
    ]
    + [
        ("auto-direct", "allowed"),
        ("auto-fork", "allowed"),
        ("auto-existing", "allowed"),
        ("auto-delayed", "allowed"),
        ("auto-conflict", "allowed"),
        ("auto-fork", "occupied"),
    ],
)
def test_publication_identity_guard_and_lost_receipt_reconciliation(
    kernel, monkeypatch, path, lost, gate
):
    services = kernel
    services.clock = lambda: 0
    services.tasks = TaskRecords(services.store)
    services.vault = SimpleNamespace(read=lambda reference: None)
    repo = services.tasks.register_repository("100", admission_mode="all_bugs")
    task = services.tasks.ensure_task(repo, "issue:1", issue_id="101", mode="implement", now=0)
    automatic = path.startswith("auto-")
    direct = path in {"direct", "auto-direct"}
    identity = {"execution_login": "worker"}
    if automatic:
        identity.update(contribution_path="auto", execution_user_id="18")
    publication = {
        "github_id": "100" if direct else "200",
        "owner": "upstream" if direct else "worker",
        "name": "repo",
    }
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repo},
            {
                "target_sha": "a" * 40,
                "scan_ready_sha": "a" * 40,
                "target_branch": "main",
                "github_owner": "upstream",
                "github_name": "repo",
                "publication_json": None if automatic else canonical_json(publication),
                "credential_reference": "fixture",
            },
        )
        records.change(
            "tasks", {"id": task}, {"input_json": canonical_json({"number": 1, "title": "sum bug"})}
        )
        records.add(
            "triage_decisions",
            {
                "id": "triage",
                "task_id": task,
                "input_revision": 1,
                "feedback_revision": 1,
                "admission_revision": 1,
                "result": "auto_repair",
                "reason_code": "bug",
                "evidence_json": canonical_json(
                    {
                        "analysis": {
                            "kind": "bug",
                            "objective": "sum",
                            "criteria": ["sum correct"],
                            "reproduction_steps": ["call sum"],
                            "ownership": "available",
                            "ownership_evidence": ["no claim"],
                        }
                    }
                ),
            },
        )
    services.budgets.authorize_initial(
        "budget",
        task,
        BudgetAmounts(
            active_ms=2000000, model_calls=10, tokens=100000, repair_rounds=5, recovery_rounds=5
        ),
        expected_revision=1,
        reason="fixture",
        now=0,
    )
    services.queue.set_pause(False, now=0)
    token = services.queue.claim(now=0)
    with services.store.write() as records:
        repository = records.one("repositories", id=repo)
        version = canonical_json(readiness(repository))
        records.add(
            "scan_batches",
            {
                "id": "scan-ready",
                "task_id": task,
                "repository_id": repo,
                "version_json": version,
                "manifest_json": "{}",
                "checks_json": "[]",
                "environment_json": "{}",
                "state": "completed",
                "created_at": 0,
            },
        )
        records.change(
            "repositories",
            {"id": repo},
            {
                "identity_json": canonical_json(identity),
                "scan_ready_json": version,
                "scan_ready_batch_id": "scan-ready",
            },
        )
    services.repositories = SimpleNamespace(require_ready=lambda *args, **kwargs: True)
    services.queue.target_gate = lambda *args, **kwargs: True
    calls, pulls = [], []
    candidate_sha = "b" * 40
    branch_sha = ""
    fork_exists = path in {"auto-existing", "auto-conflict"}
    fork_writes = []

    class Objects:
        def repository(self, identifier):
            return identifier

        def run(self, bare, *arguments, **keywords):
            nonlocal branch_sha
            calls.append(arguments)
            if arguments[0] == "ls-remote":
                return branch_sha.encode()
            if arguments[0] == "merge-base":
                return branch_sha.encode()
            if arguments[0] == "push":
                branch_sha = arguments[-1].split(":", 1)[0]
            return b""

    class Port:
        def __init__(self, **kwargs):
            pass

        def _path(self, owner, name):
            return "/repos/" + owner + "/" + name

        def repository(self, owner, name):
            return {"github_id": "100" if owner == "upstream" else "200"}

        def authenticated_user(self):
            return {"id": "18", "login": "worker"}

        def _get(self, path):
            owner = "upstream" if path == "/repos/upstream/repo" else "worker"
            if owner == "worker" and not fork_exists:
                raise GitHubReadError("not_found")
            return (
                200,
                {
                    "id": 100 if owner == "upstream" else 200,
                    "owner": {"login": owner},
                    "name": "repo",
                    "private": False,
                    "permissions": {"push": direct or owner == "worker"},
                    "fork": owner == "worker",
                    "parent": {"id": 999 if path == "/repos/worker/repo" and conflict else 100},
                },
                {},
            )

        def collection(self, path, **kwargs):
            return pulls

        def pull_request(self, owner, name, number):
            return pulls[0]

        def write(self, method, path, body):
            nonlocal fork_exists
            if path.endswith("/forks"):
                assert method == "POST" and path == "/repos/upstream/repo/forks"
                with services.store.read() as records:
                    assert records.find("business_events", kind=FORK_DISPATCHED)
                fork_writes.append(path)
                if delayed:
                    raise PermissionError("fork receipt lost")
                fork_exists = True
                return {}
            if method == "PATCH":
                pulls[0]["body"] = body["body"]
                pulls[0]["head"]["sha"] = candidate_sha
                return pulls[0]
            pulls.append(
                {
                    "id": 500,
                    "number": 2,
                    "body": body["body"],
                    "state": "open",
                    "user": {"login": "worker"},
                    "html_url": "https://github.com/upstream/repo/pull/2",
                    "head": {"sha": "b" * 40, "repo": {"id": int(publication["github_id"])}},
                    "base": {"ref": "main", "repo": {"id": 100}},
                }
            )
            pulls[-1]["head"]["ref"] = body["head"].split(":", 1)[1]
            if lost:
                raise PermissionError("receipt lost")
            return pulls[0]

    evidence = SimpleNamespace(
        require_current=lambda *args: (
            {
                "state": "verified",
                "evidence_json": canonical_json(
                    {
                        "passed": True,
                        "review_passed": True,
                        "review": {"passed": True},
                        "external_merge": {
                            "pr_id": "99",
                            "context_digest": "same",
                            "base_sha": "a" * 40,
                            "target_still_fails": True,
                        }
                        if gate == "merged_verified"
                        else None,
                    }
                ),
            },
            {"sha": candidate_sha, "base_sha": "a" * 40},
        ),
        workspaces=SimpleNamespace(backend=SimpleNamespace(objects=Objects())),
    )
    monkeypatch.setattr("repowatch.application.publication.GitHubPublisher", Port)
    monkeypatch.setattr(
        "repowatch.application.publication.IssueContexts.read",
        lambda self, token: {
            "digest": "same",
            "facts": {
                "issue": {"id": 101, "number": 1},
                "pull_requests": [{"id": 99, "state": "open"}]
                if gate == "uncertain_pr"
                else [{"id": 99, "state": "closed", "merged": True}]
                if gate.startswith("merged_")
                else [],
            },
            "decisions": {"ownership": "occupied"}
            if gate == "occupied"
            else {"pr:99": "same"}
            if gate.startswith("merged_")
            else {},
            "managed_pr_id": None,
        },
    )
    publisher = Publisher(services, evidence)
    delayed = path == "auto-delayed"
    conflict = path == "auto-conflict"
    if gate not in {"allowed", "merged_verified"}:
        outcome = publisher.publish(token, "session", {"digest": "same"})
        assert outcome["state"] == "waiting"
        assert outcome["reason"] == (
            "maintainer_occupied"
            if gate == "occupied"
            else "external_merge_verification"
            if gate == "merged_unverified"
            else "pr_relation_uncertain"
        )
        assert pulls == [] and calls == [] and fork_writes == []
        with services.store.read() as records:
            assert not records.find("publication_intents", task_id=task)
        return
    if conflict:
        with pytest.raises(PermissionError, match="同名仓库"):
            publisher.publish(token, "session", {"digest": "same"})
        assert pulls == [] and calls == [] and fork_writes == []
        return
    if delayed:
        for _ in range(2):
            with pytest.raises(PermissionError, match="Fork 创建尚未确认"):
                publisher.publish(token, "session", {"digest": "same"})
        assert len(fork_writes) == 1 and pulls == [] and calls == []
        fork_exists = True
    if lost:
        with pytest.raises(PermissionError, match="receipt lost"):
            publisher.publish(token, "session", {"digest": "same"})
        with services.store.read() as records:
            pending = dict(records.one("publication_intents", task_id=task, kind="pull_request"))
            assert pending["state"] == "unknown"
        result = publisher.reconcile_pending(token, pending)
    else:
        result = publisher.publish(token, "session", {"digest": "same"})
    assert result["pr_id"] == "500"
    assert len(pulls) == 1
    assert sum("push" in arguments for arguments in calls) == 1
    with services.store.read() as records:
        intent = records.one("publication_intents", task_id=task, kind="pull_request")
        request = json.loads(intent["request_json"])
        assert request["publication_id"] == publication["github_id"]
        if automatic:
            assert request["publication"]["owner"] == publication["owner"]
        assert intent["state"] == "confirmed"
        assert records.one("managed_pull_requests", task_id=task)["pr_id"] == "500"
        assert records.one("execution_lease", singleton=1)["holder_run_id"] is None
    if gate == "allowed" and not lost:
        candidate_sha = "c" * 40
        with services.store.write() as records:
            records.change("tasks", {"id": task}, {"state": "queued"})
            records.change("repositories", {"id": repo}, {"identity_json": None})
        token = services.queue.claim(now=0)
        with services.store.write() as records:
            records.change(
                "repositories",
                {"id": repo},
                {"identity_json": canonical_json(identity)},
            )
        updated = publisher.publish(token, "session", {"digest": "same"})
        assert updated["pr_id"] == "500" and updated["sha"] == candidate_sha
        assert len(pulls) == 1
        assert sum("push" in arguments for arguments in calls) == 2
        with services.store.read() as records:
            assert len(records.find("publication_revisions")) == 1
    assert len(fork_writes) == int(path in {"auto-fork", "auto-delayed"})


def test_pr_body_does_not_publish_probe_source_or_extra_closing_directives():
    body = pr_body(
        1,
        {"probe": {"files": {"private.py": "private content"}}, "review": {"passed": True}},
        "<!-- marker -->",
        "scan candidate fixes #999",
    )
    assert "private content" not in body
    assert "fixes #999" not in body
    assert body.count("Closes #") == 1
    for heading in ("概述", "变更内容", "设计方案/说明", "验证/验收", "review记录"):
        assert "## " + heading in body
