"""Register every delta scenario against actual reports without inferring final acceptance."""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGE = ROOT / "openspec/changes/build-github-issue-agent-harness"
CAPABILITIES = {
    "agent-runtime": (
        ["runtime/engine.py", "runtime/prototype.py", "application/runtime_stages.py"],
        ["formal_runtime", "runtime_prototype", "protocols", "memory_runtime"],
        "runtime.md",
    ),
    "deep-repository-scan": (
        ["application/scans.py", "worker/scans.py", "domain/scans.py"],
        ["scans", "repositories", "candidate_issues", "formal_runtime"],
        "workspaces-scans.md",
    ),
    "experience-memory": (
        [
            "application/memory.py",
            "application/memory_context.py",
            "adapters/memory_index.py",
        ],
        [
            "repository_memory",
            "memory_lifecycle",
            "memory_review",
            "memory_runtime",
            "memory_encoder",
            "memory_e12",
        ],
        "memory.md",
    ),
    "github-synchronization": (
        [
            "application/github_sync.py",
            "application/github_polling.py",
            "application/remote_observations.py",
        ],
        [
            "github_sync",
            "github_polling",
            "github_reads",
            "candidate_issues",
            "claim_comments",
        ],
        "local-control.md",
    ),
    "local-control-plane": (
        ["api/app.py", "api/routes.py", "api/operations.py"],
        ["control_api", "operations_api", "task_records", "control", "pr_lifecycle"],
        "local-control.md",
    ),
    "local-operations": (
        [
            "operations/launcher.py",
            "operations/locks.py",
            "operations/maintenance.py",
            "operations/reconcile.py",
            "operations/native.py",
            "operations/native_prepare.py",
            "adapters/native_client.py",
            "_native/host.ps1",
            "_native/kernel.cs",
        ],
        [
            "local_operations",
            "restore_reconciliation",
            "operations_api",
            "business_storage",
            "artifacts",
            "native_client",
            "native_workspaces",
            "native_runner",
            "native_protocol",
        ],
        "local-operations.md",
    ),
    "model-provider-management": (
        ["application/providers.py", "application/roles.py", "adapters/providers.py"],
        [
            "providers",
            "provider_adapter",
            "provider_probe",
            "credentials",
            "roles",
            "model_calls",
        ],
        "providers.md",
    ),
    "pull-request-lifecycle": (
        ["application/publication.py", "application/pr_lifecycle.py"],
        [
            "publication",
            "pr_lifecycle",
            "issue_context",
            "repairs",
            "restore_reconciliation",
        ],
        "local-control.md",
    ),
    "repair-verification": (
        [
            "application/repair_evidence.py",
            "worker/repairs.py",
            "domain/verification.py",
        ],
        ["verification", "repairs", "triage", "formal_triage", "task_records"],
        "local-control.md",
    ),
    "repository-workspaces": (
        [
            "application/repositories.py",
            "application/workspaces.py",
            "adapters/docker_workspaces.py",
            "adapters/git_objects.py",
            "adapters/native_workspaces.py",
            "adapters/workspace_backends.py",
            "worker/native_runner.py",
        ],
        [
            "repositories",
            "workspaces",
            "git_objects",
            "git_handoff",
            "dependency_runner",
            "local_operations",
            "native_workspaces",
            "native_client",
            "native_runner",
            "native_protocol",
        ],
        "workspaces-scans.md",
    ),
    "task-orchestration": (
        [
            "application/orchestration.py",
            "application/budgets.py",
            "application/control.py",
            "application/recovery.py",
        ],
        [
            "orchestration",
            "budgets",
            "progress",
            "control",
            "recovery",
            "worker_service",
            "model_calls",
        ],
        "control-recovery.md",
    ),
}
EXPERIMENTS = {
    "E1": ["provider_probe", "provider_adapter", "roles"],
    "E2": ["scans", "formal_runtime"],
    "E3": ["scans", "orchestration"],
    "E4": ["verification", "repairs"],
    "E5": ["runtime_prototype", "repairs"],
    "E6": ["verification", "repairs"],
    "E7": ["orchestration", "progress"],
    "E8": ["remote_recovery", "publication", "recovery"],
    "E9": ["repositories", "scans", "publication"],
    "E10": ["memory_lifecycle", "memory_encoder"],
    "E11": [
        "git_handoff",
        "workspaces",
        "local_operations",
        "native_client",
        "native_workspaces",
        "native_runner",
        "native_protocol",
    ],
    "E12": ["memory_e12"],
    "E13": ["github_sync", "orchestration", "scheduling_prototype"],
    "E14": ["budgets", "model_calls", "progress", "formal_runtime"],
    "E15": ["git_handoff", "workspaces", "dependency_runner", "repairs"],
    "E16": ["memory_lifecycle", "memory_runtime", "memory_review"],
    "E17": ["github_polling", "pr_lifecycle", "publication", "task_records"],
    "E18": ["control", "recovery", "worker_service", "local_operations"],
    "E19": ["triage", "issue_context", "candidate_issues", "claim_comments"],
    "E20": [
        "business_storage",
        "artifacts",
        "providers",
        "local_operations",
        "restore_reconciliation",
    ],
}


