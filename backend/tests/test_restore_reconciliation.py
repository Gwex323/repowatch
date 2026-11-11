import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_local_operations import services as services

from repowatch.application.credentials import SecretValue
from repowatch.application.storage import ConflictError
from repowatch.application.task_records import block
from repowatch.domain.task_records import canonical_json
from repowatch.operations.reconcile import RestoreReconciliation


@pytest.fixture
def restored(services):
    repository = services.tasks.register_repository("123", admission_mode="manual")
    task = services.tasks.ensure_task(repository, "issue:1", issue_id="101", now=0)
    with services.store.write() as records:
        records.change(
            "repositories",
            {"id": repository},
            {
                "github_owner": "fixture",
                "github_name": "restore",
                "target_branch": "main",
                "target_sha": "a" * 40,
                "credential_reference": "test-reference",
                "publication_json": canonical_json(
                    {"github_id": "456", "owner": "bot", "name": "restore"}
                ),
                "identity_json": canonical_json({"execution_login": "bot"}),
            },
        )
        records.change("tasks", {"id": task}, {"input_json": canonical_json({"number": 1})})
        block(records, "backup_restore_reconciliation", None, now=0)
    services.vault = SimpleNamespace(read=lambda _: SecretValue("controlled-restore-key"))
    pulls = [
        {
            "id": 789,
            "number": 2,
            "state": "open",
            "user": {"login": "bot"},
            "head": {"ref": "repowatch/issue-1-" + task[:12], "sha": "b" * 40, "repo": {"id": 456}},
            "base": {"ref": "main", "sha": "a" * 40, "repo": {"id": 123}},
            "body": (
                "Closes #1\n\n<!-- repowatch-publication:11111111-1111-1111-1111-111111111111 -->"
            ),
        }
    ]
    calls = []

    class Port:
        def _path(self, owner, name):
            return "/repos/" + owner + "/" + name

        def repository(self, owner, name):
            calls.append("repository")
            return {"github_id": "123"}

        def branch(self, *args):
            calls.append("branch")
            return "a" * 40

        def collection(self, path, *, params, max_pages):
            assert params == {"state": "all"}
            assert max_pages == 100
            calls.append("all_pulls")
            return deepcopy(pulls)

    reconciliation = RestoreReconciliation(services, adapter_factory=lambda _: Port())
    return SimpleNamespace(
        services=services,
        reconciliation=reconciliation,
        pulls=pulls,
        calls=calls,
        task=task,
        repository=repository,
    )


def test_restore_before_pr_recovers_existing_remote_binding_without_publication(restored):
    report = restored.reconciliation.report("report")
    assert report["ready"]
    assert report["candidates"][0]["already_bound"] is False
    result = restored.reconciliation.acknowledge(
        "ack", "report", report["basis_sha256"], explanation="reviewed actual remote identity"
    )
    assert (
        restored.reconciliation.acknowledge(
            "ack", "report", report["basis_sha256"], explanation="reviewed actual remote identity"
        )
        == result
    )
    with restored.services.store.read() as records:
        binding = records.one("managed_pull_requests", task_id=restored.task)
        assert binding["pr_id"] == "789"
        assert records.one("tasks", id=restored.task)["wait_reason"] == "delivery_review"
        assert records.one("global_control", singleton=1)["paused"] == 1
        assert not records.find("publication_intents")
        assert not records.find("block_reasons", code="backup_restore_reconciliation", resolved=0)
    assert restored.calls == ["repository", "branch", "all_pulls"] * 2


@pytest.mark.parametrize("problem", ["credential", "identity", "multiple"])
def test_restore_does_not_acknowledge_unconfirmed_facts(restored, problem):
    if problem == "credential":
        restored.services.vault = SimpleNamespace(read=lambda _: None)
    elif problem == "identity":
        restored.pulls[0]["head"]["repo"]["id"] = 999
    else:
        duplicate = deepcopy(restored.pulls[0])
        duplicate["id"] = 790
        restored.pulls.append(duplicate)
    report = restored.reconciliation.report("report")
    assert report["ready"] is False
    with pytest.raises(PermissionError):
        restored.reconciliation.acknowledge(
            "ack", "report", report["basis_sha256"], explanation="cannot override missing evidence"
        )
    with restored.services.store.read() as records:
        assert not records.find("managed_pull_requests")


def test_restore_report_changes_when_remote_sha_changes(restored):
    report = restored.reconciliation.report("report")
    restored.pulls[0]["head"]["sha"] = "c" * 40
    with pytest.raises(ConflictError):
        restored.reconciliation.acknowledge(
            "ack", "report", report["basis_sha256"], explanation="stale remote"
        )


def test_generic_reason_confirmation_cannot_bypass_restore_reconciliation(restored):
    with restored.services.store.read() as records:
        reason = records.find("block_reasons", code="backup_restore_reconciliation", resolved=0)[0]
    with pytest.raises(PermissionError, match="恢复备份"):
        restored.services.control.resolve_reason(
            "bypass",
            reason["id"],
            expected_revision=reason["revision"],
            now=0,
            explanation="just trust me",
        )


def test_restore_reports_do_not_echo_credentials(restored):
    value = restored.reconciliation.report("report")
    assert "controlled-restore-key" not in json.dumps(value)
