"""Read remote facts after restoring an old backup, then explicitly recover bindings."""

import json
import re

from repowatch.adapters.github import GitHubReadAdapter
from repowatch.application.budgets import remember_operation, replay_operation
from repowatch.application.safety import assert_quiescent
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import bump_task, business_event, required
from repowatch.domain.task_records import content_digest
from repowatch.operations.locks import guarded


class RestoreReconciliation:
    def __init__(self, services, *, adapter_factory=None):
        self.services, self.store = services, services.store
        self.adapter_factory = adapter_factory or (
            lambda reference: GitHubReadAdapter(
                vault=services.vault, credential_reference=reference
            )
        )

    def inspect(self):
        with self.store.read() as records:
            assert_quiescent(records, allow_unknown_remote=True)
            if not records.find("block_reasons", code="backup_restore_reconciliation", resolved=0):
                raise PermissionError("当前没有待完成的备份恢复核对")
            repositories = [dict(row) for row in records.find("repositories")]
            tasks = [dict(row) for row in records.find("tasks")]
            bindings = [dict(row) for row in records.find("managed_pull_requests")]
            revisions = [dict(row) for row in records.find("provider_revisions", state="active")]
            workspaces = [dict(row) for row in records.find("workspaces")]
        credentials, remote, candidates, failures = [], [], [], []
        references = {row["credential_reference"] for row in revisions}
        references |= {
            row["credential_reference"] for row in repositories if row["credential_reference"]
        }
        for reference in sorted(references):
            try:
                present = self.services.vault.read(reference) is not None
            except Exception:
                present = False
            credentials.append({"reference": reference, "available": present})
            if not present:
                failures.append("credential_reference_unavailable")
        for repository in repositories:
            if not repository["github_owner"] or not repository["github_name"]:
                continue  # Locally registered fixtures have no remote contribution identity.
            if any(
                item["reference"] == repository["credential_reference"] and not item["available"]
                for item in credentials
            ):
                remote.append(
                    {"repository_id": repository["id"], "state": "credential_unavailable"}
                )
                continue
            port = self.adapter_factory(repository["credential_reference"])
            owner, name = repository["github_owner"], repository["github_name"]
            identity = port.repository(owner, name)
            if identity["github_id"] != repository["github_id"]:
                raise PermissionError("恢复仓库的实际远端身份不匹配")
            target = port.branch(owner, name, repository["target_branch"])
            pulls = port.collection(
                port._path(owner, name) + "/pulls", params={"state": "all"}, max_pages=100
            )
            publication = json.loads(repository["publication_json"] or "{}")
            login = json.loads(repository["identity_json"] or "{}").get("execution_login")
            observed = []
            for pull in pulls:
                observed.append(
                    {
                        "id": str(pull["id"]),
                        "number": pull["number"],
                        "head_sha": pull["head"]["sha"],
                        "base_sha": pull["base"]["sha"],
                        "state": pull["state"],
                        "merged_at": pull.get("merged_at"),
                        "body_sha256": content_digest(pull.get("body") or ""),
                    }
                )
                for task in tasks:
                    inputs = json.loads(task["input_json"])
                    number = inputs.get("number")
                    if (
                        task["repository_id"] != repository["id"]
                        or not task["issue_id"]
                        or not number
                    ):
                        continue
                    expected_branch = f"repowatch/issue-{number}-" + task["id"][:12]
                    if pull["head"].get("ref") != expected_branch:
                        continue
                    if (
                        str(pull["base"].get("repo", {}).get("id")) != repository["github_id"]
                        or pull["base"].get("ref") != repository["target_branch"]
                        or str(pull["head"].get("repo", {}).get("id"))
                        != publication.get("github_id")
                        or pull.get("user", {}).get("login") != login
                        or not re.search(
                            r"<!-- repowatch-publication:[0-9a-f-]{36} -->", pull.get("body") or ""
                        )
                        or f"Closes #{number}\n" not in (pull.get("body") or "") + "\n"
                    ):
                        failures.append("remote_binding_identity_requires_review")
                        continue
                    existing = next((row for row in bindings if row["task_id"] == task["id"]), None)
                    if existing and existing["pr_id"] != str(pull["id"]):
                        failures.append("remote_binding_conflict")
                        continue
                    candidates.append(
                        {
                            "task_id": task["id"],
                            "repository_id": repository["id"],
                            "pr_repository_id": repository["github_id"],
                            "pr_id": str(pull["id"]),
                            "number": pull["number"],
                            "state": "merged" if pull.get("merged_at") else pull["state"],
                            "head_sha": pull["head"]["sha"],
                            "already_bound": existing is not None,
                        }
                    )
            remote.append(
                {
                    "repository_id": repository["id"],
                    "target_sha": target,
                    "pulls": sorted(observed, key=lambda item: item["id"]),
                }
            )
        if len({item["task_id"] for item in candidates}) != len(candidates):
            failures.append("multiple_remote_matches")
        facts = {
            "credentials": credentials,
            "remote": remote,
            "candidates": candidates,
            "workspace_status": [{"id": row["id"], "state": row["state"]} for row in workspaces],
            "task_revisions": [{"id": row["id"], "revision": row["revision"]} for row in tasks],
            "repository_revisions": [
                {"id": row["id"], "revision": row["revision"]} for row in repositories
            ],
            "provider_revisions": [row["id"] for row in revisions],
            "failures": sorted(set(failures)),
        }
        return {**facts, "basis_sha256": content_digest(facts), "ready": not failures}

    @guarded
    def report(self, operation_id):
        with self.store.read() as records:
            fingerprint, replay = replay_operation(
                records, operation_id, {"kind": "restore_report"}
            )
            if replay is not None:
                return replay
        result = {"report_id": operation_id, **self.inspect()}
        with self.store.write() as records:
            remember_operation(records, operation_id, fingerprint, 1, result)
        return result

    @guarded
    def acknowledge(self, operation_id, report_id, basis_sha256, *, explanation):
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError("恢复核对需要明确依据")
        request = {
            "kind": "restore_acknowledge",
            "report_id": report_id,
            "basis_sha256": basis_sha256,
            "explanation": explanation,
        }
        with self.store.read() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            saved = json.loads(required(records, "user_operations", id=report_id)["result_json"])
        current = self.inspect()
        if saved.get("basis_sha256") != basis_sha256 or current["basis_sha256"] != basis_sha256:
            raise ConflictError("恢复核对报告已过期，请重新读取远端与凭据")
        if not current["ready"]:
            raise PermissionError("凭据或远端身份仍未确认，保持恢复阻塞")
        with self.store.write() as records:
            assert_quiescent(records, allow_unknown_remote=True)
            for collection, key in (
                ("tasks", "task_revisions"),
                ("repositories", "repository_revisions"),
            ):
                for revision in current[key]:
                    if (
                        required(records, collection, id=revision["id"])["revision"]
                        != revision["revision"]
                    ):
                        raise ConflictError("恢复核对期间配置或任务发生变化")
            if sorted(
                row["id"] for row in records.find("provider_revisions", state="active")
            ) != sorted(current["provider_revisions"]):
                raise ConflictError("恢复核对期间供应商发生变化")
            for item in current["candidates"]:
                task = required(records, "tasks", id=item["task_id"])
                if not item["already_bound"]:
                    records.add(
                        "managed_pull_requests",
                        {
                            "task_id": task["id"],
                            "issue_repository_id": task["repository_id"],
                            "issue_id": task["issue_id"],
                            "pr_repository_id": item["pr_repository_id"],
                            "pr_id": item["pr_id"],
                            "state": item["state"],
                        },
                    )
                    bump_task(
                        records,
                        task,
                        state="waiting",
                        wait_reason="delivery_review",
                        stop_requested=0,
                    )
                else:
                    records.change(
                        "managed_pull_requests", {"task_id": task["id"]}, {"state": item["state"]}
                    )
            for reason in records.find(
                "block_reasons", code="backup_restore_reconciliation", resolved=0
            ):
                records.change(
                    "block_reasons",
                    {"id": reason["id"]},
                    {"resolved": 1, "revision": reason["revision"] + 1},
                )
            result = {"acknowledged": True, "bindings": current["candidates"], "paused": True}
            remember_operation(records, operation_id, fingerprint, 1, result)
            business_event(
                records,
                "restore_reconciled",
                report_id=report_id,
                explanation=explanation,
                bindings=current["candidates"],
            )
            return result