def read_reports(paths):
    cases = {}
    reports = []
    for path in paths:
        tree = ET.parse(path)
        count = 0
        for case in tree.iter("testcase"):
            module = case.get("classname", "").split(".")[-1].removeprefix("test_")
            identity = module + "::" + case.get("name", "")
            result = (
                "failed"
                if case.find("failure") is not None or case.find("error") is not None
                else "skipped"
                if case.find("skipped") is not None
                else "passed"
            )
            cases[identity] = {
                "module": module,
                "test": case.get("name"),
                "result": result,
                "report": path.relative_to(ROOT).as_posix(),
            }
            count += 1
        reports.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "testcases": count,
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "suite_timestamps": sorted(
                    {
                        node.get("timestamp")
                        for node in tree.iter("testsuite")
                        if node.get("timestamp")
                    }
                ),
            }
        )
    return cases, reports


def evidence(cases, modules):
    selected = {key: row for key, row in cases.items() if row["module"] in modules}
    absent = [
        module
        for module in modules
        if not any(row["module"] == module for row in selected.values())
    ]
    counts = {
        state: sum(row["result"] == state for row in selected.values())
        for state in ("passed", "failed", "skipped")
    }
    return {
        "testcase_ids": sorted(selected),
        "counts": counts,
        "missing_modules": absent,
        "result": "passed_in_recorded_scope"
        if selected and not absent and not counts["failed"] and not counts["skipped"]
        else "not_passed_or_incomplete",
    }


