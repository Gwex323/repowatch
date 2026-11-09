from copy import deepcopy

import pytest
from test_github_polling import polling as polling

from repowatch.application.acceptance import acceptance_view
from repowatch.application.storage import ConflictError
from repowatch.domain.pull_requests import active_reviews, effective_ci_policy, evaluate_ci
from repowatch.domain.task_records import canonical_json
from repowatch.domain.triage import TriageAnalysis


def pull():
    return {
        "id": 20,
        "number": 2,
        "state": "open",
        "title": "fix",
        "body": "fix #1",
        "updated_at": "2026-11-08T00:00:00Z",
        "merge_commit_sha": "c" * 40,
        "head": {"sha": "b" * 40, "ref": "repowatch/issue-1", "repo": {"id": 55}},
        "base": {"sha": "a" * 40, "ref": "main", "repo": {"id": 55}},
    }


def check(identifier=1, *, sha="b" * 40, conclusion="success", state="completed"):
    return {
        "id": identifier,
        "name": "tests",
        "app": {"id": 99},
        "head_sha": sha,
        "status": state,
        "conclusion": conclusion,
    }


POLICY = {"configured": True, "required": [{"name": "tests", "source": "99"}]}


@pytest.mark.parametrize("conclusion", [None, "neutral", "skipped", "cancelled", "action_required"])
def test_ci_missing_or_non_success_never_counts_as_green(conclusion):
    result = evaluate_ci(pull(), [check(conclusion=conclusion)], [], POLICY)
    assert result["state"] != "success"


def test_ci_binds_sources_sha_and_new_pending_attempt():
    assert evaluate_ci(pull(), [check(sha="d" * 40)], [], POLICY)["state"] == "pending"
    assert evaluate_ci(pull(), [{**check(), "app": {"id": 100}}], [], POLICY)["state"] == "pending"
    assert (
        evaluate_ci(pull(), [check(), check(2, state="queued", conclusion=None)], [], POLICY)[
            "state"
        ]
        == "pending"
    )
    assert (
        evaluate_ci(pull(), [check(conclusion="failure"), check(2)], [], POLICY)["state"]
        == "success"
    )
    policy = deepcopy(POLICY)
    policy["required"][0]["accepted"] = ["success", "skipped"]
    assert evaluate_ci(pull(), [check(conclusion="skipped")], [], policy)["state"] == "success"
    assert evaluate_ci(pull(), [], [], {})["state"] == "policy_required"


def test_branch_rules_cannot_be_omitted_from_necessary_checks():
    rules = {
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "protected-check", "integration_id": 88}
                    ],
                },
            }
        ]
    }
    policy = effective_ci_policy(POLICY, rules)
    assert policy["required"][-1]["ref"] == "merge"
    assert evaluate_ci(pull(), [check()], [], policy)["state"] == "pending"


def test_historical_pr_base_and_old_merge_commit_cannot_supply_current_green():
    base_policy = {
        "configured": True,
        "required": [{"name": "tests", "source": "99", "ref": "base"}],
    }
    assert (
        evaluate_ci(pull(), [check(sha="a" * 40)], [], base_policy, current_base_sha="d" * 40)[
            "state"
        ]
        == "pending"
    )
    assert (
        evaluate_ci(pull(), [check(sha="d" * 40)], [], base_policy, current_base_sha="d" * 40)[
            "state"
        ]
        == "success"
    )
    merge_policy = {
        "configured": True,
        "required": [{"name": "tests", "source": "99", "ref": "merge"}],
    }
    old_merge = {**pull(), "merge_parents": ["a" * 40, "b" * 40]}
    assert (
        evaluate_ci(old_merge, [check(sha="c" * 40)], [], merge_policy, current_base_sha="d" * 40)[
            "state"
        ]
        == "pending"
    )
    current_merge = {**pull(), "merge_parents": ["d" * 40, "b" * 40]}
    assert (
        evaluate_ci(
            current_merge, [check(sha="c" * 40)], [], merge_policy, current_base_sha="d" * 40
        )["state"]
        == "success"
    )


def test_review_withdrawal_and_old_commit_are_explicit():
    review = {
        "id": 1,
        "state": "CHANGES_REQUESTED",
        "commit_id": "a" * 40,
        "user": {"login": "maintainer"},
        "submitted_at": "2026-11-08T00:00:00Z",
    }
    assert active_reviews([review], "b" * 40)[0]["outdated"]
    withdrawn = {**review, "id": 2, "state": "DISMISSED", "submitted_at": "2026-11-08T00:01:00Z"}
    assert not active_reviews([review, withdrawn], "b" * 40)


@pytest.mark.parametrize("state", ["COMMENTED", "PENDING", "APPROVED", "DISMISSED"])
@pytest.mark.parametrize("reverse", [False, True])
def test_only_review_decisions_replace_a_change_request(state, reverse):
    request = {
        "id": 1,
        "state": "CHANGES_REQUESTED",
        "commit_id": "b" * 40,
        "user": {"login": "maintainer"},
        "submitted_at": "2026-11-08T00:00:00Z",
    }
    later = {**request, "id": 2, "state": state, "submitted_at": "2026-11-08T00:01:00Z"}
    reviews = [later, request] if reverse else [request, later]
    expected = [] if state in {"APPROVED", "DISMISSED"} else [{**request, "outdated": False}]
    assert active_reviews(reviews, "b" * 40) == expected


