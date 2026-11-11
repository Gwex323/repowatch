from test_control_api import api as api
from test_control_api import mutation


def test_operations_are_authenticated_and_do_not_start_models(api):
    client, headers, services, _ = api
    view = client.get("/api/v1/operations")
    assert view.status_code == 200
    assert view.json()["native_capability"]["supported"] is False
    assert not services.console.state()["tasks"]
    denied = client.post("/api/v1/operations/backup", json=mutation("denied"))
    assert denied.status_code == 403
    backup = client.post("/api/v1/operations/backup", headers=headers, json=mutation("backup"))
    assert backup.status_code == 200
    repeated = client.post("/api/v1/operations/backup", headers=headers, json=mutation("backup"))
    assert repeated.json() == backup.json()
    assert len(client.get("/api/v1/operations").json()["backups"]) == 1


def test_cleanup_requires_preview_and_rejects_stale_policy(api):
    client, headers, _, _ = api
    response = client.post(
        "/api/v1/operations/cleanup", headers=headers, json=mutation("preview", dry_run=True)
    )
    assert response.status_code == 200
    plan = response.json()["plan_sha256"]
    assert (
        client.post(
            "/api/v1/operations/cleanup", headers=headers, json=mutation("no-plan", dry_run=False)
        ).status_code
        == 422
    )
    policy = client.get("/api/v1/operations").json()["policy"]
    policy["artifact_days"] += 1
    assert (
        client.post(
            "/api/v1/operations/policy", headers=headers, json=mutation("policy", 1, **policy)
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/operations/cleanup",
            headers=headers,
            json=mutation("stale-cleanup", dry_run=False, plan_sha256=plan),
        ).status_code
        == 409
    )
    preview = client.post(
        "/api/v1/operations/cleanup", headers=headers, json=mutation("preview-new", dry_run=True)
    ).json()
    apply = mutation("apply", dry_run=False, plan_sha256=preview["plan_sha256"])
    result = client.post("/api/v1/operations/cleanup", headers=headers, json=apply)
    assert result.status_code == 200
    assert (
        client.post("/api/v1/operations/cleanup", headers=headers, json=apply).json()
        == result.json()
    )


def test_native_switch_is_explicit_and_never_claims_admission(api):
    client, headers, _, _ = api
    result = client.post(
        "/api/v1/operations/native", headers=headers, json=mutation("native", 1, enabled=True)
    )
    assert result.status_code == 200
    assert result.json()["admitted"] is False
    assert client.get("/api/v1/operations").json()["native"]["enabled"] is True


def test_native_view_rechecks_live_admission_without_running_tasks(api, monkeypatch):
    client, headers, services, _ = api
    client.post(
        "/api/v1/operations/native", headers=headers, json=mutation("native-view", 1, enabled=True)
    )
    calls = []

    def admitted(_records, configuration):
        calls.append(configuration)
        return object()

    monkeypatch.setattr("repowatch.operations.native.require_backend", admitted)
    assert client.get("/api/v1/operations").json()["native_capability"]["supported"] is True

    def expired(*_):
        raise PermissionError("Host session ended")

    monkeypatch.setattr("repowatch.operations.native.require_backend", expired)
    assert client.get("/api/v1/operations").json()["native_capability"]["supported"] is False
    assert calls == [{"execution_backend": "native"}]
    assert not services.console.state()["tasks"]


def test_restore_report_and_acknowledgement_require_current_facts_and_keep_pause(api):
    from repowatch.application.task_records import block

    client, headers, services, _ = api
    assert (
        client.post(
            "/api/v1/operations/restore-report", headers=headers, json=mutation("no-restore")
        ).status_code
        == 403
    )
    with services.store.write() as records:
        block(records, "backup_restore_reconciliation", None, now=0)
    assert client.get("/api/v1/operations").json()["restore_required"]
    report = client.post(
        "/api/v1/operations/restore-report", headers=headers, json=mutation("restore-report")
    ).json()
    result = client.post(
        "/api/v1/operations/restore-acknowledge",
        headers=headers,
        json=mutation(
            "restore-ack",
            report_id=report["report_id"],
            basis_sha256=report["basis_sha256"],
            explanation="reviewed current fixture state",
        ),
    )
    assert result.status_code == 200
    assert result.json()["paused"] is True
    assert client.get("/api/v1/state").json()["control"]["paused"] == 1
    assert client.get("/api/v1/operations").json()["restore_required"] is False
