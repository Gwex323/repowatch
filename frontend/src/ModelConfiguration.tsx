import { useId, useState, type FormEvent, type KeyboardEvent } from "react";
import { mutate, type Provider, type Role, type State } from "./client";
import "./model-configuration.css";

type Run = (action: () => Promise<unknown>, message?: string) => Promise<void>;
type ModelList = { models: string[]; available: boolean; error: string | null };

async function loadModelOptions(path: string, data: Record<string, unknown> = {}) {
    const result = await mutate<ModelList>(path, data);
    if (!result.available) {
        throw new Error(result.error === "authentication_or_permission" ? "供应商拒绝访问，请检查密钥。"
            : result.error === "timeout" ? "供应商请求超时。"
            : result.error === "rate_or_quota_limit" ? "供应商暂时限制请求。"
            : "供应商模型列表暂不可用。");
    }
    return result.models;
}

function ProviderModelPicker({ value, onChange, onSelect, load, selected, disabled }: {
    value: string;
    onChange: (value: string) => void;
    onSelect: (model: string) => void;
    load: () => Promise<string[]>;
    selected: string[];
    disabled?: boolean;
}) {
    const id = useId();
    const [open, setOpen] = useState(false);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");
    const [remote, setRemote] = useState<string[]>([]);
    const query = value.trim();
    const options = [...new Set(remote)].filter(model =>
        !selected.includes(model) && model.toLowerCase().includes(query.toLowerCase()));
    const toggle = async () => {
        setOpen(!open);
        if (open || loading) return;
        setLoading(true);
        setError("");
        try {
            setRemote(await load());
        } catch (failure) {
            setError(failure instanceof Error ? failure.message : "读取模型列表失败。");
        } finally {
            setLoading(false);
        }
    };
    const handleKey = (event: KeyboardEvent<HTMLInputElement>) => {
        if (event.key === "Escape") setOpen(false);
        if (query && event.key === "Enter" && !event.nativeEvent.isComposing) {
            event.preventDefault();
            onSelect(query);
        }
    };
    return <div className="model-picker">
        <label htmlFor={id}>模型</label>
        <div className="model-input">
            <input id={id} value={value} maxLength={200} disabled={disabled}
                placeholder="输入模型名，按 Enter 添加"
                onChange={event => onChange(event.target.value)} onKeyDown={handleKey}/>
            <button type="button" aria-label="展开模型列表" aria-expanded={open}
                aria-controls={id + "-options"} disabled={disabled} onClick={() => void toggle()}>
                <span aria-hidden="true">{open ? "▴" : "▾"}</span>
            </button>
        </div>
        {open && <div className="model-options" id={id + "-options"} aria-label="可选模型">
            {loading && <p className="muted" role="status">正在读取模型列表…</p>}
            {error && <p className="danger" role="alert">{error} 可手填模型。</p>}
            {!loading && !error && !options.length && <p className="muted">没有匹配的模型，可手填。</p>}
            <ul>
                {options.map(model => <li key={model}><button type="button" onClick={() => onSelect(model)}>{model}</button></li>)}
                {query && !selected.includes(query) && !options.includes(query) && <li>
                    <button type="button" onClick={() => onSelect(query)}>添加「{query}」</button>
                </li>}
            </ul>
        </div>}
    </div>;
}

