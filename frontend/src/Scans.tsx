import { useEffect, useState } from "react";
import { mutate, request, type State } from "./client";
import { dedupMessage, issueNumber, shortSha, taskStatus, type Repository, type Run } from "./workbench";

type Entry = { path: string; disposition: string; reason?: string; module: string };
type Candidate = { id: string; revision: number; state: string; unit_id: string; evidence_artifact_id?: string;
    finding: { title: string; basis: string; path: string; sha: string; start: number; end: number }; request_task_id: string | null };
export type Batch = {
    id: string; task_id: string; state: string; evidence_valid: boolean; current_ready: boolean; current_version: boolean;
    created_at: number; version: { target_sha: string; import_epoch: number };
    manifest: { entries: Entry[]; units: Array<{ id: string; path: string; start: number; end: number }> };
    coverage: Array<{ unit_id: string; input_artifact_id: string; result_artifact_id: string }>;
    recorded_coverage?: Batch["coverage"];
    checks: Array<{ id: string; argv: string[]; basis: string }>;
    baselines: Array<{ check_id: string; outcome: string; evidence: { log_artifact_id: string; preparation_report?: unknown } }>;
    attempts?: Array<{ unit_id: string; outcome: string; created_at: number }>;
    candidates: Candidate[];
};
export const currentScan = (batches: Batch[]) =>
    batches.find(batch => batch.current_ready) ?? batches.find(batch => batch.current_version && batch.state !== "superseded");
export const verifiedScan = (batches: Batch[]) =>
    batches.find(batch => batch.state === "completed" && batch.evidence_valid);

export function Artifact({ taskId, id, label }: { taskId: string; id?: string | null; label: string }) {
    const [value, setValue] = useState<{ content: string; truncated: boolean } | null>(null);
    const [error, setError] = useState("");
    const [busy, setBusy] = useState(false);
    if (!id) return <p className="muted">{label}：没有可用记录。</p>;
    return <div className="artifact">
        <button type="button" disabled={busy} onClick={() => {
            if (value) { setValue(null); return; }
            setBusy(true); setError("");
            void request<{ content: string; truncated: boolean }>(`/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(id)}`)
                .then(setValue).catch(error => setError(error.message)).finally(() => setBusy(false));
        }}>{busy ? "正在读取…" : value ? `收起${label}` : label}</button>
        {error && <p role="alert" className="danger">{error}</p>}
        {value && <><pre>{value.content}</pre>{value.truncated && <p>内容过长，已截断。</p>}</>}
    </div>;
}

export function ScanSummary({ repository, batches, openAnalysis }: {
    repository: Repository; batches: Batch[] | null; openAnalysis?: () => void;
}) {
    const batch = batches && currentScan(batches);
    const verified = batches && verifiedScan(batches);
    return <section className="workbench-panel">
        <div className="workbench-line"><h2>扫描覆盖</h2>{batch?.current_ready && <span className="badge ready">当前版本已验证</span>}</div>
        <p>当前目标 <code>{shortSha(repository.target_sha)}</code> · {batches === null ? "正在读取" :
            batch ? `${batch.coverage.length}/${batch.manifest.units.length} 个范围已核验` : "尚无当前版本的扫描结果"}</p>
        {batch && batch.manifest.units.length > 0 && <progress aria-label="当前目标已核验覆盖" max={batch.manifest.units.length} value={batch.coverage.length}/>}
        <p>最近已验证 {verified ? <><code>{shortSha(verified.version.target_sha)}</code> · 扫描完成</> : batches === null ? "正在读取" : "暂无有效结果"}</p>
        {openAnalysis && <button onClick={openAnalysis}>查看分析与发现 →</button>}
    </section>;
}

