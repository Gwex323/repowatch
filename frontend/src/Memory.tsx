import { useEffect, useState } from "react";
import { mutate, request, type State } from "./client";

type Content = Record<"symptoms" | "applicability" | "method" | "verification" | "limitations", string>;
type Memory = { id: string; revision: number; state: string; enabled: number; deleted: number; content: Content; source: unknown; delivery_state: string };
type Index = { status: { mode: string; degradation?: string; pending: number }; configuration: { revision: number; configuration_json: string }; models: string[]; selection_warning: string };
const fields: Array<[keyof Content, string]> = [["symptoms", "问题特征"], ["applicability", "适用条件"], ["method", "方法"], ["verification", "验证方式"], ["limitations", "限制与未证实假设"]];
const empty: Content = { symptoms: "", applicability: "", method: "", verification: "", limitations: "" };
const states: Record<string, string> = { success: "已验证成功", failure: "失败经验，仅作风险提示", pending_review: "待返修复核", feedback_pending: "反馈待判断", manual_unverified: "人工未验证" };

export function MemoryPanel({ state, run, repositoryId, indexOnly = false }: { state: State; run: (action: () => Promise<unknown>, message?: string) => Promise<void>; repositoryId?: string; indexOnly?: boolean }) {
    const [repository, setRepository] = useState(repositoryId ?? state.repositories[0]?.id ?? "");
    const [items, setItems] = useState<Memory[]>([]);
    const [selected, setSelected] = useState<Memory | null>(null);
    const [body, setBody] = useState<Content>(empty);
    const [sharing, setSharing] = useState(false);
    const [index, setIndex] = useState<Index | null>(null);
    const [model, setModel] = useState("");
    const [query, setQuery] = useState("");
    const [result, setResult] = useState<unknown>(null);
    const [audits, setAudits] = useState<unknown>(null);
    const reload = async () => {
        setResult(null);
        if (indexOnly) {
            const next = await request<Index>("/memory-index");
            setIndex(next);
            setModel((JSON.parse(next.configuration.configuration_json) as { model?: string }).model ?? "");
        }
        if (repository && !indexOnly) {
            const rows = await request<Memory[]>(`/repositories/${repository}/memories`);
            setItems(rows);
            setSelected(current => rows.find(item => item.id === current?.id) ?? null);
        }
    };
    useEffect(() => { void run(reload, "记忆状态已读取。"); }, [repository]);
    const act = (action: string) => selected && run(async () => {
        await mutate(`/memories/${selected.id}/${action}`, { content: body, confirmed_sanitized: sharing }, selected.revision);
        await reload(); setSharing(false);
    }, action === "delete" ? "记忆已删除；未来检索与注入停用，原任务审计和已发送请求保留。" : "记忆操作已保存并回读。");
    return <section>
        <h2>{indexOnly ? "记忆索引" : "仓库记忆"}</h2>
        {!indexOnly && <><p>经验仅供参考，当前任务仍须独立验证。语义编辑保存为人工未验证的新版本。</p>
        {!repositoryId && <label>记忆所属仓库<select value={repository} onChange={event => { setRepository(event.target.value); setSelected(null); setResult(null); setAudits(null); }}><option value="">请选择仓库</option>{state.repositories.map(item => <option key={item.id} value={item.id}>{item.github_id}</option>)}</select></label>}
        <button onClick={() => void run(reload, "记忆状态已刷新。")}>刷新记忆</button>
        <form onSubmit={event => { event.preventDefault(); void run(async () => setResult(await request(`/memories/search?repository_id=${encodeURIComponent(repository)}&query=${encodeURIComponent(query)}`)), "检索完成。"); }}>
            <label>检索经验<input value={query} onChange={event => setQuery(event.target.value)} required maxLength={16000}/></label><button disabled={!repository}>检索记忆</button>
        </form>
        {result !== null && <details open><summary>检索结果与降级状态</summary><pre>{JSON.stringify(result, null, 2)}</pre></details>}
        <div className="cards">{items.map(item => <article key={item.id}><h3>{item.content.symptoms}</h3><p>{item.deleted ? "已删除" : item.enabled ? states[item.state] : "已禁用"} · 修订 {item.revision}</p><button onClick={() => { setSelected(item); setBody(item.content); setSharing(false); }}>查看与纠正</button></article>)}</div>
        <button disabled={!repository} onClick={() => { setSelected(null); setBody(empty); }}>新增人工记录</button>
        <form onSubmit={event => { event.preventDefault(); if (selected) void act("edit"); else void run(async () => { await mutate(`/repositories/${repository}/memories`, { content: body }); await reload(); }, "人工未验证记录已保存。"); }}>
            <h3>{selected ? "记忆详情与纠正" : "新增人工未验证记忆"}</h3>
            {fields.map(([key, label]) => <label key={key}>{label}<textarea aria-label={label} value={body[key]} onChange={event => setBody(current => ({ ...current, [key]: event.target.value }))} required maxLength={16000}/></label>)}
            <button disabled={!repository || Boolean(selected?.deleted)}>保存正文</button>
        </form>
        {selected && <article>
            <p>验证状态：{states[selected.state]}；PR 交付：{selected.delivery_state}；修订 {selected.revision}</p>
            <details><summary>来源与代码版本</summary><pre>{JSON.stringify(selected.source, null, 2)}</pre></details>
            <button disabled={Boolean(selected.deleted)} onClick={() => void act(selected.enabled ? "disable" : "enable")}>{selected.enabled ? "禁用记忆" : "启用记忆"}</button>
            <p>删除会停止未来检索与注入。重建后派生索引不再包含该记录；来源审计、旧版本和已发送请求保留，无法撤回历史请求。</p><button disabled={Boolean(selected.deleted)} onClick={() => void act("delete")}>删除记忆</button>
            <h3>共享去敏经验</h3><p>先在上方正文中改写待共享的通用方法，点击共享不会提供来源仓库原始日志或代码。不确定是否可共享时保持仓库范围。</p>
            <label><input type="checkbox" style={{ width: "auto" }} checked={sharing} onChange={event => setSharing(event.target.checked)}/>我已审阅待共享正文，确认不含原始上下文或敏感信息</label>
            <button disabled={!sharing || selected.state !== "success" || !selected.enabled || Boolean(selected.deleted)} onClick={() => void act("share")}>保存去敏共享版本</button>
        </article>}
        <button disabled={!repository} onClick={() => void run(async () => setAudits(await request(`/memory-audits?repository_id=${encodeURIComponent(repository)}`)), "请求依赖审计已读取。")}>查看复用审计</button>
        {audits !== null && <details open><summary>请求依赖核验与留存范围</summary><pre>{JSON.stringify(audits, null, 2)}</pre></details>}</>}
        {index && <article><h3>记忆索引管理</h3><p>当前模式：{index.status.mode}；降级原因：{index.status.degradation ?? "无"}；待索引事件：{index.status.pending}</p><p>{index.selection_warning}；切换后需重建，缺少锁定资产时明确降级。</p>
            <label>本地语义编码模型<select value={model} onChange={event => setModel(event.target.value)}><option value="">关键词检索</option>{index.models.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
            <button onClick={() => void run(async () => { await mutate("/memory-index/configure", { model: model || null }, index.configuration.revision); await reload(); }, "索引配置已保存，需重建后核验。")}>保存索引配置</button>
            <button onClick={() => void run(async () => { await mutate("/memory-index/rebuild", {}, index.configuration.revision); await reload(); }, "索引重建结果已回读。")}>重建记忆索引</button>
        </article>}
    </section>;
}