export function ProviderPanel({ state, run }: { state: State; run: Run }) {
    const [edit, setEdit] = useState<Provider | null>(null);
    const [name, setName] = useState("");
    const [url, setUrl] = useState("");
    const [api, setApi] = useState("chat_completions");
    const [key, setKey] = useState("");
    const [models, setModels] = useState<string[]>([]);
    const [model, setModel] = useState("");
    const [catalogVersion, setCatalogVersion] = useState(0);
    const resetCatalog = () => setCatalogVersion(value => value + 1);
    const select = (provider: Provider | null) => {
        setEdit(provider);
        setName(provider?.configuration.name ?? "");
        setUrl(provider?.configuration.base_url ?? "");
        setApi(provider?.configuration.api ?? "chat_completions");
        setKey("");
        setModels(provider?.configuration.models.map(item => item.model) ?? []);
        setModel("");
        resetCatalog();
    };
    const discover = async () => {
        const baseUrl = url.trim().replace(/\/+$/, "");
        if (!baseUrl) throw new Error("请先填写供应商 API 地址。");
        if (key) return loadModelOptions("/providers/discover", { base_url: baseUrl, api_key: key });
        if (edit && baseUrl === edit.configuration.base_url)
            return loadModelOptions(`/providers/${edit.revision_id}/models`);
        throw new Error("请先填写该 API 地址的密钥。");
    };
    const save = (event: FormEvent) => {
        event.preventDefault();
        void run(async () => {
            const selected = [...new Set([...models, model.trim()].filter(Boolean))];
            const payload = {
                name, base_url: url, api, ...(key ? { api_key: key } : {}),
                ...(edit ? { chat_token_parameter: edit.configuration.chat_token_parameter, tool_choice: edit.configuration.tool_choice } : {}),
                models: selected.map(model => edit?.configuration.models.find(item => item.model === model) ?? { model }),
            };
            await mutate(edit ? `/providers/${edit.id}/edit` : "/providers/import", { payload }, edit?.revision);
            select(null);
        }, "供应商配置已保存。");
    };
    return <section>
        <h2>供应商配置</h2>
        <div className="cards">
            {state.providers.map(provider => <article key={provider.id}>
                <h3>{provider.configuration.name}</h3>
                <p className="provider-address">{provider.configuration.base_url}</p>
                <p>{provider.configuration.models.map(item => item.model).join("、") || "未添加模型"}</p>
                <button type="button" onClick={() => select(provider)}>编辑</button>
            </article>)}
        </div>
        <form onSubmit={save}>
            <h3>{edit ? `编辑 ${edit.configuration.name}` : "新增供应商"}</h3>
            <div className="fields">
                <label>名称<input value={name} onChange={event => setName(event.target.value)} required/></label>
                <label>API 地址<input type="url" value={url} required onChange={event => {
                    setUrl(event.target.value); resetCatalog();
                }}/></label>
                <label>接口<select value={api} onChange={event => setApi(event.target.value)}>
                    <option value="chat_completions">Chat Completions</option>
                    <option value="responses">Responses</option>
                </select></label>
                <label>密钥<input type="password" autoComplete="off" value={key} required={!edit}
                    placeholder={edit ? "留空沿用原密钥" : undefined} onChange={event => {
                        setKey(event.target.value); resetCatalog();
                    }}/></label>
            </div>
            <ProviderModelPicker key={catalogVersion} value={model} onChange={setModel} load={discover}
                selected={models} disabled={models.length >= 100} onSelect={option => {
                    setModels(current => current.includes(option) || current.length >= 100 ? current : [...current, option]);
                    setModel("");
                }}/>
            {models.length > 0 && <ul className="selected-models" aria-label="已选模型">
                {models.map(item => <li key={item}>{item}
                    <button type="button" aria-label={`移除模型 ${item}`}
                        onClick={() => setModels(current => current.filter(model => model !== item))}>×</button>
                </li>)}
            </ul>}
            <div className="actions">
                <button type="submit">保存配置</button>
                {edit && <button type="button" onClick={() => select(null)}>取消编辑</button>}
            </div>
        </form>
    </section>;
}

type RoleKey = "processor" | "reviewer";

function RoleCard({ role, title, saved, providers, run }: {
    role: RoleKey; title: string; saved?: Role; providers: Provider[]; run: Run;
}) {
    const [providerId, setProviderId] = useState(saved?.definition.provider_id ?? "");
    const [model, setModel] = useState(saved?.definition.selection.model ?? "");
    const provider = providers.find(item => item.id === providerId);
    const options = provider?.configuration.models ?? [];
    const configuredModel = options.find(item => item.model === model);
    const submit = (event: FormEvent) => {
        event.preventDefault();
        if (!provider || !configuredModel) return;
        const selection = saved?.definition.selection;
        const effort = saved?.definition.provider_id === providerId && selection?.model === model
            ? selection.reasoning_effort
            : configuredModel.reasoning_effort;
        void run(() => mutate(`/roles/${role}`, {
            definition: { role, provider_id: providerId, selection: { model, reasoning_effort: effort ?? null } },
        }, saved?.revision ?? 0), `${title} 模型已保存。`);
    };
    return <form onSubmit={submit} aria-label={title}>
        <h3>{title}</h3>
        <label>供应商<select value={providerId} required onChange={event => {
            setProviderId(event.target.value); setModel("");
        }}>
            <option value="">请选择供应商</option>
            {providers.map(item => <option key={item.id} value={item.id}>{item.configuration.name}</option>)}
        </select></label>
        <label>模型<select aria-label="模型" value={model} required disabled={!options.length} onChange={event => setModel(event.target.value)}>
            <option value="">{provider && !options.length ? "该供应商尚未配置模型" : "请选择模型"}</option>
            {model && !configuredModel && <option value={model} disabled>{model}（未列在供应商配置中）</option>}
            {[...new Set(options.map(item => item.model))].map(name => <option key={name} value={name}>{name}</option>)}
        </select></label>
        {provider && !options.length && <p className="muted">请先在「供应商」中添加并保存模型。</p>}
        <button type="submit" disabled={!configuredModel}>保存</button>
    </form>;
}

export function RolePanel({ state, run }: { state: State; run: Run }) {
    const roles: Array<[RoleKey, string]> = [["processor", "处理 Agent"], ["reviewer", "审查 Agent"]];
    return <section>
        <h2>角色模型</h2>
        <div className="cards">
            {roles.map(([role, title]) => {
                const saved = state.roles.find(item => item.binding_key === role);
                return <RoleCard key={`${role}:${saved?.revision ?? 0}`} role={role} title={title} saved={saved}
                    providers={state.providers} run={run}/>;
            })}
        </div>
    </section>;
}
