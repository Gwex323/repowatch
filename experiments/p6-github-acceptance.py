"""P6 sandbox 验收：模型 HTTP 夹具；产品扫描／修复／发布、Git 与 Docker 均真实。

准备只新增专用分支；不修改 main、不合并或关闭 PR、不调用付费模型。
"""

import argparse
import importlib.util
import json
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import httpx
import repowatch
from p6_samples import samples
from repowatch.adapters.credentials import WindowsCredentialVault
from repowatch.adapters.docker_workspaces import DockerWorkspaces
from repowatch.adapters.git_objects import GitObjects
from repowatch.adapters.providers import ProviderAdapter
from repowatch.api.host import HostServices
from repowatch.application.credentials import CredentialError
from repowatch.configuration import StorageSettings
from repowatch.domain.task_records import canonical_json, content_digest
from repowatch.operations.launcher import write_json
from repowatch.operations.migrate import migrate
from repowatch.worker.service import Worker

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ("Gwex323", "repowatch-p3-sandbox")
PREFIX = "/repos/" + "/".join(UPSTREAM)
spec = importlib.util.spec_from_file_location(
    "p3_fixture", ROOT / "experiments/p3-github-acceptance.py"
)
p3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p3)


def namespace(path):
    return sha256(str(path.resolve()).casefold().encode()).hexdigest()[:40]


def old_vault(route):
    return WindowsCredentialVault(namespace(ROOT / ".repowatch" / ("p3-" + route)))


def old_reference(route):
    return "github-gwex323" if route == "direct" else "github-gwx541"


def api(route, method, path, payload=None):
    if not (path == "/user" or path.startswith(PREFIX + "/")):
        raise PermissionError("P6 操作限于已授权 sandbox")
    if method != "GET" and not path.startswith(
        (PREFIX + "/pulls/", PREFIX + "/issues/")
    ):
        raise PermissionError("准备器只能修改本次已确认的 P6 测试对象元数据")
    secret = old_vault(route).read(old_reference(route))
    with httpx.Client(
        timeout=60,
        trust_env=False,
        headers={
            "Authorization": "Bearer " + secret.reveal(),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    ) as client:
        result = client.request(method, "https://api.github.com" + path, json=payload)
        if not result.is_success:
            raise RuntimeError(f"P6 GitHub {method}: HTTP {result.status_code}")
        return result.json()


def typescript_image():
    source = (ROOT / "frontend/node_modules/typescript").resolve(strict=True)
    fingerprint = sha256()
    for file in sorted(source.rglob("*")):
        if file.is_file():
            fingerprint.update(file.relative_to(source).as_posix().encode())
            fingerprint.update(file.read_bytes())
    digest = fingerprint.hexdigest()
    image = "repowatch-p6-typescript:" + digest[:12]
    inspected = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, check=False
    )
    if inspected.returncode == 0:
        return image
    directory = ROOT / ".cache/p6-typescript-image" / digest[:12]
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, directory / "typescript", dirs_exist_ok=True)
    (directory / "Dockerfile").write_text(
        "FROM node:24-bookworm-slim\nCOPY typescript /opt/repowatch/typescript\n"
        "RUN node /opt/repowatch/typescript/lib/tsc.js --version\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "docker",
            "build",
            "--pull=false",
            "--network=none",
            "-t",
            image,
            str(directory),
        ],
        check=True,
        timeout=180,
    )
    return image


