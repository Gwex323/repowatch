"""Controlled P4 continuation of the two existing P3 sandbox publications.

Only the model transport is a deterministic offline fixture. The persisted
tasks/budgets, GitHub reviews, Git/Docker repair and operator terminal actions
are real. Each mutating phase is explicit; never run against a user data dir.
"""

import argparse
import importlib.util
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "p3_acceptance", ROOT / "experiments/p3-github-acceptance.py"
)
p3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p3)

PATHS = {
    "direct": {
        "number": 16,
        "task": "f3ad2679-3c15-5c81-bb3e-409dffda5751",
        "reference": "github-gwex323",
        "subject": "mul",
        "reviewer": "fork",
    },
    "fork": {
        "number": 18,
        "task": "49b70683-9eb7-5b56-9a27-882590ae8c88",
        "reference": "github-gwx541",
        "subject": "sub",
        "reviewer": "direct",
    },
}
PREFIX = "/repos/Gwex323/repowatch-p3-sandbox"


def dump(value):
    print(json.dumps(value, ensure_ascii=False, default=str), flush=True)


def fixture(subject):
    cases = (
        [(2, 3, 6), (2, 0, 0), (-2, 3, -6)]
        if subject == "mul"
        else [(5, 2, 3), (2, 5, -3), (0, 0, 0)]
    )
    classname = subject.title() + "Regression"
    filename = "tests/test_" + subject + "_regression.py"
    content = (
        "import unittest\n\nfrom " + subject + " import " + subject + "\n\n\n"
        "class " + classname + "(unittest.TestCase):\n"
        "    def test_existing_issue_contract(self):\n"
        "        for left, right, expected in " + repr(cases) + ":\n"
        "            with self.subTest(left=left, right=right):\n"
        "                self.assertEqual(" + subject + "(left, right), expected)\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n"
    )
    probe = (
        "from pathlib import Path\nimport runpy\nimport unittest\n"
        "path = Path(" + repr(filename) + ")\n"
        "if not path.is_file():\n    print('TARGET_BUG: missing permanent regression tests')\n    raise SystemExit(1)\n"
        "namespace = runpy.run_path(str(path))\n"
        "suite = unittest.defaultTestLoader.loadTestsFromTestCase(namespace["
        + repr(classname)
        + "])\n"
        "result = unittest.TextTestRunner(verbosity=2).run(suite)\n"
        "if not result.wasSuccessful():\n    print('TARGET_BUG: regression failed')\n    raise SystemExit(1)\n"
        "print('TARGET_OK')\n"
    )
    return filename, content, probe


original_triage, original_wire = p3.triage, p3.wire


def configure_fixture(subject):
    filename, content, probe = fixture(subject)
    p3.ACTIVE = {**p3.DEFECTS[subject], "probe": probe}
    # Original triage identifies subjects by fixture object identity.
    p3.DEFECTS[subject] = p3.ACTIVE

    def triage(envelope, evidence):
        result = original_triage(envelope, evidence)
        lifecycle = evidence.get("pr_lifecycle")
        if lifecycle:
            current = evidence.get("acceptance")
            if current:
                result.update(current["content"])
            managed = (evidence.get("remote_context") or {}).get("managed_pr_id")
            pulls = (
                (evidence.get("remote_context") or {})
                .get("facts", {})
                .get("pull_requests", [])
            )
            pull = next((p for p in pulls if str(p["id"]) == str(managed)), {})
            permanent = any(
                item.get("filename") == filename for item in pull.get("files", [])
            )
            result["review_scope"] = "handled" if permanent else "in_scope"
            result["review_evidence"] = [
                "Current managed PR review requests permanent regressions for the unchanged arithmetic Issue contract; "
                + (
                    "the current PR already contains "
                    if permanent
                    else "the current PR is missing "
                )
                + filename
            ]
        return result

    def wire(request):
        body = json.loads(request.content)
        envelope = p3.envelope_from(body["messages"])
        if envelope and envelope.get("mode") == "implement":
            count = sum(item.get("role") == "tool" for item in body["messages"])
            if count == 0:
                return p3.reply(body, "read_source", {"path": p3.ACTIVE["path"]})
            if count == 1:
                return p3.reply(
                    body, "write_source", {"path": filename, "content": content}
                )
            return p3.reply(body, *p3.structured(envelope))
        return original_wire(request)

    p3.triage, p3.wire = triage, wire


class LiveAcceptance(p3.Acceptance):
    def __init__(self, *args):
        super().__init__(*args)
        # P3's one-session harness used monotonic timestamps. P4 spans sessions:
        # use the product's persistent wall clock and refresh expired role checks.
        self.services.close()
        self.services = p3.HostServices(self.settings, clock=time.time)
        self.services.adapter = p3.ProviderAdapter(
            self.services.providers, transport=httpx.MockTransport(p3.wire)
        )
        self.worker = p3.Worker(self.services)

    def now(self):
        return time.time()


