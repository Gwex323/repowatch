import { useCallback, useEffect, useState } from "react";
import { mutate, request } from "./client";

type Run = (action: () => Promise<unknown>, message?: string) => Promise<void>;
type Policy = { minimum_free_bytes: number; artifact_days: number; log_days: number; workspace_days: number };
type Operations = {
    revision: number; policy: Policy; native: {enabled: boolean}; schema: string; data_dir: string; restore_required: boolean;
    disk: { free: number; total: number }; service: { state: string; clean_stop: boolean | null };
    backups: Array<{ id: string; created_at: number }>;
    dependencies: unknown; native_capability: { supported: boolean; reason: string };
};
type Cleanup = { dry_run: boolean; plan_sha256: string; candidates: Array<{ kind: string; id: string }>; protected: Array<{ kind: string; id: string }> };

export function OperationsPanel({ run }: { run: Run }) {
    const [state, setState] = useState<Operations | null>(null);
    const [policy, setPolicy] = useState<Policy | null>(null);
    const [cleanup, setCleanup] = useState<Cleanup | null>(null);
    const [error, setError] = useState("");
    const [confirm, setConfirm] = useState(false);
    const [restoreReport, setRestoreReport] = useState<{report_id: string; basis_sha256: string; ready: boolean} | null>(null);
    const [restoreExplanation, setRestoreExplanation] = useState("");
    const load = useCallback(async () => {
        const value = await request<Operations>("/operations");
        setState(value); setPolicy(value.policy);
    }, []);
    useEffect(() => { void load().catch(failure => setError(String(failure.message))); }, [load]);
    if (!state || !policy) return <section><h2>系统与维护</h2><p role="alert">{error || "正在读取系统状态…"}</p></section>;
    const labels: Record<keyof Policy, string> = {
        minimum_free_bytes: "最低可用磁盘（字节）", artifact_days: "完成任务产物保留天数",
        log_days: "完成任务日志保留天数", workspace_days: "完成任务工作区保留天数",
    };
    return <section>
        <h2>系统与维护</h2>
        <p>服务状态：{state.service.state} · 数据版本：{state.schema}</p>
        <p>本地数据目录：<code>{state.data_dir}</code></p>
        <p>磁盘可用：{(state.disk.free / 1024 ** 3).toFixed(2)} GiB／{(state.disk.total / 1024 ** 3).toFixed(2)} GiB</p>
        <p>关闭浏览器后后台服务继续运行。升级和恢复前，请在本地启动器停止服务；恢复后先核对凭据、工作区和 GitHub 远端，再明确恢复。</p>
        <button onClick={() => void run(load, "系统状态已回读。")}>刷新系统状态</button>
        <details><summary>依赖与支持边界</summary><pre>{JSON.stringify(state.dependencies, null, 2)}</pre>
            <p>{state.native_capability.reason}</p>
            <p>默认使用 Windows 宿主与 Linux Docker 工作区。模型密钥、业务库、控制会话和 Docker socket 不挂入仓库容器。</p>
            <p>原生执行配置：{state.native.enabled ? state.native_capability.supported ? "已显式开启，当前宿主能力核验通过" : "已显式开启，但能力尚未准入，仍拒绝执行" : "未开启"}</p>
            <button onClick={() => void run(async () => { await mutate("/operations/native", {enabled: !state.native.enabled}, state.revision); await load(); }, "原生配置已保存；能力未通过的宿主仍拒绝执行。")}>{state.native.enabled ? "关闭原生执行配置" : "显式开启原生执行配置"}</button>
        </details>
        <h3>一致备份</h3>
        <p>请先全局暂停并等待运行安全结束。备份取得所有写者的维护屏障并核对数据库与归档；不会清除现有暂停原因，也不导出明文凭据。</p>
        <button onClick={() => void run(async () => { await mutate("/operations/backup", {}); await load(); }, "一致备份已完成并核验。")}>创建一致备份</button>
        <ul>{state.backups.map(item => <li key={item.id}><code>{item.id}</code> · {new Date(item.created_at * 1000).toLocaleString()}</li>)}</ul>
        <h3>恢复后的远端与凭据核对</h3>
        <p>读取当前仓库身份、目标分支及全部相关 PR，验证凭据引用；工作区必须重新物化。确认只恢复可核实的唯一交付绑定，不能解除其他阻塞或自动恢复队列。</p>
        <button disabled={!state.restore_required} onClick={() => void run(async () => { setRestoreReport(await mutate("/operations/restore-report", {})); }, "只读恢复核对报告已保存，队列保持暂停。")}>生成恢复核对报告</button>
        {!state.restore_required && <p>当前没有待完成的备份恢复核对。</p>}
        {restoreReport && <div><pre>{JSON.stringify(restoreReport, null, 2)}</pre>
            <label>恢复核对依据<textarea value={restoreExplanation} onChange={event => setRestoreExplanation(event.target.value)} required /></label>
            <button disabled={!restoreReport.ready || !restoreExplanation.trim()} onClick={() => void run(async () => { await mutate("/operations/restore-acknowledge", {report_id: restoreReport.report_id, basis_sha256: restoreReport.basis_sha256, explanation: restoreExplanation}); setRestoreReport(null); await load(); }, "恢复核对已确认；交付绑定已回读，仍需单独明确恢复。")}>确认当前恢复核对</button>
        </div>}
        <h3>保留策略</h3>
        <form onSubmit={event => { event.preventDefault(); void run(async () => { await mutate("/operations/policy", policy, state.revision); await load(); setCleanup(null); setConfirm(false); }, "保留策略已保存。尚未执行清理。"); }}>
            {(Object.keys(labels) as Array<keyof Policy>).map(key => <label key={key}>{labels[key]}<input type="number" required min={key === "minimum_free_bytes" ? 1048576 : 1} step="1" value={policy[key]} onChange={event => { setPolicy({ ...policy, [key]: Number(event.target.value) }); setCleanup(null); setConfirm(false); }} /></label>)}
            <button type="submit">保存保留策略</button>
        </form>
        <p>未完成／阻塞任务和记忆来源证据始终受保护。清理先使旧证据失效，后续验收不能复用已删除内容。</p>
        <button onClick={() => void run(async () => { setCleanup(await mutate<Cleanup>("/operations/cleanup", { dry_run: true })); setConfirm(false); }, "清理预览已生成，未删除文件。")}>预览安全清理</button>
        {cleanup && <div><p>候选 {cleanup.candidates.length} 项，受保护 {cleanup.protected.length} 项。</p>
            <pre>{JSON.stringify(cleanup, null, 2)}</pre>
            <label><input type="checkbox" checked={confirm} onChange={event => setConfirm(event.target.checked)} />我已核对候选，确认清理符合当前保留策略的完成任务内容</label>
            <button disabled={!confirm || !cleanup.dry_run || cleanup.candidates.length === 0} onClick={() => void run(async () => { setCleanup(await mutate<Cleanup>("/operations/cleanup", { dry_run: false, plan_sha256: cleanup.plan_sha256 })); setConfirm(false); await load(); }, "安全清理已完成，证据可用状态已更新。")}>执行安全清理</button>
        </div>}
    </section>;
}
