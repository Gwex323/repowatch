import { useState, type FormEvent } from "react";
import { mutate, type GitHubAccount } from "./client";

export function GitHubAccounts({ accounts, run }: {
    accounts: GitHubAccount[];
    run: (action: () => Promise<unknown>, message?: string) => Promise<void>;
}) {
    const [editing, setEditing] = useState<GitHubAccount | "new" | null>(null);
    const [username, setUsername] = useState("");
    const [token, setToken] = useState("");
    const [disconnect, setDisconnect] = useState<string | null>(null);
    const edit = (next: GitHubAccount | "new" | null) => {
        setEditing(next);
        setUsername("");
        setToken("");
    };
    const submit = (event: FormEvent) => {
        event.preventDefault();
        if (!editing) return;
        const submitted = token.trim();
        setToken("");
        void run(async () => {
            if (editing === "new")
                await mutate("/github/accounts", { username: username.trim(), token: submitted });
            else
                await mutate(`/github/accounts/${editing.id}/token`, { token: submitted }, editing.revision);
            edit(null);
        }, editing === "new" ? "GitHub 账号已绑定。" : "令牌已更新，仓库继续使用同一账号。");
    };
    return <section>
        <h2>GitHub 账号</h2>
        <p>绑定后可在多个仓库复用。默认账号只用于新仓库。</p>
        <button onClick={() => edit("new")}>绑定 GitHub 账号</button>
        {!accounts.length && <p>尚未绑定 GitHub 账号。</p>}
        {accounts.map(account => <article key={account.id}>
            <h3>@{account.login}{account.is_default ? " · 默认账号" : ""}</h3>
            <p>上次身份验证：{new Date(account.verified_at * 1000).toLocaleString()}</p>
            {!account.is_default && <button onClick={() => void run(() =>
                mutate(`/github/accounts/${account.id}/default`, {}, account.revision), "已设置新仓库的默认账号。")}>设为默认</button>}
            <button onClick={() => void run(() =>
                mutate(`/github/accounts/${account.id}/verify`, {}, account.revision), "GitHub 身份已重新核实。")}>重新验证</button>
            <button onClick={() => edit(account)}>更换令牌</button>
            {disconnect === account.id ? <>
                <span>解绑仅移除本机连接。仍在使用的仓库须先改绑。</span>
                <button onClick={() => void run(async () => {
                    await mutate(`/github/accounts/${account.id}/disconnect`, {}, account.revision);
                    setDisconnect(null);
                    if (editing !== "new" && editing?.id === account.id) edit(null);
                }, "GitHub 账号已解绑。")}>确认解绑</button>
                <button onClick={() => setDisconnect(null)}>取消</button>
            </> : <button onClick={() => setDisconnect(account.id)}>解绑</button>}
        </article>)}
        {editing && <form onSubmit={submit}>
            <h3>{editing === "new" ? "绑定 GitHub 账号" : `更换 @${editing.login} 的令牌`}</h3>
            {editing === "new" && <label>GitHub 用户名<input value={username} onChange={event => setUsername(event.target.value)} required maxLength={39} /></label>}
            <label>个人访问令牌（PAT）<input type="password" value={token} onChange={event => setToken(event.target.value)} required maxLength={512} autoComplete="off" spellCheck={false} /></label>
            <button>验证并保存</button>
            <button type="button" onClick={() => edit(null)}>取消</button>
        </form>}
    </section>;
}
