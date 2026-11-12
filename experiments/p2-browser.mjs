import { createRequire } from "node:module";
import { repositoryPage } from "./workbench-browser-navigation.mjs";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { createServer } from "node:net";

const require = createRequire(resolve("frontend/package.json"));
const { chromium, expect } = require("@playwright/test");
const root = resolve(".cache/p2-browser", String(Date.now()));
await mkdir(root, { recursive: true });
const port = await new Promise((done, fail) => {
  const server = createServer(); server.once("error", fail);
  server.listen(0, "127.0.0.1", () => {
    const value = server.address().port; server.close(() => done(value));
  });
});
const host = spawn(resolve("backend/.venv/Scripts/python.exe"), [
  "-X", "utf8", "experiments/p2_browser_host.py", "--data-dir", resolve(root, "data"),
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
  await repositoryPage(page, "fixture/p2-scan", "分析");
  await expect(page.getByText("浏览器候选", { exact: false }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "待我处理 1", exact: true })).toBeVisible();
  await expect(page.getByLabel("发现于哪次扫描", { exact: true })).toBeHidden();
  await page.getByText("筛选发现", { exact: true }).click();
  await page.getByLabel("跟进进度", { exact: true }).selectOption("linked");
  await page.getByRole("button", { name: "Issue #38", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Issue #38 · 已登记的问题", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "回看扫描发现", exact: true }).click();
  await expect(page.getByRole("heading", { name: "已跟进候选", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "← 返回扫描发现", exact: true }).click();
  await expect(page.getByLabel("跟进进度", { exact: true })).toHaveValue("linked");
  await page.getByLabel("跟进进度", { exact: true }).selectOption("");
  await page.setViewportSize({ width: 390, height: 900 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const selectedCandidate = page.locator(".workbench-row").filter({ has: page.getByRole("heading", { name: "浏览器候选", exact: true }) });
  const excludedCandidate = page.locator(".workbench-row").filter({ has: page.getByRole("heading", { name: "待排除候选", exact: true }) });
  await selectedCandidate.getByRole("button", { name: "查看发现依据 →", exact: true }).click();
  await page.getByRole("button", { name: "查看扫描依据", exact: true }).click();
  await expect(page.locator("pre").filter({ hasText: "已核对实际分块" })).toBeVisible();
  await page.getByRole("button", { name: "开始处理", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("系统会先查重");
  await page.getByRole("button", { name: "← 返回扫描发现", exact: true }).click();
  await excludedCandidate.getByRole("button", { name: "查看发现依据 →", exact: true }).click();
  await page.getByRole("button", { name: "排除此发现", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("已排除此发现");
  await page.getByRole("button", { name: "← 返回扫描发现", exact: true }).click();
  await expect(selectedCandidate.getByRole("button", { name: "开始处理", exact: true })).toHaveCount(0);
  await expect(excludedCandidate.getByText("已排除此发现", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "查看扫描任务与处理入口", exact: true }).click();
  await expect(page.getByRole("heading", { name: "任务操作" })).toBeVisible();
  await page.getByLabel("补充信息", { exact: true }).fill("补齐离线依赖并保持既有锁文件");
  await page.getByRole("button", { name: "保存补充材料", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("操作已保存");
  let state = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(state.tasks.find(task => task.id === setup.task_id).mode).toBe("scan");
  expect(state.control.paused).toBe(1);
  await page.getByRole("button", { name: "取消任务", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("操作已保存");
  state = await page.evaluate(async () => (await fetch("/api/v1/state")).json());
  expect(state.tasks.find(task => task.id === setup.task_id).state).toBe("cancelled");
  expect(failures).toEqual([]);
  await page.screenshot({ path: resolve(root, "scans.png"), fullPage: true });
  const report = { status: "passed", checks: ["valid_coverage", "scan_artifact", "candidate_request", "candidate_exclusion", "task_navigation", "supplement_preserves_scan_mode", "paused_control", "cancel", "issue_round_trip", "scan_filters", "passive_wait_excluded", "narrow_layout"], screenshot: resolve(root, "scans.png") };
  await writeFile(resolve(root, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  if (browser) await browser.close();
  host.kill();
}
