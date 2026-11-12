export async function settingsPage(page, name) {
  await page.getByRole("navigation", { name: "工作台导航" })
    .getByRole("button", { name: "设置与维护", exact: true }).click();
  await page.getByRole("navigation", { name: "设置与维护页面" })
    .getByRole("button", { name, exact: true }).click();
}

export async function repositoryPage(page, name, section) {
  await page.getByRole("navigation", { name: "工作台导航" })
    .getByRole("button", { name: "仓库", exact: true }).click();
  const all = page.getByRole("button", { name: "← 所有仓库", exact: true });
  if (await all.isVisible()) await all.click();
  await page.getByRole("button", { name: name + " →", exact: true }).click();
  await page.getByRole("navigation", { name: "仓库页面" })
    .getByRole("button", { name: section, exact: true }).click();
}

export async function taskById(page, id) {
  await settingsPage(page, "任务队列");
  const index = await page.evaluate(async id =>
    (await (await fetch("/api/v1/state")).json()).tasks.findIndex(task => task.id === id), id);
  if (index < 0) throw new Error("missing fixture task");
  await page.locator("button.task-row").nth(index).click();
}
