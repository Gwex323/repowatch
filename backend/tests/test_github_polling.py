import json
from types import SimpleNamespace

import httpx
import pytest

from repowatch.adapters.github import GitHubReadAdapter
from repowatch.api.host import HostServices
from repowatch.application.github_ports import GitHubReadError
from repowatch.application.github_reads import GitHubReads
from repowatch.application.storage import ConflictError
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate


@pytest.fixture
def polling(tmp_path):
    settings = StorageSettings(tmp_path / "polling")
    migrate(settings)
    now = [1000000]
    services = HostServices(settings, vault=object(), clock=lambda: now[0])
    repository = services.tasks.register_repository("55", admission_mode="all_bugs")
    scan = services.tasks.ensure_task(repository, "import", mode="scan", now=now[0])
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository},
            {
                "identity_json": "{}",
                "github_owner": "fixture",
                "github_name": "project",
                "target_branch": "main",
                "target_sha": "a" * 40,
            },
        )
        epoch = records.one("repositories", id=repository)["import_epoch"]
        records.add(
            "repository_imports",
            {
                "id": "import",
                "repository_id": repository,
                "import_epoch": epoch,
                "task_id": scan,
                "configuration_json": "{}",
                "created_at": now[0],
            },
        )
    wire = SimpleNamespace(
        fail_page=None,
        issues=[
            {
                "id": 10,
                "number": 1,
                "title": "bug",
                "body": "fails",
                "state": "open",
                "updated_at": "2026-11-08T00:00:00Z",
            }
        ],
        calls=[],
        sha="a" * 40,
        paginated=False,
        force_304=False,
        limited=False,
        pull=None,
        reviews=[],
        review_comments=[],
        checks=[],
        statuses=[],
        comments=[],
    )

    def handle(request):
        wire.calls.append(request)
        assert request.method == "GET"
        if wire.limited:
            return httpx.Response(429, headers={"Retry-After": "600"})
        if "/branches/" in request.url.path and "/rules/" not in request.url.path:
            return httpx.Response(
                200, json={"commit": {"sha": wire.sha}}, headers={"etag": "branch"}
            )
        if wire.pull is not None:
            if "/git/commits/" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "sha": request.url.path.rsplit("/", 1)[1],
                        "parents": [{"sha": wire.sha}, {"sha": wire.pull["head"]["sha"]}],
                    },
                )
            if request.url.path.endswith("/pulls/2"):
                return httpx.Response(200, json=wire.pull)
            if request.url.path.endswith("/reviews"):
                return httpx.Response(200, json=wire.reviews)
            if request.url.path.endswith("/pulls/2/comments"):
                return httpx.Response(200, json=wire.review_comments)
            if request.url.path.endswith("/check-runs"):
                return httpx.Response(200, json={"check_runs": wire.checks})
            if request.url.path.endswith("/statuses"):
                return httpx.Response(200, json=wire.statuses)
        if request.url.path.endswith("/issues/1/comments"):
            return httpx.Response(200, json=wire.comments)
        page = int(request.url.params.get("page", "1"))
        if request.url.path.endswith("/issues"):
            if page == wire.fail_page:
                return httpx.Response(503)
            if wire.force_304:
                return httpx.Response(304)
            return httpx.Response(
                200,
                json=wire.issues if page == 1 else [],
                headers={
                    "etag": "issues",
                    **({"link": '<ignored>; rel="next"'} if wire.paginated and page == 1 else {}),
                },
            )
        return httpx.Response(200, json=[])

    services.repositories.adapter_factory = lambda _: GitHubReadAdapter(
        transport=httpx.MockTransport(handle)
    )
    yield services, repository, wire, now
    services.close()


