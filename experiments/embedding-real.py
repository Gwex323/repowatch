import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from repowatch.runtime import embedding_probe as probe

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "Gwex323/repowatch-p3-sandbox"
NUMBERS = (1, 2, 3, 4, 15, 16, 17, 18)
QUERIES = (
    ("加法函数错误地做了减法", "1", "zh"),
    ("直接发布路径修复加法的拉取请求", "2", "zh"),
    ("通过用户派生仓库修复加法的拉取请求", "4", "zh"),
    ("乘法应返回两数的积", "15", "zh"),
    ("sub(5, 2) should return 3", "17", "code"),
    ("mul(2, 0) must equal zero", "15", "code"),
    ("add(2, 3) returns -1 instead of 5", "1", "code"),
    ("addition must preserve zero and negative operands", "3", "code"),
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def verify_snapshot(snapshot, asset):
    actual = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file()
    }
    if actual != set(asset["files"]):
        raise ValueError(f"Model file inventory mismatch: {asset['model']}")
    for filename, expected in asset["files"].items():
        if digest(snapshot / filename) != expected:
            raise ValueError(f"Model digest mismatch: {asset['model']}:{filename}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("capture", "lock", "evaluate"))
    args = parser.parse_args()
    corpus_path = ROOT / "experiments/fixtures/embedding-real.json"
    lock_path = ROOT / "experiments/embedding-assets.lock.json"
    if args.action == "capture":
        records = []
        for number in NUMBERS:
            payload = subprocess.check_output(
                ["gh", "api", f"repos/{REPOSITORY}/issues/{number}"], encoding="utf-8"
            )
            item = json.loads(payload)
            records.append(
                {
                    key: item[key]
                    for key in ("number", "title", "body", "html_url", "updated_at")
                }
            )
        save(
            corpus_path,
            {
                "captured_at": datetime.now(UTC).isoformat(),
                "repository": REPOSITORY,
                "limitations": "受控算术夹具真实远端记录；人工查询；非生产泛化或 E12 对照集",
                "documents": records,
                "queries": QUERIES,
            },
        )
        return
    if args.action == "lock":
        assets = []
        for name in probe.CANDIDATES:
            source = (
                name if not name.startswith("BAAI/") else "Qdrant/bge-small-zh-v1.5"
            )
            directory = (
                ROOT / ".cache/fastembed" / ("models--" + source.replace("/", "--"))
            )
            revision = (directory / "refs/main").read_text().strip()
            snapshot = directory / "snapshots" / revision
            files = {
                path.relative_to(snapshot).as_posix(): digest(path)
                for path in sorted(snapshot.rglob("*"))
                if path.is_file()
            }
            if not files or not any(path.endswith(".onnx") for path in files):
                raise ValueError(f"Incomplete model: {name}")
            assets.append(
                {"model": name, "source": source, "revision": revision, "files": files}
            )
        save(lock_path, {"version": 1, "models": assets})
        return
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    probe.DOCS = tuple(
        (str(item["number"]), item["title"] + "\n" + (item["body"] or ""))
        for item in corpus["documents"]
    )
    probe.QUERIES = tuple(tuple(item) for item in corpus["queries"])
    results = []
    for asset in json.loads(lock_path.read_text())["models"]:
        snapshot = (
            ROOT
            / ".cache/fastembed"
            / ("models--" + asset["source"].replace("/", "--"))
        )
        snapshot = snapshot / "snapshots" / asset["revision"]
        verify_snapshot(snapshot, asset)
        result = probe.evaluate_model(
            asset["model"], ROOT / ".cache/fastembed", None, snapshot
        )
        result["revision"] = asset["revision"]
        results.append(result)
    import importlib.metadata

    report = {
        "evaluated_at": datetime.now(UTC).isoformat(),
        "corpus_sha256": digest(corpus_path),
        "asset_lock_sha256": digest(lock_path),
        "dependency_lock_sha256": digest(ROOT / "backend/uv.lock"),
        "script_sha256": digest(Path(__file__)),
        "evaluator_sha256": digest(Path(probe.__file__)),
        "runtime": {
            name: importlib.metadata.version(name)
            for name in ("fastembed", "onnxruntime", "psutil", "sqlite-vec")
        },
        "components": probe.component_probe(),
        "models": results,
        "selection": probe.select_model(results),
        "limitations": corpus["limitations"],
    }
    save(ROOT / "experiments/results/embedding-real-20261109.json", report)
    print(json.dumps(report["selection"], ensure_ascii=False))
    if any(item.get("error") for item in results):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
