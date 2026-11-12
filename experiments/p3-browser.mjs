import { createRequire } from "node:module";
import { taskById } from "./workbench-browser-navigation.mjs";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createServer } from "node:net";

const require = createRequire(resolve("frontend/package.json"));
const { chromium, expect } = require("@playwright/test");
const root = resolve(".cache/p3-browser", String(Date.now()));
await mkdir(root, { recursive: true });
const port = await new Promise((done, fail) => {
  const server = createServer();
  server.once("error", fail);
  server.listen(0, "127.0.0.1", () => {
    const value = server.address().port;
    server.close(() => done(value));
  });
});
const host = spawn(resolve("backend/.venv/Scripts/python.exe"), [
  "-X", "utf8", "experiments/p3_browser_host.py", "--data-dir", resolve(root, "data"),
  "--port", String(port),
], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
let browser;
let errors = "";
host.stderr.on("data", chunk => { errors = (errors + chunk.toString()).slice(-3000); });
try {
  const setup = await new Promise((done, fail) => {
    let output = "";
    const timeout = setTimeout(() => fail(new Error("fixture timeout: " + errors)), 30000);
    host.once("exit", () => { clearTimeout(timeout); fail(new Error("fixture exited: " + errors)); });
    host.stdout.on("data", chunk => {
      output += chunk.toString();
      if (output.includes("\n")) { clearTimeout(timeout); done(JSON.parse(output.split("\n")[0])); }
    });
  });
  browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const failures = [];
  page.on("pageerror", error => failures.push(error.message));
  await page.goto(`http://127.0.0.1:${port}/#session=${setup.bootstrap}`);
  await expect(page.getByRole("heading", { name: "仓库工作台" })).toBeVisible();
  await taskById(page, setup.task_id);
  await page.getByLabel("任务类型", { exact: true }).selectOption("feature");
  await page.getByLabel("验收条件（JSON）", { exact: true }).fill(JSON.stringify({
    objective: "展示任务状态", plan: "增加只读状态视图", criteria: ["可读取当前任务状态"],
  }));
  await page.getByRole("button", { name: "保存新验收修订", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("验收规范已更新");
  await page.getByRole("button", { name: "明确批准当前验收规范", exact: true }).click();
  await expect(page.getByText("feature · 已由 local-user 批准", { exact: true })).toBeVisible();
  await page.reload();
  await taskById(page, setup.task_id);
  await expect(page.getByText("feature · 已由 local-user 批准", { exact: true })).toBeVisible();
  await page.getByLabel("补充信息", { exact: true }).fill("新增筛选范围，需要重新核验方案");
  await page.getByRole("button", { name: "保存补充材料", exact: true }).click();
  await expect(page.getByText("feature · 输入或策略已变化，需保存新修订", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "明确批准当前验收规范", exact: true })).toHaveCount(0);
  const state = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(state.control.paused).toBe(1);
  const detail = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.task_id);
  expect(detail.runs).toEqual([]);
  expect(detail.acceptance.approved).toBe(false);
  await taskById(page, setup.candidate_id);
  await expect(page.getByText("发现相似 Issue #42，尚无法确认是同一问题", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("现象相似，缺少相同触发条件的证据。", { exact: true })).toBeVisible();
  await page.getByLabel("你的判断依据", { exact: true }).fill("已核对独立触发条件，确认没有相同问题。");
  await page.getByRole("button", { name: "确认完整查重无相似 Issue", exact: true }).click();
  await expect.poll(async () => {
    const value = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.candidate_id);
    return value.candidate_issue_decisions.length;
  }).toBe(1);
  await page.getByText("已保存的候选查重决定", {exact: true}).click();
  const candidateDetail = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.candidate_id);
  expect(candidateDetail.candidate_issue_decisions).toHaveLength(1);
  expect(candidateDetail.candidate_issue_decisions[0].decision).toBe("none");
  expect(candidateDetail.runs).toEqual([]);
  await taskById(page, setup.relation_task_id);
  await page.getByText("修复原始记录与日志", { exact: true }).click();
  await page.getByRole("button", {name: /^读取 Runner 日志/}).click();
  await expect(page.getByText("Runner 日志正文与摘要", {exact: true})).toBeVisible();
  await page.getByLabel("补充信息", { exact: true }).fill("已核对 PR 描述和文件差异，确认无关");
  await page.getByRole("button", {name: "确认无关", exact: true}).click();
  await expect.poll(async () => {
    const value = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.relation_task_id);
    return value.relation_decisions.length;
  }).toBe(1);
  await page.getByRole("button", {name: "请求核对远端回执", exact: true}).click();
  await expect.poll(async () => {
    const value = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.relation_task_id);
    return value.mode;
  }).toBe("reconcile_publication");
  await page.reload();
  const finalState = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(finalState.control.paused).toBe(1);
  const relationDetail = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.relation_task_id);
  expect(relationDetail.relation_decisions[0].decision).toBe("unrelated");
  expect(relationDetail.publication_intents[0].state).toBe("unknown");
  expect(relationDetail.runs).toHaveLength(1);
  await taskById(page, setup.scope_tasks[0]);
  await expect(page.getByRole("heading", {name:"确认 Issue 的交付范围",exact:true})).toBeVisible();
  await page.getByLabel("目标 1 的验收条件（每行一项）", {exact:true}).fill("末页请求成功");
  await page.getByLabel("范围判断依据", {exact:true}).fill("本次只处理空游标，其他目标另行跟进。");
  await page.getByRole("button", {name:"确认收敛范围",exact:true}).click();
  await expect(page.getByRole("status")).toContainText("范围决定已保存");
  const converged = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.scope_tasks[0]);
  expect(converged.acceptance.content.objective).toBe("修复空游标");
  expect(converged.acceptance.approved).toBe(true);
  await taskById(page, setup.scope_tasks[1]);
  await page.getByLabel("处理方式", {exact:true}).selectOption("split");
  await page.getByLabel("目标 1 的验收条件（每行一项）", {exact:true}).fill("末页请求成功");
  await page.getByLabel("目标 2 的验收条件（每行一项）", {exact:true}).fill("超时仅重试一次");
  await page.getByLabel("范围判断依据", {exact:true}).fill("不同触发条件，分别查重并独立验收。");
  await page.getByRole("button", {name:"确认拆分并逐项查重",exact:true}).click();
  await expect(page.getByRole("heading", {name:"独立目标的处理进度",exact:true})).toBeVisible();
  await page.getByRole("button", {name:"查看目标 1 →",exact:true}).click();
  await expect(page.getByRole("heading", {name:"修复空游标",exact:true})).toBeVisible();
  const scopeDetail = await page.evaluate(async id => (await fetch(`/api/v1/tasks/${id}`)).json(), setup.scope_tasks[1]);
  expect(scopeDetail.scope_children).toHaveLength(2);
  expect(scopeDetail.wait_reason).toBe("scope_children");
  const currentState = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(currentState.control.paused).toBe(1);
  expect(currentState.tasks.filter(item => item.budget_owner_task_id === setup.scope_tasks[1])).toHaveLength(3);
  expect(failures).toEqual([]);
  await page.screenshot({ path: resolve(root, "acceptance.png"), fullPage: true });
  const report = {
    status: "passed", checks: ["save_spec", "approve_spec", "persist_after_reload", "stale_after_input", "paused_operations", "no_implicit_run", "candidate_lookup_decision_persisted", "pr_relation_decision_persisted", "publication_reconcile_queued_without_write", "scoped_runner_log_read", "scope_converge", "scope_split", "scope_child_navigation", "scope_budget_preserved"],
    screenshot: resolve(root, "acceptance.png"),
  };
  await writeFile(resolve(root, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  if (browser) await browser.close();
  host.kill();
}
