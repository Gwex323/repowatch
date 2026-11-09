"""按端点及配置隔离验证器，完整查询成功后推进水位。"""

import json
from datetime import UTC, datetime
from threading import Event

from repowatch.application.github_ports import GitHubReadError
from repowatch.application.issue_context import relation_fingerprint
from repowatch.application.remote_observations import (
    ensure_order,
    external_comments,
    mark_sync_change,
    save_comments,
    save_issue,
)
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords, business_event, required
from repowatch.domain.task_records import canonical_json, content_digest


class GitHubPolling:
    def __init__(self, services):
        self.services, self.store, self.clock = services, services.store, services.clock
        self.stopping = Event()

    def current(self, records, repository, run_id):
        run = required(records, "github_sync_runs", id=run_id)
        lease = required(records, "github_sync_lease", singleton=1)
        current = required(records, "repositories", id=repository["id"])
        if (
            run["state"] != "running"
            or run["owner"] != lease["owner"]
            or run["repository_id"] != repository["id"]
            or any(
                current[key] != repository[key]
                for key in (
                    "target_revision",
                    "import_epoch",
                    "credential_reference",
                    "target_branch",
                )
            )
        ):
            raise ConflictError("旧同步或目标配置已失效，须重新读取")

    def query(
        self, repository, run_id, endpoint, *, number=None, ref=None, incremental=False, force=False
    ):
        base = content_digest(
            {
                "endpoint": endpoint,
                "number": number,
                "ref": ref,
                "target_revision": repository["target_revision"],
                "import_epoch": repository["import_epoch"],
                "credential": repository["credential_reference"],
            }
        )
        with self.store.read() as records:
            self.current(records, repository, run_id)
            progress = records.one(
                "github_query_progress", repository_id=repository["id"], query_key=base
            )
            policy = required(records, "github_sync_state", repository_id=repository["id"])
            started = self.clock()
            full = (
                not incremental
                or not progress
                or progress["full_refresh_at"] is None
                or (started - progress["full_refresh_at"] >= policy["full_refresh_seconds"])
            )
            since = (
                None
                if full
                else datetime.fromtimestamp(
                    max(0, (progress["watermark_at"] or 0) - policy["overlap_seconds"]), UTC
                )
                .isoformat()
                .replace("+00:00", "Z")
            )
        params = {"since": since} if since else {}
        query_key = content_digest({"base": base, "params": params})
        adapter = self.services.repositories.adapter_factory(repository["credential_reference"])
        values = []
        for page in range(1, 1001):
            if self.stopping.is_set():
                raise GitHubReadError("sync_stopping")
            identity = {"repository_id": repository["id"], "query_key": query_key, "page": page}
            with self.store.read() as records:
                cache = records.one("github_endpoint_cache", **identity)
                cache = dict(cache) if cache else None
                usable_cache = bool(cache and cache["etag"] and not force)
                if endpoint == "branch" and cache:
                    cached = json.loads(cache["payload_json"])
                    current = required(records, "repositories", id=repository["id"])
                    if (
                        not cached
                        or cached[0].get("commit", {}).get("sha") != current["target_sha"]
                    ):
                        usable_cache = False
            kwargs = {"number": number, "ref": ref, "page": page, "params": params}
            result = adapter.synchronization_page(
                repository["github_owner"],
                repository["github_name"],
                endpoint,
                **kwargs,
                etag=cache["etag"] if usable_cache else None,
            )
            if result.status == 304 and not usable_cache:
                result = adapter.synchronization_page(
                    repository["github_owner"], repository["github_name"], endpoint, **kwargs
                )
                if result.status == 304:
                    raise GitHubReadError("uncached_304")
            items = (
                json.loads(cache["payload_json"]) if result.status == 304 else list(result.items)
            )
            has_next = bool(cache["has_next"]) if result.status == 304 else result.has_next
            with self.store.write() as records:
                self.current(records, repository, run_id)
                if endpoint == "issues":
                    changed = False
                    for item in items:
                        _, updated = save_issue(
                            self.store, records, repository["id"], item, self.clock()
                        )
                        changed = changed or updated
                    if changed:
                        mark_sync_change(records, repository["id"], self.clock())
                payload = {
                    "payload_json": canonical_json(items),
                    "has_next": int(has_next),
                    "etag": cache["etag"] if result.status == 304 else result.etag,
                    "observed_at": self.clock(),
                }
                if cache:
                    records.change("github_endpoint_cache", identity, payload)
                else:
                    records.add("github_endpoint_cache", {**identity, **payload})
            values.extend(items)
            if not has_next:
                with self.store.write() as records:
                    self.current(records, repository, run_id)
                    watermark = {
                        "success_at": started,
                        "full_refresh_at": started if full else progress["full_refresh_at"],
                        "watermark_at": max(
                            ([progress["watermark_at"] or 0] if progress else [0])
                            + [
                                datetime.fromisoformat(
                                    item["updated_at"].replace("Z", "+00:00")
                                ).timestamp()
                                for item in values
                                if item.get("updated_at")
                            ]
                        ),
                    }
                    progress_id = {"repository_id": repository["id"], "query_key": base}
                    if progress:
                        records.change("github_query_progress", progress_id, watermark)
                    else:
                        records.add("github_query_progress", {**progress_id, **watermark})
                return values
        raise GitHubReadError("collection_incomplete")

    def collection_feedback(self, repository, run_id, task, kind, values):
        if kind == "comments":
            with self.store.read() as records:
                values = external_comments(records, repository, task, values)
            values = relation_fingerprint("comments", values)
        payload = {kind: values}
        identity = {"repository_id": repository["id"], "kind": kind, "object_id": task["issue_id"]}
        digest = content_digest(payload)
        with self.store.write() as records:
            self.current(records, repository, run_id)
            previous = records.one("github_remote_objects", **identity)
            if kind == "comments":
                save_comments(records, repository["id"], values, self.clock())
                prior_values = (
                    json.loads(previous["payload_json"]).get(kind, []) if previous else []
                )
                removed = {str(item["id"]) for item in prior_values} - {
                    str(item["id"]) for item in values
                }
                for identifier in removed:
                    records.change(
                        "github_remote_objects",
                        {
                            "repository_id": repository["id"],
                            "kind": "comment",
                            "object_id": identifier,
                        },
                        {
                            "payload_json": "null",
                            "payload_sha256": content_digest(None),
                            "observed_at": self.clock(),
                        },
                    )
            if previous and previous["payload_sha256"] == digest:
                return False
            if values or previous:
                TaskRecords(self.store).observe(
                    task["id"],
                    "github:" + kind + ":" + task["issue_id"],
                    content_digest(
                        {
                            "payload": payload,
                            "previous": previous["payload_sha256"] if previous else None,
                            "revision": previous["revision"] + 1 if previous else 1,
                        }
                    ),
                    payload,
                    kind="feedback",
                    now=self.clock(),
                    records=records,
                )
                mark_sync_change(records, repository["id"], self.clock())
            updated = {
                "payload_json": canonical_json(payload),
                "payload_sha256": digest,
                "observed_at": self.clock(),
                "remote_version": None,
                "revision": previous["revision"] + 1 if previous else 1,
            }
            if previous:
                records.change("github_remote_objects", identity, updated)
            else:
                records.add("github_remote_objects", {**identity, **updated})
            business_event(records, "github_feedback_synchronized", task["id"], endpoint_kind=kind)
        return bool(values or previous)

    def __call__(self, repository, run_id):
        self.query(repository, run_id, "issues", incremental=True)
        with self.store.read() as records:
            tasks = [
                dict(task)
                for task in records.find("tasks", repository_id=repository["id"])
                if task["issue_id"] and json.loads(task["input_json"]).get("number")
            ]
        for task in tasks:
            number = json.loads(task["input_json"])["number"]
            with self.store.read() as records:
                old_comments = records.one(
                    "github_remote_objects",
                    repository_id=repository["id"],
                    kind="comments",
                    object_id=task["issue_id"],
                )
                old_values = (
                    json.loads(old_comments["payload_json"]).get("comments", [])
                    if old_comments
                    else []
                )
            comments = self.query(repository, run_id, "comments", number=number)
            if {item["id"] for item in old_values} - {item["id"] for item in comments}:
                comments = self.query(repository, run_id, "comments", number=number, force=True)
            self.collection_feedback(repository, run_id, task, "comments", comments)
        self.target(repository, run_id)
        self.pulls(repository, run_id)
        return False

    def pulls(self, repository, run_id):
        with self.store.read() as records:
            numbers = {
                json.loads(row["payload_json"])["number"]
                for row in records.find(
                    "github_observations", repository_id=repository["id"], kind="pull_request"
                )
            }
            bindings = records.find("managed_pull_requests", issue_repository_id=repository["id"])
            for binding in bindings:
                intent = records.one(
                    "publication_intents", task_id=binding["task_id"], kind="pull_request"
                )
                if intent and intent["state"] == "confirmed":
                    numbers.add(json.loads(intent["result_json"])["number"])
        for number in sorted(numbers):
            pull = self.query(repository, run_id, "pull", number=number)[0]
            if pull.get("merge_commit_sha"):
                try:
                    commit = self.query(repository, run_id, "commit", ref=pull["merge_commit_sha"])[
                        0
                    ]
                    pull["merge_parents"] = [parent["sha"] for parent in commit["parents"]]
                except GitHubReadError as error:
                    if error.code != "not_found":
                        raise
                    pull["merge_parents"] = []
            identity = {
                "repository_id": repository["id"],
                "kind": "pull",
                "object_id": str(pull["id"]),
            }
            digest = content_digest(relation_fingerprint("pull_requests", [pull]))
            with self.store.write() as records:
                self.current(records, repository, run_id)
                old = records.one("github_remote_objects", **identity)
                ensure_order(old, pull.get("updated_at"), digest)
                values = {
                    "payload_json": canonical_json(pull),
                    "payload_sha256": digest,
                    "remote_version": pull.get("updated_at"),
                    "observed_at": self.clock(),
                }
                if old:
                    records.change("github_remote_objects", identity, values)
                else:
                    records.add("github_remote_objects", {**identity, **values})
                if not old or old["payload_sha256"] != digest:
                    mark_sync_change(records, repository["id"], self.clock())
                binding = records.one(
                    "managed_pull_requests",
                    issue_repository_id=repository["id"],
                    pr_id=str(pull["id"]),
                )
                task = dict(required(records, "tasks", id=binding["task_id"])) if binding else None
            if task is None:
                continue
            reviews = self.query(repository, run_id, "reviews", number=number)
            comments = self.query(repository, run_id, "review_comments", number=number)
            checks, statuses = [], []
            refs = {pull["head"]["sha"], pull["base"]["sha"]}
            with self.store.read() as records:
                refs.add(required(records, "repositories", id=repository["id"])["target_sha"])
            if pull.get("merge_commit_sha"):
                refs.add(pull["merge_commit_sha"])
            for ref in sorted(refs):
                checks.extend(self.query(repository, run_id, "checks", ref=ref))
                statuses.extend(
                    {**item, "sha": ref}
                    for item in self.query(repository, run_id, "statuses", ref=ref)
                )
            confirmed = self.query(repository, run_id, "pull", number=number)[0]
            if relation_fingerprint("pull_requests", [pull]) != relation_fingerprint(
                "pull_requests", [confirmed]
            ) or pull.get("merge_commit_sha") != confirmed.get("merge_commit_sha"):
                raise GitHubReadError("pull_changed_during_read")
            with self.store.write() as records:
                self.current(records, repository, run_id)
                self.services.lifecycle.observe(
                    records,
                    repository,
                    task,
                    pull,
                    reviews,
                    comments,
                    checks,
                    statuses,
                    self.clock(),
                )

    def target(self, repository, run_id):
        with self.store.read() as records:
            expected_revision = required(records, "repositories", id=repository["id"])["revision"]
        branch = self.query(repository, run_id, "branch", ref=repository["target_branch"])[0]
        sha = branch["commit"]["sha"]
        rules = self.query(repository, run_id, "branch_rules", ref=repository["target_branch"])
        legacy = None
        if branch.get("protected"):
            try:
                legacy = self.query(
                    repository, run_id, "branch_protection", ref=repository["target_branch"]
                )[0]
            except GitHubReadError as error:
                if error.code != "not_found":
                    raise
        rule_payload = {"rules": rules, "legacy": legacy}
        rule_identity = {
            "repository_id": repository["id"],
            "kind": "branch_rules",
            "object_id": str(repository["target_revision"]),
        }
        with self.store.write() as records:
            self.current(records, repository, run_id)
            old_rules = records.one("github_remote_objects", **rule_identity)
            values = {
                "payload_json": canonical_json(rule_payload),
                "payload_sha256": content_digest(rule_payload),
                "remote_version": None,
                "observed_at": self.clock(),
            }
            if old_rules:
                records.change("github_remote_objects", rule_identity, values)
            else:
                records.add("github_remote_objects", {**rule_identity, **values})
        with self.store.read() as records:
            self.current(records, repository, run_id)
            current = dict(required(records, "repositories", id=repository["id"]))
            if current["revision"] != expected_revision:
                raise GitHubReadError("target_observation_changed")
            owners = records.find("repository_imports", repository_id=repository["id"])
            current_import = next(
                row for row in owners if row["import_epoch"] == repository["import_epoch"]
            )
            owner = required(records, "tasks", id=current_import["task_id"])
            configuration = json.loads(owner["configuration_json"])
        if sha != current["target_sha"]:
            self.services.repositories.record_target_drift(
                repository["id"],
                sha,
                now=self.clock(),
                owner=owner["budget_owner_task_id"],
                expected_revision=current["revision"],
                configuration=configuration,
            )
            with self.store.write() as records:
                self.current(records, repository, run_id)
                mark_sync_change(records, repository["id"], self.clock(), activity=False)
