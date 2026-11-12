"""多目标 Issue 的本地范围决定；远端查重及创建仍由既有受管任务执行。"""

import json

from repowatch.application.acceptance import record_proposal, validate_content
from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.queue_state import refresh_ready, settle_ready
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import bump_task, business_event, create_task, required
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.security import DEFAULT_REDACTOR


def scope_view(records, task):
    if task["repository_id"] is None:
        return None
    repository = required(records, "repositories", id=task["repository_id"])
    decision = records.one(
        "triage_decisions",
        task_id=task["id"],
        input_revision=task["input_revision"],
        feedback_revision=task["feedback_revision"],
        admission_revision=repository["admission_revision"],
    )
    if decision is None or decision["reason_code"] != "scope_confirmation":
        return None
    analysis = json.loads(decision["evidence_json"]).get("analysis", {})
    goals = analysis.get("independent_goals", [])
    if not goals:
        return None
    context = {
        "task_id": task["id"],
        "input_revision": task["input_revision"],
        "feedback_revision": task["feedback_revision"],
        "acceptance_revision": task["acceptance_revision"],
        "admission_revision": repository["admission_revision"],
        "target_revision": repository["target_revision"],
        "decision_id": decision["id"],
    }
    binding = records.one("managed_pull_requests", task_id=task["id"])
    publication = records.one("publication_intents", task_id=task["id"], kind="pull_request")
    return {
        "context_digest": content_digest(context),
        "goals": goals,
        "can_split": not binding and not publication,
        "can_decide": task["state"] == "waiting" and task["wait_reason"] == "approval",
    }


def split_proof(records, task_id):
    for operation in records.find("user_operations"):
        proof = json.loads(operation["result_json"])
        if (
            proof.get("issue_scope") is True
            and proof.get("kind") == "split"
            and proof.get("task_id") == task_id
        ):
            return proof
    return None


def scope_children(records, task):
    proof = split_proof(records, task["id"])
    if not proof:
        return []
    return [
        {"source_key": key, "task_id": source["task_id"]}
        for key in proof.get("child_source_keys", [])
        if (
            source := records.one(
                "task_sources", repository_id=task["repository_id"], source_key=key
            )
        )
    ]


def split_authorized(records, task):
    payload = json.loads(task["input_json"])
    origin = payload.get("scope_origin")
    if not origin:
        return True
    parent = required(records, "tasks", id=origin["task_id"])
    operation = records.one("user_operations", id=origin.get("operation_id", ""))
    proof = json.loads(operation["result_json"]) if operation else {}
    return bool(
        proof.get("issue_scope") is True
        and proof.get("kind") == "split"
        and proof.get("task_id") == parent["id"]
        and proof.get("context_digest") == origin["context_digest"]
        and task["source_key"] in proof.get("child_source_keys", [])
        and task["repository_id"] == parent["repository_id"]
        and payload.get("confirmed_goal")
        == proof["goals"][proof["child_source_keys"].index(task["source_key"])]
        and not parent["user_excluded"]
        and not parent["cancel_requested"]
    )