def test_complete_sync_reads_branch_and_keeps_same_content_revision(polling):
    services, repository, wire, now = polling
    assert services.synchronization.tick()
    status = services.synchronization.status(repository)
    assert status["last_success_at"] == now[0]
    assert status["last_result"] == "changed"
    now[0] += 7200
    assert services.synchronization.tick()
    assert (
        services.synchronization.status(repository)["last_changed_at"] == status["last_changed_at"]
    )
    assert wire.calls[-1].url.path.endswith("/branches/main")
    issue_queries = [call.url.params for call in wire.calls if call.url.path.endswith("/issues")]
    assert "since" not in issue_queries[0] and "since" in issue_queries[1]
    assert all(
        "since" not in call.url.params for call in wire.calls if call.url.path.endswith("/comments")
    )


def test_partial_failure_preserves_watermark_and_objects_then_deduplicates(polling):
    services, repository, wire, now = polling
    wire.paginated, wire.fail_page = True, 2
    services.synchronization.tick()
    assert services.synchronization.status(repository)["last_success_at"] is None
    with services.store.read() as records:
        assert len(records.find("github_observations")) == 1
        assert not records.find("github_query_progress")
        assert len(records.find("pending_signals")) == 1
    wire.fail_page = None
    now[0] += 60
    services.synchronization.tick()
    with services.store.read() as records:
        assert len(records.find("pending_signals")) == 1
        assert records.find("github_query_progress")


def test_target_drift_uses_import_budget_and_does_not_change_epoch(polling):
    services, repository, wire, now = polling
    with services.store.read() as records:
        before = dict(records.one("repositories", id=repository))
        budget_count = len(records.find("budget_ledgers"))
    wire.issues = []
    wire.sha = "b" * 40
    services.synchronization.tick()
    services.synchronization.request("again", repository)
    now[0] += 1
    services.synchronization.tick()
    with services.store.read() as records:
        current = records.one("repositories", id=repository)
        assert current["target_sha"] == wire.sha
        assert current["import_epoch"] == before["import_epoch"]
        assert len(records.find("budget_ledgers")) == budget_count
        assert len(records.find("tasks", repository_id=repository)) == 2
        assert not records.find("runs")
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_rate_limit_keeps_success_and_honors_retry_after(polling):
    services, repository, wire, now = polling
    services.synchronization.tick()
    wire.limited = True
    services.synchronization.request("limit", repository)
    services.synchronization.tick()
    state = services.synchronization.status(repository)
    assert state["last_success_at"] == now[0]
    assert state["error_code"] == "rate_limited"
    assert state["retry_at"] == now[0] + 600


def test_old_sync_configuration_cannot_commit(polling):
    services, _, _, now = polling
    repository, run_id, _ = services.synchronization._claim(now[0])
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository["id"]},
            {"target_revision": repository["target_revision"] + 1},
        )
    with pytest.raises(ConflictError):
        services.synchronization.reader.query(repository, run_id, "issues")


def test_failed_signal_save_rolls_back_observation_and_head(polling, monkeypatch):
    services, repository, wire, _ = polling
    from repowatch.application.task_records import TaskRecords

    def fail(*_args, **_kwargs):
        raise ConflictError("injected signal storage conflict")

    monkeypatch.setattr(TaskRecords, "observe", fail)
    reader = GitHubReads(services.store, services.repositories.adapter_factory(None))
    with pytest.raises(ConflictError):
        reader._observe(repository, wire.issues[0], now=0)
    with services.store.read() as records:
        assert not records.find("github_observations")
        assert not records.find("github_object_heads")
        assert not records.find("tasks", issue_id="10")


def test_same_timestamp_conflict_never_overwrites_newer_issue(polling):
    services, repository, wire, _ = polling
    reader = GitHubReads(services.store, services.repositories.adapter_factory(None))
    reader._observe(repository, wire.issues[0], now=0)
    with pytest.raises(GitHubReadError):
        reader._observe(repository, {**wire.issues[0], "title": "conflict"}, now=1)
    with services.store.read() as records:
        task = records.find("tasks", issue_id="10")[0]
        assert json.loads(task["input_json"])["title"] == "bug"


