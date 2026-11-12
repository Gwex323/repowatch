import { useState } from "react";
import { mutate, type Task } from "./client";
import { Artifact } from "./Scans";
import { record, shortSha, text, type Run } from "./workbench";

export function IssueScope({ task, run, onUpdated, openTask }: {
    task: Task; run: Run; onUpdated: () => Promise<void>; openTask: (id: string) => void;
}) {
    const scope = task.scope;
    const [kind, setKind] = useState("converge");
    const [goalIndex, setGoalIndex] = useState(0);
    const [goals, setGoals] = useState(() => scope?.goals.map(objective => ({ objective, criteria: "" })) ?? []);
    const [explanation, setExplanation] = useState("");
    if (task.scope_children?.length) return <section className="workbench-panel">
        <h3>独立目标的处理进度</h3><p>原 Issue 保留为跟踪项。各目标先查重，再由对应 Issue 独立跟进。</p>
        <div className="actions">{task.scope_children.map((child, index) => <button key={child.source_key} onClick={() => openTask(child.task_id)}>查看目标 {index + 1} →</button>)}</div>
    </section>;
    if (!scope?.can_decide) return null;
    const selected = kind === "converge" ? [goalIndex] : goals.map((_, index) => index);
    const valid = explanation.trim() && selected.every(index => goals[index]?.objective.trim() && goals[index]?.criteria.trim());
    return <section className="workbench-panel">
        <h3>确认 Issue 的交付范围</h3><p>系统发现了多个独立目标。可收敛为一个目标，或分别查重后用独立 Issue 跟进。</p>
        <div><label htmlFor="scope-kind">处理方式</label><select id="scope-kind" value={kind} onChange={event => setKind(event.target.value)}>
            <option value="converge">收敛为一个目标</option><option value="split" disabled={!scope.can_split}>拆成独立目标</option>
        </select></div>
        {!scope.can_split && <p>已有受管 PR 或发布意图，请保留原交付链并收敛范围。</p>}
        {kind === "converge" && <div><label htmlFor="scope-goal">保留的目标</label><select id="scope-goal" value={goalIndex} onChange={event => setGoalIndex(Number(event.target.value))}>
            {scope.goals.map((goal, index) => <option key={index} value={index}>{goal}</option>)}
        </select></div>}
        {selected.map(index => <div className="workbench-row" key={index}>
            <label htmlFor={`scope-title-${index}`}>目标 {index + 1}</label><input id={`scope-title-${index}`} value={goals[index].objective}
                onChange={event => setGoals(previous => previous.map((goal, i) => i === index ? { ...goal, objective: event.target.value } : goal))}/>
            <label htmlFor={`scope-criteria-${index}`}>目标 {index + 1} 的验收条件（每行一项）</label>
            <textarea id={`scope-criteria-${index}`} value={goals[index].criteria}
                onChange={event => setGoals(previous => previous.map((goal, i) => i === index ? { ...goal, criteria: event.target.value } : goal))}/>
        </div>)}
        <label htmlFor="scope-explanation">范围判断依据</label><textarea id="scope-explanation" value={explanation} onChange={event => setExplanation(event.target.value)}/>
        <p>{kind === "split" ? "确认后只登记处理请求，不修改或关闭原 Issue。各目标共用原累计预算。" : "确认后按所选目标重新分诊，不修改远端 Issue 正文。"}</p>
        <button disabled={!valid || task.state === "running"} onClick={() => void run(async () => {
            await mutate(`/tasks/${encodeURIComponent(task.id)}/scope/decide`, {
                context_digest: scope.context_digest, kind, explanation,
                goals: selected.map(index => ({ objective: goals[index].objective.trim(), criteria: goals[index].criteria.split("\n").map(line => line.trim()).filter(Boolean) })),
            }, task.revision);
            await onUpdated();
        }, "范围决定已保存，系统将按确认的目标重新核验。")}>{kind === "split" ? "确认拆分并逐项查重" : "确认收敛范围"}</button>
    </section>;
}