def build(paths):
    cases, reports = read_reports(paths)
    adaptation_path = ROOT / "experiments/results/p6-adaptation.json"
    adaptation = (
        json.loads(adaptation_path.read_text("utf-8"))
        if adaptation_path.is_file()
        else {}
    )
    adapted = adaptation.get("passed") is True and set(
        adaptation.get("samples", {})
    ) == {"python-cli", "python-web", "javascript-cli", "typescript-web"}
    native_path = ROOT / "experiments/results/p6-native-product.json"
    native = json.loads(native_path.read_text("utf-8")) if native_path.is_file() else {}
    native_passed = bool(
        native.get("passed")
        and native.get("native_admitted")
        and native.get("installed_wheel")
        and native.get("global_paused_after_acceptance")
        and native.get("adaptation", {}).get("all_product_commands_native")
        and native.get("adaptation", {}).get("cli_stdout") == "5"
    )
    capabilities = {}
    for capability, (implementation, modules, document) in CAPABILITIES.items():
        path = CHANGE / "specs" / capability / "spec.md"
        requirement, scenarios = None, []
        for line_number, line in enumerate(path.read_text("utf-8").splitlines(), 1):
            if line.startswith("### Requirement: "):
                requirement = line.removeprefix("### Requirement: ")
            if line.startswith("#### Scenario: "):
                title = line.removeprefix("#### Scenario: ")
                sample_gate = (
                    capability == "local-operations" and title == "样本包含前后端应用"
                )
                gate = capability == "local-operations" and (
                    title == "发布最终本地版本" or (sample_gate and not adapted)
                )
                scenarios.append(
                    {
                        "title": title,
                        "requirement": requirement,
                        "source": path.relative_to(ROOT).as_posix(),
                        "line": line_number,
                        "evidence_group": capability,
                        "result": "controlled_release_evidence_ready_user_acceptance_not_recorded"
                        if capability == "local-operations"
                        and title == "发布最终本地版本"
                        and adapted
                        and native_passed
                        else "not_passed_pending_final_acceptance"
                        if gate
                        else "controlled_end_to_end_recorded"
                        if sample_gate and adapted
                        else "related_controlled_regression_recorded",
                        **(
                            {
                                "direct_evidence": "experiments/results/p6-adaptation.json"
                            }
                            if sample_gate and adapted
                            else {}
                        ),
                        "scope": "关联能力回归已登记；不把模块测试数量当作该场景独立覆盖或真实模型／生产环境证明",
                    }
                )
        files = ["backend/src/repowatch/" + item for item in implementation]
        files += ["backend/tests/test_" + item + ".py" for item in modules]
        files += ["docs/development/" + document]
        missing = [item for item in files if not (ROOT / item).is_file()]
        capabilities[capability] = {
            "implementation": files[: len(implementation)],
            "tests": ["backend/tests/test_" + item + ".py" for item in modules],
            "document": "docs/development/" + document,
            "missing_paths": missing,
            "regression": evidence(cases, modules),
            "scenarios": scenarios,
        }
    unchecked = re.findall(
        r"^- \[ \] (\d+\.\d+) (.+)$",
        (CHANGE / "tasks.md").read_text("utf-8"),
        re.MULTILINE,
    )
    return {
        "verified_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "scope": "P6 受控实现／回归证据登记；逐场景记录关联证据及未通过项，非最终发布或用户验收声明",
        "reports": reports,
        "testcases": cases,
        "capabilities": capabilities,
        "experiments": {
            key: {
                **evidence(cases, modules),
                "scope": "所列报告的受控范围（日期见报告时间与 NOW）；真实供应商历史、费用及最终适配边界另见 NOW",
            }
            for key, modules in EXPERIMENTS.items()
        },
        "current_e12_report": ".cache/p6-e12-memory.json",
        "sample_adaptation": {
            "passed_in_recorded_scope": adapted,
            "path": "experiments/results/p6-adaptation.json",
            "sha256": sha256(adaptation_path.read_bytes()).hexdigest()
            if adaptation
            else None,
        },
        "native_product_qualification": {
            "passed_in_recorded_scope": native_passed,
            "path": "experiments/results/p6-native-product.json",
            "sha256": sha256(native_path.read_bytes()).hexdigest() if native else None,
        },
        "implementation_and_controlled_acceptance_complete": not unchecked
        and adapted
        and native_passed,
        "remaining_tasks": [
            {"id": identifier, "description": description}
            for identifier, description in unchecked
        ],
        "limitations": [
            "真实付费模型效果未在本轮复跑，E1 真实矩阵沿用既有历史证据，费用待核实",
            "默认语义编码选型未过真实中文召回门槛，关键词降级；E12 受控结果未证明提效",
            "原生专用账户的固定 CPython 标准库 CLI 完整链路已通过；不泛化为任意原生项目，运行时仍须实时准入"
            if native_passed
            else "原生受支持宿主完整样本尚未通过",
            "四类小型 Python／JS／TS Web／CLI 完整远端夹具链路已验证，非任意仓库或真实模型效果证明"
            if adapted
            else "完整 Python／JS／TS Web／CLI 远端链路仍待最终验收",
            "缺 LFS 实体／子模块对象仍阻塞，不将对象交接实验当作交付支持",
            "关联模块测试不自动证明每一场景独立覆盖，未登记逐场景独立执行结果的项保留审查边界",
        ],
        "final_product_accepted": False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, nargs="+", required=True)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "experiments/results/p6-acceptance.json"
    )
    args = parser.parse_args()
    result = build([path.resolve() for path in args.junit])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    missing = [
        key for key, value in result["capabilities"].items() if value["missing_paths"]
    ]
    print(
        json.dumps(
            {
                "capabilities": len(result["capabilities"]),
                "scenarios": sum(
                    len(item["scenarios"]) for item in result["capabilities"].values()
                ),
                "missing_paths": missing,
                "final_product_accepted": False,
            },
            ensure_ascii=False,
        )
    )
    return 2 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
