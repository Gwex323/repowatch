import json

from repowatch.application.blockers import check_environment, failure_details, guidance
from repowatch.application.budget_accounting import budget_view
from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.queue_state import refresh_ready, settle_ready
from repowatch.application.safety import assert_quiescent, assert_task_budget
from repowatch.application.storage import BusinessStore, ConflictError
from repowatch.application.task_records import TaskRecords, bump_task, business_event, required
from repowatch.domain.task_records import canonical_json, content_digest, require_time, stable_id


class ControlService:
    def __init__(self, store: BusinessStore, *, commands=None):
        self.store, self.commands = store, commands

    def _check_commands(self):
        if self.commands is not None:
            with self.store.read() as records:
                pending = [
                    row["id"]
                    for row in records.find("command_states")
                    if row["state"] not in {"stopped", "isolated"}
                ]
            for identifier in pending:
                self.commands.inspect(identifier)

    def pause(
        self, operation_id, *, expected_revision, now, repository_id=None, actor="local-user"
    ):
        human(actor)
        require_time(now)
        request = {
            "kind": "pause",
            "repository": repository_id,
            "revision": expected_revision,
            "actor": actor,
        }
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            collection, identity = (
                ("repositories", {"id": repository_id})
                if repository_id
                else ("global_control", {"singleton": 1})
            )
            control = required(records, collection, **identity)
            if control["revision"] != expected_revision:
                raise ConflictError("控制状态已变化，请刷新")
            settle_ready(records, now)
            records.change(collection, identity, {"paused": 1, "revision": control["revision"] + 1})
            for task in records.find("tasks", state="running"):
                if repository_id is None or task["repository_id"] == repository_id:
                    bump_task(records, task, stop_requested=1)
            result = {
                "paused": True,
                "revision": control["revision"] + 1,
                "repository_id": repository_id,
            }
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(
                records,
                "pause_requested",
                operation_id=operation_id,
                repository_id=repository_id,
                actor=actor,
            )
            return result

    def cancel(self, operation_id, task_id, *, expected_revision, now, actor="local-user"):
        human(actor)
        return TaskRecords(self.store).operate(
            operation_id,
            task_id,
            expected_revision=expected_revision,
            action="cancel",
            now=now,
            actor=actor,
        )

    def resolve_reason(
        self,
        operation_id,
        reason_id,
        *,
        expected_revision,
        now,
        explanation: str = "",
        retry_plan: dict | None = None,
        retry_limits: dict | None = None,
        actor="local-user",
        automatic=False,
    ):
        human(actor)
        require_time(now)
        if not explanation.strip() and not automatic:
            raise ValueError("处理事项必须有依据")
        self._check_commands()
        request = {
            "kind": "resolve_reason",
            "reason": reason_id,
            "revision": expected_revision,
            "explanation": explanation,
            "retry_plan": retry_plan,
            "retry_limits": retry_limits,
            "actor": actor,
            "automatic": automatic,
        }
        inspected_revision = None
        check_failure = None
        if automatic:
            with self.store.read() as records:
                _, replay = replay_operation(records, operation_id, request)
                if replay is not None:
                    return replay
                reason = required(records, "block_reasons", id=reason_id)
                if reason["revision"] != expected_revision or reason["resolved"]:
                    raise ConflictError("阻塞事项已变化或处理")
                task = records.one("tasks", id=reason["task_id"]) if reason["task_id"] else None
                inspected_revision = task["revision"] if task else None
                configuration = json.loads(task["configuration_json"]) if task else {}
                details = json.loads(reason["details_json"])
                diagnosis = details.get("diagnosis", "")
                if (
                    task
                    and task["mode"] == "scan"
                    and reason["code"] == "runtime_execution_rejected"
                    and configuration.get("execution_backend", "docker") == "docker"
                    and not diagnosis
                ):
                    diagnosis = "docker_operation_failed"
            try:
                check_environment(self.store.settings, configuration, diagnosis)
            except Exception as error:
                check_failure = failure_details(error, stage="重新检查执行环境")
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            reason = required(records, "block_reasons", id=reason_id)
            if reason["revision"] != expected_revision or reason["resolved"]:
                raise ConflictError("阻塞事项已变化或处理")
            task = records.one("tasks", id=reason["task_id"]) if reason["task_id"] else None
            if automatic and task and task["revision"] != inspected_revision:
                raise ConflictError("检查期间任务配置已变化，请重新检查")
            if automatic and check_failure:
                previous = json.loads(reason["details_json"])
                details = {**previous, **check_failure}
                records.change(
                    "block_reasons",
                    {"id": reason_id},
                    {
                        "details_json": canonical_json(details),
                        "revision": reason["revision"] + 1,
                    },
                )
                view = guidance(details.get("diagnosis", reason["code"]), details)
                result = {"reason_id": reason_id, "resolved": False, "message": view["description"]}
                remember_operation(records, operation_id, fingerprint, expected_revision, result)
                business_event(
                    records,
                    "reason_check_failed",
                    reason["task_id"],
                    reason_id=reason_id,
                    details=details,
                )
                return result
            if reason["code"] == "backup_restore_reconciliation":
                if automatic:
                    result = {
                        "reason_id": reason_id,
                        "resolved": False,
                        "message": "请先在系统与维护中完成恢复核对，再点击解决。",
                    }
                    remember_operation(
                        records, operation_id, fingerprint, expected_revision, result
                    )
                    return result
                raise PermissionError("恢复备份必须先通过系统页的只读恢复核对及显式确认")
            if automatic:
                try:
                    assert_quiescent(records)
                    if task and not task["user_excluded"] and not task["cancel_requested"]:
                        assert_task_budget(records, task)
                except PermissionError as error:
                    result = {"reason_id": reason_id, "resolved": False, "message": str(error)}
                    remember_operation(
                        records, operation_id, fingerprint, expected_revision, result
                    )
                    return result
            else:
                assert_quiescent(records)
            if automatic:
                details = json.loads(reason["details_json"])
                view = guidance(details.get("diagnosis", reason["code"]), details)
                explanation = "用户按建议处理后点击重新检查。系统已核对旧执行及远端操作状态。"
                retry_plan = {
                    "steps": view["suggestions"],
                    "success_condition": "原失败步骤完成并取得有效产物或验证证据",
                }
            retry_id = None
            if task and not task["user_excluded"] and not task["cancel_requested"]:
                assert_task_budget(records, task)
                if reason["code"] not in {
                    "budget_exhausted",
                    "budget_revoked",
                    "lease_expired",
                    "abandoned_run",
                }:
                    if automatic:
                        ledger = required(
                            records, "budget_ledgers", owner_task_id=task["budget_owner_task_id"]
                        )
                        available = budget_view(ledger)["available"]
                        retry_limits = {
                            "max_attempts": 1,
                            "max_steps": 32,
                            "max_active_ms": min(300000, available["active_ms"]),
                        }
                        if available["recovery_rounds"] < 1 or available["active_ms"] < 1:
                            result = {
                                "reason_id": reason_id,
                                "resolved": False,
                                "message": "剩余恢复次数或有效时长不足，请先在任务预算中追加额度。",
                            }
                            remember_operation(
                                records, operation_id, fingerprint, expected_revision, result
                            )
                            return result
                    if (
                        not retry_plan
                        or not retry_plan.get("steps")
                        or not all(
                            isinstance(step, str) and step.strip() for step in retry_plan["steps"]
                        )
                        or not retry_plan.get("success_condition")
                        or not retry_limits
                        or set(retry_limits) != {"max_attempts", "max_steps", "max_active_ms"}
                        or any(
                            type(value) is not int or not 0 < value <= 2**53 - 1
                            for value in retry_limits.values()
                        )
                    ):
                        raise ValueError("需要具体处理步骤、可观察目标及有限重试授权")
                    ledger = required(
                        records, "budget_ledgers", owner_task_id=task["budget_owner_task_id"]
                    )
                    available = budget_view(ledger)["available"]
                    if (
                        retry_limits["max_active_ms"] > available["active_ms"]
                        or retry_limits["max_attempts"] > available["recovery_rounds"]
                    ):
                        raise PermissionError("有界重试超出原账本剩余授权")
                    configuration = json.loads(task["configuration_json"])
                    configuration["recovery_strategy"] = {
                        "operation_id": operation_id,
                        "plan": retry_plan,
                        "limits": retry_limits,
                    }
                    bump_task(records, task, configuration_json=canonical_json(configuration))
                    retry_id = stable_id("retry", operation_id)
                    records.add(
                        "retry_authorizations",
                        {
                            "id": retry_id,
                            "task_id": task["id"],
                            "reason_id": reason_id,
                            "plan_json": canonical_json(retry_plan),
                            "limits_json": canonical_json(retry_limits),
                            "configuration_sha256": content_digest(configuration),
                        },
                    )
            records.change(
                "block_reasons",
                {"id": reason_id},
                {"resolved": 1, "revision": reason["revision"] + 1},
            )
            result = {
                "reason_id": reason_id,
                "resolved": True,
                "retry_authorization_id": retry_id,
                "global_paused": bool(required(records, "global_control", singleton=1)["paused"]),
            }
            if automatic:
                if records.find("block_reasons", resolved=0):
                    result["message"] = "此项已解决，请继续处理其余阻塞。"
                else:
                    try:
                        self._resume_records(records, now)
                        result.update(
                            global_paused=False,
                            message="阻塞已处理并恢复队列，系统将重新执行并验证。",
                        )
                        business_event(
                            records,
                            "control_resumed",
                            operation_id=operation_id,
                            actor=actor,
                            repository_id=None,
                        )
                    except PermissionError as error:
                        result["message"] = "此项已解决，队列仍暂停。" + str(error)
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(
                records,
                "reason_resolved",
                reason["task_id"],
                operation_id=operation_id,
                reason_id=reason_id,
                explanation=explanation,
                retry_authorization_id=retry_id,
                actor=actor,
            )
            return result

    def _resume_records(self, records, now, repository_id=None):
        candidates = [
            task
            for task in records.find("tasks")
            if (repository_id is None or task["repository_id"] == repository_id)
            and not task["user_excluded"]
            and not task["cancel_requested"]
            and (
                task["state"] == "blocked"
                or task["wait_reason"] in {"safe_stop", "resume_required"}
            )
            and not (
                task["repository_id"]
                and required(records, "repositories", id=task["repository_id"])["removed"]
            )
        ]
        for task in candidates:
            assert_task_budget(records, task)
        settle_ready(records, now)
        collection, identity = (
            ("repositories", {"id": repository_id})
            if repository_id
            else ("global_control", {"singleton": 1})
        )
        control = required(records, collection, **identity)
        records.change(collection, identity, {"paused": 0, "revision": control["revision"] + 1})
        for task in candidates:
            waiting = bool(records.find("scan_dependencies", waiting_task_id=task["id"]))
            bump_task(
                records,
                task,
                state="waiting" if waiting else "queued",
                wait_reason="scan" if waiting else None,
                stop_requested=0,
                budget_wait_reason=None,
            )
        refresh_ready(records, now)

    def resume(
        self, operation_id, *, expected_revision, now, repository_id=None, actor="local-user"
    ):
        human(actor)
        require_time(now)
        request = {
            "kind": "resume",
            "repository": repository_id,
            "revision": expected_revision,
            "actor": actor,
        }
        self._check_commands()
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            collection, identity = (
                ("repositories", {"id": repository_id})
                if repository_id
                else ("global_control", {"singleton": 1})
            )
            control = required(records, collection, **identity)
            if control["revision"] != expected_revision:
                raise ConflictError("控制状态已变化，请刷新")
            assert_quiescent(records)
            if records.find("block_reasons", resolved=0):
                raise PermissionError("仍有未解决的阻塞原因")
            self._resume_records(records, now, repository_id)
            result = {
                "paused": False,
                "revision": control["revision"] + 1,
                "repository_id": repository_id,
            }
            remember_operation(records, operation_id, fingerprint, expected_revision, result)
            business_event(
                records,
                "control_resumed",
                operation_id=operation_id,
                actor=actor,
                repository_id=repository_id,
            )
            return result