function BatchDetails({ batch, openTask }: { batch: Batch; openTask: (id: string) => void }) {
    const coverage = new Map(batch.coverage.map(row => [row.unit_id, row]));
    const recorded = new Set(batch.recorded_coverage?.map(row => row.unit_id) ?? []);
    const gaps = batch.manifest.units.filter(unit => !coverage.has(unit.id));
    const invalid = gaps.filter(unit => recorded.has(unit.id)).length;
    const pathGaps = batch.manifest.entries.filter(entry => entry.disposition === "gap");
    return <section className="workbench-panel">
        <h2>扫描缺口</h2>
        <p>{gaps.length ? `${gaps.length} 个范围缺少有效结果${invalid ? `，其中 ${invalid} 个已有记录但证据失效` : ""}。` : "此批次的扫描分块均有有效结果。"}
            {pathGaps.length > 0 && `另有 ${pathGaps.length} 个文件未能读取。`}</p>
        {gaps.length > 0 && <details><summary>查看未覆盖范围</summary><ul>{gaps.map(unit => <li key={unit.id}>{unit.path} · {recorded.has(unit.id) ? "证据已失效" : "尚无有效结果"}</li>)}</ul></details>}
        <button onClick={() => openTask(batch.task_id)}>查看扫描任务与处理入口</button>
        <details><summary>扫描清单与排除依据</summary><ul>{batch.manifest.entries.map(entry => <li key={entry.path}>{entry.path} · {entry.module} · {entry.reason ?? entry.disposition}</li>)}</ul></details>
        <details><summary>分块输入与核验记录</summary>{batch.manifest.units.map(unit => {
            const proof = coverage.get(unit.id);
            return <div className="workbench-row" key={unit.id}><p>{unit.path} · [{unit.start}, {unit.end})</p>
                {proof ? <div className="actions">
                    <Artifact taskId={batch.task_id} id={proof.input_artifact_id} label="查看扫描输入"/>
                    <Artifact taskId={batch.task_id} id={proof.result_artifact_id} label="查看核验记录"/>
                </div> : <p>缺少有效结果</p>}</div>;
        })}</details>
        <details><summary>检查基线与日志</summary>{batch.checks.length === 0 && <p>此批次没有适用的声明检查。</p>}
            {batch.checks.map(check => {
                const baseline = batch.baselines.find(row => row.check_id === check.id);
                return <div className="workbench-row" key={check.id}><p>{check.argv.join(" ")} · {check.basis}</p>
                    <p>{({ passed: "通过", existing_failed: "已有失败", not_applicable: "不适用" })[baseline?.outcome ?? ""] ?? baseline?.outcome ?? "尚未执行"}</p>
                    <Artifact taskId={batch.task_id} id={baseline?.evidence.log_artifact_id} label="查看检查日志"/>
                    {Boolean(baseline?.evidence.preparation_report) && <pre>{JSON.stringify(baseline?.evidence.preparation_report, null, 2)}</pre>}
                </div>;
            })}
        </details>
    </section>;
}

