import importlib.util
import json
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def acceptance(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "experiments"))
    spec = importlib.util.spec_from_file_location(
        "p6_acceptance", root / "experiments/p6-github-acceptance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_four_samples_have_real_source_markers_that_disappear_after_fix(acceptance):
    fixtures = acceptance.samples()
    assert set(fixtures) == {"python-cli", "python-web", "javascript-cli", "typescript-web"}
    for sample in fixtures.values():
        assert sample.marker in sample.files[sample.path]
        assert sample.marker not in sample.fixed
        assert sample.probe_path.startswith("tests/")
        assert sample.path != sample.probe_path
        assert "TARGET_BUG" in sample.probe and "TARGET_OK" in sample.probe
        assert sample.probe_argv[0] in {"python", "node"}
        assert sample.route in {"direct", "fork"}


def test_sample_builds_are_declared_without_unavailable_registry_dependencies(acceptance):
    for sample in acceptance.samples().values():
        if "package.json" in sample.files:
            package = json.loads(sample.files["package.json"])
            assert not package.get("dependencies") and not package.get("devDependencies")
            assert package["scripts"]["test"] and package["scripts"]["build"]
    ts = acceptance.samples()["typescript-web"]
    assert "typecheck" in json.loads(ts.files["package.json"])["scripts"]
    assert "/opt/repowatch/typescript/lib/tsc.js" in ts.files["build.mjs"]


def test_operator_api_rejects_other_repositories_before_reading_credentials(acceptance):
    with pytest.raises(PermissionError):
        acceptance.api("direct", "PATCH", "/repos/other/production/pulls/1", {})
    with pytest.raises(PermissionError):
        acceptance.api("direct", "POST", acceptance.PREFIX + "/git/refs", {})


def test_feedback_fixture_returns_json_text_for_the_structured_result(acceptance):
    harness = acceptance.Harness.__new__(acceptance.Harness)
    harness.model_calls = 0
    envelope = {
        "protocol_version": "1",
        "context": {},
        "mode": "distill",
        "objective": "逐条核对新反馈是否推翻原经验",
    }
    request = httpx.Request(
        "POST",
        "https://fixture.invalid/v1/chat/completions",
        json={
            "model": "fixture-model",
            "messages": [
                {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)},
                {"role": "tool", "content": "{}"},
            ],
        },
    )
    response = harness.wire(request).json()
    arguments = json.loads(
        response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    )
    assert isinstance(arguments["summary"], str)
    assert json.loads(arguments["summary"])["unrelated"] is False


@pytest.fixture
def adaptation(acceptance):
    spec = importlib.util.spec_from_file_location(
        "p6_adaptation", acceptance.ROOT / "experiments/p6-adaptation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reports = {}
    for key, sample in acceptance.samples().items():
        reports[key] = {
            "sample": key,
            "route": sample.route,
            "model": "deterministic_http_fixture",
            "paid_model_calls": 0,
            "global_paused_after_acceptance": True,
            "installed_wheel": key == "typescript-web",
            "preparation": {"branch": "p6/" + key},
            "operator_actions": [],
            "actual_pr": {
                "id": "10",
                "head_repository_id": "1447899115" if sample.route == "direct" else "1447913051",
                "base_ref": "p6/" + key,
                "head_sha": "candidate",
            },
            "product": {
                "task_id": key,
                "task_state": "waiting",
                "wait_reason": "pr_review",
                "frozen": {"sha": "candidate"},
                "publication": {"sha": "candidate", "pr_id": "10"},
                "input": {"number": 9},
                "memory_states": ["success"],
                "evidence": {
                    "passed": True,
                    "review_passed": True,
                    "base_probe": {"exit_code": 1},
                    "candidate_probe": {"exit_code": 0},
                },
            },
            "adaptation": {
                "candidate_sha": "candidate",
                "build_exit_code": 0,
                "browser": {"passed": True, "errors": []},
                "cli": {"argv": ["fixture"], "exit_code": 0, "stdout": "5"},
            },
        }
    return module, reports


def test_evidence_summary_keeps_final_gate_and_fixture_limits(adaptation):
    module, reports = adaptation
    result = module.summarize(reports)
    assert result["passed"] and len(result["samples"]) == 4
    assert result["final_product_accepted"] is False
    assert result["model"] == "deterministic_http_fixture"


@pytest.mark.parametrize(
    "damage",
    [
        "stale_sha",
        "browser_error",
        "no_baseline_failure",
        "missing_installed",
        "unpaused",
        "missing_sample",
    ],
)
def test_evidence_summary_refuses_incomplete_or_mismatched_proofs(adaptation, damage):
    module, reports = adaptation
    target = reports["typescript-web"]
    if damage == "stale_sha":
        target["adaptation"]["candidate_sha"] = "old"
    elif damage == "browser_error":
        target["adaptation"]["browser"]["errors"] = ["runtime error"]
    elif damage == "no_baseline_failure":
        target["product"]["evidence"]["base_probe"]["exit_code"] = 0
    elif damage == "missing_installed":
        target["installed_wheel"] = False
    elif damage == "unpaused":
        target["global_paused_after_acceptance"] = False
    else:
        del reports["python-cli"]
    with pytest.raises(ValueError):
        module.summarize(reports)
