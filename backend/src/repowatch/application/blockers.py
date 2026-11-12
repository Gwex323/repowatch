"""阻塞原因与可执行的处理建议。"""

import json
import traceback
from pathlib import Path

from repowatch.security import DEFAULT_REDACTOR

GUIDANCE = {
    "docker_unavailable": (
        "无法连接 Docker 引擎",
        ["启动 Docker Desktop，等待 Linux 引擎运行正常。"],
    ),
    "docker_image_missing": ("扫描镜像不可用", ["在 Docker Desktop 中准备任务配置所指定的镜像。"]),
    "docker_operation_failed": (
        "Docker 操作失败",
        ["根据下方 Docker 错误检查镜像、磁盘空间和容器状态。"],
    ),
    "docker_timeout": (
        "Docker 操作超时",
        ["检查 Docker Desktop 是否正常响应，以及磁盘和内存是否充足。"],
    ),
    "runtime_execution_rejected": (
        "任务执行失败",
        ["根据失败步骤和错误详情修复环境或任务配置，然后重新检查。"],
    ),
    "scan_safety_boundary": ("扫描条件未满足", ["根据错误详情补齐源码、依赖或执行环境。"]),
    "service_exit_unconfirmed": (
        "上次服务退出未确认",
        ["确认旧服务和仓库命令已停止，然后重新检查。"],
    ),
    "budget_exhausted": ("任务预算已耗尽", ["在任务预算中追加所需额度，然后重新检查。"]),
    "budget_revoked": ("任务预算已撤销", ["确认任务是否继续，在任务预算中处理授权。"]),
    "budget_bound_exceeded": (
        "执行用量超过预留上限",
        ["核对任务用量与执行上限，调整任务配置和预算。"],
    ),
    "no_progress": ("连续尝试未取得进展", ["在任务详情补充定位材料或调整处理策略，再重新检查。"]),
    "oscillation": ("任务反复回到相同状态", ["在任务详情调整处理策略或补充能区分方案的证据。"]),
    "bounded_retry_exhausted": (
        "本次有限重试未取得进展",
        ["在任务详情补充新材料或调整策略，再重新检查。"],
    ),
    "stage_timeout": ("执行阶段超时", ["检查运行日志和环境，必要时调整任务的阶段执行上限。"]),
    "stage_step_limit": (
        "执行阶段达到步骤上限",
        ["检查任务范围和运行日志，调整处理策略或阶段上限。"],
    ),
    "lease_expired": ("旧运行的执行权已过期", ["在任务详情核对旧运行，确认遗留命令已停止。"]),
    "abandoned_run": ("旧运行已放弃，等待恢复", ["核对旧运行报告和当前任务预算，然后重新检查。"]),
    "backup_restore_reconciliation": (
        "备份恢复后尚未完成核对",
        ["进入系统与维护，生成恢复核对报告并确认核对结果。"],
    ),
    "target_read_failed": ("无法读取目标分支", ["检查 GitHub 账号权限、网络及仓库目标分支配置。"]),
    "runtime_version_conflict": (
        "运行期间配置或版本发生变化",
        ["核对任务和仓库的当前配置，然后重新检查。"],
    ),
    "workspace_adapter_missing": (
        "任务执行环境未配置",
        ["在仓库和任务配置中选择受支持的执行环境。"],
    ),
    "storage_failure": ("运行或证据无法保存", ["检查磁盘剩余空间、数据目录权限和数据库状态。"]),
    "progress_evidence_unavailable": (
        "进展证据不可用",
        ["在任务详情查看缺失产物，恢复文件或重新取得验证证据。"],
    ),
    "artifact_persistence_pending": (
        "产物写入尚未确认",
        ["在任务详情核对旧运行和产物，检查磁盘及写入权限。"],
    ),
    "runtime_reported_blocked": (
        "分析发现尚未解决的问题",
        ["查看下方未解决问题，在任务详情补充材料或调整配置。"],
    ),
    "role_configuration_missing": (
        "任务模型尚未配置",
        ["进入设置与维护的角色模型页面，为处理和审查角色选择模型并保存。"],
    ),
    "role_capability_missing_or_expired": (
        "模型能力核验缺失或已过期",
        ["进入角色模型或诊断页面，运行所选模型的角色能力核验。"],
    ),
    "shared_scan_cancelled": (
        "依赖的扫描任务已取消",
        ["在仓库配置中重新登记扫描，核对依赖任务后重新检查。"],
    ),
    "command_unconfirmed": (
        "旧命令状态尚未确认",
        ["在任务详情核对旧运行及命令状态，等待命令停止后重新检查。"],
    ),
}