def decide_scope(
    store,
    operation_id,
    task_id,
    *,
    expected_revision,
    context_digest,
    kind,
    goals,
    explanation,
    now,
    actor="local-user",
):
    human(actor)
    if (
        kind not in {"converge", "split"}
        or not isinstance(explanation, str)
        or not explanation.strip()
    ):
        raise ValueError("需要选择收敛或拆分，并说明范围判断依据")
    if not isinstance(goals, list) or not (1 if kind == "converge" else 2) <= len(goals) <= 20:
        raise ValueError("收敛须有一个目标，拆分须有 2—20 个独立目标")
    if kind == "converge" and len(goals) != 1:
        raise ValueError("收敛仅保留一个交付目标")
    if DEFAULT_REDACTOR.clean({"goals": goals, "explanation": explanation}) != {
        "goals": goals,
        "explanation": explanation,
    }:
        raise ValueError("范围决定不能包含凭据")
    for goal in goals:
        if not isinstance(goal, dict) or set(goal) != {"objective", "criteria"}:
            raise ValueError("每个目标须有 objective 和 criteria")
        validate_content("bug", goal)
        if len(goal["objective"]) > 4000 or len(goal["criteria"]) > 100:
            raise ValueError("目标或验收条件过长")
    if len({goal["objective"].strip() for goal in goals}) != len(goals):
        raise ValueError("独立目标不能重复")
    request = {
        "action": "issue_scope",
        "task_id": task_id,
        "revision": expected_revision,
        "context_digest": context_digest,
        "kind": kind,
        "goals": goals,
        "explanation": explanation,
        "actor": actor,
    }
    with store.write() as records:
        fingerprint, replay = replay_operation(records, operation_id, request)
        if replay is not None:
            return replay
        task = required(records, "tasks", id=task_id)
        scope = scope_view(records, task)
        if (
            task["revision"] != expected_revision
            or not scope
            or not scope["can_decide"]
            or scope["context_digest"] != context_digest
            or task["user_excluded"]
            or task["cancel_requested"]
            or not task["issue_id"]
            or split_proof(records, task_id)
        ):
            raise ConflictError("范围依据已变化，请重新核对当前 Issue")
        if kind == "split" and not scope["can_split"]:
            raise ConflictError("已有受管 PR 或发布意图，须保留原交付链并收敛范围")
        repository = required(records, "repositories", id=task["repository_id"])
        payload = json.loads(task["input_json"])
        resolution = {
            "kind": kind,
            "operation_id": operation_id,
            "context_digest": context_digest,
            "goals": goals,
            "explanation": explanation,
            "original_goals": scope["goals"],
            "admission_revision": repository["admission_revision"],
            "target_revision": repository["target_revision"],
            "child_source_keys": [],
        }
        settle_ready(records, now)
        if kind == "split":
            for index, goal in enumerate(goals):
                source = f"scope:{task_id}:{context_digest}:{index}"
                child = create_task(
                    records,
                    task["repository_id"],
                    source,
                    None,
                    "triage",
                    now,
                    owner=task["budget_owner_task_id"],
                    input_payload={
                        "issue_lookup_required": True,
                        "finding": {"title": goal["objective"], "basis": explanation},
                        "scope_origin": {
                            "task_id": task_id,
                            "operation_id": operation_id,
                            "context_digest": context_digest,
                            "issue_number": payload.get("number"),
                        },
                        "confirmed_goal": goal,
                    },
                )
                records.change(
                    "tasks",
                    {"id": child},
                    {
                        "selected": 1,
                        "configuration_json": task["configuration_json"],
                    },
                )
                resolution["child_source_keys"].append(source)
        payload["human_context"] = {
            **payload.get("human_context", {}),
            "scope_resolution": resolution,
        }
        bump_task(
            records,
            task,
            input_json=canonical_json(payload),
            input_revision=task["input_revision"] + 1,
            feedback_revision=task["feedback_revision"] + 1,
            state="waiting" if kind == "split" else "queued",
            mode="triage",
            wait_reason="scope_children" if kind == "split" else None,
            pending_transition_json=None,
        )
        updated = required(records, "tasks", id=task_id)
        if kind == "converge":
            previous = records.one(
                "task_acceptance_specs", task_id=task_id, revision=task["acceptance_revision"]
            )
            acceptance_kind = previous["kind"] if previous else "bug"
            content = json.loads(previous["content_json"]) if previous else {}
            content.pop("independent_goals", None)
            content.update(goals[0])
            revision = record_proposal(
                records, updated, repository, {"kind": acceptance_kind, "content": content}
            )
            records.change(
                "task_acceptance_specs",
                {"task_id": task_id, "revision": revision},
                {
                    "approved_revision": revision,
                    "approved_by": actor,
                    "approved_feedback_revision": updated["feedback_revision"],
                },
            )
            records.change("tasks", {"id": task_id}, {"acceptance_revision": revision})
        records.add(
            "task_input_versions",
            {
                "task_id": task_id,
                "input_revision": updated["input_revision"],
                "feedback_revision": updated["feedback_revision"],
                "input_json": canonical_json(payload),
            },
        )
        children = [
            {
                "source_key": key,
                "task_id": required(
                    records, "task_sources", repository_id=task["repository_id"], source_key=key
                )["task_id"],
            }
            for key in resolution["child_source_keys"]
        ]
        result = {
            "issue_scope": True,
            "task_id": task_id,
            "kind": kind,
            "children": children,
            "goals": goals,
            "context_digest": context_digest,
            "child_source_keys": resolution["child_source_keys"],
            "parent_input_revision": updated["input_revision"],
            "parent_feedback_revision": updated["feedback_revision"],
            "admission_revision": repository["admission_revision"],
            "target_revision": repository["target_revision"],
        }
        remember_operation(records, operation_id, fingerprint, expected_revision, result)
        business_event(
            records,
            "issue_scope_decided",
            task_id,
            resolution_kind=kind,
            context_digest=context_digest,
            actor=actor,
            children=result["children"],
        )
        refresh_ready(records, now)
        return result
