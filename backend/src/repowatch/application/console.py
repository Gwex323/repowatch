"""控制面只读投影；读取或刷新不领取任务、不发起模型。"""

import json

from repowatch.application.acceptance import acceptance_view
from repowatch.application.artifacts import EventReader
from repowatch.application.blockers import guidance
from repowatch.application.task_records import required
from repowatch.domain.task_records import content_digest
from repowatch.security import DEFAULT_REDACTOR


def public_record(row):
    return DEFAULT_REDACTOR.clean(
        {
            key.removesuffix("_json"): json.loads(value) if value is not None else None
            for key, value in row.items()
            if key.endswith("_json")
        }
        | {key: value for key, value in row.items() if not key.endswith("_json")}
    )


class Console:
    def __init__(self, store, budgets, providers, roles):
        self.store, self.budgets, self.providers, self.roles = store, budgets, providers, roles
        self.events = EventReader(store)

    def state(self):
        with self.store.read() as records:
            repositories = [public_record(row) for row in records.find("repositories", removed=0)]
            active_repositories = {repository["id"] for repository in repositories}
            tasks = [
                public_record(row)
                for row in records.find("tasks")
                if row["repository_id"] is None or row["repository_id"] in active_repositories
            ]
            active_tasks = {task["id"] for task in tasks}
            delivery = {
                row["task_id"]: {key: row[key] for key in ("number", "delivery_state", "ci_state")}
                for row in records.find("pr_lifecycle")
            }
            for task in tasks:
                task["delivery"] = delivery.get(task["id"])
                if task["state"] != "waiting" or task["wait_reason"] not in {
                    "information",
                    "human_information",
                    "approval",
                }:
                    continue
                current = next(
                    (
                        row
                        for row in records.find("triage_decisions", task_id=task["id"])
                        if row["input_revision"] == task["input_revision"]
                        and row["feedback_revision"] == task["feedback_revision"]
                        and row["admission_revision"]
                        == required(records, "repositories", id=task["repository_id"])[
                            "admission_revision"
                        ]
                    ),
                    None,
                )
                if current is None:
                    continue
                task["triage_reason"] = current["reason_code"]
                if current["reason_code"] == "issue_lookup_required":
                    evidence = json.loads(current["evidence_json"])
                    relation = evidence.get("analysis", {}).get("issue_relation", {})
                    number = relation.get("issue_number")
                    if type(number) is int and any(
                        item["number"] == number for item in evidence.get("issues", [])
                    ):
                        task["similar_issue_number"] = number
            result = {
                "control": public_record(required(records, "global_control", singleton=1)),
                "repositories": repositories,
                "tasks": tasks,
                "reasons": [
                    public_record(row)
                    for row in records.find("block_reasons", resolved=0)
                    if row["task_id"] is None or row["task_id"] in active_tasks
                ],
                "renewals": [
                    public_record(row)
                    for row in records.find("budget_renewals")
                    if row["task_id"] in active_tasks or row["owner_task_id"] in active_tasks
                ],
                "capabilities": [public_record(row) for row in records.find("capability_checks")],
                "diagnostics": [
                    public_record(row) for row in records.find("diagnostic_authorizations")
                ],
            }
            for repository in result["repositories"]:
                sync = records.one("github_sync_state", repository_id=repository["id"])
                repository["synchronization"] = public_record(sync) if sync else None
                if sync:
                    repository["synchronization"]["running"] = bool(
                        records.find(
                            "github_sync_runs", repository_id=repository["id"], state="running"
                        )
                    )
            for reason in result["reasons"]:
                details = reason["details"]
                if not details.get("stage") and reason["task_id"]:
                    from repowatch.application.blockers import execution_stage

                    runs = records.find("runs", task_id=reason["task_id"])
                    latest = max(runs, key=lambda row: row["started_at"] or 0, default=None)
                    if latest:
                        details["stage"] = execution_stage(records, latest["id"])
                reason["guidance"] = guidance(details.get("diagnosis", reason["code"]), details)
        for task in tasks:
            task["budget"] = self.budgets.view(task["budget_owner_task_id"])
        result["providers"], result["roles"] = self.providers.list(), self.roles.list()
        return result

    def task(self, identifier):
        from repowatch.application.issue_scopes import scope_children, scope_view

        with self.store.read() as records:
            task_row = required(records, "tasks", id=identifier)
            repository = records.one("repositories", id=task_row["repository_id"])
            if repository and repository["removed"]:
                raise LookupError("仓库已从工作台移除")
            task = public_record(task_row)
            task["acceptance"] = DEFAULT_REDACTOR.clean(acceptance_view(records, task))
            task["scope"] = DEFAULT_REDACTOR.clean(scope_view(records, task))
            task["scope_children"] = scope_children(records, task)
            task["triage_decisions"] = [
                public_record(row) for row in records.find("triage_decisions", task_id=identifier)
            ]
            contexts = records.find("issue_contexts", task_id=identifier)
            latest = max(contexts, key=lambda item: item["created_at"], default=None)
            task["issue_context"] = public_record(latest) if latest else None
            task["publication_intents"] = [
                public_record(row)
                for row in records.find("publication_intents", task_id=identifier)
            ]
            task["repair_sessions"] = [
                public_record(row) for row in records.find("repair_sessions", task_id=identifier)
            ]
            task["relation_decisions"] = [
                public_record(row) for row in records.find("relation_decisions", task_id=identifier)
            ]
            task["candidate_issue_decisions"] = [
                public_record(row)
                for row in records.find("candidate_issue_decisions", task_id=identifier)
            ]
            task["distillation_intents"] = [
                public_record(row)
                for row in records.find("distillation_intents", task_id=identifier)
            ]
            lifecycle = records.one("pr_lifecycle", task_id=identifier)
            task["pr_lifecycle"] = public_record(lifecycle) if lifecycle else None
            if lifecycle:
                task["pr_lifecycle"]["scope_decisions"] = [
                    public_record(row)
                    for row in records.find("review_scope_decisions", task_id=identifier)
                ]
            task["runs"] = [public_record(row) for row in records.find("runs", task_id=identifier)]
            task["artifacts"] = [
                {
                    "id": artifact["id"],
                    "sha256": artifact["sha256"],
                    "available": artifact["available"],
                    "context": DEFAULT_REDACTOR.clean(json.loads(artifact["context_json"])),
                }
                for run in task["runs"]
                for artifact in records.find("artifacts", run_id=run["id"])
            ]
            repository = records.one("repositories", id=task["repository_id"])
            for session in task["repair_sessions"]:
                frozen = session.get("frozen") or {}
                evidence = session.get("evidence") or {}
                artifact_ids = evidence.get("artifact_ids")
                available = {item["id"] for item in task["artifacts"] if item["available"]}
                session["current_context"] = bool(
                    repository
                    and session["state"] == "verified"
                    and all(
                        frozen.get(key) == task.get(key)
                        for key in ("input_revision", "feedback_revision", "acceptance_revision")
                    )
                    and all(
                        frozen.get(key) == repository[key]
                        for key in ("admission_revision", "target_revision", "import_epoch")
                    )
                    and frozen.get("base_sha") == repository["target_sha"]
                    and frozen.get("configuration_sha256")
                    == content_digest(json.loads(task_row["configuration_json"]))
                    and isinstance(artifact_ids, list)
                    and bool(artifact_ids)
                    and all(identifier in available for identifier in artifact_ids)
                )
            task["reasons"] = [
                public_record(row) for row in records.find("block_reasons", task_id=identifier)
            ]
            task["progress"] = [
                public_record(row)
                for row in records.find(
                    "budget_progress", owner_task_id=task["budget_owner_task_id"]
                )
            ]
            task["attempts"] = [
                public_record(row) for row in records.find("progress_attempts", task_id=identifier)
            ]
            task["progress_windows"] = [
                public_record(row)
                for row in records.find(
                    "progress_windows", owner_task_id=task["budget_owner_task_id"]
                )
            ]
        task["budget"] = self.budgets.view(task["budget_owner_task_id"])
        return task

    def artifact(self, task_id, artifact_id, artifacts, *, now):
        with self.store.read() as records:
            required(records, "tasks", id=task_id)
            artifact = required(records, "artifacts", id=artifact_id)
            run = required(records, "runs", id=artifact["run_id"])
            if run["task_id"] != task_id:
                raise PermissionError("产物不属于当前任务")
        artifact = artifacts.require(artifact_id, now=now, block_on_failure=False)
        with (artifacts.root / artifact["relative_path"]).open("rb") as source:
            content = source.read(1024 * 1024 + 1)
        return {
            "artifact_id": artifact_id,
            "sha256": artifact["sha256"],
            "truncated": len(content) > 1024 * 1024,
            "content": DEFAULT_REDACTOR.clean(content[: 1024 * 1024].decode("utf-8", "replace")),
        }