STEPS = {
    "git-fetch": "拉取仓库代码",
    "workspace": "创建扫描工作区",
    "workspace-recovery": "重建工作区",
}


def guidance(code, details):
    title, suggestions = GUIDANCE.get(
        code, ("任务暂时无法继续", ["根据错误详情检查任务配置和运行记录，然后重新检查。"])
    )
    if code.startswith("provider_"):
        title = "模型供应商请求失败"
        suggestions = ["在设置与维护中核对供应商地址、密钥、模型权限与额度，并运行连接诊断。"]
    description = details.get("detail") or details.get("message")
    if not description and details.get("unresolved"):
        description = json.dumps(details["unresolved"], ensure_ascii=False)
    if not description:
        description = (
            title
            if code in GUIDANCE and code != "runtime_execution_rejected"
            else "历史记录未保存具体异常，请按建议检查。再次失败时系统会记录新的诊断。"
        )
    return DEFAULT_REDACTOR.clean(
        {
            "title": title,
            "description": description,
            "stage": details.get("stage", ""),
            "suggestions": details.get("suggestions") or suggestions,
        }
    )


def failure_details(error, *, stage=""):
    from pydantic import ValidationError

    from repowatch.adapters.docker_workspaces import WorkspaceError
    from repowatch.adapters.git_objects import GitObjectError
    from repowatch.adapters.providers import ProviderRequestError

    details = {"stage": stage, "exception_type": type(error).__name__}
    if isinstance(error, WorkspaceError):
        details.update(error.details)
    elif isinstance(error, ValidationError):
        details["detail"] = (
            "结果字段校验失败。"
            + "、".join(
                ".".join(str(part) for part in item["loc"]) + " " + item["type"]
                for item in error.errors(include_input=False, include_context=False)
            )[:1600]
        )
    elif isinstance(error, ValueError):
        details["detail"] = str(error)[:2000]
    elif isinstance(error, (PermissionError, GitObjectError)):
        details["detail"] = str(error)[:2000]
    elif isinstance(error, ProviderRequestError):
        details.update(detail=str(error), provider_error=error.code, status_code=error.status_code)
    else:
        frames = traceback.extract_tb(error.__traceback__)
        origin = frames[-1] if frames else None
        location = (
            f"{Path(origin.filename).name} 的 {origin.name} 第 {origin.lineno} 行"
            if origin
            else stage
        )
        details["detail"] = f"{location}发生 {type(error).__name__}，执行未完成。"
    return DEFAULT_REDACTOR.clean(details)


def execution_stage(records, run_id):
    reservations = records.find("budget_reservations", run_id=run_id)
    latest = max(reservations, key=lambda row: row["created_at"], default=None)
    if latest:
        key = latest["operation_key"].split(":", 1)[0]
        return STEPS.get(key, key)
    return "准备任务执行"


def check_environment(settings, configuration, diagnosis):
    from repowatch.adapters.docker_workspaces import DockerWorkspaces

    if not diagnosis.startswith("docker_"):
        return
    backend = DockerWorkspaces(settings.data_dir / "workspaces", None)
    backend.docker("version", "--format", "{{.Server.Version}}", timeout=10)
    backend.docker(
        "image", "inspect", configuration.get("workspace_image", "alpine/git:latest"), timeout=10
    )
