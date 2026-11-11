import os
from hashlib import sha256

import pytest

from repowatch.adapters.native_client import (
    PROTOCOL,
    QUALIFICATIONS,
    NativeHostClient,
    windows_process_owner,
)
from repowatch.domain.task_records import canonical_json, content_digest


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "native-host"
    for name in ("host", "requests", "responses/commands", "workspaces", "runtime/python"):
        (root / name).mkdir(parents=True, exist_ok=True)
    source = b"fixed trusted host code"
    (root / "host/host.ps1").write_bytes(source)
    runtime = {"python/python.exe": sha256(b"fixed native tool").hexdigest()}
    (root / "runtime/python/python.exe").write_bytes(b"fixed native tool")
    profile = {
        "protocol": PROTOCOL,
        "runtime_id": "sha256:" + content_digest(runtime),
        "runtime_files": runtime,
        "tools": {"python": "python/python.exe"},
        "controller_sid": "controller",
        "execution_sid": "runner",
    }
    (root / "profile.json").write_text(canonical_json(profile), encoding="utf-8")
    ready = {
        "protocol": PROTOCOL,
        "profile_sha256": content_digest(profile),
        "generation": "a" * 32,
        "pid": 12345,
        "created": 99,
        "stopped": False,
        "qualifications": sorted(QUALIFICATIONS),
    }
    (root / "responses/ready.json").write_text(canonical_json(ready), encoding="utf-8")
    state = {"elevated": False, "created": 99, "sid": "runner"}

    def process_probe(pid):
        return (
            {"sid": "controller", "created": 5, "elevated": False}
            if pid == os.getpid()
            else state.copy()
        )

    kwargs = {
        "assets": {"host.ps1": sha256(source).hexdigest()},
        "process_probe": process_probe,
        "access_guard": lambda *_args, **_kwargs: None,
    }
    return root, profile, ready, state, kwargs


def check_payload():
    return {
        "workspace_id": "repowatch-native-" + "b" * 32,
        "argv": ["python", "app.py"],
        "timeout_ms": 5000,
        "deadline_unix_ms": 9999999999999,
        "manifest_sha256": "c" * 64,
    }


def test_native_host_binding_is_live_and_request_identity_is_idempotent(host):
    root, _, ready, _, kwargs = host
    client = NativeHostClient(root, **kwargs)
    first = client.submit("check", check_payload(), request_id="d" * 32)
    assert client.submit("check", check_payload(), request_id="d" * 32) == first
    with pytest.raises(PermissionError, match="更换内容"):
        client.submit(
            "check", {**check_payload(), "argv": ["python", "other.py"]}, request_id="d" * 32
        )
    assert client.response(first) is None
    response = {**first, "state": "stopped"}
    (root / "responses" / (first["request_id"] + ".json")).write_text(
        canonical_json(response), encoding="utf-8"
    )
    assert client.response(first) == response
    ready["generation"] = "e" * 32
    (root / "responses/ready.json").write_text(canonical_json(ready), encoding="utf-8")
    with pytest.raises(PermissionError, match="代次已变化"):
        client.response(first)


@pytest.mark.parametrize(
    "damage",
    [
        "pid_reuse",
        "wrong_user",
        "elevated",
        "stopped",
        "unqualified",
        "code_changed",
        "profile_changed",
    ],
)
def test_native_configuration_or_stale_proof_cannot_admit_a_host(host, damage):
    root, _, ready, state, kwargs = host
    if damage == "pid_reuse":
        state["created"] = 100
    elif damage == "wrong_user":
        state["sid"] = "other"
    elif damage == "elevated":
        state["elevated"] = True
    elif damage == "stopped":
        ready["stopped"] = True
    elif damage == "unqualified":
        ready["qualifications"] = []
    elif damage == "code_changed":
        (root / "host/host.ps1").write_text("changed", encoding="utf-8")
    else:
        ready["profile_sha256"] = "different"
    (root / "responses/ready.json").write_text(canonical_json(ready), encoding="utf-8")
    with pytest.raises(PermissionError):
        NativeHostClient(root, **kwargs)


