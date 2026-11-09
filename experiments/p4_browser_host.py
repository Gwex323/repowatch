"""P4 浏览器验收宿主：临时数据、只读 HTTP 夹具，无远端写入或模型调用。"""

import argparse
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from repowatch.adapters.github import GitHubReadAdapter
from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.configuration import StorageSettings
from repowatch.domain.task_records import canonical_json
from repowatch.domain.triage import TriageAnalysis
from repowatch.operations.migrate import migrate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    settings = StorageSettings(args.data_dir.resolve())
    migrate(settings)
    services = HostServices(settings, vault=SimpleNamespace())
    repository = services.tasks.register_repository("55", admission_mode="all_bugs")
    scan = services.tasks.ensure_task(
        repository, "import", mode="scan", now=time.time()
    )
    wire = {"sha": "a" * 40, "failure": False, "calls": 0}
    issue = {
        "id": 555,
        "number": 1,
        "title": "sum fails",
        "body": "handle negative sums",
        "state": "open",
        "updated_at": "2026-11-08T00:00:00Z",
    }
    pull = {
        "id": 777,
        "number": 2,
        "title": "fix sum",
        "body": "fix #1",
        "state": "open",
        "updated_at": "2026-11-08T00:00:00Z",
        "head": {"sha": "b" * 40, "ref": "repowatch/issue-1", "repo": {"id": 55}},
        "base": {"sha": "a" * 40, "ref": "main", "repo": {"id": 55}},
    }

    def transport(request):
        if request.method != "GET":
            raise AssertionError("浏览器同步夹具只允许 GET")
        wire["calls"] += 1
        if wire["failure"]:
            return httpx.Response(503)
        path = request.url.path
        if path.endswith("/issues"):
            return httpx.Response(200, json=[issue])
        if path.endswith("/branches/main") and "/rules/" not in path:
            return httpx.Response(200, json={"commit": {"sha": wire["sha"]}})
        if path.endswith("/pulls/2"):
            return httpx.Response(200, json=pull)
        if path.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 888,
                        "state": "CHANGES_REQUESTED",
                        "commit_id": "b" * 40,
                        "submitted_at": "2026-11-08T00:00:00Z",
                        "body": "extend to negatives",
                        "user": {"login": "maintainer"},
                    }
                ],
            )
        if path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        return httpx.Response(200, json=[])

    services.repositories.adapter_factory = lambda _: GitHubReadAdapter(
        transport=httpx.MockTransport(transport)
    )
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository},
            {
                "identity_json": '{"execution_login":"fixture"}',
                "github_owner": "fixture",
                "github_name": "p4-browser",
                "target_branch": "main",
                "target_sha": "a" * 40,
            },
        )
        epoch = records.one("repositories", id=repository)["import_epoch"]
        records.add(
            "repository_imports",
            {
                "id": "fixture-import",
                "repository_id": repository,
                "import_epoch": epoch,
                "task_id": scan,
                "configuration_json": "{}",
                "created_at": time.time(),
            },
        )
    services.synchronization.tick()
    with services.store.read() as records:
        task_id = records.find("tasks", issue_id="555")[0]["id"]
    services.tasks.bind_managed_pr(task_id, "55", "777")
    with services.store.write() as records:
        records.add(
            "publication_intents",
            {
                "id": "fixture-publication",
                "task_id": task_id,
                "repository_id": repository,
                "issue_id": "555",
                "kind": "pull_request",
                "request_json": "{}",
                "request_sha256": "f" * 64,
                "state": "confirmed",
                "result_json": '{"number":2}',
                "created_at": time.time(),
            },
        )
    services.synchronization.request("fixture-pr", repository)
    services.synchronization.tick()
    with services.store.write() as records:
        task = records.one("tasks", id=task_id)
        lifecycle = records.one("pr_lifecycle", task_id=task_id)
        analysis = TriageAnalysis(
            kind="feature",
            objective="handle negative sums",
            criteria=["sum(-2,3)==1"],
            plan="extend the sum implementation",
            review_scope="expanded",
            review_evidence=["maintainer requests new inputs"],
        )
        records.add(
            "review_scope_decisions",
            {
                "task_id": task_id,
                "context_sha256": lifecycle["feedback_sha256"],
                "decision": "pending",
                "analysis_json": analysis.model_dump_json(),
                "input_revision": task["input_revision"],
                "feedback_revision": task["feedback_revision"],
                "acceptance_revision": task["acceptance_revision"],
                "actor": "processor",
            },
        )
        records.change(
            "tasks",
            {"id": task_id},
            {"state": "waiting", "wait_reason": "review_scope", "manual_priority": 100},
        )
    sessions = LocalSessions(f"http://127.0.0.1:{args.port}")
    app = create_app(
        services, sessions=sessions, static_dir=Path("frontend/dist").resolve()
    )

    @app.post("/api/v1/fixture/change")
    def change(data: dict):
        if data.get("target"):
            wire["sha"] = "d" * 40
        wire["failure"] = bool(data.get("failure"))
        return {"calls": wire["calls"]}

    app.router.routes.insert(0, app.router.routes.pop())
    print(
        canonical_json(
            {
                "bootstrap": sessions.bootstrap_token,
                "task_id": task_id,
                "repository_id": repository,
            }
        ),
        flush=True,
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        access_log=False,
        proxy_headers=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