def prepare(sample):
    directory = ROOT / ".repowatch" / ("p6-" + sample.key)
    directory.mkdir(parents=True, exist_ok=True)
    metadata_file = directory / "acceptance.json"
    if metadata_file.is_file():
        return json.loads(metadata_file.read_text("utf-8"))
    image = typescript_image() if sample.key == "typescript-web" else sample.image
    objects = GitObjects(directory / "preparation-objects")
    parent = objects.fetch(
        "sandbox",
        *UPSTREAM,
        "main",
        secret=old_vault("direct").read(old_reference("direct")),
    )
    branch = "p6/acceptance-20261110-" + sample.key
    commit = objects.commit_files(
        "sandbox",
        parent,
        {path: ("100644", data.encode()) for path, data in sample.files.items()},
        message="P6: prepare isolated " + sample.key + " acceptance sample",
    )
    remote = (
        objects.run(
            objects.repository("sandbox"),
            "ls-remote",
            "--heads",
            "https://github.com/" + "/".join(UPSTREAM) + ".git",
            "refs/heads/" + branch,
            secret=old_vault("direct").read(old_reference("direct")),
        )
        .decode()
        .strip()
    )
    if remote:
        existing = objects.fetch(
            "sandbox",
            *UPSTREAM,
            branch,
            secret=old_vault("direct").read(old_reference("direct")),
        )
        tree = (
            objects.run(
                objects.repository("sandbox"), "rev-parse", existing + "^{tree}"
            )
            .decode()
            .strip()
        )
        if tree != commit["tree_sha"]:
            raise PermissionError("已有 P6 样本分支内容不同，拒绝覆盖")
        commit["sha"] = existing
    else:
        objects.run(
            objects.repository("sandbox"),
            "push",
            "--porcelain",
            "https://github.com/" + "/".join(UPSTREAM) + ".git",
            commit["sha"] + ":refs/heads/" + branch,
            secret=old_vault("direct").read(old_reference("direct")),
        )
    settings = StorageSettings(directory / "data")
    if not settings.business_database.exists():
        migrate(settings)
    vault = WindowsCredentialVault(namespace(settings.data_dir))
    reference = "github-p6-" + sample.route
    try:
        vault.read(reference)
    except CredentialError:
        vault.create(
            reference, old_vault(sample.route).read(old_reference(sample.route))
        )
    result = {
        "sample": sample.key,
        "route": sample.route,
        "branch": branch,
        "base_sha": commit["sha"],
        "tree_sha": commit["tree_sha"],
        "image": image,
        "reference": reference,
        "prepared_by": "acceptance_operator",
        "github_writes": True,
        "fixture_files_sha256": content_digest(sample.files),
    }
    write_json(metadata_file, result)
    return result


