"""Actual native product/GitHub chain in a new P6 sandbox branch; fixture models only."""

import argparse
import importlib.util
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from p6_samples import samples
from repowatch.adapters.git_objects import GitObjects
from repowatch.adapters.native_client import NativeHostClient, native_command_status
from repowatch.adapters.native_workspaces import NativeWorkspaces
from repowatch.application.repair_evidence import file_manifest
from repowatch.domain.task_records import content_digest
from repowatch.operations.launcher import write_json

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "p6_github", ROOT / "experiments/p6-github-acceptance.py"
)
p6 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(p6)


def command(host, backend, workspace, argv):
    """Independent operator build/run, with a recorded identity before the startup gate."""
    files = backend.snapshot(workspace, workspace["task_id"])
    payload = {
        "workspace_id": workspace["id"],
        "argv": argv,
        "timeout_ms": 90000,
        "deadline_unix_ms": round((time.time() + 90) * 1000),
        "manifest_sha256": content_digest(file_manifest(files)),
    }
    receipt = host.submit("check", payload)
    identifier = receipt["request_id"]
    identity = {
        "kind": "native",
        "request_id": identifier,
        "receipt": receipt,
        "host_root": str(host.root),
        "profile_sha256": content_digest(host.profile),
        "job_name": "Local\\RepoWatchNative-" + identifier,
    }
    audit = ROOT / ".cache/native-preview-receipts" / (identifier + ".json")
    write_json(audit, identity)
    started = False
    deadline = time.monotonic() + 95
    try:
        while time.monotonic() < deadline:
            result = host.response(receipt)
            if result is not None:
                content = host.log(receipt, result)
                if (
                    result.get("exit_code") != 0
                    or result.get("active_processes") != 0
                    or result.get("log_truncated")
                    or result.get("timed_out")
                    or result.get("control_stopped")
                ):
                    raise AssertionError(
                        "Native operator command failed: " + json.dumps(result)
                    )
                return result, content
            if not started:
                bound = host.binding(receipt)
                if bound is not None:
                    identity.update(bound)
                    write_json(audit, identity)
                    host.start(receipt)
                    started = True
            time.sleep(0.05)
        raise TimeoutError("Native operator command has no verified result")
    except BaseException:
        write_json(
            audit.with_suffix(".stop.json"), native_command_status(identity, stop=True)
        )
        raise


