"""API 只读同步的持久调度与独立租约，不领取 Worker 执行位。"""

import json
import time
from threading import Event
from uuid import uuid4

from repowatch.adapters.windows_jobs import current_process, process_identity
from repowatch.application.budgets import human, remember_operation, replay_operation
from repowatch.application.github_ports import GitHubReadError
from repowatch.application.github_reads import GitHubReads
from repowatch.application.storage import ConflictError, StorageError
from repowatch.application.task_records import business_event, required
from repowatch.domain.synchronization import SynchronizationPolicy
from repowatch.domain.task_records import canonical_json, require_time
from repowatch.operations.locks import guarded


def process_stopped(identity):
    if identity.get("platform") != "windows" or identity.get("created") is None:
        return False
    try:
        return process_identity(identity["pid"]) != identity["created"]
    except OSError:
        return False


class IssueSynchronization:
    def __init__(self, store, adapter_factory, *, clock=time.time):
        self.store, self.adapter_factory, self.clock = store, adapter_factory, clock
        self.stopping = Event()

    def __call__(self, repository, run_id):
        reader = GitHubReads(self.store, self.adapter_factory(repository["credential_reference"]))
        reader.reconcile()
        with self.store.read() as records:
            before = {
                row["observation_id"]
                for row in records.find("github_object_heads", repository_id=repository["id"])
            }
        for page in range(1, 1001):
            if self.stopping.is_set():
                raise ValueError("服务停止，完整同步未完成")
            result = reader.read_page(
                f"sync:{run_id}:{page}", repository["id"], page=page, now=self.clock()
            )
            if result["state"] != "completed":
                raise ValueError("同步页面未完成")
            if not result["result"]["has_next"]:
                with self.store.read() as records:
                    after = {
                        row["observation_id"]
                        for row in records.find(
                            "github_object_heads", repository_id=repository["id"]
                        )
                    }
                return before != after
        raise ValueError("同步分页超过有界上限")