class Harness(p3.Acceptance):
    def __init__(self, sample, metadata):
        self.sample, self.metadata = sample, metadata
        self.settings = StorageSettings(
            ROOT / ".repowatch" / ("p6-" + sample.key) / "data"
        )
        self.services = HostServices(self.settings, clock=time.time)
        self.services.adapter = ProviderAdapter(
            self.services.providers, transport=httpx.MockTransport(self.wire)
        )
        self.owner, self.name = UPSTREAM
        self.reference, self.fork = metadata["reference"], sample.route == "fork"
        self.worker = Worker(self.services)
        self.model_calls = 0

    def now(self):
        return time.time()

    def wire(self, request):
        self.model_calls += 1
        body = json.loads(request.content)
        envelope = p3.envelope_from(body["messages"])
        if envelope is None:
            raise AssertionError("unexpected fixture model request")
        if "__probe__" in envelope:
            return p3.wire(request)
        results = p3.tool_results(body["messages"])
        count = sum(item.get("role") == "tool" for item in body["messages"])
        mode = envelope["mode"]
        if mode == "scan":
            if count == 0:
                return p3.reply(body, "read_source", {})
            if count == 1:
                payload = results[0]
                source = (
                    bytes.fromhex(payload["content_hex"])
                    if "content_hex" in payload
                    else payload["content"].encode()
                )
                unit = payload["unit"]
                offset = source.find(self.sample.marker.encode())
                findings = []
                if unit["path"] == self.sample.path and offset >= 0:
                    findings = [
                        {
                            "title": "P6 "
                            + self.sample.key
                            + ": addition returns a difference",
                            "basis": "Actual supplied implementation subtracts despite the declared sum contract",
                            "start": unit["start"] + offset,
                            "end": unit["start"] + offset + len(self.sample.marker),
                        }
                    ]
                return p3.reply(
                    body,
                    "write_analysis",
                    {
                        "analysis": "Reviewed the entire supplied chunk",
                        "input_sha256": unit["sha256"],
                        "findings": findings,
                        "complete": True,
                    },
                )
        elif mode == "investigate":
            if count == 0:
                return p3.reply(body, "read_evidence", {})
            if count == 1:
                context = results[0].get("remote_context") or {}
                marker = "完整 Issue 查重结果："
                lookup = None
                if marker in envelope["objective"]:
                    lookup, _ = json.JSONDecoder().raw_decode(
                        envelope["objective"].split(marker, 1)[1]
                    )
                matches = [
                    item
                    for item in (lookup or {}).get("issues", [])
                    if item.get("title", "").startswith("P6 " + self.sample.key + ":")
                ]
                relation = {
                    "relation": "same"
                    if matches
                    else "none"
                    if lookup is not None
                    else "uncertain",
                    "evidence": "Compared all returned issue pages using this fixture's unique P6 scope",
                }
                if matches:
                    relation["issue_number"] = min(
                        matches, key=lambda item: item["number"]
                    )["number"]
                prs = [
                    {
                        "id": str(pull["id"]),
                        "relation": "same"
                        if pull.get("base", {}).get("ref") == self.metadata["branch"]
                        and any(
                            file.get("filename") == self.sample.path
                            for file in pull.get("files", [])
                        )
                        else "unrelated",
                        "evidence": "Compared target branch and actual changed file paths for this isolated sample",
                    }
                    for pull in context.get("facts", {}).get("pull_requests", [])
                ]
                analysis = {
                    "kind": "bug",
                    "objective": "P6 " + self.sample.key + ": return the sum",
                    "criteria": [
                        "Adding 2 and 3 returns 5 through the supported interface"
                    ],
                    "reproduction_steps": ["Run the tracked test and the target probe"],
                    "ownership": "available",
                    "ownership_evidence": [
                        "Current controlled issue has no other assignee or claim"
                    ],
                    "issue_relation": relation,
                    "pr_relations": prs,
                }
                return p3.reply(body, "write_analysis", {"analysis": analysis})
        elif mode == "reproduce":
            if count == 0:
                return p3.reply(body, "read_source", {"path": self.sample.path})
            if count == 1:
                return p3.reply(
                    body,
                    "write_analysis",
                    {
                        "plan": {
                            "files": {self.sample.probe_path: self.sample.probe},
                            "argv": self.sample.probe_argv,
                            "target_failure": "TARGET_BUG",
                            "target_success": "TARGET_OK",
                            "purpose": "Same sum probe on base and frozen candidate",
                        }
                    },
                )
        elif mode == "implement":
            if count == 0:
                return p3.reply(body, "read_source", {"path": self.sample.path})
            if count == 1:
                return p3.reply(
                    body,
                    "write_source",
                    {"path": self.sample.path, "content": self.sample.fixed},
                )
        elif mode in {"review", "distill"}:
            if count == 0:
                return p3.reply(body, "read_evidence", {})
            summary = (
                p3.REVIEW
                if mode == "review"
                else canonical_json(
                    {
                        "unrelated": False,
                        "rationale": "The sample's earlier failed attempt remains pending review; no claim that later feedback is unrelated",
                    }
                )
                if "逐条核对新反馈" in envelope["objective"]
                else canonical_json(
                    {
                        "symptoms": "P6 controlled sample returned a difference rather than a sum",
                        "applicability": "Only this source repository, task and frozen candidate",
                        "method": "Correct addition after comparing actual source and target probes",
                        "verification": "Actual Docker base probe failed, candidate probe and regression passed; independent review recorded",
                        "limitations": "Deterministic model fixture; no production generalization or maintainer acceptance claimed",
                    }
                )
            )
            return p3.reply(body, *p3.structured(envelope, summary))
        elif mode not in {"scan", "investigate", "reproduce", "implement"}:
            raise AssertionError("unsupported fixture mode: " + mode)
        return p3.reply(body, *p3.structured(envelope))

    def import_repository(self):
        data = {
            "owner": self.owner,
            "name": self.name,
            "target_branch": self.metadata["branch"],
            "contribution_path": "fork" if self.fork else "direct",
            "fork_owner": "gwx541" if self.fork else None,
            "fork_name": "repowatch-p3-sandbox" if self.fork else None,
            "credential_reference": self.reference,
        }
        with self.services.store.read() as records:
            old = records.one("repositories", github_id="1447899115")
        if old:
            if old["target_branch"] != data["target_branch"]:
                raise AssertionError("acceptance repository target changed")
            return {"repository_id": old["id"]}
        preflight = self.services.repositories.preflight(
            "p6-preflight", data, now=self.now()
        )
        return self.services.repositories.save(
            "p6-import",
            preflight["preflight_id"],
            {
                "mode": "manual",
                "allowed_labels": [],
                "excluded_labels": [],
                "severity_labels": {},
                "claim_comment": False,
                "claim_template": "P6 controlled acceptance",
            },
            expected_revision=0,
            now=self.now(),
            scan_policy={"chunk_bytes": 16000, "exclusions": {}},
        )


