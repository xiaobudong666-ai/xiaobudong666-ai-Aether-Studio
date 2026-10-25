import { expect, test } from "@playwright/test";

const enabled = process.env.AETHER_G72_BROWSER_E2E === "1";
const email = process.env.AETHER_BOOTSTRAP_ADMIN_EMAIL || "admin@aether.local";
const password = process.env.AETHER_BOOTSTRAP_ADMIN_PASSWORD || "";

test.skip(!enabled, "仅在隔离的 G72 FAKE-only Compose 网络中运行");
if (enabled && !password) {
  throw new Error("AETHER_BOOTSTRAP_ADMIN_PASSWORD must be supplied by the disposable CI job");
}

test.setTimeout(180_000);

test("G72 浏览器正常入口→专用 FAKE-only API→Worker，权利阻断且不可采纳/发布", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "输入内容" })).toBeVisible();

  const projectsResponse = await page.request.get("/api/projects");
  expect(projectsResponse.status()).toBe(200);
  const projects = await projectsResponse.json() as Array<{ id: string; name: string }>;
  const project = projects.find((candidate) => candidate.name.startsWith("FAKE G72 browser E2E "));
  expect(project, "synthetic browser fixture must exist before opening the UI").toBeTruthy();

  await page.getByText("高级编辑", { exact: false }).click();
  await expect(page.getByLabel("选择项目")).toBeVisible();
  await page.getByLabel("选择项目").selectOption(project!.id);
  await expect(page.getByText("G72 · FAKE-only口播")).toBeVisible();
  await expect(page.getByText("服务端权威状态：可创建")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "打开生成任务" }).click();

  const imageChoice = page.getByRole("checkbox", { name: /图片版本 1/ });
  await expect(imageChoice).toBeVisible({ timeout: 30_000 });
  await imageChoice.check();
  await page.getByLabel("生成主题").fill("Synthetic browser E2E: FAKE-only talking head");
  await page.getByLabel("确认合成测试边界").check();

  const preflightResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "POST"
      && url.pathname.endsWith(`/projects/${project!.id}/talking-head/fake-generation-tasks/validate`);
  });
  await page.getByRole("button", { name: "执行服务端预检" }).click();
  const preflightResponse = await preflightResponsePromise;
  expect(preflightResponse.status()).toBe(200);
  await expect(page.getByText(/FAKE-only口播预检通过/)).toBeVisible();

  const createResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "POST"
      && url.pathname.endsWith(`/projects/${project!.id}/talking-head/fake-generation-tasks`);
  });
  await page.getByRole("button", { name: "提交生成" }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.status()).toBe(202);
  await expect(page.getByText(/FAKE-only口播任务 .* 已进入服务端Worker队列/)).toBeVisible();

  await expect(page.getByText(/等待权利审核/)).toBeVisible({ timeout: 120_000 });
  await expect(page.getByText("FAKE-only模拟产物：不可采纳或发布。")).toBeVisible();
  await expect(page.getByRole("button", { name: "用于快速制作" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "应用到口播草稿" })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("g72-fake-only-normal-entry-worker.png"), fullPage: true });
});
