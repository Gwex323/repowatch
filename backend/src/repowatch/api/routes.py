"""已认证的本地管理路由；模型／命令只保存受管任务，不在请求内执行。"""

import json

from fastapi import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from repowatch.api.guard import GuardedRouter
from repowatch.domain.budget import BudgetAmounts


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=200)
    expected_revision: int | None = Field(default=None, strict=True, ge=0)
    data: dict = Field(default_factory=dict)

    def revision(self):
        if self.expected_revision is None:
            raise ValueError("操作缺少预期修订")
        return self.expected_revision


def routes(services):
    router = GuardedRouter(services.settings, prefix="/api/v1")

    @router.get("/state")
    def state():
        return services.console.state()

    @router.get("/tasks/{task_id}")
    def task(task_id: str):
        return services.console.task(task_id)

    @router.post("/tasks/{task_id}/review-scope/decide")
    def review_scope(task_id: str, body: Mutation):
        return services.lifecycle.decide(
            body.operation_id,
            task_id,
            expected_revision=body.revision(),
            context_sha256=body.data["context_sha256"],
            action=body.data["action"],
            explanation=body.data["explanation"],
        )

    @router.get("/events")
    def events(after: int = 0, limit: int = 100, task_id: str | None = None):
        return services.console.events.read(after=after, limit=limit, task_id=task_id)

    @router.get("/events/stream")
    def event_stream(request: Request, after: int = 0, task_id: str | None = None):
        cursor = request.headers.get("last-event-id")
        if cursor is not None:
            if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 19:
                raise ValueError("事件游标无效")
            after = int(cursor)
        if not 0 <= after <= 9223372036854775807:
            raise ValueError("事件游标无效")
        batch = services.console.events.read(after=after, limit=100, task_id=task_id)

        def frames():
            # Bounded batches release the read transaction before streaming. EventSource
            # reconnects with Last-Event-ID; every reconnect rechecks the local session.
            yield "retry: 1500\n: bounded event batch\n\n"
            for event in batch["events"]:
                data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event['sequence']}\nevent: business\ndata: {data}\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    @router.get("/providers")
    def providers():
        return services.providers.list()

    @router.post("/repositories/preflight")
    def repository_preflight(body: Mutation):
        return services.repositories.preflight(body.operation_id, body.data, now=services.clock())

    @router.get("/repositories/{repository_id}/memories")
    def repository_memories(repository_id: str):
        return services.memories.list(repository_id)

    @router.get("/memories/search")
    def search_memories(repository_id: str, query: str, limit: int = 5):
        with services.store.read() as records:
            if records.one("repositories", id=repository_id) is None:
                raise LookupError("仓库不存在")
        return services.memory_index.search(repository_id, query, limit=limit)

    @router.get("/memories/{identifier}")
    def memory_detail(identifier: str):
        return services.memories.get(identifier)

    @router.post("/memories/{identifier}/{action}")
    def memory_operation(identifier: str, action: str, body: Mutation):
        return services.memories.operate(
            body.operation_id,
            identifier,
            expected_revision=body.revision(),
            action=action,
            data=body.data,
        )

    @router.post("/repositories/{repository_id}/memories")
    def manual_memory(repository_id: str, body: Mutation):
        identifier = services.memories.record_manual(
            repository_id,
            body.operation_id,
            body.data["content"],
            now=services.clock(),
            operation_id=body.operation_id,
        )
        return services.memories.get(identifier)

    @router.get("/memory-index")
    def memory_index_status():
        return {
            "status": services.memory_index.status(),
            "configuration": services.memory_index.configuration(),
            "models": [
                "jinaai/jina-embeddings-v2-base-zh",
                "jinaai/jina-embeddings-v2-base-code",
                "BAAI/bge-small-zh-v1.5",
            ],
            "selection_warning": "候选尚未通过真实语料选型门槛；默认使用关键词检索",
        }

    @router.post("/memory-index/configure")
    def configure_memory_index(body: Mutation):
        from repowatch.application.budgets import remember_operation, replay_operation
        from repowatch.application.storage import ConflictError
        from repowatch.domain.task_records import canonical_json

        model = body.data.get("model")
        if model not in [None, *memory_index_status()["models"]]:
            raise ValueError("未锁定的编码模型")
        request = {"memory_index": body.data, "revision": body.revision()}
        with services.store.write() as records:
            fingerprint, replay = replay_operation(records, body.operation_id, request)
            if replay is not None:
                return replay
            current = records.one("memory_index_configuration", singleton=1)
            if current["revision"] != body.revision():
                raise ConflictError("索引配置已变化")
            result = {
                "revision": current["revision"] + 1,
                "configuration_json": canonical_json({"model": model}),
            }
            records.change("memory_index_configuration", {"singleton": 1}, result)
            remember_operation(records, body.operation_id, fingerprint, body.revision(), result)
        return result

    @router.post("/memory-index/rebuild")
    def rebuild_memory_index(body: Mutation):
        from repowatch.application.budgets import remember_operation, replay_operation
        from repowatch.application.storage import ConflictError

        request = {"memory_index_rebuild": True, "revision": body.revision()}
        with services.store.write() as records:
            fingerprint, replay = replay_operation(records, body.operation_id, request)
            if replay is not None:
                return replay
            if (
                records.one("memory_index_configuration", singleton=1)["revision"]
                != body.revision()
            ):
                raise ConflictError("索引配置已变化")
        result = services.memory_index.rebuild()
        with services.store.write() as records:
            fingerprint, replay = replay_operation(records, body.operation_id, request)
            if replay is not None:
                return replay
            remember_operation(records, body.operation_id, fingerprint, body.revision(), result)
        return result

    @router.get("/memory-audits")
    def memory_audits(repository_id: str):
        with services.store.read() as records:
            runs = {
                row["id"]
                for row in records.find("runs")
                if records.one("tasks", id=row["task_id"])["repository_id"] == repository_id
            }
            return {
                "request_checks": [
                    dict(row)
                    for row in records.find("memory_request_audits")
                    if row["run_id"] in runs
                ],
                "scope": (
                    "记录请求前依赖核验，实际发送以调用账本为准。"
                    "删除记忆不撤回已发送请求，也不删除原任务审计。"
                ),
            }

    @router.get("/repositories/{repository_id}/scans")
    def scans(repository_id: str):
        return services.scans.view(repository_id)

    @router.post("/repositories/{repository_id}/sync")
    def request_sync(repository_id: str, body: Mutation):
        if body.data:
            raise ValueError("同步请求不接受附加参数")
        return services.synchronization.request(body.operation_id, repository_id)

    @router.get("/repositories/{repository_id}/sync")
    def sync_status(repository_id: str):
        return services.synchronization.status(repository_id)

    @router.post("/repositories/{repository_id}/sync/configure")
    def sync_configure(repository_id: str, body: Mutation):
        return services.synchronization.configure(
            body.operation_id, repository_id, body.data, expected_revision=body.revision()
        )

    @router.post("/scan-candidates/{candidate_id}/decide")
    def candidate_decide(candidate_id: str, body: Mutation):
        return services.scans.decide_candidate(
            body.operation_id,
            candidate_id,
            body.data["action"],
            expected_revision=body.revision(),
            now=services.clock(),
        )

    @router.post("/repositories/import")
    def repository_import(body: Mutation):
        return services.repositories.save(
            body.operation_id,
            body.data["preflight_id"],
            body.data.get("policy", {}),
            scan_policy=body.data.get("scan_policy"),
            expected_revision=body.revision(),
            now=services.clock(),
        )

    @router.post("/providers/import")
    def import_providers(body: Mutation):
        return services.providers.save(body.operation_id, body.data["payload"])

    @router.post("/providers/{provider_id}/edit")
    def edit_provider(provider_id: str, body: Mutation):
        return services.providers.save(
            body.operation_id,
            body.data["payload"],
            provider_id=provider_id,
            expected_revision=body.revision(),
        )

    @router.post("/providers/{revision_id}/models")
    def model_list(revision_id: str, body: Mutation):
        # Explicit host read; no inference request and no queue bypass.
        return services.adapter.list_models(body.operation_id, revision_id)

    @router.get("/providers/batches/{batch_id}")
    def provider_batch(batch_id: str):
        return services.providers.status(batch_id)

    @router.get("/roles")
    def roles():
        return services.roles.list()

    @router.post("/roles/{key}")
    def bind_role(key: str, body: Mutation):
        return services.roles.set_binding(
            body.operation_id,
            key,
            body.data["definition"],
            expected_revision=body.revision(),
        )

    @router.post("/control/{action}")
    def control(action: str, body: Mutation):
        if action not in {"pause", "resume"}:
            raise ValueError("未知控制操作")
        return getattr(services.control, action)(
            body.operation_id,
            expected_revision=body.revision(),
            now=services.clock(),
            repository_id=body.data.get("repository_id"),
        )

    @router.post("/tasks/{task_id}/operate")
    def operate(task_id: str, body: Mutation):
        return services.tasks.operate(
            body.operation_id,
            task_id,
            expected_revision=body.revision(),
            action=body.data["action"],
            payload=body.data.get("payload"),
            now=services.clock(),
        )

    @router.get("/tasks/{task_id}/artifacts/{artifact_id}")
    def artifact(task_id: str, artifact_id: str):
        return services.console.artifact(
            task_id, artifact_id, services.artifacts, now=services.clock()
        )

    @router.post("/tasks/{task_id}/relations/decide")
    def relation_decide(task_id: str, body: Mutation):
        return services.issue_contexts.decide(
            body.operation_id, task_id, expected_revision=body.revision(), **body.data
        )

    @router.post("/tasks/{task_id}/candidate-issue/decide")
    def candidate_issue_decide(task_id: str, body: Mutation):
        return services.candidate_issues.decide(
            body.operation_id, task_id, expected_revision=body.revision(), **body.data
        )

    @router.post("/reasons/{reason_id}/resolve")
    def resolve(reason_id: str, body: Mutation):
        return services.control.resolve_reason(
            body.operation_id,
            reason_id,
            expected_revision=body.revision(),
            now=services.clock(),
            explanation=body.data["explanation"],
            retry_plan=body.data.get("retry_plan"),
            retry_limits=body.data.get("retry_limits"),
        )

    @router.post("/diagnostics")
    def diagnostic(body: Mutation):
        return services.diagnostics.request(
            body.operation_id,
            body.data["kind"],
            body.data["parameters"],
            BudgetAmounts(**body.data["limits"]),
            expected_revision=body.revision(),
            now=services.clock(),
            duration=body.data.get("duration", 60),
        )

    @router.post("/tasks/{task_id}/budget/initial")
    def initial_budget(task_id: str, body: Mutation):
        return services.budgets.authorize_initial(
            body.operation_id,
            task_id,
            BudgetAmounts(**body.data["limits"]),
            expected_revision=body.revision(),
            reason=body.data["reason"],
            now=services.clock(),
        )

    @router.post("/budget/renewals/{renewal_id}/decide")
    def renewal(renewal_id: str, body: Mutation):
        return services.budgets.decide_renewal(
            body.operation_id,
            renewal_id,
            expected_revision=body.revision(),
            decision=body.data["decision"],
            reason=body.data["reason"],
            now=services.clock(),
            increase=BudgetAmounts(**body.data["increase"]) if body.data.get("increase") else None,
        )

    @router.get("/tasks/{task_id}/budget/adjustment-basis")
    def adjustment_basis(task_id: str):
        return services.adjustments.basis(task_id)

    @router.post("/tasks/{task_id}/budget/adjust")
    def adjustment(task_id: str, body: Mutation):
        return services.adjustments.approve(
            body.operation_id,
            task_id,
            BudgetAmounts(**body.data["increase"]),
            expected_revision=body.revision(),
            expected_basis=body.data["basis_sha256"],
            reason=body.data["reason"],
            now=services.clock(),
        )

    @router.post("/budget/renewals/{renewal_id}/refresh")
    def refresh(renewal_id: str, body: Mutation):
        return services.budgets.refresh_renewal(
            body.operation_id,
            renewal_id,
            expected_revision=body.revision(),
            reason=body.data["reason"],
            now=services.clock(),
        )

    @router.post("/runs/{run_id}/report")
    def report(run_id: str, body: Mutation):
        # Report identity binds current database facts; it cannot replay any side effect.
        return services.recovery.report(run_id, now=services.clock())

    @router.post("/recovery/{report_id}/abandon")
    def abandon(report_id: str, body: Mutation):
        return services.recovery.abandon(
            body.operation_id,
            report_id,
            expected_basis=body.data["basis_sha256"],
            explanation=body.data["explanation"],
            now=services.clock(),
        )

    return router