def verify(sample, metadata, host):
    directory = ROOT / ".repowatch" / ("p6-" + sample.key)
    report_path = directory / "report.json"
    report = json.loads(report_path.read_text("utf-8"))
    product = report["product"]
    backend = NativeWorkspaces(host, GitObjects(directory / "data/git-objects"))
    workspace = backend.create(
        product["repository_id"],
        product["frozen"]["sha"],
        task_id=product["task_id"],
        mode="check",
    )
    if not workspace["ready"]:
        raise AssertionError("Native frozen source is unavailable")
    try:
        build, _ = command(host, backend, workspace, sample.build)
        cli, output = command(
            host, backend, workspace, ["python", sample.path, "2", "3"]
        )
        if output.decode("utf-8-sig").strip() != "5":
            raise AssertionError("Native candidate CLI output differs from 5")
    finally:
        backend.remove(workspace)
    services = p6.HostServices(p6.StorageSettings(directory / "data"))
    try:
        with services.store.read() as records:
            commands = [dict(row) for row in records.find("command_states")]
            control = records.one("global_control", singleton=1)
        if not commands or any(
            row["state"] != "stopped"
            or json.loads(row["identity_json"]).get("kind") != "native"
            for row in commands
        ):
            raise AssertionError(
                "Product commands are not exclusively verified native commands"
            )
        if not control["paused"]:
            raise AssertionError("Native acceptance data must remain paused")
    finally:
        services.close()
    report["adaptation"] = {
        "candidate_sha": product["frozen"]["sha"],
        "runtime_id": host.runtime_id,
        "build_exit_code": build["exit_code"],
        "cli_exit_code": cli["exit_code"],
        "cli_stdout": "5",
        "all_product_commands_native": True,
        "product_command_count": len(commands),
        "independent_operator_build_and_cli": True,
    }
    write_json(report_path, report)
    public = {
        "verified_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "native_admitted": True,
        "final_product_accepted": False,
        "model": "deterministic_http_fixture",
        "paid_model_calls": 0,
        "sample": sample.key,
        "environment": "Windows x64, dedicated ordinary execution account, per-command AppContainer",
        "runtime_id": host.runtime_id,
        "preparation": {
            key: value
            for key, value in metadata.items()
            if key not in {"native_host_root", "reference"}
        },
        "actual_pr": report["actual_pr"],
        "product": product,
        "adaptation": report["adaptation"],
        "installed_wheel": report["installed_wheel"],
        "global_paused_after_acceptance": True,
        "operator_actions": report["operator_actions"]
        + ["independent native build and CLI run"],
        "private_report": report_path.relative_to(ROOT).as_posix(),
        "private_report_sha256": sha256(report_path.read_bytes()).hexdigest(),
        "limitations": [
            "Pinned CPython stdlib CLI only; third-party native dependency preparation unavailable",
            "Fixture models do not demonstrate paid model repair efficacy",
            "No merge or release",
        ],
    }
    write_json(ROOT / "experiments/results/p6-native-product.json", public)
    print(
        json.dumps(
            {
                "native_product_passed": True,
                "pr": report["actual_pr"]["number"],
                "native_commands": len(commands),
            }
        ),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "run", "verify"])
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--allow-github-writes", action="store_true")
    parser.add_argument("--resume-host-publication-failure", action="store_true")
    parser.add_argument("--resume-native-source-budget", action="store_true")
    parser.add_argument("--resume-native-freeze", action="store_true")
    parser.add_argument("--resume-native-frozen-reference", action="store_true")
    parser.add_argument("--resume-native-terminal-goal", action="store_true")
    args = parser.parse_args()
    host = NativeHostClient(args.native_root)
    sample = replace(
        samples()["python-cli"], key="native-python-cli", image=host.runtime_id
    )
    if args.action in {"prepare", "run"} and not args.allow_github_writes:
        raise PermissionError("Explicit P6 sandbox writes required")
    if args.action == "verify":
        metadata = json.loads(
            (ROOT / ".repowatch/p6-native-python-cli/acceptance.json").read_text(
                "utf-8"
            )
        )
    else:
        metadata = p6.prepare(sample)
        if metadata["image"] != host.runtime_id:
            raise PermissionError("Pinned native runtime changed since preparation")
        metadata.update(execution_backend="native", native_host_root=str(host.root))
        write_json(ROOT / ".repowatch/p6-native-python-cli/acceptance.json", metadata)
        print(
            json.dumps({"native_sample_prepared": True, "branch": metadata["branch"]}),
            flush=True,
        )
    if args.action == "run":

        def resume_terminal_goal(run, task_id, configuration):
            with run.services.store.read() as records:
                reasons = [
                    dict(row) for row in records.find("block_reasons", resolved=0)
                ]
                task = dict(records.one("tasks", id=task_id))
                commands = [dict(row) for row in records.find("command_states")]
                sessions = [
                    dict(row)
                    for row in records.find("repair_sessions", task_id=task_id)
                ]
                attempts = [
                    dict(row)
                    for row in records.find(
                        "progress_attempts", task_id=task_id, objective_key="repair"
                    )
                ]
                artifacts = [dict(row) for row in records.find("artifacts")]
                if records.find("publication_intents", task_id=task_id):
                    raise PermissionError(
                        "Publication may already have started; explicit reconciliation required"
                    )
            if (
                len(reasons) != 2
                or {row["code"] for row in reasons}
                != {"bounded_retry_exhausted", "runtime_execution_rejected"}
                or any(row["task_id"] != task_id for row in reasons)
            ):
                raise PermissionError(
                    "Only the recorded completed-goal integration failure can be retried"
                )
            existing = json.loads(task["configuration_json"])
            strategy = existing.pop("recovery_strategy", None)
            if (
                existing != configuration
                or not strategy
                or strategy.get("operation_id") != "p6-native-frozen-reference-fix"
            ):
                raise PermissionError("Known terminal retry scope changed")
            if any(row["state"] != "stopped" for row in commands):
                raise PermissionError("Earlier native commands are not stopped")
            last = max(sessions, key=lambda row: row["created_at"])
            frozen = json.loads(last["frozen_json"])
            same_run = [row for row in attempts if row["run_id"] == last["run_id"]]
            if len(same_run) != 1:
                raise PermissionError("Missing terminal verifier attempt")
            comparison = json.loads(same_run[0]["evidence_json"])["comparison"]
            if comparison["probe_outcome"] != "passed" or not all(
                item["passed"] for item in comparison["regressions"]
            ):
                raise PermissionError(
                    "A failing candidate cannot be treated as completed"
                )
            reviews = [
                row
                for row in artifacts
                if json.loads(row["context_json"]).get("producer")
                == "independent_review"
                and json.loads(row["context_json"]).get("candidate_sha")
                == frozen["sha"]
            ]
            if not reviews:
                raise PermissionError("Missing actual independent review")
            for row in reviews:
                saved = run.services.artifacts.require(row["id"], now=run.now())
                decision = json.loads(
                    (run.services.artifacts.root / saved["relative_path"]).read_text(
                        "utf-8"
                    )
                )
                if (
                    not decision["passed"]
                    or not decision["probe_valid"]
                    or decision["weakened_acceptance"]
                ):
                    raise PermissionError("Review did not establish completion")
            for reason in reasons:
                run.services.control.resolve_reason(
                    "p6-native-terminal-goal-" + reason["code"],
                    reason["id"],
                    expected_revision=reason["revision"],
                    now=run.now(),
                    explanation="Actual candidate probe, regression and independent review passed; caller omitted the existing goal_completed signal for a terminal repair. Fixed caller only, persistent guard unchanged, all earlier facts and limits retained",
                    retry_plan={
                        "steps": [
                            "Reverify the same Issue from frozen inputs",
                            "Record a completed repair goal only after actual probes, regression and independent review pass",
                            "Publish through existing identity and evidence guards",
                        ],
                        "success_condition": "Verified terminal repair completes without inventing new progress or relaxing failed-retry protection",
                    },
                    retry_limits={
                        "max_attempts": 1,
                        "max_steps": 200,
                        "max_active_ms": 500000,
                    },
                )
            with run.services.store.read() as records:
                control = dict(records.one("global_control", singleton=1))
            run.services.control.resume(
                "p6-native-terminal-goal-resume",
                expected_revision=control["revision"],
                now=run.now(),
            )

        def resume_native_freeze(run, task_id, configuration):
            refresh_reference = args.resume_native_frozen_reference
            with run.services.store.read() as records:
                reasons = [
                    dict(row) for row in records.find("block_reasons", resolved=0)
                ]
                task = dict(records.one("tasks", id=task_id))
                commands = [dict(row) for row in records.find("command_states")]
                sessions = [
                    dict(row)
                    for row in records.find("repair_sessions", task_id=task_id)
                ]
                unavailable = [
                    dict(row)
                    for row in records.find(
                        "workspaces",
                        task_id=task_id,
                        state="ready" if refresh_reference else "unavailable",
                    )
                ]
            if (
                len(reasons) != 1
                or reasons[0]["code"] != "runtime_execution_rejected"
                or reasons[0]["task_id"] != task_id
                or json.loads(reasons[0]["details_json"]).get("detail")
                != (
                    "原生工作区材料与登记身份不匹配"
                    if refresh_reference
                    else "工作区实际卷不存在，已撤销就绪"
                )
            ):
                raise PermissionError(
                    "Only the recorded native freeze-role mismatch can be retried"
                )
            existing = json.loads(task["configuration_json"])
            strategy = existing.pop("recovery_strategy", None)
            if (
                existing != configuration
                or not strategy
                or strategy.get("operation_id")
                != (
                    "p6-native-freeze-role-fix"
                    if refresh_reference
                    else "p6-native-source-read-bound"
                )
            ):
                raise PermissionError("Original native retry scope changed")
            if any(row["state"] != "stopped" for row in commands):
                raise PermissionError("Earlier native commands are not stopped")
            checked = False
            backend = NativeWorkspaces(host, None)
            for row in unavailable:
                workspace = json.loads(row["snapshot_json"])
                if workspace.get("mode") != "review":
                    continue
                matching = [
                    session
                    for session in sessions
                    if session["workspace_id"] == row["id"]
                ]
                if len(matching) != 1:
                    continue
                files = backend.snapshot(
                    workspace
                    if refresh_reference
                    else {**workspace, "mode": "implement"},
                    task_id,
                )
                if (
                    content_digest(file_manifest(files))
                    != json.loads(matching[0]["frozen_json"])["manifest_sha256"]
                ):
                    raise PermissionError(
                        "Earlier frozen source changed; role mismatch is not the only failure"
                    )
                checked = True
            if not checked:
                raise PermissionError(
                    "Recorded frozen-role mismatch is not independently reproducible"
                )
            run.services.control.resolve_reason(
                "p6-native-frozen-reference-fix"
                if refresh_reference
                else "p6-native-freeze-role-fix",
                reasons[0]["id"],
                expected_revision=reasons[0]["revision"],
                now=run.now(),
                explanation=(
                    "Acceptance operator verified frozen bytes and both registered/native read-only roles. The coordinator retained an old implement reference for its diff read; it now reloads the registered read-only workspace, preserving write refusal and earlier failures"
                    if refresh_reference
                    else "Acceptance operator reproduced DB review versus native implement metadata with unchanged frozen bytes; native freeze now demotes both boundaries and rejects stale write references. Earlier candidate and failures retained; reverify afresh"
                ),
                retry_plan={
                    "steps": [
                        "Create a fresh native processing workspace",
                        "Freeze native port and storage roles together",
                        "Repeat original probes, regression and independent review before publication",
                    ],
                    "success_condition": "Native frozen workspace identity remains valid through guarded PR publication",
                },
                retry_limits={
                    "max_attempts": 1,
                    "max_steps": 200,
                    "max_active_ms": 500000,
                },
            )
            with run.services.store.read() as records:
                control = dict(records.one("global_control", singleton=1))
            run.services.control.resume(
                "p6-native-frozen-reference-resume"
                if refresh_reference
                else "p6-native-freeze-role-resume",
                expected_revision=control["revision"],
                now=run.now(),
            )

        def resume_source_budget(run, task_id, configuration):
            with run.services.store.read() as records:
                reasons = [
                    dict(row) for row in records.find("block_reasons", resolved=0)
                ]
                task = dict(records.one("tasks", id=task_id))
                proposals = [
                    dict(row)
                    for row in records.find(
                        "budget_renewals", task_id=task_id, state="pending"
                    )
                ]
                commands = [dict(row) for row in records.find("command_states")]
            if (
                len(reasons) != 1
                or reasons[0]["code"] != "budget_bound_exceeded"
                or reasons[0]["task_id"] != task_id
                or json.loads(task["configuration_json"]) != configuration
            ):
                raise PermissionError(
                    "Only the recorded native source-step bound failure can be resumed"
                )
            if any(row["state"] != "stopped" for row in commands):
                raise PermissionError(
                    "Earlier commands have not all been verified stopped"
                )
            for proposal in proposals:
                run.services.budgets.decide_renewal(
                    "p6-native-no-cumulative-increase-" + proposal["id"],
                    proposal["id"],
                    expected_revision=proposal["revision"],
                    decision="reject",
                    now=run.now(),
                    reason="Acceptance operator: cumulative budget remains sufficient; correct the native source read step's 1-second estimate to a bounded 30 seconds, preserving all measured usage",
                )
            run.services.control.resolve_reason(
                "p6-native-source-read-bound",
                reasons[0]["id"],
                expected_revision=reasons[0]["revision"],
                now=run.now(),
                explanation="Native source validation measured 2284ms against an unsuitable 1000ms tool reservation; native-only read bound calibrated to 30000ms, Docker unchanged and no cumulative increase",
                retry_plan={
                    "steps": [
                        "Revalidate the same native host and runtime",
                        "Read source under the calibrated finite step bound",
                        "Continue the same Issue's original repair and verification",
                    ],
                    "success_condition": "Source read and repair evidence complete within original cumulative limits",
                },
                retry_limits={
                    "max_attempts": 1,
                    "max_steps": 200,
                    "max_active_ms": 500000,
                },
            )
            with run.services.store.read() as records:
                control = dict(records.one("global_control", singleton=1))
            run.services.control.resume(
                "p6-native-source-read-resume",
                expected_revision=control["revision"],
                now=run.now(),
            )

        def resume_known_failure(run, task_id, configuration):
            with run.services.store.read() as records:
                reasons = [dict(row) for row in records.find("block_reasons")]
                commands = [dict(row) for row in records.find("command_states")]
                task = dict(records.one("tasks", id=task_id))
            if (
                len(reasons) != 1
                or reasons[0]["code"] != "scan_safety_boundary"
                or reasons[0]["task_id"] != task_id
            ):
                raise PermissionError(
                    "Expected one recorded host-publication scan failure"
                )
            details = json.loads(reasons[0]["details_json"])
            if details.get("detail") != "原生宿主实际身份、提升状态或创建时间不匹配":
                raise PermissionError(
                    "Unknown scan failure cannot be retried by this acceptance helper"
                )
            if not commands or any(row["state"] != "stopped" for row in commands):
                raise PermissionError(
                    "Every earlier native command must be verified stopped"
                )
            existing = json.loads(task["configuration_json"])
            strategy = existing.pop("recovery_strategy", None)
            if existing != configuration:
                raise PermissionError(
                    "Known retry cannot replace other task configuration"
                )
            if reasons[0]["resolved"]:
                if (
                    not strategy
                    or strategy.get("operation_id") != "p6-native-host-publication-fix"
                ):
                    raise PermissionError(
                        "Existing bounded retry authorization is missing"
                    )
            else:
                run.services.control.resolve_reason(
                    "p6-native-host-publication-fix",
                    reasons[0]["id"],
                    expected_revision=reasons[0]["revision"],
                    now=run.now(),
                    explanation="Acceptance operator: host exited publishing its completed command; atomic replacement fixed and tested, new independently qualified host bound; previous command stop proof and failure retained",
                    retry_plan={
                        "steps": [
                            "Revalidate the new host identity and pinned code",
                            "Resume the same sample's scan and collect current native results",
                        ],
                        "success_condition": "Native scan completes with recorded runner evidence and no unresolved safety block",
                    },
                    retry_limits={
                        "max_attempts": 1,
                        "max_steps": 100,
                        "max_active_ms": 300000,
                    },
                )
            with run.services.store.read() as records:
                control = dict(records.one("global_control", singleton=1))
                task = dict(records.one("tasks", id=task_id))
            if control["paused"] or task["state"] == "blocked":
                run.services.control.resume(
                    "p6-native-hostfix-resume",
                    expected_revision=control["revision"],
                    now=run.now(),
                )

        p6.execute(
            sample,
            metadata,
            before_resume=resume_known_failure
            if args.resume_host_publication_failure
            else None,
            before_repair_resume=resume_terminal_goal
            if args.resume_native_terminal_goal
            else resume_native_freeze
            if args.resume_native_freeze or args.resume_native_frozen_reference
            else resume_source_budget
            if args.resume_native_source_budget
            else None,
        )
    if args.action in {"run", "verify"}:
        verify(sample, metadata, host)


if __name__ == "__main__":
    main()
