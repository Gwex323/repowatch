import json

from repowatch.application.acceptance import validate_content
from repowatch.application.budgets import remember_operation, replay_operation
from repowatch.application.queue_state import refresh_ready, settle_ready
from repowatch.application.remote_observations import mark_sync_change
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import (
    TaskRecords,
    bump_task,
    business_event,
    execution_record,
    required,
)
from repowatch.domain.pull_requests import active_reviews, effective_ci_policy, evaluate_ci
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.domain.triage import TriageAnalysis, proposed_acceptance
from repowatch.security import DEFAULT_REDACTOR


class PullRequestLifecycle:
    def __init__(self, services):
        self.services, self.store = services, services.store

    def view(self, task_id):
        with self.store.read() as records:
            row = records.one("pr_lifecycle", task_id=task_id)
            if row is None:
                return None
            return DEFAULT_REDACTOR.clean(
                {
                    **dict(row),
                    "snapshot": json.loads(row["snapshot_json"]),
                    "scope_decisions": [
                        dict(item)
                        for item in records.find("review_scope_decisions", task_id=task_id)
                    ],
                }
            )

    def classify(self, token, analysis, acceptance, artifact_id):
        artifact = self.services.artifacts.require(
            artifact_id, run_id=token.run_id, now=self.services.clock()
        )
        source = json.loads(
            (self.services.artifacts.root / artifact["relative_path"]).read_text("utf-8")
        )
        if (
            json.loads(artifact["context_json"]).get("producer") != "triage_analysis"
            or TriageAnalysis.model_validate(source) != analysis
        ):
            raise PermissionError("评审范围分析与当前运行的不可变产物不匹配")
        with self.store.write() as records:
            run = execution_record(records, token, self.services.clock())
            task = required(records, "tasks", id=run["task_id"])
            lifecycle = records.one("pr_lifecycle", task_id=task["id"])
            if lifecycle is None:
                return "in_scope"
            if lifecycle["delivery_state"] != "open":
                return "handled"
            if lifecycle["feedback_sha256"] != json.loads(run["snapshot_json"]).get(
                "pr_context_sha256"
            ):
                raise ConflictError("评审上下文在本次领取后变化，须按新版本重读")
            if (
                task["input_revision"] != run["input_revision"]
                or task["feedback_revision"] != run["feedback_revision"]
            ):
                raise ConflictError("评审分析期间输入发生变化")
            identity = {"task_id": task["id"], "context_sha256": lifecycle["feedback_sha256"]}
            old = records.one("review_scope_decisions", **identity)
            if (
                old
                and old["decision"] == "approve"
                and old["feedback_revision"] == task["feedback_revision"]
            ):
                return "in_scope"
            analysis_data = analysis.model_dump(mode="json")
            decision = "pending"
            facts = json.loads(lifecycle["snapshot_json"])
            signals = json.loads(run["snapshot_json"])["signals"]
            no_action = not (
                facts["requested_changes"]
                or facts["review_comments"]
                or facts["ci"]["state"] == "failed"
                or any(
                    signal["kind"] == "input"
                    or signal["payload"].get("comments")
                    or signal["payload"].get("review_scope_explanation")
                    for signal in signals
                )
            )
            if no_action:
                decision = "handled"
            elif analysis.review_evidence and analysis.review_scope == "in_scope" and acceptance:
                content = acceptance["content"]
                proposal = proposed_acceptance(analysis)
                unchanged = bool(
                    proposal
                    and proposal["kind"] == acceptance["kind"]
                    and proposal["content"] == content
                )
                if unchanged:
                    decision = "in_scope"
                    specification = required(
                        records,
                        "task_acceptance_specs",
                        task_id=task["id"],
                        revision=task["acceptance_revision"],
                    )
                    if (
                        specification["input_revision"] == task["input_revision"]
                        and specification["approved_revision"] is not None
                        and specification["approved_feedback_revision"] != task["feedback_revision"]
                        and specification["admission_revision"]
                        == required(records, "repositories", id=task["repository_id"])[
                            "admission_revision"
                        ]
                        and specification["target_revision"]
                        == required(records, "repositories", id=task["repository_id"])[
                            "target_revision"
                        ]
                    ):
                        revision = task["acceptance_revision"] + 1
                        records.add(
                            "task_acceptance_specs",
                            {
                                **dict(specification),
                                "revision": revision,
                                "feedback_revision": task["feedback_revision"],
                                "approved_revision": revision,
                                "approved_feedback_revision": task["feedback_revision"],
                            },
                        )
                        records.add(
                            "acceptance_carryovers",
                            {
                                "task_id": task["id"],
                                "revision": revision,
                                "source_revision": task["acceptance_revision"],
                                "context_sha256": lifecycle["feedback_sha256"],
                                "artifact_id": artifact_id,
                            },
                        )
                        bump_task(records, task, acceptance_revision=revision)
                        task = required(records, "tasks", id=task["id"])
            elif analysis.review_scope == "handled" and analysis.review_evidence:
                decision = "handled"
            values = {
                "decision": decision,
                "analysis_json": canonical_json(analysis_data),
                "input_revision": task["input_revision"],
                "feedback_revision": task["feedback_revision"],
                "acceptance_revision": task["acceptance_revision"],
                "actor": "processor",
            }
            if old:
                records.change("review_scope_decisions", identity, values)
            else:
                records.add("review_scope_decisions", {**identity, **values})
            business_event(
                records,
                "review_scope_classified",
                task["id"],
                decision=decision,
                artifact_id=artifact_id,
                context_sha256=lifecycle["feedback_sha256"],
            )
            if decision in {"pending", "handled"}:
                TaskRecords(self.store).acknowledge(
                    token, now=self.services.clock(), records=records
                )
            return decision

    def observe(self, records, repository, task, pull, reviews, comments, checks, statuses, now):
        pull = {
            key: value
            for key, value in pull.items()
            if key not in {"reviews", "review_comments", "files"}
        }
        task = dict(required(records, "tasks", id=task["id"]))
        repository = dict(required(records, "repositories", id=repository["id"]))
        binding = required(records, "managed_pull_requests", task_id=task["id"])
        if str(pull["id"]) != binding["pr_id"]:
            raise ConflictError("PR 稳定身份不匹配")
        if records.one("github_sync_state", repository_id=repository["id"]) is None:
            records.add("github_sync_state", {"repository_id": repository["id"]})
        policy_row = required(records, "github_sync_state", repository_id=repository["id"])
        branch_rules = records.one(
            "github_remote_objects",
            repository_id=repository["id"],
            kind="branch_rules",
            object_id=str(repository["target_revision"]),
        )
        effective = effective_ci_policy(
            json.loads(policy_row["ci_policy_json"]),
            json.loads(branch_rules["payload_json"]) if branch_rules else {},
        )
        current_base = repository["target_sha"] if pull["state"] == "open" else pull["base"]["sha"]
        ci = evaluate_ci(pull, checks, statuses, effective, current_base_sha=current_base)
        snapshot = {
            "pull": pull,
            "reviews": reviews,
            "review_comments": comments,
            "checks": checks,
            "statuses": statuses,
            "ci": ci,
            "policy_revision": policy_row["ci_policy_revision"],
            "effective_ci_policy": effective,
            "requested_changes": active_reviews(reviews, pull["head"]["sha"]),
            "issue_state": json.loads(task["input_json"]).get("state", "unknown"),
            "current_base_sha": current_base,
            "observed_merge_sha": pull.get("merge_commit_sha"),
        }
        from repowatch.application.issue_context import relation_fingerprint

        digest = content_digest({**snapshot, "pull": relation_fingerprint("pull_requests", [pull])})
        feedback_digest = content_digest(
            {
                "head": pull["head"]["sha"],
                "reviews": snapshot["requested_changes"],
                "comments": comments,
                "ci": ci if ci["state"] == "failed" else None,
                "issue_state": snapshot["issue_state"],
            }
        )
        old = records.one("pr_lifecycle", task_id=task["id"])
        if old and old["snapshot_sha256"] == digest:
            return False
        delivery = "merged" if pull.get("merged_at") or pull.get("merged") else pull["state"]
        values = {
            "revision": old["revision"] + 1 if old else 1,
            "snapshot_json": canonical_json(DEFAULT_REDACTOR.clean(snapshot)),
            "snapshot_sha256": digest,
            "feedback_sha256": feedback_digest,
            "number": pull["number"],
            "head_sha": pull["head"]["sha"],
            "base_sha": current_base,
            "merge_sha": pull.get("merge_commit_sha"),
            "delivery_state": delivery,
            "issue_state": json.loads(task["input_json"]).get("state", "unknown"),
            "ci_state": ci["state"],
            "observed_at": now,
        }
        if old:
            records.change("pr_lifecycle", {"task_id": task["id"]}, values)
        else:
            records.add("pr_lifecycle", {"task_id": task["id"], **values})
        records.change("managed_pull_requests", {"task_id": task["id"]}, {"state": delivery})
        settle_ready(records, now)
        if delivery != "open":
            changes = {
                "state": "ended",
                "wait_reason": None,
                "outcome": "pr_merged" if delivery == "merged" else "pr_closed_unmerged",
            }
            if task["state"] == "running":
                changes = {
                    "stop_requested": 1,
                    "pending_transition_json": canonical_json(
                        {
                            **changes,
                            "mode": task["mode"],
                            "input_revision": task["input_revision"],
                            "feedback_revision": task["feedback_revision"],
                            "admission_revision": repository["admission_revision"],
                        }
                    ),
                }
            bump_task(records, task, **changes)
        elif (
            snapshot["requested_changes"]
            or comments
            or ci["state"] == "failed"
            or (old and old["head_sha"] != pull["head"]["sha"])
            or (
                old
                and json.loads(old["snapshot_json"]).get("requested_changes")
                != snapshot["requested_changes"]
            )
        ):
            prior = json.loads(old["snapshot_json"]) if old else {}
            projection = {
                "head": pull["head"]["sha"],
                "reviews": snapshot["requested_changes"],
                "comments": comments,
                "ci": ci if ci["state"] == "failed" else None,
            }
            previous = {
                "head": prior.get("pull", {}).get("head", {}).get("sha"),
                "reviews": prior.get("requested_changes", []),
                "comments": prior.get("review_comments", []),
                "ci": prior.get("ci") if prior.get("ci", {}).get("state") == "failed" else None,
            }
            if projection != previous:
                TaskRecords(self.store).observe(
                    task["id"],
                    "github:pr-feedback:" + binding["pr_id"],
                    content_digest({"context": digest, "revision": values["revision"]}),
                    {"pr_feedback": projection, "context_sha256": digest},
                    kind="feedback",
                    now=now,
                    records=records,
                )
                current = required(records, "tasks", id=task["id"])
                if (
                    not current["user_excluded"]
                    and not current["cancel_requested"]
                    and current["state"] != "blocked"
                ):
                    if current["state"] != "running":
                        bump_task(records, current, state="queued", mode="triage", wait_reason=None)
        mark_sync_change(records, repository["id"], now)
        business_event(
            records,
            "pr_lifecycle_updated",
            task["id"],
            delivery=delivery,
            ci_state=ci["state"],
            context_sha256=digest,
        )
        refresh_ready(records, now)
        return True

    def decide(
        self, operation_id, task_id, *, expected_revision, context_sha256, action, explanation
    ):
        if action not in {"approve", "revise", "clarify", "reject"} or not explanation.strip():
            raise ValueError("须选择评审范围决定并提供依据")
        request = {
            "kind": "review_scope",
            "task_id": task_id,
            "revision": expected_revision,
            "context": context_sha256,
            "action": action,
            "explanation": explanation,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            task = required(records, "tasks", id=task_id)
            lifecycle = required(records, "pr_lifecycle", task_id=task_id)
            scope = required(
                records, "review_scope_decisions", task_id=task_id, context_sha256=context_sha256
            )
            if (
                task["revision"] != expected_revision
                or task["state"] == "running"
                or lifecycle["feedback_sha256"] != context_sha256
                or scope["input_revision"] != task["input_revision"]
                or scope["feedback_revision"] != task["feedback_revision"]
                or scope["acceptance_revision"] != task["acceptance_revision"]
            ):
                raise ConflictError("评审范围或任务修订已变化")
            if scope["decision"] not in {"pending", "revise", "clarify"}:
                raise ConflictError("该评审决定已完成")
            if task["user_excluded"] or task["cancel_requested"] or task["state"] == "blocked":
                raise ConflictError("任务当前不允许评审范围推进")
            records.change(
                "review_scope_decisions",
                {"task_id": task_id, "context_sha256": context_sha256},
                {"decision": action, "actor": "local-user"},
            )
            settle_ready(records, self.services.clock())
            changes = {"state": "waiting", "wait_reason": "review_scope"}
            if action == "approve":
                changes = {"state": "queued", "mode": "triage", "wait_reason": None}
                analysis = TriageAnalysis.model_validate(json.loads(scope["analysis_json"]))
                proposal = proposed_acceptance(analysis)
                if proposal is None:
                    raise ValueError("缺少可批准的具体方案与验收规范")
                repository = required(records, "repositories", id=task["repository_id"])
                revision = task["acceptance_revision"] + 1
                records.add(
                    "task_acceptance_specs",
                    {
                        "task_id": task_id,
                        "revision": revision,
                        "kind": proposal["kind"],
                        "content_json": validate_content(proposal["kind"], proposal["content"]),
                        "input_revision": task["input_revision"],
                        "feedback_revision": task["feedback_revision"],
                        "admission_revision": repository["admission_revision"],
                        "target_revision": repository["target_revision"],
                    },
                )
                changes["acceptance_revision"] = revision
                records.change(
                    "task_acceptance_specs",
                    {"task_id": task_id, "revision": revision},
                    {
                        "approved_revision": revision,
                        "approved_by": "local-user",
                        "approved_feedback_revision": task["feedback_revision"],
                    },
                )
            elif action == "reject":
                changes["wait_reason"] = "pr_review"
            elif action in {"revise", "clarify"}:
                TaskRecords(self.store).observe(
                    task_id,
                    "user:review-scope:" + operation_id,
                    content_digest(request),
                    {
                        "action": action,
                        "review_scope_explanation": DEFAULT_REDACTOR.clean(explanation),
                    },
                    kind="feedback",
                    now=self.services.clock(),
                    records=records,
                )
                task = required(records, "tasks", id=task_id)
                changes = {"state": "queued", "mode": "triage", "wait_reason": None}
            bump_task(records, task, **changes)
            result = {"decision": action}
            remember_operation(records, operation_id, fingerprint, task["revision"] + 1, result)
            business_event(
                records,
                "review_scope_decided",
                task_id,
                action=action,
                explanation=DEFAULT_REDACTOR.clean(explanation),
            )
            refresh_ready(records, self.services.clock())
            return result