@pytest.mark.parametrize("state", ["COMMENTED", "PENDING"])
def test_sync_preserves_change_request_after_non_decision_review(polling, state):
    services, repository, wire, now = polling
    task_id = bind_pull(polling)
    request = {
        "id": 33,
        "state": "CHANGES_REQUESTED",
        "commit_id": "b" * 40,
        "submitted_at": "2026-11-08T00:00:00Z",
        "body": "handle negatives",
        "user": {"login": "maintainer"},
    }
    wire.reviews = [request]
    services.synchronization.request("change-request", repository)
    assert services.synchronization.tick()
    wire.reviews.append(
        {**request, "id": 34, "state": state, "submitted_at": "2026-11-08T00:01:00Z"}
    )
    now[0] += 1
    services.synchronization.request("follow-up-review", repository)
    assert services.synchronization.tick()
    changes = services.lifecycle.view(task_id)["snapshot"]["requested_changes"]
    assert changes == [{**request, "outdated": False}]


def bind_pull(fixture):
    services, repository, wire, now = fixture
    assert services.synchronization.tick()
    with services.store.read() as records:
        task = dict(records.find("tasks", issue_id="10")[0])
    services.tasks.bind_managed_pr(task["id"], "55", "20")
    with services.store.write() as records:
        records.add(
            "publication_intents",
            {
                "id": "published",
                "task_id": task["id"],
                "repository_id": repository,
                "issue_id": "10",
                "kind": "pull_request",
                "request_json": "{}",
                "request_sha256": "f" * 64,
                "state": "confirmed",
                "result_json": canonical_json({"number": 2, "sha": "b" * 40}),
                "created_at": now[0],
            },
        )
        records.change(
            "tasks", {"id": task["id"]}, {"state": "waiting", "wait_reason": "pr_review"}
        )
    wire.pull = pull()
    return task["id"]


def test_pr_reviews_paginate_deduplicate_and_only_queue_worker(polling):
    services, repository, wire, now = polling
    task_id = bind_pull(polling)
    # GET /pulls/{number} supplies a count; /pulls/{number}/comments supplies a list.
    wire.pull["review_comments"] = 3
    wire.reviews = [
        {
            "id": 33,
            "state": "CHANGES_REQUESTED",
            "commit_id": "b" * 40,
            "submitted_at": "2026-11-08T00:00:00Z",
            "body": "handle negatives",
            "user": {"login": "maintainer"},
        }
    ]
    services.synchronization.request("reviews", repository)
    assert services.synchronization.tick()
    view = services.lifecycle.view(task_id)
    assert view["snapshot"]["requested_changes"][0]["id"] == 33
    with services.store.read() as records:
        feedback = len(records.find("pending_signals", task_id=task_id))
        assert records.one("tasks", id=task_id)["mode"] == "triage"
        assert not records.find("runs")
    now[0] += 1
    services.synchronization.request("same", repository)
    services.synchronization.tick()
    with services.store.read() as records:
        assert len(records.find("pending_signals", task_id=task_id)) == feedback
    assert all(
        "since" not in call.url.params
        for call in wire.calls
        if call.url.path.endswith(("/reviews", "/check-runs", "/statuses"))
    )


@pytest.mark.parametrize("merged", [True, False])
def test_delivery_issue_and_code_validation_remain_separate(polling, merged):
    services, repository, wire, _ = polling
    task_id = bind_pull(polling)
    wire.pull.update(state="closed", updated_at="2026-11-08T00:01:00Z")
    if merged:
        wire.pull["merged_at"] = "2026-11-08T00:01:00Z"
    wire.issues[0].update(state="closed", updated_at="2026-11-08T00:01:00Z")
    services.synchronization.request("closed", repository)
    services.synchronization.tick()
    lifecycle = services.lifecycle.view(task_id)
    assert lifecycle["delivery_state"] == ("merged" if merged else "closed")
    assert lifecycle["issue_state"] == "closed"
    with services.store.read() as records:
        task = records.one("tasks", id=task_id)
        assert task["state"] == "ended"
        assert task["outcome"] == ("pr_merged" if merged else "pr_closed_unmerged")
        assert not records.find("repair_sessions")
        assert len(records.find("managed_pull_requests")) == 1
        assert not records.find("runs")


