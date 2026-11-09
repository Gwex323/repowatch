import asyncio
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from repowatch.adapters.github import GitHubReadAdapter
from repowatch.adapters.storage.sqlite import SQLiteBusinessStore
from repowatch.api.app import create_app
from repowatch.api.host import HostServices
from repowatch.api.security import LocalSessions
from repowatch.api.synchronizer import synchronize
from repowatch.application.github_sync import GitHubSynchronization, IssueSynchronization
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import TaskRecords
from repowatch.configuration import StorageSettings
from repowatch.operations.migrate import migrate


@pytest.fixture
def synchronization(tmp_path):
    settings = StorageSettings(tmp_path / "sync")
    migrate(settings)
    store = SQLiteBusinessStore(settings)
    repository = TaskRecords(store).register_repository("123", admission_mode="manual")
    with store.write() as records:
        records.change("repositories", {"id": repository}, {"identity_json": "{}"})
    now, calls = [0], []

    def reader(repository, run_id):
        calls.append((repository["id"], run_id))
        return False

    service = GitHubSynchronization(store, reader, clock=lambda: now[0])
    yield store, repository, now, calls, service
    store.close()


def test_restart_compensates_once_and_pause_does_not_block_reads(synchronization):
    store, repository, now, calls, service = synchronization
    assert service.tick()
    assert service.status(repository)["next_due_at"] == 86400
    now[0] = 10 * 86400
    restarted = GitHubSynchronization(store, service.reader, clock=lambda: now[0])
    assert restarted.tick()
    assert not restarted.tick()
    assert len(calls) == 2
    with store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 1
        assert not records.find("runs")
        assert not records.find("budget_ledgers")


def test_configurable_clock_and_new_task_activate_idle_repository(synchronization):
    store, repository, now, calls, service = synchronization
    service.tick()
    service.configure(
        "policy",
        repository,
        {
            "active_seconds": 600,
            "idle_seconds": 3600,
            "lookback_seconds": 1200,
        },
        expected_revision=1,
    )
    service.tick()
    assert service.status(repository)["next_due_at"] == 3600
    TaskRecords(store).ensure_task(repository, "new-task", now=0)
    now[0] = 599
    assert not service.tick()
    now[0] = 600
    assert service.tick()
    assert len(calls) == 3
    with pytest.raises(ConflictError):
        service.configure("old", repository, {}, expected_revision=1)


def test_triggers_before_read_merge_and_operation_replay_is_stable(synchronization):
    _, repository, _, calls, service = synchronization
    requested = service.request("manual-1", repository)
    assert service.request("manual-1", repository) == requested
    service.request("manual-2", repository)
    assert service.tick()
    assert service.status(repository)["completed_sequence"] == 2
    assert not service.tick()
    assert len(calls) == 1
    with pytest.raises(ConflictError):
        service.request("manual-1", "other")


def test_live_lease_survives_expiry_and_inflight_request_requires_next_read(synchronization):
    store, repository, now, _, service = synchronization
    entered, release = Event(), Event()

    def blocking_reader(*_args):
        entered.set()
        assert release.wait(10)
        return False

    service.reader = blocking_reader
    other = GitHubSynchronization(store, blocking_reader, clock=lambda: now[0])
    service.request("before", repository)
    with ThreadPoolExecutor() as executor:
        running = executor.submit(service.tick)
        try:
            assert entered.wait(10)
            now[0] = 1000
            assert not other.tick()
            service.request("during", repository)
        finally:
            release.set()
        assert running.result()
    assert service.status(repository)["completed_sequence"] == 1
    assert service.tick()
    assert service.status(repository)["completed_sequence"] == 2


def test_dead_owner_recovery_fences_old_completion(synchronization):
    store, repository, now, calls, service = synchronization
    service.request("manual", repository)
    _, old_run, sequence = service._claim(0)
    now[0] = 121
    restarted = GitHubSynchronization(
        store, service.reader, clock=lambda: now[0], stopped=lambda _: True
    )
    assert restarted.tick()
    assert len(calls) == 1
    assert restarted.status(repository)["completed_sequence"] == sequence
    with store.read() as records:
        assert records.one("github_sync_runs", id=old_run)["error_code"] == "interrupted_sync"
    with pytest.raises(LookupError):
        service._finish(repository, old_run, sequence)


def test_failure_keeps_success_watermark_and_manual_request_obeys_backoff(synchronization):
    store, repository, now, _, service = synchronization
    service.tick()
    now[0] = 100

    def fail(*_args):
        raise RuntimeError("private response must never be persisted")

    service.reader = fail
    service.request("manual", repository)
    assert service.tick()
    status = service.status(repository)
    assert status["last_success_at"] == 0
    assert status["completed_sequence"] == 0
    assert status["error_code"] == "sync_read_failed"
    assert status["retry_at"] == 160
    assert not service.tick()
    with store.read() as records:
        assert records.one("global_control", singleton=1)["paused"] == 1


