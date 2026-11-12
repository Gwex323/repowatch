import { useEffect, useRef, useState, type ReactNode } from "react";
import { request, type State, type Task } from "./client";
import { MemoryPanel } from "./Memory";
import { Repositories } from "./Repositories";
import { Scans, ScanSummary, type Batch } from "./Scans";
import { countAttention, deliveryStatus, issueNumber, repositoryName, shortSha, taskStatus, taskTitle, type Repository, type Run } from "./workbench";

const sections = { overview: "概览", analysis: "分析", issues: "问题与修复", config: "配置" };
type Section = keyof typeof sections;

function WorkbenchRepository({ repository, state, run, openTask, openInbox, scanEvent, selectedTask, renderDetail, closeTask, busy }: {
    repository: Repository; state: State; run: Run; openTask: (id: string) => void; scanEvent: number;
    selectedTask: Task | null; renderDetail: (openSource?: () => void) => ReactNode;
    closeTask: () => void; busy: boolean;
    openInbox: (repositoryId: string) => void;
}) {
    const [tab, setTab] = useState<Section>("overview");
    const [batches, setBatches] = useState<Batch[] | null>(null);
    const [loadError, setLoadError] = useState("");
    const [sourceTaskId, setSourceTaskId] = useState<string | null>(null);
    const [memory, setMemory] = useState(false);
    const detail = Boolean(selectedTask);
    const newest = useRef(0);
    const reload = async () => {
        const read = ++newest.current;
        setLoadError("");
        try {
            const result = await request<{ batches: Batch[] }>(`/repositories/${encodeURIComponent(repository.id)}/scans`);
            if (read === newest.current) setBatches(result.batches.sort((a, b) => b.created_at - a.created_at));
        } catch (failure) {
            if (read === newest.current) { setBatches([]); setLoadError(failure instanceof Error ? failure.message : "无法读取扫描结果。"); }
        }
    };
    useEffect(() => { void reload(); return () => { newest.current++; }; }, [repository.revision, scanEvent]);
    const tasks = state.tasks.filter(task => task.repository_id === repository.id);
    const issues = tasks.filter(task => task.issue_id);
    const deliveries = issues.filter(task => task.delivery);
    const attention = countAttention(state, repository.id);
    const scanTasks = tasks.filter(task => task.mode === "scan" && !["ended", "cancelled"].includes(task.state));
    const switchTab = (value: Section) => { setTab(value); closeTask(); };
    return <>
        <div className="workbench-intro">
            <h1>{repositoryName(repository)}</h1>
            <p>{repository.target_branch ? `目标分支：${repository.target_branch}` : "尚未配置目标分支"}{repository.paused ? " · 已暂停" : ""}</p>
        </div>
        <nav className="workbench-tabs" aria-label="仓库页面">
            {Object.entries(sections).map(([key, title]) => <button key={key} aria-current={tab === key ? "page" : undefined}
                className={tab === key ? "active" : ""} onClick={() => switchTab(key as Section)}>{title}</button>)}
        </nav>
        <div hidden={detail || tab !== "overview"} className="workbench-grid">
            {attention > 0 && <section className="workbench-panel workbench-wide">
                <div className="workbench-line"><div><h2>需要你处理</h2><p>{attention} 项需要判断或操作。</p></div>
                    <button onClick={() => openInbox(repository.id)}>查看此仓库的待办 →</button></div>
                {tasks.filter(task => task.triage_reason === "issue_lookup_required").map(task => <div key={task.id} className="workbench-row workbench-line">
                    <p>{taskStatus(task)}</p><button onClick={() => openTask(task.id)}>对比依据 →</button>
                </div>)}
            </section>}
            <div className="workbench-stack">
                {loadError && <p role="alert" className="danger">{loadError}</p>}
                <ScanSummary repository={repository} batches={batches} openAnalysis={() => switchTab("analysis")}/>
                {scanTasks.filter(task => task.budget.status === "configuration_required").map(task => <section key={task.id} className="workbench-panel">
                    <h2>扫描待配置</h2><p>扫描已登记，首次运行前需要配置预算。</p>
                    <button onClick={() => openTask(task.id)}>查看扫描预算与配置 →</button>
                </section>)}
            </div>
            <section className="workbench-panel">
                <h2>修复交付</h2>
                {deliveries.length > 0 ? deliveries.slice(0, 3).map(task => <div key={task.id} className="workbench-row">
                    <h3>{taskTitle(task)}</h3><p>PR #{task.delivery!.number} · {deliveryStatus(task.delivery!)}</p>
                    <button onClick={() => openTask(task.id)}>查看修复详情 →</button>
                </div>) : <p>{issues.length ? `${issues.length} 个 Issue 正在跟进，尚无 PR 记录。` : "暂未跟进 Issue。"}</p>}
                <button onClick={() => switchTab("issues")}>查看全部问题与修复 →</button>
            </section>
        </div>
        <div hidden={detail || tab !== "analysis"}>
            <Scans repository={repository} batches={batches} loadingError={loadError} reload={reload} state={state}
                run={run} openTask={openTask} sourceTaskId={sourceTaskId} onSourceShown={() => setSourceTaskId(null)}/>
        </div>
        {tab === "issues" && !detail && <section className="workbench-panel">
            <h2>仓库 Issue</h2>
            {issues.map(task => <div key={task.id} className="workbench-row workbench-line">
                <div><h3>{taskTitle(task)}</h3><p>{taskStatus(task)}{task.delivery && ` · PR #${task.delivery.number} · ${deliveryStatus(task.delivery)}`}</p></div>
                <button onClick={() => openTask(task.id)}>{issueNumber(task) ? "查看 Issue 与修复 →" : "查看详情 →"}</button>
            </div>)}
            {!issues.length && <p>暂无可跟进的 GitHub Issue。可先查看分析发现。</p>}
        </section>}
        {tab === "config" && !detail && <div className="workbench-stack">
            <Repositories repositoryId={repository.id} state={state} run={run}/>
            <section className="workbench-panel">
                <div className="workbench-line"><h2>仓库记忆</h2><button disabled={busy} onClick={() => setMemory(!memory)}>{memory ? "收起记忆" : "查看与管理记忆"}</button></div>
                {memory && <MemoryPanel repositoryId={repository.id} state={state} run={run}/>}
            </section>
        </div>}
        {selectedTask && renderDetail(batches?.some(batch => batch.candidates.some(candidate => candidate.request_task_id === selectedTask.id)) ?
            () => { setSourceTaskId(selectedTask.id); setTab("analysis"); closeTask(); } : undefined)}
    </>;
}