export function Scans({ repository, batches, loadingError, reload, state, run, openTask, sourceTaskId, onSourceShown }: {
    repository: Repository; batches: Batch[] | null; loadingError: string; reload: () => Promise<void>;
    state: State; run: Run; openTask: (id: string) => void; sourceTaskId?: string | null; onSourceShown: () => void;
}) {
    const [findingId, setFindingId] = useState<string | null>(null);
    const [version, setVersion] = useState("all");
    const [area, setArea] = useState("");
    const [follow, setFollow] = useState("");
    const [history, setHistory] = useState("");
    const [filtersOpen, setFiltersOpen] = useState(false);
    const list = batches ?? [];
    const current = currentScan(list);
    const verified = verifiedScan(list);
    const selectedBatch = list.find(batch => batch.id === history) ?? current;
    const tasks = new Map(state.tasks.map(task => [task.id, task]));
    const findings = list.flatMap(batch => {
        const byPath = new Map(batch.manifest.entries.map(entry => [entry.path, entry.module]));
        return batch.candidates.map(candidate => ({ candidate, batch, module: byPath.get(candidate.finding.path) || "未分类",
            task: tasks.get(candidate.request_task_id ?? "") }));
    });
    const selected = findings.find(item => item.candidate.id === findingId);
    useEffect(() => {
        if (sourceTaskId && batches) {
            const source = batches.flatMap(batch => batch.candidates).find(candidate => candidate.request_task_id === sourceTaskId);
            if (source) setFindingId(source.id);
            onSourceShown();
        }
    }, [sourceTaskId, batches]);
    const filtered = findings.filter(({ batch, module, candidate, task }) =>
        (version === "all" || (version === "current" ? batch.id === current?.id : batch.id === verified?.id)) &&
        (!area || area === module) && (!follow ||
            (follow === "linked" ? Boolean(issueNumber(task)) : follow === "review" ? task?.triage_reason === "issue_lookup_required" : candidate.state === "local")));
    const decide = (candidate: Candidate, action: "select" | "exclude") => run(async () => {
        await mutate(`/scan-candidates/${encodeURIComponent(candidate.id)}/decide`, { action }, candidate.revision);
        await reload();
    }, action === "select" ? "已登记。系统会先查重，再决定如何跟进。" : "已排除此发现。");
    if (selected) {
        const { candidate, batch, task } = selected;
        const evidence = batch.coverage.find(row => row.unit_id === candidate.unit_id && row.result_artifact_id === candidate.evidence_artifact_id);
        return <section className="workbench-panel">
            <button onClick={() => setFindingId(null)}>← 返回扫描发现</button>
            <h2>{candidate.finding.title}</h2>
            <p>{candidate.finding.basis}</p>
            <p>代码位置 <code>{candidate.finding.path}</code> · 提交 <code>{candidate.finding.sha}</code> · 字节范围 [{candidate.finding.start}, {candidate.finding.end})</p>
            <Artifact taskId={batch.task_id} id={evidence?.result_artifact_id} label="查看扫描依据"/>
            {task && <p>{taskStatus(task)}</p>}
            <div className="actions">
                {candidate.state === "local" && <button onClick={() => void decide(candidate, "select")}>开始处理</button>}
                {task && <button onClick={() => openTask(task.id)}>{issueNumber(task) ? `Issue #${issueNumber(task)}` :
                    task.triage_reason === "issue_lookup_required" ? "对比依据" : "查看处理进度"}</button>}
                {candidate.state !== "excluded" && <button onClick={() => void decide(candidate, "exclude")}>排除此发现</button>}
            </div>
        </section>;
    }
    return <>
        {loadingError && <p role="alert" className="danger">{loadingError}</p>}
        <div className="workbench-grid">
            <div className="workbench-stack">
                <ScanSummary repository={repository} batches={batches}/>
                <section className="workbench-panel workbench-line">
                    <div><label htmlFor="scan-batch">查看扫描批次</label><select id="scan-batch" value={history} onChange={event => setHistory(event.target.value)}>
                        <option value="">当前目标</option>{list.map(batch => <option key={batch.id} value={batch.id}>
                            {shortSha(batch.version.target_sha)} · 导入 {batch.version.import_epoch} · {batch.evidence_valid && batch.state === "completed" ? "已验证" : "未验证"}
                        </option>)}
                    </select></div>
                    <button onClick={() => void reload()}>刷新扫描</button>
                </section>
                {selectedBatch && <BatchDetails key={selectedBatch.id} batch={selectedBatch} openTask={openTask}/>}
            </div>
            <section className="workbench-panel">
                <h2>扫描发现</h2>
                {filtered.map(({ candidate, task, batch, module }) => <div className="workbench-row" key={candidate.id}>
                    <div className="workbench-line"><h3>{candidate.finding.title}</h3>
                        {candidate.state !== "excluded" && task && issueNumber(task) && <button onClick={() => openTask(task.id)}>Issue #{issueNumber(task)}</button>}
                    </div>
                    {candidate.state === "excluded" ? <p>已排除此发现</p> :
                        task?.triage_reason === "issue_lookup_required" && <p className="workbench-callout">{dedupMessage(task)}</p>}
                    <p className="muted">{batch.id === current?.id ? "当前目标的扫描" : batch.id === verified?.id ? "上次已验证的扫描" : "历史扫描"} · {module}</p>
                    <div className="actions">
                        <button onClick={() => setFindingId(candidate.id)}>查看发现依据 →</button>
                        {candidate.state === "local" && <button onClick={() => void decide(candidate, "select")}>开始处理</button>}
                        {candidate.state !== "excluded" && task && !issueNumber(task) && <button onClick={() => openTask(task.id)}>{task.triage_reason === "issue_lookup_required" ? "对比依据 →" : taskStatus(task)}</button>}
                    </div>
                </div>)}
                {!filtered.length && <p>{batches === null ? "正在读取扫描发现…" : findings.length ? "没有符合筛选条件的发现。" : "暂无扫描发现。"}</p>}
                {findings.length > 0 && <details open={filtersOpen} onToggle={event => setFiltersOpen(event.currentTarget.open)}><summary>筛选发现{version !== "all" || area || follow ? " · 已启用" : ""}</summary>
                    <div className="fields">
                        <div><label htmlFor="finding-scan">发现于哪次扫描</label><select id="finding-scan" value={version} onChange={event => setVersion(event.target.value)}><option value="all">全部扫描</option><option value="current">当前目标的扫描</option><option value="verified">最近已验证的扫描</option></select></div>
                        <div><label htmlFor="finding-area">代码区域</label><select id="finding-area" value={area} onChange={event => setArea(event.target.value)}><option value="">全部区域</option>{Array.from(new Set(findings.map(row => row.module))).sort().map(item => <option key={item}>{item}</option>)}</select></div>
                        <div><label htmlFor="finding-status">跟进进度</label><select id="finding-status" value={follow} onChange={event => setFollow(event.target.value)}><option value="">全部进度</option><option value="local">尚未处理</option><option value="review">相似 Issue 待确认</option><option value="linked">由 Issue 跟进</option></select></div>
                    </div>
                </details>}
            </section>
        </div>
    </>;
}
