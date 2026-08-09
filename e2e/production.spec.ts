import { stat } from "node:fs/promises";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

const enabled = process.env.AETHER_PRODUCTION_E2E === "1";
const email = process.env.AETHER_PRODUCTION_E2E_EMAIL || "admin@aether.local";
const password = process.env.AETHER_PRODUCTION_E2E_PASSWORD || "";
const fixturePath = resolve("e2e/fixtures/browser-real.mp4");

test.skip(!enabled, "仅在真实 Docker 全栈验收中运行");
test.setTimeout(300_000);

test("浏览器真实完成上传、预览、入轨、渲染、刷新恢复与下载", async ({ page }, testInfo) => {
  await page.goto("/");
  await page.getByLabel("邮箱").fill(email);
  await page.getByLabel("密码").fill(password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByText("素材库", { exact: true })).toBeVisible();

  const projectName = `真实成片验收-${Date.now()}`;
  await page.getByPlaceholder("输入新项目名称").fill(projectName);
  await page.getByRole("button", { name: "创建项目" }).click();
  await expect(page.getByRole("option", { name: `${projectName}（版本 1）` })).toHaveCount(1);

  await page.getByLabel("选择媒体文件").setInputFiles(fixturePath);
  await page.getByRole("button", { name: "上传媒体" }).click();
  await expect(page.getByText("browser-real.mp4", { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(page.getByRole("status")).toContainText("已上传并完成媒体信息检测");

  const video = page.getByLabel("预览素材 browser-real.mp4");
  await expect(video).toBeVisible();
  await expect.poll(async () => video.evaluate((element) => (element as HTMLVideoElement).readyState), {
    timeout: 60_000,
  }).toBeGreaterThan(0);
  await page.getByRole("button", { name: "播放" }).click();
  await expect.poll(async () => video.evaluate((element) => (element as HTMLVideoElement).currentTime), {
    timeout: 15_000,
  }).toBeGreaterThan(0.05);
  await page.getByRole("button", { name: "暂停" }).click();
  await page.getByRole("button", { name: "回到开头" }).click();
  await expect.poll(async () => video.evaluate((element) => (element as HTMLVideoElement).currentTime)).toBeLessThan(0.05);
  await page.screenshot({ path: testInfo.outputPath("05-real-media-preview.png"), fullPage: true });

  await page.getByRole("button", { name: "+ 添加到时间线" }).click();
  await expect(page.getByRole("button", { name: /选择片段，时长/ })).toBeVisible();
  await page.getByRole("button", { name: /选择片段，时长/ }).click();
  await expect(page.getByText("已选片段")).toBeVisible();
  await expect(page.getByText("视频轨道 1", { exact: true })).toBeVisible();
  await page.getByLabel("时间线位置").fill("0.5");
  await expect(page.getByLabel("时间线位置")).toHaveValue("0.5");

  await page.getByRole("button", { name: "提交渲染任务" }).click();
  await expect(page.getByText(/排队中|正在分派|渲染中|已完成/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText("已完成", { exact: true })).toBeVisible({ timeout: 180_000 });
  await expect(page.getByRole("link", { name: "下载 MP4 成片" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("06-render-completed.png"), fullPage: true });

  await page.reload();
  await expect(page.getByText("素材库", { exact: true })).toBeVisible();
  await page.getByLabel("选择项目").selectOption({ label: `${projectName}（版本 3）` });
  await expect(page.getByText("已完成", { exact: true })).toBeVisible({ timeout: 30_000 });

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载 MP4 成片" }).click();
  const download = await downloadPromise;
  const downloadPath = await download.path();
  expect(downloadPath).toBeTruthy();
  expect((await stat(downloadPath!)).size).toBeGreaterThan(1_000);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText("素材库", { exact: true })).toBeVisible();
  const hasHorizontalOverflow = await page.evaluate(() => (
    document.documentElement.scrollWidth > document.documentElement.clientWidth
  ));
  expect(hasHorizontalOverflow).toBe(false);
  await page.screenshot({ path: testInfo.outputPath("07-real-flow-mobile.png"), fullPage: true });
});