class GitHubSynchronization:
    def __init__(self, store, reader, *, clock=time.time, owner=None, stopped=process_stopped):
        self.store, self.reader, self.clock = store, reader, clock
        self.owner, self.stopped = owner or str(uuid4()), stopped

    def stop(self):
        stopping = getattr(self.reader, "stopping", None)
        if stopping is not None:
            stopping.set()

    def configure(self, operation_id, repository_id, data, *, expected_revision):
        policy = SynchronizationPolicy.model_validate(data).model_dump()
        ci_policy = {
            "configured": policy.pop("ci_configured"),
            "required": policy.pop("required_checks"),
        }
        request = {
            "kind": "sync_policy",
            "repository_id": repository_id,
            "revision": expected_revision,
            "policy": {**policy, "ci_policy": ci_policy},
        }
        now = self.clock()
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, request)
            if replay is not None:
                return replay
            row = self._state(records, repository_id)
            if row["revision"] != expected_revision:
                raise ConflictError("同步策略修订过期")
            revision = row["revision"] + 1
            previous_ci = json.loads(row["ci_policy_json"])
            previous_ci = {
                "configured": previous_ci.get("configured", False),
                "required": previous_ci.get("required", []),
            }
            records.change(
                "github_sync_state",
                {"repository_id": repository_id},
                {
                    **policy,
                    "ci_policy_json": canonical_json(ci_policy),
                    "ci_policy_revision": row["ci_policy_revision"] + (previous_ci != ci_policy),
                    "revision": revision,
                    "next_due_at": now,
                },
            )
            result = {"revision": revision}
            remember_operation(records, operation_id, fingerprint, revision, result)
            business_event(records, "github_sync_configured", repository_id=repository_id)
            return result

    @staticmethod
    def active(records, repository_id, row, now):
        return (
            (
                row["last_changed_at"] is not None
                and now - row["last_changed_at"] < row["lookback_seconds"]
            )
            or any(
                task["state"] not in {"ended", "cancelled"}
                for task in records.find("tasks", repository_id=repository_id)
            )
            or any(
                pull["state"] == "open"
                for pull in records.find("managed_pull_requests", issue_repository_id=repository_id)
            )
        )

    @staticmethod
    def _state(records, repository_id):
        required(records, "repositories", id=repository_id)
        if records.one("github_sync_state", repository_id=repository_id) is None:
            records.add("github_sync_state", {"repository_id": repository_id})
        return required(records, "github_sync_state", repository_id=repository_id)

    def request(self, operation_id, repository_id, *, actor="local-user"):
        human(actor)
        payload = {"kind": "github_sync", "repository_id": repository_id, "actor": actor}
        with self.store.write() as records:
            fingerprint, replay = replay_operation(records, operation_id, payload)
            if replay is not None:
                return replay
            repository = required(records, "repositories", id=repository_id)
            if repository["identity_json"] is None:
                raise PermissionError("仓库尚未完成正式导入")
            row = self._state(records, repository_id)
            sequence = row["requested_sequence"] + 1
            records.change(
                "github_sync_state",
                {"repository_id": repository_id},
                {"requested_sequence": sequence},
            )
            result = {"repository_id": repository_id, "requested_sequence": sequence}
            remember_operation(records, operation_id, fingerprint, 1, result)
            return result

    def status(self, repository_id):
        with self.store.read() as records:
            required(records, "repositories", id=repository_id)
            row = records.one("github_sync_state", repository_id=repository_id)
            return dict(row) if row else None

    def _claim(self, now):
        with self.store.write() as records:
            lease = records.one("github_sync_lease", singleton=1)
            if lease:
                if lease["expires_at"] > now or not self.stopped(json.loads(lease["process_json"])):
                    return None
                for run in records.find("github_sync_runs", state="running"):
                    records.change(
                        "github_sync_runs",
                        {"id": run["id"]},
                        {"state": "failed", "finished_at": now, "error_code": "interrupted_sync"},
                    )
                records.remove("github_sync_lease", singleton=1)
            repositories = {
                row["id"]: dict(row)
                for row in records.find("repositories")
                if row["identity_json"] is not None and not row["removed"]
            }
            candidates = []
            for identifier in repositories:
                row = self._state(records, identifier)
                if row["last_success_at"] is not None:
                    interval = row[
                        "active_seconds"
                        if self.active(records, identifier, row, now)
                        else "idle_seconds"
                    ]
                    due = min(row["next_due_at"], row["last_success_at"] + interval)
                else:
                    due = row["next_due_at"]
                pending = row["requested_sequence"] > row["completed_sequence"]
                if row["retry_at"] <= now and (pending or due <= now):
                    candidates.append((due, identifier, row["requested_sequence"]))
            if not candidates:
                return None
            _, identifier, sequence = min(candidates)
            run_id = str(uuid4())
            records.add(
                "github_sync_lease",
                {
                    "singleton": 1,
                    "owner": self.owner,
                    "process_json": canonical_json(current_process()),
                    "expires_at": now + 120,
                },
            )
            records.add(
                "github_sync_runs",
                {
                    "id": run_id,
                    "repository_id": identifier,
                    "owner": self.owner,
                    "requested_sequence": sequence,
                    "started_at": now,
                    "state": "running",
                    "initial_change_sequence": required(
                        records, "github_sync_state", repository_id=identifier
                    )["change_sequence"],
                },
            )
            return repositories[identifier], run_id, sequence

    @guarded
    def tick(self):
        now = self.clock()
        require_time(now)
        claimed = self._claim(now)
        if claimed is None:
            return False
        repository, run_id, sequence = claimed
        try:
            changed = bool(self.reader(repository, run_id))
        except StorageError:
            raise
        except GitHubReadError as error:
            self._finish(
                repository["id"], run_id, sequence, error=error.code, retry_after=error.retry_after
            )
        except Exception:
            self._finish(repository["id"], run_id, sequence, error="sync_read_failed")
        else:
            self._finish(repository["id"], run_id, sequence, changed=changed)
        return True

    def _finish(self, repository_id, run_id, sequence, *, error=None, changed=False, retry_after=0):
        now = self.clock()
        require_time(now)
        with self.store.write() as records:
            lease = required(records, "github_sync_lease", singleton=1)
            if lease["owner"] != self.owner:
                raise PermissionError("旧同步器不能确认新租约")
            run = required(records, "github_sync_runs", id=run_id)
            if (
                run["owner"] != self.owner
                or run["state"] != "running"
                or run["repository_id"] != repository_id
                or run["requested_sequence"] != sequence
            ):
                raise PermissionError("同步运行已结束或不属于当前请求")
            row = required(records, "github_sync_state", repository_id=repository_id)
            if error:
                if error == "source_order_uncertain":
                    for cached in records.find(
                        "github_endpoint_cache", repository_id=repository_id
                    ):
                        records.change(
                            "github_endpoint_cache",
                            {
                                "repository_id": repository_id,
                                "query_key": cached["query_key"],
                                "page": cached["page"],
                            },
                            {"etag": None},
                        )
                failures = row["failures"] + 1
                values = {
                    "failures": failures,
                    "error_code": error,
                    "retry_at": now + max(retry_after, min(86400, 60 * 2 ** min(failures - 1, 11))),
                    "last_result": "failed",
                }
            else:
                last_changed = now if changed else row["last_changed_at"]
                active = last_changed is not None and now - last_changed < row["lookback_seconds"]
                active = (
                    active
                    or any(
                        task["state"] not in {"ended", "cancelled"}
                        for task in records.find("tasks", repository_id=repository_id)
                    )
                    or any(
                        pull["state"] == "open"
                        for pull in records.find(
                            "managed_pull_requests", issue_repository_id=repository_id
                        )
                    )
                )
                values = {
                    "last_success_at": now,
                    "last_changed_at": last_changed,
                    "next_due_at": now + row["active_seconds" if active else "idle_seconds"],
                    "completed_sequence": sequence,
                    "retry_at": 0,
                    "failures": 0,
                    "error_code": None,
                    "last_result": "changed"
                    if changed or row["change_sequence"] > run["initial_change_sequence"]
                    else "unchanged",
                }
            records.change("github_sync_state", {"repository_id": repository_id}, values)
            records.change(
                "github_sync_runs",
                {"id": run_id},
                {
                    "state": "failed" if error else "completed",
                    "finished_at": now,
                    "error_code": error,
                },
            )
            records.remove("github_sync_lease", singleton=1)
            business_event(
                records,
                "github_sync_finished",
                repository_id=repository_id,
                run_id=run_id,
                error_code=error,
            )
