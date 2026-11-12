import { createRequire } from "node:module";
import { repositoryPage, taskById } from "./workbench-browser-navigation.mjs";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createServer } from "node:net";

const require = createRequire(resolve("frontend/package.json"));
const { chromium, expect } = require("@playwright/test");
const root = resolve(".cache/p4-browser", String(Date.now()));
await mkdir(root, { recursive: true });
const port = await new Promise((done, fail) => {
  const server = createServer();
  server.once("error", fail);
  server.listen(0, "127.0.0.1", () => { const value = server.address().port; server.close(() => done(value)); });
});
const host = spawn(resolve("backend/.venv/Scripts/python.exe"), ["-X", "utf8", "experiments/p4_browser_host.py", "--data-dir", resolve(root, "data"), "--port", String(port)], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
let browser;
let stderr = "";
host.stderr.on("data", chunk => { stderr = (stderr + chunk.toString()).slice(-4000); });
try {
  const setup = await new Promise((done, fail) => {
    let output = "";
    const timer = setTimeout(() => fail(new Error("fixture startup: " + stderr)), 30000);
    host.once("exit", () => { clearTimeout(timer); fail(new Error("fixture exited: " + stderr)); });
    host.stdout.on("data", chunk => { output += chunk.toString(); if (output.includes("\n")) { clearTimeout(timer); done(JSON.parse(output.split("\n")[0])); } });
  });
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${port}/#session=${setup.bootstrap}`);
  await expect(page.getByRole("heading", { name: "仓库工作台" })).toBeVisible();
  await repositoryPage(page, "fixture/p4-browser", "配置");
  await expect(page.getByRole("heading", { name: "GitHub 巡检" })).toBeVisible();
  await page.getByText("巡检与必要 CI 检查配置", {exact: true}).click();
  await page.getByLabel("活跃间隔（分钟）").fill("180");
  await page.getByRole("button", {name: "保存巡检配置", exact: true}).click();
  await expect(page.getByRole("status")).toContainText("巡检与必要检查策略已保存");
  const syncState = () => page.evaluate(async id => (await fetch(`/api/v1/repositories/${id}/sync`)).json(), setup.repository_id);
  const check = async () => {
    await expect(page.getByRole("button", {name: "检查更新", exact: true})).toBeEnabled();
    const before = await syncState();
    await page.getByRole("button", {name: "检查更新", exact: true}).click();
    await expect.poll(async () => { const state = await syncState(); return state.requested_sequence > before.requested_sequence && state.completed_sequence >= state.requested_sequence; }).toBe(true);
  };
  await check();
  await page.evaluate(async () => {
    const { csrf } = await (await fetch("/api/v1/session")).json();
    await fetch("/api/v1/fixture/change", {method:"POST", headers:{"Content-Type":"application/json", "X-RepoWatch-Session":csrf}, body:JSON.stringify({target:true})});
  });
  await check();
  await expect.poll(() => page.evaluate(async () => (await (await fetch("/api/v1/state")).json()).repositories[0].target_sha)).toBe("d".repeat(40));
  await page.screenshot({path: resolve(root, "synchronization.png"), fullPage:true});
  await taskById(page, setup.task_id);
  await expect(page.getByRole("heading", {name:"PR #2", exact:true})).toBeVisible();
  await expect(page.getByRole("link", {name:"GitHub CI 检查", exact:true})).toHaveAttribute("href", /\/pull\/2\/checks$/);
  await expect(page.getByRole("link", {name:"GitHub 评审与变更", exact:true})).toHaveAttribute("href", /\/pull\/2\/files$/);
  await expect(page.getByRole("link", {name:"GitHub 合并页", exact:true})).toHaveAttribute("href", /\/pull\/2#partial-pull-merging$/);
  await expect(page.getByRole("button", {name:"批准方案与验收变化", exact:true})).toBeDisabled();
  await page.getByLabel("决定依据／澄清信息").fill("确认只增加所列负数场景，保留原有回归要求。");
  await page.getByRole("button", {name:"批准方案与验收变化", exact:true}).click();
  await expect(page.getByRole("heading", {name:"评审范围判断：approve", exact:true})).toBeVisible();
  await page.screenshot({path: resolve(root, "reviews.png"), fullPage:true});
  await page.reload();
  const facts = await page.evaluate(async id => {
    const state = await (await fetch("/api/v1/state")).json();
    const task = await (await fetch(`/api/v1/tasks/${id}`)).json();
    return {paused:state.control.paused, runs:task.runs.length, approved:task.acceptance.approved, delivery:task.pr_lifecycle.delivery_state};
  }, setup.task_id);
  if (!facts.paused || facts.runs !== 0 || !facts.approved || facts.delivery !== "open" || errors.length) throw new Error(JSON.stringify({facts, errors}));
  await writeFile(resolve(root,"report.json"), JSON.stringify({passed:7, checks:["巡检配置持久化", "人工检查更新", "目标漂移与补扫", "暂停期间无模型", "PR/CI/评审视图", "范围批准入口", "刷新保持批准与唯一 PR"], facts, errors},null,2));
  console.log(JSON.stringify({report:resolve(root,"report.json"),passed:7}));
} finally {
  await browser?.close();
  host.kill();
}