export function CandidateComparison({ task, run, onUpdated }: { task: Task; run: Run; onUpdated: () => Promise<void> }) {
    const [chosen, setChosen] = useState<number | null>(null);
    const [explanation, setExplanation] = useState("");
    const decision = task.triage_decisions?.filter(row => row.reason_code === "issue_lookup_required"
        && row.input_revision === task.input_revision && row.feedback_revision === task.feedback_revision
        && typeof row.evidence.lookup_digest === "string").sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))[0];
    if (task.state !== "waiting" || !decision) return null;
    const evidence = decision.evidence;
    const relation = record(record(evidence.analysis).issue_relation);
    const issues = (Array.isArray(evidence.issues) ? evidence.issues.map(record) : [])
        .filter((row): row is Record<string, unknown> & { number: number } => typeof row.number === "number" && row.number > 0);
    const suggested = issues.find(row => row.number === relation.issue_number);
    const match = issues.find(row => row.number === (chosen ?? suggested?.number));
    const reasons = Array.isArray(relation.evidence) ? relation.evidence.map(text).filter(Boolean) : [text(relation.evidence)].filter(Boolean);
    const submit = (choice: "same" | "none") => run(async () => {
        await mutate(`/tasks/${encodeURIComponent(task.id)}/candidate-issue/decide`, {
            context_digest: evidence.lookup_digest, decision: choice, issue_number: choice === "same" ? match?.number : null, explanation,
        }, task.revision);
        await onUpdated();
    }, "判断已保存；系统将重新核对后再继续。");
    return <article className="workbench-panel">
        <h3>{suggested ? `发现相似 Issue #${suggested.number}，尚无法确认是同一问题` : "系统已查重，仍无法确认是否有相同 Issue"}</h3>
        <p>{text(record(task.input.finding).basis) || "在下方核对已有 Issue 与扫描发现是否描述同一问题。"}</p>
        {reasons.length > 0 ? <><h4>系统的判断依据</h4><ul>{reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul></> :
            <p>没有可单独展示的判断说明，可查看完整查重记录。</p>}
        {issues.length > 0 && <div><label htmlFor="compare-issue">对比的 Issue</label><select id="compare-issue" value={match?.number ?? ""} onChange={event => setChosen(event.target.value ? Number(event.target.value) : 0)}>
            <option value="">选择已有 Issue</option>{issues.map(issue => <option key={issue.number} value={issue.number}>
                #{issue.number} · {text(issue.title) || "未保存标题"}
            </option>)}
        </select></div>}
        {match && <div className="workbench-row"><h4>Issue #{match.number} · {text(match.title)}</h4><p className="workbench-body">{text(match.body) || "此 Issue 没有可查看的正文。"}</p></div>}
        <Artifact taskId={task.id} id={text(evidence.lookup_artifact_id)} label="查看完整查重记录"/>
        <label>你的判断依据<textarea value={explanation} onChange={event => setExplanation(event.target.value)} required/></label>
        <div className="actions">
            <button disabled={!match || !explanation.trim()} onClick={() => void submit("same")}>确认同一问题{match ? ` · Issue #${match.number}` : ""}</button>
            <button disabled={!explanation.trim()} onClick={() => void submit("none")}>确认完整查重无相似 Issue</button>
        </div>
    </article>;
}

export function RepairEvidence({ task }: { task: Task }) {
    const sessions = task.repair_sessions ?? [];
    const session = [...sessions].reverse().find(item => item.state === "verified" && item.evidence?.passed === true && item.evidence?.review_passed === true);
    const frozen = record(session?.frozen);
    const evidence = record(session?.evidence);
    const before = record(evidence.base_probe);
    const after = record(evidence.candidate_probe);
    const review = record(evidence.review);
    const probe = record(evidence.probe);
    const comparisons = Array.isArray(evidence.comparisons) ? evidence.comparisons.map(record) : [];
    const latest = session?.current_context === true;
    const reviewId = task.artifacts?.find(item => item.context.producer === "independent_review" &&
        item.context.candidate_sha === frozen.sha && Array.isArray(evidence.artifact_ids) && evidence.artifact_ids.includes(item.id))?.id;
    return <article className="workbench-panel">
        <div className="workbench-line"><h3>修复与验证</h3>{session && <span className={`badge ${latest ? "ready" : ""}`}>{latest ? "修复验证通过" : "有历史验证结果"}</span>}</div>
        {!session ? <p>暂无通过宿主核验的修复证据。</p> : <>
            <p>验证版本 <code>{shortSha(text(frozen.sha))}</code>{!latest && " · 不代表当前输入或返修版本已通过"}{latest && " · 发布前仍会重新核验实际内容和证据"}</p>
            <details><summary>原问题如何复现</summary><p>{text(probe.target_failure) || "修改前探针记录了目标失败。"}</p>
                <Artifact key={text(before.log_artifact_id)} taskId={task.id} id={text(before.log_artifact_id)} label="查看修改前日志"/></details>
            <details><summary>改了什么</summary><p>修复候选基于 <code>{shortSha(text(frozen.base_sha))}</code>，冻结提交 <code>{text(frozen.sha) || "未记录"}</code>。</p>
                <p>有受管 PR 时，可在 PR 区域查看完整改动。</p></details>
            <details><summary>如何验证</summary><p>{text(probe.target_success) || "修改后探针通过。"}{comparisons.length > 0 && ` · ${comparisons.filter(row => row.passed === true).length}/${comparisons.length} 项检查对比通过`}</p>
                <Artifact key={text(after.log_artifact_id)} taskId={task.id} id={text(after.log_artifact_id)} label="查看修改后日志"/></details>
            <details><summary>独立审查</summary><p>{text(review.rationale) || "已通过与冻结版本绑定的独立审查。"}</p>
                <Artifact key={reviewId} taskId={task.id} id={reviewId} label="查看独立审查记录"/></details>
        </>}
    </article>;
}