def execute(sample, metadata, *, before_resume=None, before_repair_resume=None):
    run = Harness(sample, metadata)
    try:
        if metadata.get("execution_backend") == "native":
            from repowatch.operations.native import NativeConfiguration

            with run.services.store.read() as records:
                native_configuration = dict(
                    records.one("operations_configuration", singleton=1)
                )
            bound_native = json.loads(native_configuration["native_json"])
            if (
                not bound_native.get("enabled")
                or bound_native.get("host_root") != metadata["native_host_root"]
            ):
                NativeConfiguration(run.services.store).configure(
                    "p6-native-host-bind-"
                    + content_digest(metadata["native_host_root"])[:16],
                    enabled=True,
                    expected_revision=native_configuration["revision"],
                    host_root=metadata["native_host_root"],
                )
        run.bootstrap_provider()
        imported = run.import_repository()
        configuration = {
            "workspace_image": metadata["image"],
            "check_timeout": 90,
            "max_input_tokens": 100000,
        }
        if metadata.get("execution_backend") == "native":
            configuration["execution_backend"] = "native"
        with run.services.store.read() as records:
            repository = records.one("repositories", id=imported["repository_id"])
        if not repository["scan_ready_batch_id"]:
            scan = run.scan_task()
            if before_resume is None:
                run.configure(scan["id"], configuration)
            else:
                before_resume(run, scan["id"], configuration)
            run.budget(scan["id"], "p6-scan")
            run.services.queue.set_pause(False, now=run.now())
            run.run_until_idle()
        with run.services.store.read() as records:
            repository = dict(records.one("repositories", id=imported["repository_id"]))
            candidates = [
                dict(row)
                for row in records.find(
                    "scan_candidates", batch_id=repository["scan_ready_batch_id"]
                )
                if json.loads(row["finding_json"])["path"] == sample.path
            ]
        if len(candidates) != 1:
            raise AssertionError("expected exactly one actual sample candidate")
        candidate = candidates[0]
        if not candidate["request_task_id"]:
            run.services.scans.decide_candidate(
                "p6-select",
                candidate["id"],
                "select",
                expected_revision=candidate["revision"],
                now=run.now(),
            )
        with run.services.store.read() as records:
            task_id = records.one("scan_candidates", id=candidate["id"])[
                "request_task_id"
            ]
            task = dict(records.one("tasks", id=task_id))
        if before_repair_resume is not None:
            before_repair_resume(run, task_id, configuration)
        elif task["state"] == "queued":
            run.configure(task_id, configuration)
        if task["state"] == "queued" or before_repair_resume is not None:
            run.budget(
                task["budget_owner_task_id"],
                "p6-repair-" + task["budget_owner_task_id"],
            )
        run.run_until_idle(steps=60)
        with run.services.store.read() as records:
            task_id = records.one("scan_candidates", id=candidate["id"])[
                "request_task_id"
            ]
            task = dict(records.one("tasks", id=task_id))
            intent = dict(
                records.one("publication_intents", task_id=task_id, kind="pull_request")
                or {}
            )
            sessions = [
                dict(row) for row in records.find("repair_sessions", task_id=task_id)
            ]
            memories = [
                dict(row)
                for row in records.find("memory_heads", source_task_id=task_id)
            ]
            bindings = [
                dict(row)
                for row in records.find("managed_pull_requests", task_id=task_id)
            ]
        if task["wait_reason"] != "pr_review" or intent.get("state") != "confirmed":
            raise AssertionError("product did not confirm the PR publication")
        receipt = json.loads(intent["result_json"])
        number = receipt["number"]
        actual = api(sample.route, "GET", PREFIX + "/pulls/" + str(number))
        if (
            actual["head"]["sha"] != receipt["sha"]
            or actual["base"]["ref"] != metadata["branch"]
            or actual["base"]["repo"]["id"] != 1447899115
        ):
            raise AssertionError("actual PR identity differs from product receipt")
        if not actual["title"].startswith("P6 "):
            actual = api(
                sample.route,
                "PATCH",
                PREFIX + "/pulls/" + str(number),
                {"title": "P6 " + sample.key + ": " + actual["title"]},
            )
        published_sessions = [
            item
            for item in sessions
            if item["state"] == "verified"
            and item["evidence_json"]
            and json.loads(item["frozen_json"])["sha"] == receipt["sha"]
        ]
        if len(published_sessions) != 1:
            raise AssertionError(
                "publication must identify one verified candidate session"
            )
        session = published_sessions[0]
        evidence = json.loads(session["evidence_json"])
        if (
            evidence["base_probe"]["exit_code"] == 0
            or evidence["candidate_probe"]["exit_code"] != 0
        ):
            raise AssertionError("invalid actual before/after proof")
        report = {
            "sample": sample.key,
            "route": sample.route,
            "preparation": metadata,
            "product": {
                "repository_id": repository["id"],
                "candidate_id": candidate["id"],
                "task_id": task_id,
                "issue_id": task["issue_id"],
                "input": json.loads(task["input_json"]),
                "task_state": task["state"],
                "wait_reason": task["wait_reason"],
                "publication": receipt,
                "bindings": bindings,
                "frozen": json.loads(session["frozen_json"]),
                "evidence": evidence,
                "memory_states": [row["state"] for row in memories],
                "budget": run.services.budgets.view(task["budget_owner_task_id"]),
            },
            "actual_pr": {
                "id": str(actual["id"]),
                "number": number,
                "url": actual["html_url"],
                "base_ref": actual["base"]["ref"],
                "head_ref": actual["head"]["ref"],
                "base_sha": actual["base"]["sha"],
                "head_sha": actual["head"]["sha"],
                "head_repository_id": str(actual["head"]["repo"]["id"]),
            },
            "model": "deterministic_http_fixture",
            "paid_model_calls": 0,
            "operator_actions": [
                "prepare isolated sample branch",
                "prefix new PR title with P6",
            ],
            "github_writes": True,
            "model_calls_this_invocation": run.model_calls,
            "installed_wheel": Path(repowatch.__file__)
            .resolve()
            .is_relative_to(Path(sys.prefix).resolve())
            and "site-packages" in Path(repowatch.__file__).parts,
        }
        with run.services.store.read() as records:
            control = records.one("global_control", singleton=1)
        run.services.control.pause(
            "p6-acceptance-finished",
            expected_revision=control["revision"],
            now=run.now(),
        )
        report["global_paused_after_acceptance"] = True
        write_json(run.settings.data_dir.parent / "report.json", report)
        run.log(
            "p6_published",
            sample=sample.key,
            issue=report["product"]["input"]["number"],
            pr=number,
            head=actual["head"]["sha"],
        )
        return report
    finally:
        run.services.close()


