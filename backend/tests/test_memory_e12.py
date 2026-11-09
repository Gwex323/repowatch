import ast
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
from langchain_core.tools import tool
from test_memory_runtime import prepare
from test_repository_memory import CONTENT
from test_roles import kernel as kernel

from repowatch.adapters.memory_index import MemoryIndex
from repowatch.adapters.providers import ProviderAdapter
from repowatch.application.task_records import TaskRecords
from repowatch.domain.budget import BudgetAmounts
from repowatch.domain.task_records import canonical_json
from repowatch.runtime.engine import FormalRuntime, StagePlan, ToolBinding


def source_tool(code):
    @tool
    def read_source(path: str) -> str:
        """读取本次待修复函数；不提供隐藏测试或当前答案。"""
        assert path == "case.py"
        return code

    return read_source


def test_e12_thirty_temporal_repository_isolated_pairs(kernel, tmp_path):
    services = kernel
    source_task, token = prepare(services)
    memory_id = services.memories.record_verified("memory-intent", CONTENT, now=0)
    memory = services.memories.get(memory_id)
    services.memories.operate(
        "share-e12-seed",
        memory_id,
        expected_revision=memory["revision"],
        action="share",
        data={"content": CONTENT, "confirmed_sanitized": True},
    )
    services.queue.complete(token, now=0, state="ended")
    with services.store.read() as records:
        source_repository = records.one("tasks", id=source_task)["repository_id"]
    services.memory_index = MemoryIndex(services.settings, services.memories)
    services.memory_index.rebuild()
    tasks = TaskRecords(services.store)
    reports, dataset = [], []
    moment = [1]
    services.clock = lambda: moment[0]

    def execute(code, function, pairs, name):
        path = tmp_path / (name + ".py")
        checks = "\n".join(
            f"assert {function}({left},{right}) == {left * right}" for left, right in pairs
        )
        path.write_text(code + "\n" + checks + "\n", encoding="utf-8")
        return subprocess.run([sys.executable, "-I", str(path)], capture_output=True, timeout=10)

    for case in range(30):
        name = f"product_case_{case}"
        code = f"def {name}(left, right):\n    return left + right\n"
        query = "乘法返回积，保留零与负数行为"
        tests = [(case + 2, case + 3), (-case - 1, 3), (case + 1, 0)]
        item = {
            "id": case,
            "scope": "same" if case % 2 == 0 else "cross",
            "source_sha256": hashlib.sha256(code.encode()).hexdigest(),
            "query": query,
            "source": code,
            "hidden_test_count": len(tests),
        }
        dataset.append(item)
        before = execute(code, name, tests, f"before-{case}")
        assert before.returncode != 0
        repository = (
            source_repository
            if case % 2 == 0
            else tasks.register_repository(f"held-out-e12-{case}", admission_mode="all_bugs")
        )
        for enabled in (False, True):
            identifier = tasks.ensure_task(
                repository, f"e12-{case}-{enabled}", mode="triage", now=moment[0]
            )
            with services.store.write() as records:
                records.change(
                    "tasks",
                    {"id": identifier},
                    {
                        "configuration_json": canonical_json(
                            {
                                "memory_enabled": enabled,
                                "code_version": {
                                    "base_sha": item["source_sha256"],
                                    "tree_sha": item["source_sha256"],
                                    "content_manifest_sha256": item["source_sha256"],
                                },
                            }
                        )
                    },
                )
            services.budgets.authorize_initial(
                f"e12-budget-{case}-{enabled}",
                identifier,
                BudgetAmounts(
                    active_ms=600000,
                    model_calls=5,
                    tokens=200000,
                    repair_rounds=2,
                    recovery_rounds=2,
                ),
                expected_revision=1,
                reason="E12 确定性隔离对照",
                now=moment[0],
            )
            current = services.queue.claim(now=moment[0])
            assert current is not None
            with services.store.read() as records:
                assert records.one("runs", id=current.run_id)["task_id"] == identifier
            requests = []

            read_source = source_tool(code)

            def wire(request, requests=requests):
                payload = json.loads(request.content)
                requests.append(payload)
                envelope = next(
                    json.loads(message["content"])
                    for message in payload["messages"]
                    if message["role"] == "user"
                    and message.get("content", "").startswith('{"protocol_version"')
                )
                tool_results = [
                    message for message in payload["messages"] if message["role"] == "tool"
                ]
                if not tool_results:
                    function_name, arguments = "read_source", {"path": "case.py"}
                else:
                    visible = tool_results[-1]["content"]
                    tree = ast.parse(visible)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                            node.op = ast.Mult()
                    candidate = ast.unparse(tree)
                    function_name, arguments = (
                        "StructuredResult",
                        {
                            "context": envelope["context"],
                            "mode": envelope["mode"],
                            "status": "completed",
                            "summary": candidate,
                            "artifacts": [],
                            "unresolved": [],
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "id": "e12",
                        "object": "chat.completion",
                        "created": 0,
                        "model": payload["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": str(len(requests)),
                                            "type": "function",
                                            "function": {
                                                "name": function_name,
                                                "arguments": json.dumps(arguments),
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": len(json.dumps(payload)) // 4,
                            "completion_tokens": len(json.dumps(arguments)) // 4,
                            "total_tokens": (len(json.dumps(payload)) + len(json.dumps(arguments)))
                            // 4,
                        },
                    },
                )

            outcomes = []

            def verify(
                result,
                events,
                name=name,
                tests=tests,
                case=case,
                enabled=enabled,
                outcomes=outcomes,
            ):
                outcome = execute(result.summary, name, tests, f"candidate-{case}-{enabled}")
                review = execute(
                    result.summary, name, [(0, 9), (-5, -6), (7, 1)], f"review-{case}-{enabled}"
                )
                passed = outcome.returncode == 0 and review.returncode == 0
                outcomes.append(passed)
                return {
                    "comparison": {
                        "validation_passed": passed,
                        "independent_probe_passed": review.returncode == 0,
                    },
                    "verified_fact_ids": [],
                }

            services.adapter = ProviderAdapter(
                services.providers, transport=httpx.MockTransport(wire)
            )
            started = time.perf_counter()
            FormalRuntime(services).run(
                current,
                StagePlan(
                    f"e12-{case}",
                    f"e12-{case}",
                    query,
                    "investigate",
                    "processor",
                    (ToolBinding(read_source, BudgetAmounts(active_ms=1000)),),
                    verify,
                    record_attempt=False,
                ),
            )
            elapsed = time.perf_counter() - started
            services.queue.complete(current, now=moment[0], state="ended")
            budget = services.budgets.view(identifier)["used"]
            reports.append(
                {
                    "case": case,
                    "memory": enabled,
                    "scope": item["scope"],
                    "task_id": identifier,
                    "run_id": current.run_id,
                    "source_task_id": source_task,
                    "source_precedes_target": True,
                    "passed": outcomes[-1],
                    "seconds": elapsed,
                    "model_calls": budget["model_calls"],
                    "fixture_usage_tokens": budget["tokens"],
                    "memory_injected": any(
                        "独立记忆参考块" in json.dumps(payload, ensure_ascii=False)
                        for payload in requests
                    ),
                }
            )
            moment[0] += 1
    assert len(reports) == 60
    assert all(row["passed"] for row in reports)
    assert all(row["memory_injected"] == row["memory"] for row in reports)
    summaries = {}
    for enabled in (False, True):
        rows = [row for row in reports if row["memory"] == enabled]
        summaries["with_memory" if enabled else "without_memory"] = {
            "cases": len(rows),
            "validation_pass_rate": sum(row["passed"] for row in rows) / len(rows),
            "seconds": sum(row["seconds"] for row in rows),
            "model_calls": sum(row["model_calls"] for row in rows),
            "fixture_usage_tokens": sum(row["fixture_usage_tokens"] for row in rows),
        }
    root = Path(__file__).resolve().parents[2]
    report = {
        "experiment": "E12",
        "dataset": dataset,
        "dataset_sha256": hashlib.sha256(canonical_json(dataset).encode()).hexdigest(),
        "seed_sources": [source_task],
        "same_repository_cases": 15,
        "cross_repository_cases": 15,
        "model": "deterministic_http_ast_fixture",
        "summary": summaries,
        "runs": reports,
        "limitations": (
            "单一算术缺陷族的受控对照；真实 SDK、业务预算与子进程验证，"
            "HTTP 模型为确定性 AST 夹具。token 为夹具回报，非账单。"
            "不能证明真实模型收益或生产泛化；20% 仅为目标。"
        ),
    }
    (root / "experiments/results/e12-memory.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