@pytest.mark.parametrize(
    "change",
    [
        {"timeout_ms": True},
        {"timeout_ms": 0},
        {"workspace_id": "../other"},
        {"manifest_sha256": "unknown"},
        {"argv": ["python", "bad\0argument"]},
        {"extra_host_script": "unexpected"},
    ],
)
def test_native_requests_reject_widened_or_unbounded_inputs(host, change):
    root, _, _, _, kwargs = host
    client = NativeHostClient(root, **kwargs)
    with pytest.raises(PermissionError):
        client.submit("check", {**check_payload(), **change})
    assert list((root / "requests").glob("*.json")) == []


def test_native_queued_request_blocks_file_access_before_child_binding(host):
    root, _, _, _, kwargs = host
    client = NativeHostClient(root, **kwargs)
    payload = check_payload()
    receipt = client.submit("check", payload)
    with pytest.raises(PermissionError, match="待绑定"):
        client.assert_workspace_idle(payload["workspace_id"])
    response_path = root / "responses" / (receipt["request_id"] + ".json")
    response_path.write_text(canonical_json({**receipt, "state": "stopped"}), encoding="utf-8")
    client.assert_workspace_idle(payload["workspace_id"])
    response_path.write_text(
        canonical_json({**receipt, "state": "stopped", "request_sha256": "forged"}),
        encoding="utf-8",
    )
    with pytest.raises(PermissionError):
        client.assert_workspace_idle(payload["workspace_id"])


def test_native_response_rejects_foreign_request_digest_and_path_escape(host):
    root, _, _, _, kwargs = host
    client = NativeHostClient(root, **kwargs)
    receipt = client.submit("check", check_payload())
    (root / "responses" / (receipt["request_id"] + ".json")).write_text(
        canonical_json({**receipt, "request_sha256": "other"}), encoding="utf-8"
    )
    with pytest.raises(PermissionError, match="归属"):
        client.response(receipt)
    with pytest.raises(PermissionError, match="身份无效"):
        client.response({**receipt, "request_id": "../../other"})


def test_native_profile_access_failure_never_falls_back_to_a_shell(host):
    root, _, _, _, kwargs = host

    def reject(*_args, **_kwargs):
        raise PermissionError("Unsafe ACL")

    kwargs["access_guard"] = reject
    with pytest.raises(PermissionError, match="Unsafe ACL"):
        NativeHostClient(root, **kwargs)


@pytest.mark.skipif(os.name != "nt", reason="Windows token query")
def test_real_native_process_owner_is_bound_to_creation_time():
    proof = windows_process_owner(os.getpid())
    assert proof["sid"].startswith("S-1-")
    assert type(proof["created"]) is int and proof["created"] > 0
    assert type(proof["elevated"]) is bool


def test_native_profile_and_runtime_changes_invalidate_an_existing_client(host):
    root, profile, _, _, kwargs = host
    client = NativeHostClient(root, **kwargs)
    runtime = root / "runtime/python/python.exe"
    original = runtime.read_bytes()
    runtime.write_bytes(b"replaced executable")
    with pytest.raises(PermissionError, match="工具链文件摘要"):
        client.require_ready()
    runtime.write_bytes(original)
    profile["unexpected_change"] = True
    (root / "profile.json").write_text(canonical_json(profile), encoding="utf-8")
    with pytest.raises(PermissionError, match="准入后发生变化"):
        client.require_ready()


def test_native_gate_requires_the_actual_isolation_token_and_respects_cancellation(host):
    root, _, _, _, kwargs = host
    original_probe = kwargs["process_probe"]
    child = {"sid": "runner", "created": 100, "elevated": False, "appcontainer": False}
    kwargs["process_probe"] = lambda pid: child.copy() if pid == 222 else original_probe(pid)
    client = NativeHostClient(root, **kwargs)
    receipt = client.submit("check", check_payload())
    bound_path = root / "responses/commands" / (receipt["request_id"] + ".bound.json")
    bound_path.write_text(canonical_json({"pid": 222, "created": 100}), encoding="utf-8")
    with pytest.raises(PermissionError, match="隔离令牌"):
        client.start(receipt)
    gate = root / "requests" / (receipt["request_id"] + ".start")
    assert not gate.exists()
    child["appcontainer"] = True
    gate.with_suffix(".cancel").touch()
    with pytest.raises(PermissionError, match="已取消"):
        client.start(receipt)
    assert not gate.exists()
