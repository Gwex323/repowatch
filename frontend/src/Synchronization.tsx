import { useEffect, useRef, useState, type FormEvent } from "react";
import { mutate, type State } from "./client";

type Run = (action: () => Promise<unknown>, message?: string) => Promise<void>;
const time = (value: number | null | undefined) => value == null ? "尚无记录" : new Date(value * 1000).toLocaleString();

export function Synchronization({ repository, run }: { repository: State["repositories"][number]; run: Run }) {
    const sync = repository.synchronization;
    const [active, setActive] = useState(120);
    const [idle, setIdle] = useState(1440);
    const [lookback, setLookback] = useState(7);
    const [checks, setChecks] = useState("[]");
    const [configured, setConfigured] = useState(false);
    const [busy, setBusy] = useState(false);
    const operation = useRef<string | null>(null);
    useEffect(() => {
        setActive((sync?.active_seconds ?? 7200) / 60);
        setIdle((sync?.idle_seconds ?? 86400) / 60);
        setLookback((sync?.lookback_seconds ?? 604800) / 86400);
        setConfigured(sync?.ci_policy?.configured ?? false);
        setChecks(JSON.stringify(sync?.ci_policy?.required ?? [], null, 2));
    }, [sync?.revision]);
    const check = () => {
        if (busy) return;
        setBusy(true);
        operation.current ??= crypto.randomUUID();
        void run(async () => {
            const result = await mutate(`/repositories/${repository.id}/sync`, {}, undefined, operation.current!);
            operation.current = null;
            return result;
        }, "检查请求已登记；同步读取可在暂停期间进行，扫描等待恢复。").finally(() => setBusy(false));
    };
    const save = (event: FormEvent) => {
        event.preventDefault();
        void run(() => mutate(`/repositories/${repository.id}/sync/configure`, {
            active_seconds: active * 60, idle_seconds: idle * 60, lookback_seconds: lookback * 86400,
            overlap_seconds: sync?.overlap_seconds ?? 300,
            full_refresh_seconds: sync?.full_refresh_seconds ?? 604800,
            ci_configured: configured, required_checks: JSON.parse(checks),
        }, sync?.revision ?? 1), "巡检与必要检查策略已保存。");
    };
    const queued = sync && sync.requested_sequence > sync.completed_sequence;
    const names: Record<string, string> = { changed: "发现变化", unchanged: "无变化", failed: "读取失败" };
    return <div className="synchronization">
        <h4>GitHub 巡检</h4>
        <p>读取状态：{sync?.running ? "读取中" : queued ? "等待读取" : names[sync?.last_result ?? ""] ?? "尚未读取"} · 最后成功：{time(sync?.last_success_at)}</p>
        <p>目标 SHA：{repository.target_sha ?? "待确认"} · 扫描：{repository.scan_ready_sha === repository.target_sha && repository.scan_ready_sha ? "当前版本已就绪" : "等待扫描／验证"}</p>
        <p>下次到期：{time(sync?.next_due_at)} · 退避至：{sync?.retry_at ? time(sync.retry_at) : "无"} {sync?.error_code && `· ${sync.error_code}`}</p>
        <p>检查请求：{sync?.requested_sequence ?? 0} · 已完成：{sync?.completed_sequence ?? 0}</p>
        <button disabled={busy || Boolean(queued)} onClick={check}>{busy ? "登记中…" : "检查更新"}</button>
        <details><summary>巡检与必要 CI 检查配置</summary><form onSubmit={save}>
            <div className="fields">
                <label>活跃间隔（分钟）<input type="number" min="1" step="1" value={active} onChange={event => setActive(Number(event.target.value))} required /></label>
                <label>空闲间隔（分钟）<input type="number" min="1" step="1" value={idle} onChange={event => setIdle(Number(event.target.value))} required /></label>
                <label>最近变化回看（天）<input type="number" min="1" step="1" value={lookback} onChange={event => setLookback(Number(event.target.value))} required /></label>
            </div>
            <p>PR、CI 和评审共用此频率。默认活跃 2 小时、空闲 24 小时，回看 7 天。</p>
            <label className="checkbox"><input type="checkbox" checked={configured} onChange={event => setConfigured(event.target.checked)} />已确认必要检查集合</label>
            <label>必要检查（JSON）<textarea value={checks} onChange={event => setChecks(event.target.value)} required /></label>
            <p>每项填写 name、source、kind（check/status）和 ref（head/merge/base）；source 为 GitHub App ID 或状态发布者。只有 success 默认通过，skipped/neutral 须显式列入 accepted。</p>
            <button>保存巡检配置</button>
        </form></details>
    </div>;
}
