import json

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, StrictBool

from repowatch.application.memory import MemorySourceUnavailable, RepositoryMemory
from repowatch.application.queue_state import refresh_ready
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import (
    bump_task,
    business_event,
    create_task,
    execution_record,
    required,
)
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.memory import MemoryContent
from repowatch.domain.task_records import canonical_json
from repowatch.runtime.engine import StagePlan, ToolBinding


class FeedbackDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unrelated: StrictBool
    rationale: str


class MemoryWorker:
    def __init__(self, services, runtime=None):
        self.services, self.runtime = services, runtime
        self.memory = RepositoryMemory(services.store, services.artifacts)

    def schedule(self):
        with self.services.store.write() as records:
            pending = [
                ("distill", row["id"], row["task_id"], None)
                for row in records.find("distillation_intents", state="pending")
            ]
            pending += [
                ("feedback", row["memory_id"], row["source_task_id"], row["revision"])
                for row in records.find("memory_heads", state="feedback_pending")
                if not row["deleted"] and row["enabled"]
            ]
            pending += [
                ("feedback", row["memory_id"], row["source_task_id"], row["revision"])
                for row in records.find("memory_heads", state="pending_review")
                if not row["deleted"]
                and row["enabled"]
                and row["feedback_revision"] != row["reviewed_feedback_revision"]
            ]
            for kind, identifier, task_id, revision in pending:
                source = required(records, "tasks", id=task_id)
                if (
                    source["state"] == "running"
                    or source["cancel_requested"]
                    or source["user_excluded"]
                ):
                    continue
                key = f"memory:{kind}:{identifier}:{revision}"
                existing = records.one(
                    "task_sources", repository_id=source["repository_id"], source_key=key
                )
                if existing:
                    maintenance = required(records, "tasks", id=existing["task_id"])
                    if (
                        maintenance["state"] == "waiting"
                        and maintenance["wait_reason"] == "memory_source_unavailable"
                        and not maintenance["user_excluded"]
                        and not maintenance["cancel_requested"]
                    ):
                        bump_task(records, maintenance, state="queued", wait_reason=None)
                    continue
                task = create_task(
                    records,
                    source["repository_id"],
                    key,
                    None,
                    "distill",
                    self.services.clock(),
                    owner=source["budget_owner_task_id"],
                    input_payload={"kind": kind, "identifier": identifier, "revision": revision},
                )
                configuration = json.loads(source["configuration_json"])
                records.change(
                    "tasks", {"id": task}, {"configuration_json": canonical_json(configuration)}
                )
            refresh_ready(records, self.services.clock())

    def run(self, token):
        services = self.services
        with services.store.read() as records:
            run = execution_record(records, token, services.clock())
            task = required(records, "tasks", id=run["task_id"])
            request = json.loads(task["input_json"])
            if request["kind"] == "distill":
                intent = dict(required(records, "distillation_intents", id=request["identifier"]))
                source_task = required(records, "tasks", id=intent["task_id"])
                session = dict(required(records, "repair_sessions", id=intent["session_id"]))
                already = records.one(
                    "repository_memories",
                    repository_id=source_task["repository_id"],
                    source_key="verified:" + intent["id"],
                )
                if already:
                    payload = None
                else:
                    payload = {
                        "evidence": json.loads(session["evidence_json"] or "{}"),
                        "frozen": json.loads(session["frozen_json"] or "{}"),
                    }
            else:
                memory = self.memory._view(
                    records, required(records, "repository_memories", id=request["identifier"])
                )
                if (
                    memory["deleted"]
                    or not memory["enabled"]
                    or memory["revision"] != request["revision"]
                    or memory["state"] not in {"feedback_pending", "pending_review"}
                ):
                    payload = None
                else:
                    payload = memory
                source_task = required(records, "tasks", id=memory["source_task_id"])
                session = dict(
                    required(records, "repair_sessions", id=memory["source"]["session_id"])
                )
                payload = (
                    {
                        "memory": memory,
                        "feedback": [
                            dict(item)
                            for item in records.find(
                                "pending_signals", task_id=source_task["id"], kind="feedback"
                            )
                        ],
                    }
                    if payload is not None
                    else None
                )
            if run["budget_owner_task_id"] != source_task["budget_owner_task_id"]:
                raise PermissionError("提炼必须计入来源任务预算")
            unavailable = source_task["user_excluded"] or source_task["cancel_requested"]
        if unavailable:
            return self.wait_for_source(token)
        if payload is not None:
            read = []

            @tool
            def read_evidence(artifact_id: str = "") -> str:
                """仅可读取本次来源证据和已绑定产物，不能读取或写入工作区。"""
                read.append(True)
                if not artifact_id:
                    return canonical_json(payload)
                allowed = json.loads(session["evidence_json"] or "{}").get("artifact_ids", [])
                if request["kind"] == "feedback":
                    allowed += [item["id"] for item in memory["source"].get("artifacts", [])]
                if artifact_id not in allowed:
                    raise PermissionError("产物不属于提炼来源")
                artifact = services.artifacts.require(artifact_id, now=services.clock())
                if (services.artifacts.root / artifact["relative_path"]).stat().st_size > 32000:
                    raise ValueError("证据过大，需分块归档")
                return (services.artifacts.root / artifact["relative_path"]).read_text("utf-8")

            def verify(result, _events):
                if not read or result.status != "completed":
                    raise PermissionError("提炼／反馈复核必须读取证据并完成结构化结果")
                with services.store.read() as records:
                    execution_record(records, token, services.clock())
                if request["kind"] == "distill":
                    content = MemoryContent.model_validate_json(result.summary)
                    identifier = self.memory.record_verified(
                        intent["id"], content.model_dump(), now=services.clock()
                    )
                    return {"comparison": {"memory_id": identifier}, "verified_fact_ids": []}
                decision = FeedbackDecision.model_validate_json(result.summary)
                if not decision.rationale.strip():
                    raise ValueError("反馈判断缺少依据")
                try:
                    with services.store.write() as records:
                        execution_record(records, token, services.clock())
                        self.memory.review_feedback(
                            memory["id"],
                            expected_revision=memory["revision"],
                            feedback_revision=memory["feedback_revision"],
                            unrelated=decision.unrelated,
                            source_sha256=memory["source"]["source_sha256"],
                            records=records,
                        )
                except ConflictError:
                    return {"comparison": {"feedback_stale": True}, "verified_fact_ids": []}
                return {"comparison": decision.model_dump(), "verified_fact_ids": []}

            frozen = json.loads(session["frozen_json"] or "{}")
            code = {
                "base_sha": frozen["base_sha"],
                "candidate_sha": frozen["sha"],
                "tree_sha": frozen["tree_sha"],
                "content_manifest_sha256": frozen["manifest_sha256"],
            }
            objective = (
                "仅依证据提炼仓库经验。未证实根因写入 limitations，不能冒充事实。"
                "StructuredResult.summary 是 JSON，包含 symptoms、applicability、method、"
                "verification、limitations 五个非空字符串，不输出密钥；不声明可跨仓共享。"
                if request["kind"] == "distill"
                else "逐条核对新反馈是否推翻原经验。证据不能确认无关时 unrelated=false。"
                "StructuredResult.summary 为 JSON：unrelated 布尔值、rationale 非空依据。"
            )
            try:
                self.runtime.run(
                    token,
                    StagePlan(
                        "memory:" + request["identifier"],
                        "memory:" + request["identifier"],
                        objective,
                        "distill",
                        "processor",
                        (ToolBinding(read_evidence, BudgetAmounts(active_ms=1000)),),
                        verify,
                        code_version=code,
                        record_attempt=False,
                    ),
                )
            except MemorySourceUnavailable:
                self.runtime._cancel_unstarted(token)
                return self.wait_for_source(token)
        if request["kind"] == "distill":
            with services.store.write() as records:
                execution_record(records, token, services.clock())
                records.change("distillation_intents", {"id": intent["id"]}, {"state": "consumed"})
        index = getattr(services, "memory_index", None)
        index_result = None
        if index is not None:
            try:
                index_result = index.rebuild()
            except StorageError:
                raise
            except Exception as error:
                index_result = {"state": "deferred", "error": type(error).__name__}
                with services.store.write() as records:
                    execution_record(records, token, services.clock())
                    business_event(
                        records,
                        "memory_index_deferred",
                        task["id"],
                        error_type=type(error).__name__,
                    )
        services.queue.complete(
            token, now=services.clock(), state="ended", outcome="memory_completed"
        )
        return {"state": "ended", "outcome": "memory_completed", "index": index_result}

    def wait_for_source(self, token):
        self.services.queue.complete(
            token,
            now=self.services.clock(),
            state="waiting",
            wait_reason="memory_source_unavailable",
        )
        with self.services.store.read() as records:
            run = required(records, "runs", id=token.run_id)
            task = required(records, "tasks", id=run["task_id"])
            return {
                "state": task["state"],
                "reason": task["wait_reason"] or task["outcome"] or "memory_source_unavailable",
            }
