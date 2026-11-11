"""Publish redacted evidence only when every real P6 sample has passed."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from p6_samples import samples

ROOT = Path(__file__).resolve().parents[1]


def summarize(reports):
    expected = samples()
    if set(reports) != set(expected):
        raise ValueError("All four sample reports are required")
    result = {}
    for key, sample in expected.items():
        report = reports[key]
        product = report["product"]
        proof = product["evidence"]
        actual = report["actual_pr"]
        adaptation = report["adaptation"]
        candidate = product["frozen"]["sha"]
        head_repository = "1447899115" if sample.route == "direct" else "1447913051"
        if not (
            report["sample"] == key
            and report["route"] == sample.route
            and report["model"] == "deterministic_http_fixture"
            and report["paid_model_calls"] == 0
            and report["global_paused_after_acceptance"]
            and product["task_state"] == "waiting"
            and product["wait_reason"] == "pr_review"
            and product["publication"]["sha"] == candidate
            and product["publication"]["pr_id"] == actual["id"]
            and actual["head_repository_id"] == head_repository
            and actual["base_ref"] == report["preparation"]["branch"]
            and actual["head_sha"] == candidate == adaptation["candidate_sha"]
            and proof["passed"]
            and proof["review_passed"]
            and proof["base_probe"]["exit_code"] != 0
            and proof["candidate_probe"]["exit_code"] == 0
            and "success" in product["memory_states"]
            and all(
                state in {"success", "failure", "pending_review"}
                for state in product["memory_states"]
            )
            and adaptation["build_exit_code"] == 0
        ):
            raise ValueError(f"Incomplete or inconsistent product evidence: {key}")
        if sample.preview:
            if not adaptation["browser"]["passed"] or adaptation["browser"]["errors"]:
                raise ValueError(f"Browser acceptance failed: {key}")
        elif adaptation["cli"] != {
            "argv": adaptation["cli"]["argv"],
            "exit_code": 0,
            "stdout": "5",
        }:
            raise ValueError(f"Actual CLI acceptance failed: {key}")
        result[key] = {
            "route": sample.route,
            "issue": {
                "number": product["input"]["number"],
                "url": "https://github.com/Gwex323/repowatch-p3-sandbox/issues/"
                + str(product["input"]["number"]),
            },
            "pr": actual,
            "preparation": {
                k: v for k, v in report["preparation"].items() if k != "reference"
            },
            "task_id": product["task_id"],
            "candidate_sha": candidate,
            "base_probe_exit_code": proof["base_probe"]["exit_code"],
            "candidate_probe_exit_code": proof["candidate_probe"]["exit_code"],
            "independent_review_passed": proof["review_passed"],
            "memory_states": product["memory_states"],
            "adaptation": adaptation,
            "installed_wheel": report.get("installed_wheel", False),
            "global_paused_after_acceptance": True,
            "operator_actions": report["operator_actions"],
            "prior_failures": report.get("prior_failures", []),
            "prior_failures_note": report.get("prior_failures_note"),
            "installed_package": report.get("installed_package"),
            "private_report": f".repowatch/p6-{key}/report.json",
            "private_report_sha256": sha256(
                json.dumps(report, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
        }
    if not result["typescript-web"]["installed_wheel"]:
        raise ValueError(
            "The installed wheel must complete the TypeScript remote chain"
        )
    return {
        "verified_at": datetime.now(UTC).isoformat(),
        "passed": True,
        "environment": "Windows + Docker Desktop Linux containers + Microsoft Edge",
        "model": "deterministic_http_fixture",
        "paid_model_calls": 0,
        "samples": result,
        "scope": "Small offline-installable Python and JS/TS Web/CLI fixtures; actual scan, Issue, repair, review, PR, memory, build and runtime",
        "limitations": [
            "Model outputs are fixtures; this does not establish paid model repair efficacy",
            "No arbitrary registry dependencies, private services or specialized hardware verified",
            "Native product acceptance has a separate gate; current status is recorded in NOW and the native reports",
            "PRs remain open for review; no merge or release was performed",
        ],
        "final_product_accepted": False,
    }


def main():
    reports = {
        key: json.loads((ROOT / f".repowatch/p6-{key}/report.json").read_text("utf-8"))
        for key in samples()
    }
    result = summarize(reports)
    output = ROOT / "experiments/results/p6-adaptation.json"
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"passed": True, "samples": list(result["samples"])}))


if __name__ == "__main__":
    main()