export function RepositoryWorkbench({ state, run, openTask, openInbox, closeTask, selectedTask, renderDetail, scanEvent, busy }: {
    state: State; run: Run; openTask: (id: string) => void; closeTask: () => void; selectedTask: Task | null;
    renderDetail: (openSource?: () => void) => ReactNode; scanEvent: number; busy: boolean;
    openInbox: (repositoryId: string) => void;
}) {
    const [id, setId] = useState<string | null>(null);
    const [adding, setAdding] = useState(false);
    const repository = state.repositories.find(repo => repo.id === id);
    if (repository) return <div className="workbench-stack">
        <div><button onClick={() => { closeTask(); setId(null); }}>← 所有仓库</button></div>
        <WorkbenchRepository key={repository.id} repository={repository} state={state} run={run} openTask={openTask} openInbox={openInbox} scanEvent={scanEvent}
            selectedTask={selectedTask} renderDetail={renderDetail} closeTask={closeTask} busy={busy}/>
    </div>;
    return <div className="workbench-stack">
        <div className="workbench-line workbench-intro">
            <div><h1>仓库工作台</h1><p>查看各仓库的分析进度、待办与修复交付。</p></div>
            <button onClick={() => setAdding(!adding)}>{adding ? "收起登记表单" : "登记仓库"}</button>
        </div>
        {adding && <Repositories state={state} run={run}/>}
        <section className="workbench-panel">
            {state.repositories.length ? <table className="workbench-matrix" aria-label="各仓库进度">
                <thead><tr><th scope="col">仓库</th><th scope="col">分析进度</th><th scope="col">待我处理</th><th scope="col">修复交付</th></tr></thead>
                <tbody>{state.repositories.map(repo => {
                    const tasks = state.tasks.filter(task => task.repository_id === repo.id);
                    const scan = tasks.find(task => task.mode === "scan" && task.state === "running");
                    const prs = tasks.flatMap(task => task.delivery ? [task.delivery] : []);
                    const actionCount = countAttention(state, repo.id);
                    const scanLabel = repo.paused ? "仓库已暂停" : scan ? "扫描中" : repo.scan_ready_sha && repo.scan_ready_sha === repo.target_sha ?
                        "已登记扫描结果" : repo.target_sha ? "待扫描或核验" : "尚未观测";
                    return <tr key={repo.id}>
                        <td><button onClick={() => { closeTask(); setAdding(false); setId(repo.id); }}>{repositoryName(repo)} →</button></td>
                        <td><span className="workbench-mobile-label">分析进度：</span><span className="badge">{scanLabel}</span>{repo.target_sha && <p>目标 <code>{shortSha(repo.target_sha)}</code></p>}</td>
                        <td><span className="workbench-mobile-label">待我处理：</span>{actionCount ? `${actionCount} 项需要你处理` : "暂无待办"}</td>
                        <td><span className="workbench-mobile-label">修复交付：</span>{prs.length === 1 ? `PR #${prs[0].number} · ${deliveryStatus(prs[0])}` :
                            prs.length ? `${prs.length} 个 PR 记录` : "暂无 PR"}</td>
                    </tr>;
                })}</tbody>
            </table> : <p>还没有仓库。登记 GitHub 仓库后，可以在这里查看扫描与修复进度。</p>}
        </section>
    </div>;
}
