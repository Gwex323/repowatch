import json

from repowatch.adapters.storage.artifact_files import verify_file
from repowatch.application.budgets import remember_operation, replay_operation
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import business_event, required
from repowatch.domain.memory import MemoryContent
from repowatch.domain.task_records import canonical_json, content_digest, require_time, stable_id
from repowatch.security import DEFAULT_REDACTOR


class MemorySourceUnavailable(RuntimeError):
    pass


def require_memory_source(records, task):
    if task["mode"] != "distill":
        return
    request = json.loads(task["input_json"])
    if request.get("kind") == "distill":
        source_id = required(records, "distillation_intents", id=request["identifier"])["task_id"]
    elif request.get("kind") == "feedback":
        source_id = required(records, "memory_heads", memory_id=request["identifier"])[
            "source_task_id"
        ]
    else:
        return
    source = required(records, "tasks", id=source_id)
    if source["user_excluded"] or source["cancel_requested"]:
        raise MemorySourceUnavailable("来源任务已排除或取消，记忆维护等待明确重新启用")


class RepositoryMemory:
    def __init__(self, store, artifacts):
        self.store, self.artifacts = store, artifacts

    def record_manual(self, repository_id, key, content, *, now, operation_id=None):
        return self._save(
            repository_id,
            "manual:" + key,
            content,
            "manual_unverified",
            {},
            now,
            operation_id=operation_id,
        )

    def record_attempt(self, session_id, key, content, artifact_ids, *, now):
        if not artifact_ids:
            raise ValueError("失败方法必须引用实际证据")
        with self.store.read() as records:
            session = dict(required(records, "repair_sessions", id=session_id))
            task = dict(required(records, "tasks", id=session["task_id"]))
            if session["state"] == "verified":
                raise ValueError("已验证会话不能作为失败尝试保存")
            source = self._source(records, session, task, artifact_ids, now, block_on_failure=False)
            source["verification_state"] = "failure"
        return self._save(
            task["repository_id"],
            "failure:" + session_id + ":" + key,
            content,
            "failure",
            source,
            now,
        )

    def record_verified(self, intent_id, content, *, now):
        with self.store.read() as records:
            intent = dict(required(records, "distillation_intents", id=intent_id))
            session = dict(required(records, "repair_sessions", id=intent["session_id"]))
            task = dict(required(records, "tasks", id=intent["task_id"]))
            evidence = json.loads(session["evidence_json"] or "{}")
            frozen = json.loads(session["frozen_json"] or "{}")
            if (
                session["task_id"] != task["id"]
                or session["state"] != "verified"
                or not evidence.get("passed")
                or not evidence.get("review_passed")
                or intent["source_sha256"]
                != content_digest({"frozen": frozen, "evidence": evidence})
                or intent["feedback_revision"] != frozen.get("feedback_revision")
            ):
                raise PermissionError("成功记忆缺少匹配的验证与独审来源")
            review = required(records, "runs", id=evidence["review_run_id"])
            if (
                review["task_id"] != task["id"]
                or review["parent_run_id"] != session["run_id"]
                or review["state"] != "ended"
                or json.loads(review["snapshot_json"]).get("mode") != "review"
            ):
                raise PermissionError("独审来源归属或状态无效")
            source = self._source(records, session, task, evidence["artifact_ids"], now)
            source.update(
                {
                    "intent_id": intent_id,
                    "source_sha256": intent["source_sha256"],
                    "feedback_revision": intent["feedback_revision"],
                }
            )
        return self._save(
            task["repository_id"],
            "verified:" + intent_id,
            content,
            "success",
            source,
            now,
        )

    def _source(self, records, session, task, artifact_ids, now, *, block_on_failure=True):
        if not artifact_ids:
            raise PermissionError("记忆来源没有实际产物")
        frozen = json.loads(session["frozen_json"] or "{}")
        references = []
        for identifier in dict.fromkeys(artifact_ids):
            artifact = self.artifacts.require(
                identifier, now=now, block_on_failure=block_on_failure
            )
            run = required(records, "runs", id=artifact["run_id"])
            if run["task_id"] != task["id"]:
                raise PermissionError("产物不属于来源任务")
            references.append(
                {key: artifact[key] for key in ("id", "run_id", "relative_path", "sha256")}
            )
        return {
            "task_id": task["id"],
            "run_id": session["run_id"],
            "session_id": session["id"],
            "issue_id": task["issue_id"],
            "base_sha": frozen.get("base_sha", session["base_sha"]),
            "candidate_sha": frozen.get("sha"),
            "source_sha256": content_digest(
                {"frozen": frozen, "evidence": json.loads(session["evidence_json"] or "{}")}
            ),
            "feedback_revision": frozen.get("feedback_revision", task["feedback_revision"]),
            "artifacts": references,
        }

    def _save(self, repository_id, key, content, state, source, now, *, operation_id=None):
        require_time(now)
        body = MemoryContent.model_validate(content).model_dump()
        if DEFAULT_REDACTOR.clean(body) != body:
            raise ValueError("记忆正文包含凭据")
        identifier = stable_id("repository-memory", repository_id, key)
        original_state = state
        with self.store.write() as records:
            if operation_id:
                fingerprint, replay = replay_operation(
                    records,
                    operation_id,
                    {"manual_memory_repository": repository_id, "key": key, "content": body},
                )
                if replay is not None:
                    return replay["id"]
            required(records, "repositories", id=repository_id)
            if source:
                session = required(records, "repair_sessions", id=source["session_id"])
                task = required(records, "tasks", id=source["task_id"])
                if task["user_excluded"] or task["cancel_requested"]:
                    raise MemorySourceUnavailable("来源任务已排除或取消")
                if task["repository_id"] != repository_id or session["task_id"] != task["id"]:
                    raise PermissionError("记忆来源仓库不匹配")
                for reference in source["artifacts"]:
                    artifact = required(records, "artifacts", id=reference["id"])
                    if not artifact["available"] or artifact["sha256"] != reference["sha256"]:
                        raise ConflictError("记忆提交前产物已失效")
                if state == "success":
                    intent = required(records, "distillation_intents", id=source["intent_id"])
                    frozen = json.loads(session["frozen_json"] or "{}")
                    evidence = json.loads(session["evidence_json"] or "{}")
                    if session["state"] != "verified" or intent["source_sha256"] != content_digest(
                        {"frozen": frozen, "evidence": evidence}
                    ):
                        raise ConflictError("提炼期间来源证据已变化")
                if task["feedback_revision"] != source["feedback_revision"]:
                    state = "pending_review"
            values = {
                "id": identifier,
                "repository_id": repository_id,
                "source_key": key,
                "state": state,
                "content_json": canonical_json(body),
                "source_json": canonical_json(source),
                "created_at": now,
            }
            existing = records.one("repository_memories", id=identifier)
            if existing:
                previous_source = json.loads(existing["source_json"])
                comparable_source = dict(source)
                if "verification_state" not in previous_source:
                    comparable_source.pop("verification_state", None)
                if (
                    any(
                        existing[field] != values[field]
                        for field in ("repository_id", "source_key", "content_json")
                    )
                    or previous_source != comparable_source
                ):
                    raise ConflictError("同一记忆来源已保存不同内容")
                if operation_id:
                    remember_operation(records, operation_id, fingerprint, 1, {"id": identifier})
                return identifier
            records.add("repository_memories", values)
            if operation_id:
                remember_operation(records, operation_id, fingerprint, 1, {"id": identifier})
            if state == "pending_review":
                records.change(
                    "memory_heads",
                    {"memory_id": identifier},
                    {"feedback_revision": task["feedback_revision"], "prior_state": original_state},
                )
            business_event(
                records, "memory_recorded", source.get("task_id"), memory_id=identifier, state=state
            )
        return identifier

    def list(self, repository_id):
        with self.store.read() as records:
            required(records, "repositories", id=repository_id)
            return [
                self._view(records, row)
                for row in records.find("repository_memories", repository_id=repository_id)
            ]

    def _view(self, records, row):
        source = json.loads(row["source_json"])
        head = required(records, "memory_heads", memory_id=row["id"])
        version = required(
            records, "memory_versions", memory_id=row["id"], revision=head["content_revision"]
        )
        lifecycle = (
            records.one("pr_lifecycle", task_id=source["task_id"])
            if source.get("task_id")
            else None
        )
        return {
            **{
                key: value
                for key, value in row.items()
                if key not in {"content_json", "source_json"}
            },
            **dict(head),
            "content": json.loads(version["content_json"]),
            "source": source,
            "visibility": "repository",
            "delivery_state": lifecycle["delivery_state"] if lifecycle else "unknown",
        }

    def get(self, identifier):
        with self.store.read() as records:
            return self._view(records, required(records, "repository_memories", id=identifier))

    def operate(self, operation_id, identifier, *, expected_revision, action, data):
        request = {
            "memory_id": identifier,
            "revision": expected_revision,
            "action": action,
            "data": data,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            required(records, "repository_memories", id=identifier)
            head = required(records, "memory_heads", memory_id=identifier)
            if head["revision"] != expected_revision or head["deleted"]:
                raise ConflictError("记忆已变化或删除，请刷新")
            changes = {"revision": expected_revision + 1}
            if action == "edit":
                body = MemoryContent.model_validate(data["content"]).model_dump()
                if DEFAULT_REDACTOR.clean(body) != body:
                    raise ValueError("正文包含凭据")
                records.add(
                    "memory_versions",
                    {
                        "memory_id": identifier,
                        "revision": changes["revision"],
                        "content_json": canonical_json(body),
                    },
                )
                changes.update(
                    content_revision=changes["revision"],
                    state="manual_unverified",
                    prior_state="manual_unverified",
                )
            elif action in {"disable", "enable"}:
                changes["enabled"] = int(action == "enable")
                if action == "enable" and head["source_task_id"]:
                    task = required(records, "tasks", id=head["source_task_id"])
                    if task["feedback_revision"] != head["reviewed_feedback_revision"]:
                        changes.update(
                            state="feedback_pending", feedback_revision=task["feedback_revision"]
                        )
            elif action == "delete":
                changes.update(deleted=1, enabled=0)
            elif action == "share":
                if head["state"] != "success" or not head["enabled"]:
                    raise PermissionError("只有有效成功经验可共享")
                body = MemoryContent.model_validate(data["content"]).model_dump()
                encoded = canonical_json(body)
                import re

                if DEFAULT_REDACTOR.clean(body) != body or re.search(
                    r"https?://|[A-Za-z]:[\\/]|```|\b[0-9a-f]{32,}\b", encoded
                ):
                    raise ValueError("共享内容包含原始路径、代码块、标识或凭据")
                if data.get("confirmed_sanitized") is not True:
                    raise PermissionError("共享必须明确确认已去敏，不确定时保持仓库范围")
                values = {
                    "memory_id": identifier,
                    "source_revision": changes["revision"],
                    "content_revision": head["content_revision"],
                    "content_json": encoded,
                    "actor": "local_user",
                }
                if records.one("memory_shares", memory_id=identifier):
                    records.change("memory_shares", {"memory_id": identifier}, values)
                else:
                    records.add("memory_shares", values)
            else:
                raise ValueError("未知记忆操作")
            records.change("memory_heads", {"memory_id": identifier}, changes)
            records.add(
                "memory_index_events", {"memory_id": identifier, "revision": changes["revision"]}
            )
            result = {
                "id": identifier,
                "revision": changes["revision"],
                "action": action,
                "audit_retained": True,
                "past_requests_retracted": False,
            }
            business_event(records, "memory_" + action, memory_id=identifier)
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            return result

    def review_feedback(
        self,
        identifier,
        *,
        expected_revision,
        feedback_revision,
        unrelated,
        source_sha256,
        records=None,
    ):
        from contextlib import nullcontext

        with nullcontext(records) if records is not None else self.store.write() as records:
            memory = required(records, "repository_memories", id=identifier)
            source = json.loads(memory["source_json"])
            head = required(records, "memory_heads", memory_id=identifier)
            task = required(records, "tasks", id=head["source_task_id"])
            if (
                head["deleted"]
                or not head["enabled"]
                or head["revision"] != expected_revision
                or head["feedback_revision"] != feedback_revision
                or task["feedback_revision"] != feedback_revision
                or source.get("source_sha256") != source_sha256
                or head["state"] not in {"feedback_pending", "pending_review"}
            ):
                raise ConflictError("反馈复核已过期，不能恢复被修改或删除的经验")
            state = head["prior_state"] if unrelated else "pending_review"
            if state in {"pending_review", "feedback_pending"} and unrelated:
                state = source.get("verification_state", memory["state"])
                if state == "pending_review" and source.get("intent_id"):
                    state = "success"
            changes = {
                "revision": expected_revision + 1,
                "state": state,
                "reviewed_feedback_revision": feedback_revision,
            }
            records.change("memory_heads", {"memory_id": identifier}, changes)
            records.add(
                "memory_index_events", {"memory_id": identifier, "revision": changes["revision"]}
            )
            business_event(
                records,
                "memory_feedback_reviewed",
                task["id"],
                memory_id=identifier,
                unrelated=unrelated,
                revision=changes["revision"],
            )

    def eligible(self, records, identifier, repository_id, revision=None):
        memory = records.one("repository_memories", id=identifier)
        if memory is None:
            return None
        view = self._view(records, memory)
        if (
            view["deleted"]
            or not view["enabled"]
            or view["state"] not in {"success", "failure"}
            or (revision is not None and view["revision"] != revision)
        ):
            return None
        shared = None
        if memory["repository_id"] != repository_id:
            shared = records.one("memory_shares", memory_id=identifier)
            if (
                not shared
                or shared["content_revision"] != view["content_revision"]
                or view["state"] != "success"
            ):
                return None
        task_id = view["source"].get("task_id")
        if task_id:
            task = required(records, "tasks", id=task_id)
            if task["feedback_revision"] != view["reviewed_feedback_revision"]:
                return None
            if any(
                not required(records, "artifacts", id=item["id"])["available"]
                or not verify_file(self.artifacts.root, item["relative_path"], item["sha256"])[
                    "valid"
                ]
                for item in view["source"].get("artifacts", [])
            ):
                return None
        if memory["repository_id"] != repository_id:
            return {
                "id": identifier,
                "revision": view["revision"],
                "visibility": "shared",
                "state": "success",
                "content": json.loads(shared["content_json"]),
                "source": {"memory_id": identifier},
            }
        return view
