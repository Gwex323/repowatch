import { useState, type FormEvent } from "react";
import { mutate, type State } from "./client";
import { Scans } from "./Scans";
import { Synchronization } from "./Synchronization";

type Preflight = { preflight_id: string; identity: { github_id: string } };

function parseRepositoryUrl(value: string) {
    const match = /^https:\/\/github\.com\/([a-z0-9_.-]{1,100})\/([a-z0-9_.-]{1,104})\/?$/i.exec(value.trim());
    const owner = match?.[1];
    const name = match?.[2].replace(/\.git$/i, "");
    if (!owner || !name || name.length > 100 || [".", ".."].includes(owner) || [".", ".."].includes(name))
        throw new Error("请粘贴 GitHub 仓库首页链接，例如 https://github.com/owner/repository。");
    return { owner, name };
}

export function Repositories({ state, run, openTask }: { state: State; run: (action: () => Promise<unknown>, message?: string) => Promise<void>; openTask: (id: string) => void }) {
    const [repositoryUrl, setRepositoryUrl] = useState("");
    const [target, setTarget] = useState("");
    const [path, setPath] = useState("auto");
    const [forkOwner, setForkOwner] = useState("");
    const [forkName, setForkName] = useState("");
    const [reference, setReference] = useState("");
    const [accountChoice, setAccountChoice] = useState<string | null>(null);
    const accounts = state.github_accounts ?? [];
    const account = accountChoice ?? accounts.find(item => item.is_default)?.id ?? (accounts.length ? "" : "legacy");
    const [loadedRepository, setLoadedRepository] = useState<{ id: string; revision: number } | null>(null);
    const [registering, setRegistering] = useState(false);
    const [mode, setMode] = useState("");
    const [allowed, setAllowed] = useState("");
    const [excluded, setExcluded] = useState("");
    const [severity, setSeverity] = useState("{}");
    const [claim, setClaim] = useState(false);
    const [template, setTemplate] = useState("RepoWatch 将按已确认范围处理此 Issue。");
    const [scanPolicy, setScanPolicy] = useState('{"chunk_bytes":16000,"exclusions":{}}');
    const split = (value: string) => value.split(",").map(label => label.trim()).filter(Boolean);
    const register = (event: FormEvent) => {
        event.preventDefault();
        if (registering) return;
        setRegistering(true);
        void run(async () => {
            const location = parseRepositoryUrl(repositoryUrl);
            const result = await mutate<Preflight>("/repositories/preflight", {
                ...location, target_branch: target || null, contribution_path: path,
                fork_owner: path === "fork" ? forkOwner : null,
                fork_name: path === "fork" ? forkName : null,
                ...(account === "legacy" ? { credential_reference: reference || null } : { github_account_id: account }),
            });
            const existing = state.repositories.find(repository => repository.github_id === result.identity.github_id);
            if (loadedRepository && existing?.id !== loadedRepository.id)
                throw new Error("仓库身份已变化，请刷新仓库列表后重新编辑。");
            const editingPolicy = loadedRepository !== null;
            const policy = editingPolicy ? {
                mode: mode || null, allowed_labels: split(allowed), excluded_labels: split(excluded),
                severity_labels: JSON.parse(severity), claim_comment: claim, claim_template: template,
            } : existing?.policy ?? {};
            await mutate("/repositories/import", {
                preflight_id: result.preflight_id,
                policy,
                ...(editingPolicy ? { scan_policy: JSON.parse(scanPolicy) } : {}),
            }, loadedRepository?.revision ?? existing?.revision ?? 0);
            setRepositoryUrl(""); setLoadedRepository(null); setTarget(""); setPath("auto");
            setAccountChoice(null); setReference(""); setForkOwner(""); setForkName("");
        }, "仓库已登记并排入完整扫描；如需运行，请在任务页配置预算。")
            .finally(() => setRegistering(false));
    };
    return <section><h2>仓库登记与配置</h2>
        <p>每次导入都登记新的完整扫描。目标变化后已有任务及 PR 保留原目标，等待明确处理。</p>
        <form onSubmit={register}>
            <div className="fields">
                <label>GitHub 仓库链接<input type="url" value={repositoryUrl} onChange={event => { setRepositoryUrl(event.target.value); setLoadedRepository(null); }} placeholder="https://github.com/owner/repository" required /></label>
                <label>目标分支（空白使用默认分支）<input value={target} onChange={event => setTarget(event.target.value)} /></label>
                <label>贡献路径<select value={path} onChange={event => setPath(event.target.value)}><option value="auto">发布 PR 时自动选择</option><option value="direct">直接分支</option><option value="fork">指定自己的 Fork</option></select></label>
                {path === "fork" && <><label>Fork 所有者<input value={forkOwner} onChange={event => setForkOwner(event.target.value)} required /></label><label>Fork 仓库名<input value={forkName} onChange={event => setForkName(event.target.value)} required /></label></>}
                <label>GitHub 执行账号<select value={account} onChange={event => setAccountChoice(event.target.value)} required>
                    <option value="" disabled>选择已绑定账号</option>
                    {accounts.map(item => <option key={item.id} value={item.id}>@{item.login}{item.is_default ? "（默认）" : ""}</option>)}
                    <option value="legacy">使用旧凭据引用</option>
                </select></label>
                {account === "legacy" && <label>宿主 GitHub 凭据引用<input value={reference} onChange={event => setReference(event.target.value)} required /></label>}
            </div>
            {path === "auto" && <p>登记时只读取仓库。获准发布 PR 时，有上游推送权限就用上游分支，否则按需复用或创建你的 Fork。</p>}
            {!accounts.length && <p>可在「系统与维护」绑定 GitHub 账号，登记仓库时直接选用。</p>}
            {loadedRepository && <details key={loadedRepository.id}><summary>准入与扫描策略（可选）</summary>
            <div className="fields">
                <label>准入模式<select value={mode} onChange={event => setMode(event.target.value)}><option value="">默认</option><option value="manual">手动选择</option><option value="labels">标签准入</option><option value="all_bugs">全部合格 Bug</option></select></label>
                <label>允许标签（逗号分隔）<input value={allowed} onChange={event => setAllowed(event.target.value)} /></label>
                <label>排除标签（逗号分隔）<input value={excluded} onChange={event => setExcluded(event.target.value)} /></label>
                <label>严重程度映射（JSON，0—4）<textarea value={severity} onChange={event => setSeverity(event.target.value)} required /></label>
                <label><input type="checkbox" checked={claim} onChange={event => setClaim(event.target.checked)} />启用认领评论</label>
                <label>认领模板<textarea value={template} onChange={event => setTemplate(event.target.value)} required={claim} /></label>
                <label>扫描策略（JSON：分块字节数、排除路径与依据）<textarea value={scanPolicy} onChange={event => setScanPolicy(event.target.value)} required /></label>
            </div>
            {mode === "labels" && !split(allowed).length && <p className="danger">允许标签待配置；不会自动放宽准入。</p>}
            <p>评论时点：准入、Issue／PR 门禁及扫描均通过，即将实际修复时。默认不修改 assignee。</p>
            {claim && <blockquote>{template}</blockquote>}
            </details>}
            <button disabled={registering}>{loadedRepository ? "保存仓库配置" : "登记仓库"}</button>
        </form>
        {state.repositories.map(repository => <article key={repository.id}><h3>{repository.github_owner}/{repository.github_name || repository.github_id}</h3>
            <p>修订 {repository.revision} · 导入代次 {repository.import_epoch} · 目标 {repository.target_branch || "未配置"}</p>
            <p>扫描就绪：{repository.scan_ready_sha || "未就绪"} · {repository.policy?.mode ?? "待配置"}</p>
            {repository.credential_reference && <p>执行账号：{accounts.find(item => item.id === repository.credential_reference)?.login ?? repository.identity?.execution_login ?? "旧凭据"}</p>}
            <Scans repositoryId={repository.id} run={run} openTask={openTask} />
            <Synchronization repository={repository} run={run} />
            <button onClick={() => {
                setRepositoryUrl(repository.github_owner && repository.github_name
                    ? `https://github.com/${repository.github_owner}/${repository.github_name}` : "");
                setTarget(repository.target_branch ?? ""); setPath(repository.contribution_path ?? "auto");
                const saved = repository.credential_reference ?? "";
                const bound = accounts.some(item => item.id === saved);
                setAccountChoice(bound ? saved : "legacy"); setReference(bound ? "" : saved);
                setForkOwner(repository.publication?.owner ?? ""); setForkName(repository.publication?.name ?? "");
                setLoadedRepository({ id: repository.id, revision: repository.revision });
                const policy = repository.policy;
                setScanPolicy(JSON.stringify(repository.scan_policy ?? { chunk_bytes: 16000, exclusions: {} }));
                setMode(policy?.mode ?? "");
                setAllowed(policy?.allowed_labels.join(",") ?? "");
                setExcluded(policy?.excluded_labels.join(",") ?? "");
                setSeverity(JSON.stringify(policy?.severity_labels ?? {}));
                setClaim(policy?.claim_comment ?? false);
                setTemplate(policy?.claim_template ?? "RepoWatch 将按已确认范围处理此 Issue。");
            }}>编辑仓库配置</button>
            <button onClick={() => void run(() => mutate(`/control/${repository.paused ? "resume" : "pause"}`, { repository_id: repository.id }, repository.revision))}>{repository.paused ? "显式恢复仓库" : "暂停仓库"}</button>
        </article>)}
    </section>;
}