def test_review_scope_approval_expires_when_feedback_changes(polling):
    services, repository, wire, _ = polling
    task_id = bind_pull(polling)
    services.synchronization.request("first", repository)
    services.synchronization.tick()
    with services.store.write() as records:
        task = dict(records.one("tasks", id=task_id))
        lifecycle = records.one("pr_lifecycle", task_id=task_id)
        digest = lifecycle["feedback_sha256"]
        analysis = TriageAnalysis(
            kind="feature", objective="add negatives", criteria=["negative sums"], plan="extend sum"
        )
        records.add(
            "review_scope_decisions",
            {
                "task_id": task_id,
                "context_sha256": digest,
                "decision": "pending",
                "analysis_json": analysis.model_dump_json(),
                "input_revision": task["input_revision"],
                "feedback_revision": task["feedback_revision"],
                "acceptance_revision": task["acceptance_revision"],
                "actor": "processor",
            },
        )
    result = services.lifecycle.decide(
        "approve",
        task_id,
        expected_revision=task["revision"],
        context_sha256=digest,
        action="approve",
        explanation="approve explicit scope",
    )
    assert result["decision"] == "approve"
    with services.store.read() as records:
        task = dict(records.one("tasks", id=task_id))
        assert acceptance_view(records, task)["approved"]
    services.tasks.observe(
        task_id, "new-review", "2", {"review": "new scope"}, kind="feedback", now=1000001
    )
    with services.store.read() as records:
        task = dict(records.one("tasks", id=task_id))
        assert not acceptance_view(records, task)["approved"]
    with pytest.raises(ConflictError):
        services.lifecycle.decide(
            "different",
            task_id,
            expected_revision=task["revision"],
            context_sha256=digest,
            action="approve",
            explanation="cannot reuse old scope",
        )


def test_ci_pending_to_success_preserves_review_context_and_does_not_queue_amend(polling):
    services, repository, wire, _ = polling
    task_id = bind_pull(polling)
    services.synchronization.configure(
        "ci-policy",
        repository,
        {
            "ci_configured": True,
            "required_checks": POLICY["required"],
        },
        expected_revision=1,
    )
    services.synchronization.tick()
    pending = services.lifecycle.view(task_id)
    assert pending["ci_state"] == "pending"
    with services.store.read() as records:
        feedback = records.one("tasks", id=task_id)["feedback_revision"]
    wire.checks = [check()]
    services.synchronization.request("green", repository)
    services.synchronization.tick()
    green = services.lifecycle.view(task_id)
    assert green["ci_state"] == "success"
    assert green["feedback_sha256"] == pending["feedback_sha256"]
    with services.store.read() as records:
        task = records.one("tasks", id=task_id)
        assert task["feedback_revision"] == feedback
        assert task["wait_reason"] == "pr_review"


@pytest.mark.parametrize("scope", ["in_scope", "expanded", "uncertain", "handled"])
def test_review_classification_preserves_approval_only_for_verified_original_scope(polling, scope):
    services, repository, wire, now = polling
    task_id = bind_pull(polling)
    with services.store.read() as records:
        task = records.one("tasks", id=task_id)
    content = {"objective": "correct sum", "criteria": ["sum is correct"], "plan": "fix addition"}
    services.tasks.operate(
        "spec",
        task_id,
        expected_revision=task["revision"],
        action="set_acceptance",
        payload={"kind": "feature", "content": content},
        now=now[0],
    )
    with services.store.read() as records:
        task = records.one("tasks", id=task_id)
    services.tasks.operate(
        "approve-original",
        task_id,
        expected_revision=task["revision"],
        action="approve_acceptance",
        now=now[0],
    )
    wire.reviews = [
        {
            "id": 44,
            "state": "CHANGES_REQUESTED",
            "commit_id": "b" * 40,
            "submitted_at": "2026-11-08T00:01:00Z",
            "body": "adjust current behavior",
            "user": {"login": "maintainer"},
        }
    ]
    services.synchronization.request("read-pr", repository)
    services.synchronization.tick()
    with services.store.write() as records:
        for task in records.find("tasks", repository_id=repository):
            records.change(
                "tasks",
                {"id": task["id"]},
                {"state": "ended" if task["id"] != task_id else "queued"},
            )
    services.queue.set_pause(False, now=now[0])
    token = services.queue.claim(now=now[0])
    assert token is not None
    with services.store.read() as records:
        acceptance = acceptance_view(records, records.one("tasks", id=task_id))
    analysis = TriageAnalysis(
        kind="feature",
        **content,
        review_scope=scope,
        review_evidence=["matched requested change to current approved criteria"],
    )
    artifact_id = services.artifacts.register(
        token,
        "scope-analysis",
        analysis.model_dump_json().encode(),
        context={"producer": "triage_analysis"},
        now=now[0],
    )
    decision = services.lifecycle.classify(token, analysis, acceptance, artifact_id)
    assert decision == ("pending" if scope in {"expanded", "uncertain"} else scope)
    with services.store.read() as records:
        current = acceptance_view(records, records.one("tasks", id=task_id))
        if scope == "in_scope":
            assert current["approved"]
            carry = records.one(
                "acceptance_carryovers", task_id=task_id, revision=current["revision"]
            )
            assert carry["artifact_id"] == artifact_id
            original = records.one(
                "task_acceptance_specs", task_id=task_id, revision=carry["source_revision"]
            )
            assert original["approved_feedback_revision"] != current["approved_feedback_revision"]
        else:
            assert not current["approved"]
