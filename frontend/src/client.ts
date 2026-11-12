export type Amounts = Record<"active_ms" | "model_calls" | "tokens" | "repair_rounds" | "recovery_rounds", number>;
export type Budget = {
    revision: number;
    status: string;
    warning: boolean;
    limits: Amounts;
    used: Amounts;
    reserved: Amounts;
    available: Amounts;
};
export type Task = {
    id: string;
    repository_id: string | null;
    mode: string;
    state: string;
    revision: number;
    wait_reason: string | null;
    outcome: string | null;
    acceptance_revision?: number;
    acceptance?: { revision: number; kind: "bug" | "feature" | "refactor"; content: Record<string, unknown>; approved_revision: number | null; approved_by: string | null; current: boolean; approved: boolean; approval_source_revision?: number | null } | null;
    triage_decisions?: Array<{ id: string; result: string; reason_code: string; evidence: Record<string, unknown>; input_revision: number; admission_revision: number }>;
    issue_context?: { digest: string; facts: { issue: Record<string, unknown>; comments: unknown[]; pull_requests: Array<{ id: number; number: number; title: string; html_url: string; state: string }> } } | null;
    publication_intents?: Array<{id: string; kind: string; state: string; request: Record<string, unknown>; result: Record<string, unknown> | null}>;
    repair_sessions?: Array<{id: string; state: string; frozen: Record<string, unknown> | null; evidence: Record<string, unknown> | null}>;
    relation_decisions?: unknown[];
    candidate_issue_decisions?: unknown[];
    distillation_intents?: unknown[];
    pr_lifecycle?: {
        number: number; delivery_state: string; issue_state: string; ci_state: string;
        head_sha: string; base_sha: string; merge_sha: string | null; snapshot_sha256: string;
        observed_at: number;
        feedback_sha256: string;
        snapshot: Record<string, unknown>;
        scope_decisions: Array<{context_sha256: string; decision: string; analysis: Record<string, unknown>}>;
    } | null;
    artifacts?: Array<{id: string; sha256: string; available: number; context: Record<string, unknown>}>;
    budget_owner_task_id: string;
    input: Record<string, unknown>;
    configuration: Record<string, unknown>;
    budget: Budget;
    runs?: Array<{
        id: string;
        state: string;
        snapshot: Record<string, unknown>;
    }>;
    progress?: unknown[];
    progress_windows?: unknown[];
    attempts?: unknown[];
};
export type Provider = {
    id: string;
    revision: number;
    revision_id: string;
    credential_revision: number;
    configuration: {
        name: string;
        base_url: string;
        api: string;
        chat_token_parameter?: "max_tokens" | "max_completion_tokens" | null;
        tool_choice?: "auto" | "required";
        models: Array<{
            model: string;
            reasoning_effort?: string | null;
        }>;
    };
};
export type Reason = {
    id: string;
    task_id: string | null;
    code: string;
    revision: number;
    details: Record<string, unknown>;
};
export type Renewal = {
    id: string;
    task_id: string;
    owner_task_id: string;
    revision: number;
    state: string;
    report: Record<string, unknown>;
};
export type Role = {
    binding_key: string;
    revision: number;
    definition: {
        role: string;
        provider_id: string;
        selection: {
            model: string;
            reasoning_effort: string | null;
        };
    };
};
export type GitHubAccount = {
    id: string;
    login: string;
    is_default: number;
    verified_at: number;
    revision: number;
};
export type State = {
    github_accounts?: GitHubAccount[];
    control: {
        paused: number;
        revision: number;
    };
    repositories: Array<{
        id: string;
        github_id: string;
        paused: number;
        revision: number;
        github_owner?: string;
        github_name?: string;
        target_branch?: string;
        target_sha?: string;
        synchronization?: {
            revision: number; active_seconds: number; idle_seconds: number; lookback_seconds: number;
            overlap_seconds: number; full_refresh_seconds: number; running: boolean;
            last_success_at: number | null; last_changed_at: number | null; next_due_at: number;
            retry_at: number; error_code: string | null; last_result: string | null;
            requested_sequence: number; completed_sequence: number;
            ci_policy: {configured: boolean; required: unknown[]};
        } | null;
        contribution_path?: string;
        credential_reference?: string;
        identity?: { execution_login: string } | null;
        import_epoch?: number;
        scan_ready_sha?: string;
        publication?: { owner: string; name: string };
        policy?: { mode: string; allowed_labels: string[]; excluded_labels: string[]; severity_labels: Record<string, number>; claim_comment: boolean; claim_template: string };
        scan_policy?: { chunk_bytes?: number; exclusions?: Record<string, string> };
    }>;
    tasks: Task[];
    providers: Provider[];
    roles: Role[];
    reasons: Reason[];
    renewals: Renewal[];
    diagnostics: Array<{
        task_id: string;
        kind: string;
        state: string;
        result: unknown;
    }>;
    capabilities: unknown[];
};
export type BusinessEvent = {
    sequence: number;
    task_id: string | null;
    kind: string;
    payload: Record<string, unknown>;
};
const errors: Record<string, string> = {
    github_account_missing: "GitHub 账号已解绑，请重新选择。",
    github_account_mismatch: "令牌对应的 GitHub 用户与所选账号不符。",
    github_account_exists: "该 GitHub 账号已绑定，请使用「更换令牌」。",
    github_account_in_use: "仍有仓库使用该账号，请先在仓库页改绑。",
    github_account_preflight_stale: "预检使用的账号凭据已变更，请重新预检。",
    github_authentication_or_permission: "GitHub 拒绝了请求。请检查令牌是否有效，并确认目标仓库及权限已授权。",
    github_not_found: "GitHub 找不到目标资源，或所选账号没有访问权限。",
    github_direct_write_permission_missing: "所选账号没有上游仓库的推送权限，请检查授权或选择 Fork 路径。",
    github_fork_write_or_relationship_missing: "Fork 必须属于当前账号、指向指定上游，并允许该账号推送。",
    github_issue_path_disabled: "上游仓库未启用 Issues。",
    github_rate_limited: "GitHub 限制了请求频率，请稍后重试。",
    github_transport_or_protocol_error: "暂时无法与 GitHub 通信，请稍后重试。",
    session_required: "控制会话已失效，请从本地启动入口重新打开。",
    revision_or_operation_conflict: "记录已变化或操作标识冲突，请刷新核对后重新提交。",
    operation_not_permitted: "当前前置条件未满足，请先处理阻塞、预算或旧运行。",
    invalid_request: "提交内容无效，请核对必填项和有限额度。",
    storage_unavailable: "无法持久化，执行已停止；请修复存储并核对运行。",
    credential_or_provider_unavailable: "凭据或供应商保存未完成，请核对配置和批次状态。",
};
let csrf = "";
let initialization: Promise<void> | undefined;
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
    const response = await fetch(`/api/v1${path}`, { credentials: "same-origin", ...options });
    const data = await response.json();
    if (!response.ok)
        throw new Error(errors[data.error] ?? `请求失败：${data.error ?? response.status}`);
    return data as T;
}
export function initialize(): Promise<void> {
    if (initialization)
        return initialization;
    initialization = (async () => {
        const parameters = new URLSearchParams(location.hash.slice(1));
        const token = parameters.get("session");
        if (token) { history.replaceState(null, "", location.pathname + location.search); sessionStorage.removeItem("repowatch-event-cursor"); }
        const session = token
            ? await request<{
                csrf: string;
            }>("/session/bootstrap", { method: "POST", headers: { Authorization: `Bearer ${token}` } })
            : await request<{
                csrf: string;
            }>("/session");
        csrf = session.csrf;
    })();
    return initialization;
}
export function mutate<T>(path: string, data: Record<string, unknown>, expectedRevision?: number, operationId: string = crypto.randomUUID()): Promise<T> {
    return request<T>(path, { method: "POST", headers: { "Content-Type": "application/json", "X-RepoWatch-Session": csrf }, body: JSON.stringify({ operation_id: operationId, expected_revision: expectedRevision, data }) });
}
export const initialAmounts: Amounts = { active_ms: 600000, model_calls: 30, tokens: 100000, repair_rounds: 5, recovery_rounds: 5 };
export const labels: Record<keyof Amounts, string> = { active_ms: "有效时长（毫秒）", model_calls: "模型请求", tokens: "Tokens", repair_rounds: "修复轮数", recovery_rounds: "恢复轮数" };
