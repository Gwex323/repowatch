import type { State, Task } from "./client";

export type Repository = State["repositories"][number];
export type Run = (action: () => Promise<unknown>, message?: string) => Promise<void>;

export const record = (value: unknown): Record<string, unknown> =>
    typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
export const text = (value: unknown) => typeof value === "string" ? value : "";
export const shortSha = (sha?: string | null) => sha ? sha.slice(0, 8) : "尚未观测";
export const repositoryName = (repo: Repository) =>
    [repo.github_owner, repo.github_name].filter(Boolean).join("/") || repo.github_id;
export const issueNumber = (task?: Task) =>
    typeof task?.input.number === "number" && task.input.number > 0 && task.issue_id ? task.input.number : null;
export const taskTitle = (task: Task) => {
    const number = issueNumber(task);
    const title = text(task.input.title) || text(record(task.input.finding).title);
    return number ? `Issue #${number}${title ? ` · ${title}` : ""}` :
        title || ({ scan: "仓库扫描", triage: "问题分诊", repair: "修复任务", diagnose: "运行诊断", review: "独立审查" }[task.mode] ?? "任务");
};

const taskStates: Record<string, string> = {
    queued: "排队中", running: "执行中", blocked: "已阻塞", waiting: "等待中",
    ended: "已结束", cancelled: "已取消",
};
const waitStates: Record<string, string> = {
    information: "需要补充信息", human_information: "需要补充信息", approval: "需要确认验收范围",
    review_scope: "需要判断评审范围", pr_merge_conflict: "需要处理 PR 冲突",
    target_configuration: "需要确认目标配置", pr_head_changed: "需要核对 PR 版本",
    delivery_review: "需要核对发布结果", resume_required: "需要明确恢复",
    scan: "等待扫描", pr_review: "等待 PR 更新", pr_terminal_verification: "等待核验交付结果",
    safe_stop: "已安全停止", external_pr_same_problem: "等待外部 PR",
    scope_children: "由独立目标分别跟进", external_pr: "等待外部 PR", admission: "等待选择处理",
};
const humanWait = new Set(["information", "human_information", "approval", "review_scope", "pr_merge_conflict",
    "target_configuration", "pr_head_changed", "delivery_review", "resume_required"]);
export const requiresInput = (task: Task) => task.state === "waiting" && humanWait.has(task.wait_reason ?? "");
export const dedupMessage = (task: Task) => task.similar_issue_number ?
    `发现相似 Issue #${task.similar_issue_number}，尚无法确认是同一问题` : "系统已查重，仍无法确认是否有相同 Issue";
export const taskStatus = (task: Task) => task.state === "waiting" && task.triage_reason === "issue_lookup_required"
    ? dedupMessage(task)
    : (task.state === "waiting" && task.wait_reason ? waitStates[task.wait_reason] : undefined) ?? taskStates[task.state] ?? "查看任务详情";

export const pendingRenewals = (state: State) => state.renewals.filter(item => ["pending", "stale"].includes(item.state));
export const taskAttention = (state: State) => {
    const reasons = new Set(state.reasons.map(reason => reason.task_id));
    const renewals = new Set(pendingRenewals(state).map(item => item.owner_task_id));
    return state.tasks.filter(task => requiresInput(task) && !reasons.has(task.id)
        && !renewals.has(task.budget_owner_task_id));
};
export const countAttention = (state: State, repositoryId?: string) => {
    const tasks = new Set(state.tasks.filter(task => !repositoryId || task.repository_id === repositoryId).map(task => task.id));
    return state.reasons.filter(reason => !repositoryId || (reason.task_id && tasks.has(reason.task_id))).length +
        pendingRenewals(state).filter(item => !repositoryId || tasks.has(item.task_id) || tasks.has(item.owner_task_id)).length +
        taskAttention(state).filter(task => tasks.has(task.id)).length;
};
const ciStates: Record<string, string> = { pending: "等待 CI", success: "CI 已通过", failed: "CI 未通过",
    policy_required: "待配置 CI 检查", unknown: "尚无 CI 结果" };
const prStates: Record<string, string> = {
    pr_merged: "已合并", merged: "已合并", pr_closed_unmerged: "关闭未合并", closed: "关闭未合并",
};
export const deliveryStatus = (delivery: NonNullable<Task["delivery"]>) =>
    prStates[delivery.delivery_state] ?? ciStates[delivery.ci_state] ?? "待核对交付状态";
