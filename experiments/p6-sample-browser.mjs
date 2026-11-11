import { createRequire } from "node:module";
import { resolve } from "node:path";
import { mkdir, writeFile } from "node:fs/promises";

const values = new Map();
for (let index = 2; index < process.argv.length; index += 2) values.set(process.argv[index], process.argv[index + 1]);
const sample = values.get("--sample");
const url = new URL(values.get("--url"));
if (!["python-web", "typescript-web"].includes(sample) || url.hostname !== "127.0.0.1" || url.protocol !== "http:") throw new Error("controlled loopback sample required");
const require = createRequire(resolve("frontend/package.json"));
const { chromium, expect } = require("@playwright/test");
const directory = resolve(".cache/p6-sample-browser", sample);
await mkdir(directory, { recursive: true });
const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
    const page = await browser.newPage({ viewport: { width: 1100, height: 700 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(url.href);
    await expect(page.locator("h1")).toContainText(sample === "python-web" ? "P6 Python Web" : "P6 TypeScript Web");
    await expect(page.locator("#result")).toHaveText("5");
    await page.reload();
    await expect(page.locator("#result")).toHaveText("5");
    if (errors.length) throw new Error(JSON.stringify(errors));
    await page.screenshot({ path: resolve(directory, "sample.png"), fullPage: true });
    const report = { sample, passed: true, checks: ["actual HTTP document", "correct result from candidate", "reload retains result", "no browser runtime errors"], externalModels: false, errors };
    await writeFile(resolve(directory, "report.json"), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
} finally { await browser.close(); }