def test_remote_watermark_and_unchanged_content_do_not_follow_host_clock(polling):
    services, repository, wire, now = polling
    services.synchronization.tick()
    changed_at = services.synchronization.status(repository)["last_changed_at"]
    wire.issues[0]["updated_at"] = "2026-11-08T00:01:00Z"
    now[0] += 7200
    services.synchronization.tick()
    assert services.synchronization.status(repository)["last_changed_at"] == changed_at
    assert services.synchronization.status(repository)["last_result"] == "unchanged"
    wire.issues[0]["updated_at"] = "2026-11-08T00:00:00Z"
    services.synchronization.request("late-same-content", repository)
    services.synchronization.tick()
    assert services.synchronization.status(repository)["error_code"] == "source_order_uncertain"
    queries = [call.url.params for call in wire.calls if call.url.path.endswith("/issues")]
    assert queries[1]["since"].startswith("2026-")


def test_no_cache_304_is_reread_and_never_means_repository_unchanged(polling):
    services, repository, wire, _ = polling
    wire.force_304 = True
    services.synchronization.tick()
    assert services.synchronization.status(repository)["error_code"] == "uncached_304"
    assert services.synchronization.status(repository)["last_success_at"] is None
    assert len(wire.calls) == 2


def test_older_comment_cannot_replace_newer_body(polling):
    services, repository, wire, now = polling
    wire.comments = [{"id": 91, "body": "new", "updated_at": "2026-11-08T00:01:00Z"}]
    services.synchronization.tick()
    wire.comments = [{"id": 91, "body": "old", "updated_at": "2026-11-08T00:00:00Z"}]
    services.synchronization.request("old-comment", repository)
    now[0] += 1
    services.synchronization.tick()
    assert services.synchronization.status(repository)["error_code"] == "source_order_uncertain"
    with services.store.read() as records:
        comment = records.one(
            "github_remote_objects", repository_id=repository, kind="comment", object_id="91"
        )
        assert json.loads(comment["payload_json"])["body"] == "new"


def test_returning_to_already_pending_sha_reuses_scan_and_budget(polling):
    services, repository, wire, _ = polling
    wire.issues = []
    with services.store.write() as records:
        current = records.one("repositories", id=repository)
        imported = records.find("repository_imports", repository_id=repository)[0]
        records.change(
            "tasks",
            {"id": imported["task_id"]},
            {
                "input_json": json.dumps(
                    {
                        "target_sha": "a" * 40,
                        "target_revision": current["target_revision"],
                        "import_epoch": current["import_epoch"],
                        "scan_policy_revision": current["scan_policy_revision"],
                    }
                )
            },
        )
    services.synchronization.tick()
    wire.sha = "b" * 40
    services.synchronization.request("move", repository)
    services.synchronization.tick()
    wire.sha = "a" * 40
    services.synchronization.request("return", repository)
    services.synchronization.tick()
    with services.store.read() as records:
        assert len(records.find("tasks", repository_id=repository)) == 2
        assert len(records.find("budget_ledgers")) == 1


def test_target_only_change_is_reported_without_extending_issue_pr_activity(polling):
    services, repository, wire, _ = polling
    wire.issues = []
    wire.sha = "b" * 40
    services.synchronization.tick()
    state = services.synchronization.status(repository)
    assert state["last_result"] == "changed"
    assert state["last_changed_at"] is None


def test_late_branch_response_cannot_roll_back_a_newer_worker_observation(polling):
    services, repository, wire, _ = polling
    original = services.repositories.adapter_factory(None).transport
    raced = []

    def handle(request):
        result = original.handle_request(request)
        if (
            request.url.path.endswith("/branches/main")
            and "/rules/" not in request.url.path
            and not raced
        ):
            raced.append(True)
            with services.store.write() as records:
                current = records.one("repositories", id=repository)
                records.change(
                    "repositories",
                    {"id": repository},
                    {"target_sha": "b" * 40, "revision": current["revision"] + 1},
                )
            wire.sha = "b" * 40
        return result

    services.repositories.adapter_factory = lambda _: GitHubReadAdapter(
        transport=httpx.MockTransport(handle)
    )
    services.synchronization.tick()
    assert services.synchronization.status(repository)["error_code"] == "target_observation_changed"
    with services.store.read() as records:
        assert records.one("repositories", id=repository)["target_sha"] == "b" * 40
