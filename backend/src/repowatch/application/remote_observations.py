"""远端对象、稳定任务和反馈信号的单事务保存。"""

import json
from datetime import datetime

from repowatch.application.github_ports import GitHubReadError
from repowatch.application.task_records import TaskRecords, required
from repowatch.domain.task_records import canonical_json, content_digest, stable_id


def ensure_order(previous, version, digest):
    if previous is None:
        return
    old = previous["remote_version"]
    if old and not version:
        raise GitHubReadError("source_order_uncertain")
    if old and version:
        earlier = datetime.fromisoformat(old.replace("Z", "+00:00"))
        later = datetime.fromisoformat(version.replace("Z", "+00:00"))
        if later < earlier:
            raise GitHubReadError("source_order_uncertain")
    if previous["payload_sha256"] == digest:
        return
    if not old or not version:
        raise GitHubReadError("source_order_uncertain")
    earlier = datetime.fromisoformat(old.replace("Z", "+00:00"))
    later = datetime.fromisoformat(version.replace("Z", "+00:00"))
    if later <= earlier:
        raise GitHubReadError("source_order_uncertain")


def mark_sync_change(records, repository_id, now, *, activity=True):
    row = required(records, "github_sync_state", repository_id=repository_id)
    records.change(
        "github_sync_state",
        {"repository_id": repository_id},
        {
            "last_changed_at": now if activity else row["last_changed_at"],
            "change_sequence": row["change_sequence"] + 1,
        },
    )


def save_comments(records, repository_id, values, now):
    changed = False
    for item in values:
        if type(item.get("id")) is not int or item["id"] <= 0:
            raise GitHubReadError("invalid_comment_identity")
        identity = {"repository_id": repository_id, "kind": "comment", "object_id": str(item["id"])}
        digest = content_digest(item)
        previous = records.one("github_remote_objects", **identity)
        ensure_order(previous, item.get("updated_at"), digest)
        updated = {
            "payload_json": canonical_json(item),
            "payload_sha256": digest,
            "remote_version": item.get("updated_at"),
            "observed_at": now,
        }
        if previous:
            records.change("github_remote_objects", identity, updated)
        else:
            records.add("github_remote_objects", {**identity, **updated})
        changed = changed or previous is None or previous["payload_sha256"] != digest
    return changed


def save_issue(store, records, repository_id, item, now):
    from repowatch.application.github_reads import issue_input

    kind = "pull_request" if "pull_request" in item else "issue"
    object_id, digest = str(item["id"]), content_digest(item)
    identifier = stable_id("github-observation", repository_id, kind, object_id, digest)
    identity = {"repository_id": repository_id, "kind": kind, "object_id": object_id}
    head = records.one("github_object_heads", **identity)
    previous = required(records, "github_observations", id=head["observation_id"]) if head else None
    meaning = issue_input(item)
    previous_meaning = issue_input(json.loads(previous["payload_json"])) if previous else None
    projected_previous = (
        {**previous, "payload_sha256": content_digest(previous_meaning)} if previous else None
    )
    ensure_order(projected_previous, item.get("updated_at"), content_digest(meaning))
    signal_id = None
    if kind == "issue":
        tasks = TaskRecords(store)
        task_id = tasks.ensure_task(
            repository_id, "github:issue:" + object_id, issue_id=object_id, now=now, records=records
        )
        task = required(records, "tasks", id=task_id)
        payload = issue_input(item)
        if json.loads(task["input_json"]) != payload:
            signal_id = tasks.observe(
                task_id,
                "github:issue:" + object_id,
                content_digest({"input": payload, "version": item.get("updated_at")}),
                payload,
                now=now,
                records=records,
            )
    if records.one("github_observations", id=identifier) is None:
        records.add(
            "github_observations",
            {
                "id": identifier,
                **identity,
                "payload_sha256": digest,
                "payload_json": canonical_json(item),
                "remote_version": item.get("updated_at"),
            },
        )
        records.add(
            "memory_source_facts",
            {
                "observation_id": identifier,
                "repository_id": repository_id,
                "signal_id": signal_id,
                "visibility": "repository",
            },
        )
    if head:
        records.change("github_object_heads", identity, {"observation_id": identifier})
    else:
        records.add("github_object_heads", {**identity, "observation_id": identifier})
    return identifier, meaning != previous_meaning


def external_comments(records, repository, task, values):
    claim = records.one("publication_intents", task_id=task["id"], kind="claim")
    receipt = json.loads(claim["result_json"] or "{}") if claim else {}
    request = json.loads(claim["request_json"]) if claim else {}
    login = json.loads(repository["identity_json"] or "{}").get("execution_login", "").casefold()
    return [
        item
        for item in values
        if not (
            claim
            and claim["state"] == "confirmed"
            and receipt.get("id") is not None
            and str(item.get("id")) == str(receipt["id"])
            and login
            and (item.get("user") or {}).get("login", "").casefold() == login
            and item.get("body") == request.get("body")
            and item.get("updated_at") == item.get("created_at")
        )
    ]
