import { useState } from "react";
import { mutate, type State, type Task } from "./client";

export function PullRequestLifecycle({ task, repository, run, onUpdated }: {
    task: Task; run: (action: () => Promise<unknown>, message?: string) => Promise<void>;
    repository?: State["repositories"][number];
    onUpdated: () => Promise<void>;
}) {
    const lifecycle = task.pr_lifecycle;
    const [explanation, setExplanation] = useState("");
    if (!lifecycle) return null;
    const pull = lifecycle.snapshot.pull as { html_url?: string } | undefined;
    const proposedUrl = pull?.html_url ?? (repository?.github_owner && repository?.github_name
        ? `https://github.com/${encodeURIComponent(repository.github_owner)}/${encodeURIComponent(repository.github_name)}/pull/${lifecycle.number}` : "");
    const pullUrl = /^https:\/\/github\.com\/[^/?#]+\/[^/?#]+\/pull\/\d+$/.test(proposedUrl) ? proposedUrl : null;
    const scope = lifecycle.scope_decisions.find(item => item.context_sha256 === lifecycle.feedback_sha256);
    const labels: Record<string, string> = { open: "开放", merged: "已合并", closed: "关闭未合并", success: "通过", pending: "等待", failed: "失败", policy_required: "待确认必要检查" };
    return <article><h3>受管 PR #{lifecycle.number} · {labels[lifecycle.delivery_state] ?? lifecycle.delivery_state}</h3>
        <p>Issue 状态：{lifecycle.issue_state} · 最近观测的 PR CI：{labels[lifecycle.ci_state] ?? lifecycle.ci_state} · {new Date(lifecycle.observed_at * 1000).toLocaleString()}</p>
        <p>head：{lifecycle.head_sha} · base：{lifecycle.base_sha} · 合并 ref：{lifecycle.merge_sha ?? "无"}</p>
        <p>代码验证由修复证据记录；评审批准、PR 合并和 Issue 关闭分别保存。</p>
        {pullUrl && <div className="actions"><a href={`${pullUrl}/checks`} target="_blank" rel="noreferrer">GitHub CI 检查</a><a href={`${pullUrl}/files`} target="_blank" rel="noreferrer">GitHub 评审与变更</a><a href={`${pullUrl}#partial-pull-merging`} target="_blank" rel="noreferrer">GitHub 合并页</a></div>}
        {repository && <button onClick={() => void run(() => mutate(`/repositories/${repository.id}/sync`, {}), "PR 与仓库检查请求已登记；请等待只读同步完成。")}>检查 PR 更新</button>}
        {task.acceptance?.approval_source_revision && <p>当前范围未变化，沿用验收修订 {task.acceptance.approval_source_revision} 的原批准；分析与来源记录已保存。</p>}
        <details><summary>查看 CI 来源、attempt、评审及对应提交</summary><pre>{JSON.stringify(lifecycle.snapshot, null, 2)}</pre></details>
        {scope && <><h4>评审范围判断：{scope.decision}</h4><pre>{JSON.stringify(scope.analysis, null, 2)}</pre></>}
        {scope && ["pending", "revise", "clarify"].includes(scope.decision) && <>
            <p>对照当前验收规范与上方方案。批准只适用于当前意见、输入和验收修订；修改方案请使用验收规范编辑入口。</p>
            <label>决定依据／澄清信息<textarea value={explanation} onChange={event => setExplanation(event.target.value)} /></label>
            {(["approve", "revise", "clarify", "reject"] as const).map(action => <button key={action} disabled={!explanation.trim() || task.state === "running"} onClick={() => void run(async () => {
                await mutate(`/tasks/${task.id}/review-scope/decide`, {context_sha256:lifecycle.feedback_sha256, action, explanation}, task.revision);
                await onUpdated();
            })}>{{approve:"批准方案与验收变化", revise:"要求修改方案", clarify:"补充澄清", reject:"拒绝本次范围变化"}[action]}</button>)}
        </>}
        {task.wait_reason === "pr_merge_conflict" && <p>PR head 与当前 base 存在合并冲突，请在原分支处理后检查更新，再重新分诊。</p>}
    </article>;
}