def verify(sample, metadata):
    directory = ROOT / ".repowatch" / ("p6-" + sample.key)
    path = directory / "report.json"
    report = json.loads(path.read_text("utf-8"))
    task = report["product"]["task_id"]
    repository = report["product"]["repository_id"]
    frozen = report["product"]["frozen"]
    backend = DockerWorkspaces(
        directory / "preview-workspaces", GitObjects(directory / "data/git-objects")
    )
    workspace = backend.create(
        repository, frozen["sha"], task_id=task, mode="check", image=metadata["image"]
    )
    if not workspace["ready"]:
        raise AssertionError("frozen preview source is unavailable")
    container = "repowatch-p6-preview-" + uuid4().hex
    common = [
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=128m",
        "--memory=512m",
        "--cpus=2",
        "--mount",
        "type=volume,src=" + workspace["volume"] + ",dst=/source,readonly",
    ]
    try:
        build = backend.docker(
            "run",
            "--rm",
            "--network=none",
            *common,
            workspace["image"],
            "sh",
            "-c",
            "cp -a /source/. /tmp/app; cd /tmp/app; " + shlex.join(sample.build),
        )
        evidence = {
            "candidate_sha": frozen["sha"],
            "image": workspace["image"],
            "build_argv": sample.build,
            "build_exit_code": build.returncode,
            "build_stdout_sha256": sha256(build.stdout).hexdigest(),
        }
        if sample.preview:
            backend.docker(
                "run",
                "-d",
                "--name",
                container,
                "--network=bridge",
                "--publish",
                "127.0.0.1::8080",
                *common,
                workspace["image"],
                *sample.preview,
            )
            observed = json.loads(backend.docker("inspect", container).stdout)[0]
            binding = observed["NetworkSettings"]["Ports"]["8080/tcp"][0]
            if binding["HostIp"] != "127.0.0.1":
                raise AssertionError("preview must only publish on host loopback")
            url = "http://127.0.0.1:" + binding["HostPort"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    if httpx.get(url, timeout=1, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            else:
                raise AssertionError("sample preview did not become ready")
            subprocess.run(
                [
                    "node",
                    "experiments/p6-sample-browser.mjs",
                    "--sample",
                    sample.key,
                    "--url",
                    url,
                ],
                cwd=ROOT,
                check=True,
                timeout=60,
            )
            browser_path = (
                ROOT / ".cache/p6-sample-browser" / sample.key / "report.json"
            )
            evidence["browser"] = json.loads(browser_path.read_text("utf-8"))
            evidence["preview_boundary"] = (
                "read-only source volume, no host mounts or credentials, loopback publication"
            )
        else:
            argv = (
                ["python", "/source/" + sample.path, "2", "3"]
                if sample.path.endswith(".py")
                else ["node", "/source/" + sample.path, "2", "3"]
            )
            result = backend.docker(
                "run", "--rm", "--network=none", *common, workspace["image"], *argv
            )
            if result.stdout.strip() != b"5":
                raise AssertionError(
                    "actual compiled CLI did not return the expected result"
                )
            evidence["cli"] = {
                "argv": argv,
                "exit_code": result.returncode,
                "stdout": "5",
            }
        report["adaptation"] = evidence
        write_json(path, report)
        print(
            json.dumps(
                {
                    "verified": sample.key,
                    "build_exit_code": 0,
                    "browser": bool(sample.preview),
                    "candidate_sha": frozen["sha"],
                }
            ),
            flush=True,
        )
    finally:
        backend.docker("rm", "-f", container, check=False)
        backend.remove(workspace)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["preflight", "prepare", "run", "verify", "image", "pause"]
    )
    parser.add_argument("sample", choices=list(samples()))
    parser.add_argument("--allow-github-writes", action="store_true")
    args = parser.parse_args()
    sample = samples()[args.sample]
    if args.action == "pause":
        directory = ROOT / ".repowatch" / ("p6-" + sample.key)
        settings = StorageSettings(directory / "data")
        services = HostServices(settings)
        try:
            with services.store.read() as records:
                control = records.one("global_control", singleton=1)
            if not control["paused"]:
                services.control.pause(
                    "p6-acceptance-finished",
                    expected_revision=control["revision"],
                    now=time.time(),
                )
            path = directory / "report.json"
            report = json.loads(path.read_text("utf-8"))
            report["global_paused_after_acceptance"] = True
            write_json(path, report)
            print(json.dumps({"sample": sample.key, "paused": True}), flush=True)
        finally:
            services.close()
        return
    if args.action == "image":
        if sample.key != "typescript-web":
            raise ValueError("Only the TypeScript sample needs a compiler image")
        print(json.dumps({"image": typescript_image()}), flush=True)
        return
    if args.action == "verify":
        metadata = json.loads(
            (ROOT / ".repowatch" / ("p6-" + sample.key) / "acceptance.json").read_text(
                "utf-8"
            )
        )
        verify(replace(sample, image=metadata["image"]), metadata)
        return
    if args.action == "preflight":
        print(
            json.dumps(
                {
                    "route": sample.route,
                    "login": api(sample.route, "GET", "/user")["login"],
                }
            )
        )
        return
    if not args.allow_github_writes:
        raise PermissionError("必须明确允许本次 P6 sandbox 写入")
    metadata = prepare(sample)
    print(
        json.dumps(
            {
                "prepared": sample.key,
                "branch": metadata["branch"],
                "base_sha": metadata["base_sha"],
            }
        ),
        flush=True,
    )
    if args.action == "run":
        execute(replace(sample, image=metadata["image"]), metadata)
        verify(replace(sample, image=metadata["image"]), metadata)


if __name__ == "__main__":
    main()