def test_repeated_payload_does_not_keep_idle_repository_active(synchronization):
    store, repository, now, _, service = synchronization
    service.reader = lambda *_: True
    assert service.tick()
    assert service.status(repository)["next_due_at"] == 7200
    service.reader = lambda *_: False
    now[0] = 604801
    assert service.tick()
    status = service.status(repository)
    assert status["last_changed_at"] == 0
    assert status["next_due_at"] == now[0] + 86400
    TaskRecords(store).ensure_task(repository, "issue", now=now[0])
    service.request("active-task", repository)
    assert service.tick()
    assert service.status(repository)["next_due_at"] == now[0] + 7200


def test_loop_waits_for_read_before_shutdown():
    entered, release = Event(), Event()

    class SlowService:
        def tick(self):
            entered.set()
            assert release.wait(10)
            return True

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(synchronize(SlowService(), stop))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            stop.set()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())


def hold_sync_in_child(data_dir, entered, release):
    store = SQLiteBusinessStore(StorageSettings(data_dir))

    def reader(*_args):
        entered.set()
        if not release.wait(20):
            raise RuntimeError("未收到测试释放信号")
        return False

    try:
        GitHubSynchronization(store, reader, clock=lambda: 0).tick()
    finally:
        store.close()


def test_two_api_processes_share_independent_sync_lease(synchronization):
    store, _, _, calls, service = synchronization
    context = multiprocessing.get_context("spawn")
    entered, release = context.Event(), context.Event()
    child = context.Process(
        target=hold_sync_in_child, args=(store.settings.data_dir, entered, release)
    )
    child.start()
    try:
        assert entered.wait(15)
        assert not service.tick()
        assert not calls
        with store.read() as records:
            assert not records.find("runs")
    finally:
        release.set()
        child.join(20)
        if child.is_alive():
            child.terminate()
            child.join(5)
    assert child.exitcode == 0
    assert not service.tick()


def test_default_reader_paginates_and_reuses_cache_without_new_change(synchronization):
    store, repository, now, _, service = synchronization
    calls = []

    def handle(request):
        assert request.method == "GET"
        page = int(request.url.params["page"])
        calls.append((page, request.headers.get("if-none-match")))
        if request.headers.get("if-none-match"):
            return httpx.Response(304)
        return httpx.Response(
            200,
            json=[
                {"id": page, "number": page, "title": "bug", "updated_at": "2026-11-08T00:00:00Z"}
            ],
            headers={"etag": str(page), **({"link": '<ignored>; rel="next"'} if page == 1 else {})},
        )

    with store.write() as records:
        records.change(
            "repositories",
            {"id": repository},
            {
                "github_owner": "upstream",
                "github_name": "fixture",
            },
        )
    service.reader = IssueSynchronization(
        store,
        lambda _: GitHubReadAdapter(transport=httpx.MockTransport(handle)),
        clock=lambda: now[0],
    )
    assert service.tick()
    assert service.status(repository)["last_changed_at"] == 0
    now[0] = 7200
    assert service.tick()
    assert calls == [(1, None), (2, None), (1, "1"), (2, "2")]
    assert service.status(repository)["last_changed_at"] == 0
    with store.read() as records:
        assert len(records.find("pending_signals")) == 2
        assert not records.find("runs")


def test_api_lifespan_starts_and_drains_synchronization(tmp_path):
    settings = StorageSettings(tmp_path / "api-sync")
    migrate(settings)
    services = HostServices(settings, vault=object(), clock=lambda: 0)
    repository = services.tasks.register_repository("api-sync", admission_mode="manual")
    with services.store.write() as records:
        records.change("repositories", {"id": repository}, {"identity_json": "{}"})
    entered = Event()

    def reader(*_args):
        entered.set()
        return False

    services.synchronization.reader = reader
    sessions = LocalSessions()
    origin = "http://127.0.0.1:8765"
    with TestClient(
        create_app(services, sessions=sessions), base_url=origin, client=("127.0.0.1", 5000)
    ) as client:
        assert entered.wait(10)
        opened = client.post(
            "/api/v1/session/bootstrap",
            headers={
                "Origin": origin,
                "Authorization": "Bearer " + sessions.bootstrap_token,
            },
        )
        assert opened.status_code == 200
        assert client.get("/api/v1/health").status_code == 200
        payload = {"operation_id": "check-updates"}
        headers = {"Origin": origin, "X-RepoWatch-Session": opened.json()["csrf"]}
        response = client.post(
            f"/api/v1/repositories/{repository}/sync", json=payload, headers=headers
        )
        assert response.status_code == 200
        assert (
            client.post(
                f"/api/v1/repositories/{repository}/sync", json=payload, headers=headers
            ).json()
            == response.json()
        )
        status = client.get(f"/api/v1/repositories/{repository}/sync")
        assert status.status_code == 200
        assert status.json()["requested_sequence"] == 1
    reopened = SQLiteBusinessStore(settings)
    with reopened.read() as records:
        assert records.one("github_sync_state", repository_id=repository)["last_success_at"] == 0
        assert not records.find("github_sync_runs", state="running")
        assert not records.find("github_sync_lease")
    reopened.close()