def open_run(key):
    item = PATHS[key]
    return LiveAcceptance(
        str((ROOT / ".repowatch" / ("p3-" + key)).resolve()),
        "Gwex323",
        "repowatch-p3-sandbox",
        item["reference"],
        key == "fork",
    )


def github(run, method, path, payload=None):
    secret = run.services.vault.read(run.reference).reveal()
    with httpx.Client(
        timeout=60,
        headers={
            "Authorization": "Bearer " + secret,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    ) as client:
        response = client.request(method, "https://api.github.com" + path, json=payload)
        if response.status_code >= 400:
            # Never print headers, token or raw request objects.
            raise RuntimeError(
                f"GitHub {method} {path}: HTTP {response.status_code}: {response.json().get('message')}"
            )
        return response.json()


def status(run, item):
    with run.services.store.read() as records:
        task = dict(records.one("tasks", id=item["task"]))
        owner = dict(records.one("tasks", id=task["budget_owner_task_id"]))
        ledger = dict(records.one("budget_ledgers", owner_task_id=owner["id"]))
        repository = dict(records.one("repositories", id=task["repository_id"]))
        sessions = [
            dict(row) for row in records.find("repair_sessions", task_id=task["id"])
        ]
        publications = [
            dict(row) for row in records.find("publication_intents", task_id=task["id"])
        ]
        blockers = [dict(row) for row in records.find("block_reasons", resolved=0)]
        tasks = [
            {
                k: row[k]
                for k in (
                    "id",
                    "mode",
                    "state",
                    "wait_reason",
                    "outcome",
                    "budget_owner_task_id",
                )
            }
            for row in records.find("tasks")
        ]
    return {
        "task": {
            key: task[key]
            for key in (
                "id",
                "mode",
                "state",
                "wait_reason",
                "outcome",
                "revision",
                "acceptance_revision",
                "feedback_revision",
                "budget_owner_task_id",
            )
        },
        "repository": {
            key: repository[key]
            for key in ("id", "target_sha", "target_revision", "revision")
        },
        "ledger": ledger,
        "lifecycle": run.services.lifecycle.view(task["id"]),
        "sync": run.services.synchronization.status(repository["id"]),
        "repair_sessions": sessions,
        "publication_intents": publications,
        "blockers": blockers,
        "tasks": tasks,
    }


def save(run, item, phase):
    result = status(run, item)
    folder = ROOT / ".cache/p4-github"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (str(item["number"]) + "-" + phase + ".json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    dump(
        {
            "report": str(target),
            "task": result["task"],
            "sync_error": (result["sync"] or {}).get("error_code"),
            "blockers": [
                {key: b[key] for key in ("code", "task_id")} for b in result["blockers"]
            ],
        }
    )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "phase",
        choices=(
            "migrate",
            "review",
            "sync",
            "configure-ci",
            "repair",
            "pause",
            "status",
            "merge",
            "close",
        ),
    )
    parser.add_argument("path", choices=PATHS)
    args = parser.parse_args()
    item = PATHS[args.path]
    if args.phase == "migrate":
        from repowatch.configuration import StorageSettings
        from repowatch.operations.migrate import migrate

        folder = ROOT / ".repowatch" / ("p3-" + args.path)
        backup = ROOT / ".cache/p4-github" / ("backup-" + args.path)
        backup.mkdir(parents=True, exist_ok=True)
        for name in ("business.sqlite3", "checkpoints.sqlite3"):
            destination = backup / name
            if destination.exists():
                raise RuntimeError(
                    "Preserve the pre-migration backup; migrate was already run"
                )
            with (
                sqlite3.connect(folder / name) as source,
                sqlite3.connect(destination) as target,
            ):
                source.backup(target)
        migrate(StorageSettings(folder))
        dump({"migrated": args.path})
        return
    configure_fixture(item["subject"])
    run = open_run(args.path)
    try:
        if args.phase == "review":
            reviewer = open_run(item["reviewer"])
            try:
                pull = github(reviewer, "GET", PREFIX + "/pulls/" + str(item["number"]))
                assert pull["state"] == "open" and not pull["merged"]
                marker = "P4 controlled regression-review acceptance"
                reviews = github(
                    reviewer,
                    "GET",
                    PREFIX + "/pulls/" + str(item["number"]) + "/reviews",
                )
                if any(marker in (r.get("body") or "") for r in reviews):
                    raise RuntimeError(
                        "The controlled review already exists; use sync/repair"
                    )
                result = github(
                    reviewer,
                    "POST",
                    PREFIX + "/pulls/" + str(item["number"]) + "/reviews",
                    {
                        "commit_id": pull["head"]["sha"],
                        "event": "REQUEST_CHANGES",
                        "body": marker
                        + ". Please add a permanent unittest regression in "
                        + fixture(item["subject"])[0]
                        + " covering the existing Issue arithmetic acceptance cases plus a zero/negative edge case. Preserve the arithmetic fix, other contributors' changes and the original Issue scope. Run the regression and update this same PR. This is a controlled product lifecycle test with an offline deterministic model fixture.",
                    },
                )
                dump(
                    {
                        "review_id": result["id"],
                        "state": result["state"],
                        "commit_id": result["commit_id"],
                        "author": result["user"]["login"],
                    }
                )
            finally:
                reviewer.services.close()
        elif args.phase == "configure-ci":
            repo_id = status(run, item)["repository"]["id"]
            policy = run.services.synchronization.status(repo_id)
            from repowatch.domain.synchronization import SynchronizationPolicy

            run.services.synchronization.configure(
                "p4-no-required-ci-" + args.path,
                repo_id,
                SynchronizationPolicy.model_validate(
                    {
                        "active_seconds": policy["active_seconds"],
                        "idle_seconds": policy["idle_seconds"],
                        "lookback_seconds": policy["lookback_seconds"],
                        "overlap_seconds": policy["overlap_seconds"],
                        "full_refresh_seconds": policy["full_refresh_seconds"],
                        "ci_configured": True,
                        "required_checks": [],
                    }
                ).model_dump(),
                expected_revision=policy["revision"],
            )
            dump({"ci_configured": True, "required_checks": []})
        elif args.phase == "sync":
            reader = run.services.synchronization.reader

            def diagnostic_reader(repository, run_id):
                try:
                    return reader(repository, run_id)
                except Exception:
                    import traceback

                    traceback.print_exc()
                    raise

            run.services.synchronization.reader = diagnostic_reader
            before = status(run, item)
            repo = before["repository"]["id"]
            run.services.synchronization.request("p4-live-" + uuid4().hex, repo)
            assert run.services.synchronization.tick()
            save(run, item, "sync")
        elif args.phase == "repair":
            # Synchronization discovers all sandbox Issues. Keep execution
            # confined to the two explicitly authorized existing publications.
            with run.services.store.read() as records:
                unrelated = [
                    dict(task)
                    for task in records.find("tasks")
                    if task["issue_id"]
                    and task["id"] != item["task"]
                    and task["state"] not in {"ended", "cancelled"}
                ]
            for task in unrelated:
                run.services.tasks.operate(
                    "p4-local-isolation-" + uuid4().hex,
                    task["id"],
                    expected_revision=task["revision"],
                    action="exclude",
                    now=run.now(),
                )
            run.bootstrap_provider()
            run.services.queue.set_pause(False, now=run.now())
            try:
                run.run_until_idle(steps=50)
            finally:
                save(run, item, "repair")
        elif args.phase == "pause":
            run.services.queue.set_pause(True, now=run.now())
            save(run, item, "paused")
        elif args.phase in {"merge", "close"}:
            assert (args.phase, args.path) in {("merge", "direct"), ("close", "fork")}
            current = save(run, item, "pre-" + args.phase)
            assert (
                current["task"]["state"] == "waiting"
                and current["task"]["wait_reason"] == "pr_review"
            )
            revised = [
                s for s in current["repair_sessions"] if s["state"] == "verified"
            ]
            assert len(revised) >= 2, (
                "The original P3 publication alone is insufficient"
            )
            operator = open_run("direct")
            try:
                endpoint = PREFIX + "/pulls/" + str(item["number"])
                pull = github(operator, "GET", endpoint)
                assert pull["state"] == "open" and not pull["merged"]
                assert pull["head"]["sha"] == current["lifecycle"]["head_sha"]
                assert current["lifecycle"]["snapshot"]["ci"]["state"] == "success"
                assert not any(
                    not review.get("outdated")
                    for review in current["lifecycle"]["snapshot"]["requested_changes"]
                )
                verified_heads = {
                    json.loads(session["frozen_json"])["sha"] for session in revised
                }
                assert (
                    len(verified_heads) >= 2 and pull["head"]["sha"] in verified_heads
                )
                assert any(
                    entry.get("filename") == fixture(item["subject"])[0]
                    and entry.get("status") != "removed"
                    for entry in github(operator, "GET", endpoint + "/files")
                )
                result = (
                    github(
                        operator,
                        "PUT",
                        endpoint + "/merge",
                        {
                            "sha": pull["head"]["sha"],
                            "merge_method": "merge",
                            "commit_title": "P4 controlled acceptance: merge revised PR #16",
                        },
                    )
                    if args.phase == "merge"
                    else github(operator, "PATCH", endpoint, {"state": "closed"})
                )
                dump(
                    {
                        "operator_action": args.phase,
                        "number": item["number"],
                        "merged": result.get("merged"),
                        "state": result.get("state"),
                        "sha": result.get("sha"),
                    }
                )
            finally:
                operator.services.close()
        else:
            save(run, item, "status")
    finally:
        run.services.close()


if __name__ == "__main__":
    main()
