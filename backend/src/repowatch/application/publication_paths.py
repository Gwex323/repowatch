"""在发布门槛内选择远端分支所在仓库；创建 Fork 前持久登记发送。"""

import json

from repowatch.application.github_ports import GitHubReadError
from repowatch.application.task_records import business_event, execution_record, required

FORK_DISPATCHED = "publication_fork_creation_dispatched"


def publication_target(repository, request):
    """Legacy configurations predate the destination stored in PR requests."""
    configured = json.loads(repository["publication_json"] or "null")
    resolved = request.get("publication") or configured
    if (
        not resolved
        or resolved["github_id"] != request["publication_id"]
        or (configured and configured["github_id"] != resolved["github_id"])
    ):
        raise PermissionError("发布仓库身份与原 PR 意图不匹配")
    return resolved


def require_actor(port, repository):
    identity = json.loads(repository["identity_json"])
    user = port.authenticated_user()
    if (
        user["id"] != identity["execution_user_id"]
        or user["login"].casefold() != identity["execution_login"].casefold()
    ):
        raise PermissionError("执行账号与仓库登记不符，请重新预检")
    return user


def _metadata(port, owner, name, *, optional=False):
    try:
        _, data, _ = port._get(port._path(owner, name))
    except GitHubReadError as error:
        if optional and error.code == "not_found":
            return None
        raise
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or data["id"] <= 0
        or not isinstance(data.get("owner"), dict)
        or not isinstance(data["owner"].get("login"), str)
        or data["owner"]["login"].casefold() != owner.casefold()
        or not isinstance(data.get("name"), str)
        or data["name"].casefold() != name.casefold()
        or data.get("archived")
        or data.get("disabled")
    ):
        raise PermissionError("发布仓库不可用或远端身份不匹配")
    return data


def _pushable(data):
    return isinstance(data.get("permissions"), dict) and data["permissions"].get("push") is True


def _target(data, path):
    return {
        "github_id": str(data["id"]),
        "owner": data["owner"]["login"],
        "name": data["name"],
        "path": path,
    }


def _check_fork(fork, upstream_id):
    parent = fork.get("parent")
    if (
        fork.get("fork") is not True
        or not isinstance(parent, dict)
        or str(parent.get("id")) != upstream_id
    ):
        raise PermissionError("执行账号下存在同名仓库，但它不是指定上游的 Fork")
    if not _pushable(fork):
        raise PermissionError("Fork 尚不可推送，请检查 Fork 状态和令牌权限后重试")
    return _target(fork, "fork")


def _fork_dispatched(records, dispatch):
    return any(
        json.loads(event["payload_json"]) == dispatch
        for event in records.find("business_events", kind=FORK_DISPATCHED)
    )


def resolve_automatic_target(port, services, token, repository):
    user = require_actor(port, repository)
    owner, name = repository["github_owner"], repository["github_name"]
    upstream = _metadata(port, owner, name)
    upstream_id = repository["github_id"]
    if str(upstream["id"]) != upstream_id:
        raise PermissionError("发布上游仓库身份发生变化")
    dispatch = {"upstream_id": upstream_id, "execution_user_id": user["id"]}
    with services.store.read() as records:
        sent = _fork_dispatched(records, dispatch)
    if not sent and _pushable(upstream):
        return _target(upstream, "direct")
    if upstream.get("private") is not False or upstream.get("allow_forking") is False:
        raise PermissionError("没有上游推送权限，且该仓库不能使用自动 Fork 路径")

    login = user["login"]
    fork = _metadata(port, login, name, optional=True)
    if fork is not None:
        return _check_fork(fork, upstream_id)

    if sent:
        raise PermissionError("Fork 创建尚未确认；请检查 GitHub 上的 Fork 后重试发布")
    services.queue.guard(token, now=services.clock(), purpose="boundary")
    with services.store.write() as records:
        run = execution_record(records, token, services.clock())
        saved = required(records, "repositories", id=repository["id"])
        if saved["revision"] != repository["revision"]:
            raise PermissionError("登记配置已变化，不能创建 Fork")
        sent = _fork_dispatched(records, dispatch)
        if not sent:
            business_event(records, FORK_DISPATCHED, run["task_id"], **dispatch)
    if not sent:
        try:
            port.write("POST", port._path(owner, name) + "/forks", {})
        except PermissionError:
            # An error response or transport failure does not establish that nothing was created.
            pass
        fork = _metadata(port, login, name, optional=True)
        if fork is not None:
            return _check_fork(fork, upstream_id)
    raise PermissionError("Fork 创建尚未确认；请检查 GitHub 上的 Fork 后重试发布")


def recheck_automatic_target(port, repository, publication):
    user = require_actor(port, repository)
    remote = _metadata(port, publication["owner"], publication["name"])
    if str(remote["id"]) != publication["github_id"]:
        raise PermissionError("发布仓库身份发生变化")
    if publication["path"] == "fork":
        if publication["owner"].casefold() != user["login"].casefold():
            raise PermissionError("发布 Fork 不属于登记的执行账号")
        _check_fork(remote, repository["github_id"])
    elif publication["path"] != "direct" or not _pushable(remote):
        raise PermissionError("发布分支所在仓库已不可推送")
