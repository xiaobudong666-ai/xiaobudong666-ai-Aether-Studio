import { expect, test } from "@playwright/test";

const testPassword = process.env.AETHER_PLAYWRIGHT_PASSWORD;
if (!testPassword) throw new Error("Playwright 测试密码尚未初始化");

async function signIn(page: import("@playwright/test").Page) {
  await page.getByLabel("邮箱").fill("admin@aether.local");
  await page.getByLabel("密码").fill(testPassword!);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByText("素材库", { exact: true })).toBeVisible();
}

test("中文登录、项目创建和兼容文件导出均可操作", async ({ page }, testInfo) => {
  await page.goto("/");

  await expect(page).toHaveTitle(/AI 漫剧视频工作台/);
  await expect(page.locator("html")).toHaveAttribute("lang", "zh-CN");
  await expect(page.getByRole("heading", { name: "Aether Studio" })).toBeVisible();
  await expect(page.getByText("登录你的安全漫剧工作区")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("01-login-desktop.png"), fullPage: true });

  await signIn(page);
  await expect(page.getByText("画面监看 · 480p 代理目标")).toBeVisible();
  await expect(page.getByText("属性与任务")).toBeVisible();
  await expect(page.getByText(/时间线轨道（标准格式 v1\.1）/)).toBeVisible();
  await expect(page.getByText(/项目 \d+\/50/)).toBeVisible();

  const projectName = `中文交互验收-${Date.now()}`;
  await page.getByPlaceholder("输入新项目名称").fill(projectName);
  await page.getByRole("button", { name: "创建项目" }).click();

  const projectOption = page.getByRole("option", { name: `${projectName}（版本 1）` });
  await expect(projectOption).toHaveCount(1);
  const projectId = await projectOption.getAttribute("value");
  expect(projectId).toBeTruthy();
  await expect(page.getByLabel("选择项目")).toHaveValue(projectId!);
  await expect(page.getByRole("status")).toContainText("已创建");

  await expect(page.getByText("上传真实媒体")).toBeVisible();
  await expect(page.getByRole("button", { name: "选择媒体文件" })).toBeVisible();
  await expect(page.getByText("尚未选择文件")).toBeVisible();
  await expect(page.getByText("Choose File")).toHaveCount(0);
  await expect(page.getByText("No file chosen")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "提交渲染任务" })).toBeDisabled();
  await expect(page.getByText("OpenCut 内核 0.2.10")).toBeVisible();

  const [openCutDownload] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "导出 OpenCut 快照" }).click(),
  ]);
  expect(openCutDownload.suggestedFilename()).toMatch(/\.opencut\.json$/);

  const [openReelDownload] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "导出 OpenReel 项目" }).click(),
  ]);
  expect(openReelDownload.suggestedFilename()).toMatch(/\.openreel\.json$/);

  await page.screenshot({ path: testInfo.outputPath("02-workbench-desktop.png"), fullPage: true });
});

test("错误提示、窄屏布局和退出登录可用", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByText("登录你的安全漫剧工作区")).toBeVisible();

  await page.getByLabel("邮箱").fill("admin@aether.local");
  await page.getByLabel("密码").fill("wrong-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("alert")).toContainText("邮箱或密码不正确");
  await page.screenshot({ path: testInfo.outputPath("03-login-error-mobile.png"), fullPage: true });

  await page.getByLabel("密码").fill(testPassword);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByText("素材库", { exact: true })).toBeVisible();
  await expect(page.getByPlaceholder("输入新项目名称")).toBeVisible();

  const libraryBox = await page.getByText("素材库", { exact: true }).boundingBox();
  const canvasBox = await page.getByText("画面监看 · 480p 代理目标", { exact: true }).boundingBox();
  const inspectorBox = await page.getByText("属性与任务", { exact: true }).boundingBox();
  const timelineBox = await page.getByText("时间线轨道（标准格式 v1.1）", { exact: false }).boundingBox();
  expect(libraryBox && canvasBox && inspectorBox && timelineBox).toBeTruthy();
  expect(libraryBox!.y).toBeLessThan(canvasBox!.y);
  expect(canvasBox!.y).toBeLessThan(inspectorBox!.y);
  expect(inspectorBox!.y).toBeLessThan(timelineBox!.y);

  const hasHorizontalOverflow = await page.evaluate(() => (
    document.documentElement.scrollWidth > document.documentElement.clientWidth
  ));
  expect(hasHorizontalOverflow).toBe(false);
  await page.screenshot({ path: testInfo.outputPath("04-workbench-mobile.png"), fullPage: true });

  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByText("登录你的安全漫剧工作区")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: "登录" })).toBeVisible();
});

test("只读成员在界面和接口两层都不能修改项目", async ({ page }, testInfo) => {
  await page.goto("/");
  await signIn(page);

  const viewerEmail = `viewer-${Date.now()}@aether.local`;
  const createViewer = await page.request.post("/api/admin/users", {
    headers: { "X-Aether-CSRF": "1" },
    data: {
      email: viewerEmail,
      displayName: "浏览器只读验收",
      password: testPassword,
      role: "viewer",
    },
  });
  expect(createViewer.status()).toBe(201);

  await page.getByRole("button", { name: "退出登录" }).click();
  await page.getByLabel("邮箱").fill(viewerEmail);
  await page.getByLabel("密码").fill(testPassword);
  await page.getByRole("button", { name: "登录" }).click();

  await expect(page.getByText(/只读成员/).first()).toBeVisible();
  await expect(page.getByText("当前为只读权限，不能修改项目。")).toBeVisible();
  await expect(page.getByRole("button", { name: "创建项目" })).toBeDisabled();
  await expect(page.getByLabel("媒体文件输入")).toBeDisabled();

  const blockedWrite = await page.request.post("/api/projects", {
    headers: { "X-Aether-CSRF": "1" },
    data: { name: "不应创建成功的项目" },
  });
  expect(blockedWrite.status()).toBe(403);
  expect(await blockedWrite.json()).toMatchObject({
    detail: { code: "PERMISSION_DENIED" },
  });
  await page.screenshot({ path: testInfo.outputPath("05-viewer-readonly.png"), fullPage: true });
});
