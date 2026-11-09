import json

from repowatch.application.memory import RepositoryMemory
from repowatch.application.task_records import execution_record, required
from repowatch.domain.task_records import canonical_json, stable_id


class MemoryContextChanged(RuntimeError):
    pass


class MemoryContext:
    def __init__(self, services):
        self.services = services
        self.memory = RepositoryMemory(services.store, services.artifacts)

    def prepare(self, token, key, objective, mode):
        from repowatch.adapters.memory_index import MemoryIndex, canonical_result

        with self.services.store.read() as records:
            run = execution_record(records, token, self.services.clock())
            task = required(records, "tasks", id=run["task_id"])
            snapshot = json.loads(run["snapshot_json"])
            previous = [
                row
                for row in records.find("memory_injections", run_id=token.run_id, stage_key=key)
                if "memories" in json.loads(row["block_json"])
            ]
        items = []
        if mode not in {"review", "distill", "diagnose"} and snapshot.get("configuration", {}).get(
            "memory_enabled", True
        ):
            index = getattr(self.services, "memory_index", None) or MemoryIndex(
                self.services.settings, self.memory
            )
            query = (objective + "\n" + canonical_json(snapshot.get("input", {})))[:16000]
            items = index.search(task["repository_id"], query, max_chars=5000)["items"]
        if not items and not previous and ":memory-reset:" not in key:
            return None
        dependencies = [{"memory_id": item["id"], "revision": item["revision"]} for item in items]
        block = {
            "warning": "历史经验仅作参考，失败经验只作风险提示；当前任务仍须独立验证",
            "memories": [json.loads(canonical_result(item)) for item in items],
        }
        identifier = stable_id(
            "memory-injection",
            token.run_id,
            key,
            canonical_json(dependencies),
            canonical_json(block),
        )
        with self.services.store.write() as records:
            execution_record(records, token, self.services.clock())
            if records.one("memory_injections", id=identifier) is None:
                records.add(
                    "memory_injections",
                    {
                        "id": identifier,
                        "run_id": token.run_id,
                        "stage_key": key,
                        "dependencies_json": canonical_json(dependencies),
                        "block_json": canonical_json(block),
                        "created_at": self.services.clock(),
                    },
                )
        return {
            "id": identifier,
            "block": block,
            "dependencies": dependencies,
            "repository_id": task["repository_id"],
        }

    def guard(self, token, injection, request_key=None):
        if injection is None:
            return
        with self.services.store.write() as records:
            execution_record(records, token, self.services.clock())
            for reference in injection["dependencies"]:
                if not self.memory.eligible(
                    records,
                    reference["memory_id"],
                    injection["repository_id"],
                    reference["revision"],
                ):
                    raise MemoryContextChanged("记忆依赖失效，须舍弃旧会话与压缩缓存")
            if request_key:
                identifier = stable_id("memory-request", token.run_id, injection["id"], request_key)
                if records.one("memory_request_audits", id=identifier) is None:
                    records.add(
                        "memory_request_audits",
                        {
                            "id": identifier,
                            "run_id": token.run_id,
                            "injection_id": injection["id"],
                            "dependencies_json": canonical_json(injection["dependencies"]),
                            "created_at": self.services.clock(),
                        },
                    )

    def guard_children(self, token, child_ids, key, request_key=None):
        if not child_ids:
            return
        with self.services.store.read() as records:
            run = execution_record(records, token, self.services.clock())
            task = required(records, "tasks", id=run["task_id"])
            descendants = set(child_ids)
            runs = records.find("runs")
            while True:
                expanded = descendants | {
                    row["id"] for row in runs if row["parent_run_id"] in descendants
                }
                if expanded == descendants:
                    break
                descendants = expanded
            dependencies = {}
            for identifier in descendants:
                for row in records.find("memory_injections", run_id=identifier):
                    for reference in json.loads(row["dependencies_json"]):
                        dependencies[(reference["memory_id"], reference["revision"])] = reference
        if not dependencies:
            return
        references = [dependencies[identity] for identity in sorted(dependencies)]
        identifier = stable_id("inherited-memory", token.run_id, key, canonical_json(references))
        with self.services.store.write() as records:
            execution_record(records, token, self.services.clock())
            if records.one("memory_injections", id=identifier) is None:
                records.add(
                    "memory_injections",
                    {
                        "id": identifier,
                        "run_id": token.run_id,
                        "stage_key": key,
                        "dependencies_json": canonical_json(references),
                        "block_json": canonical_json({"inherited_from": sorted(child_ids)}),
                        "created_at": self.services.clock(),
                    },
                )
        self.guard(
            token,
            {"id": identifier, "dependencies": references, "repository_id": task["repository_id"]},
            request_key,
        )
