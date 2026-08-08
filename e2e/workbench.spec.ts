import { expect, test } from "@playwright/test";

const testPassword = process.env.AETHER_PLAYWRIGHT_PASSWORD;
if (!testPassword) throw new Error("AETHER_PLAYWRIGHT_PASSWORD was not initialized by Playwright config");

test("creates a project and exposes the real-media workbench", async ({
  page,
}, testInfo) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Aether Studio" })).toBeVisible();
  await page.getByLabel("Email").fill("admin@aether.local");
  await page.getByLabel("Password").fill(testPassword);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page.getByText("Library & Materials")).toBeVisible();
  await expect(page.getByText("Canvas Monitor (480p Proxy Target)")).toBeVisible();
  await expect(page.getByText("Property Inspector & Tasks")).toBeVisible();
  await expect(page.getByText(/Timeline tracks \(Canonical v1\.1\)/)).toBeVisible();

  const projectName = `Playwright M0-0 ${Date.now()}`;
  await page.getByPlaceholder("New project name").fill(projectName);
  await page.getByRole("button", { name: "Create Project" }).click();

  const projectOption = page.getByRole("option", {
    name: `${projectName} (r1)`,
  });
  await expect(projectOption).toHaveCount(1);

  const projectId = await projectOption.getAttribute("value");
  expect(projectId).toBeTruthy();
  await expect(
    page.locator(".project-select-container select"),
  ).toHaveValue(projectId!);

  await expect(page.getByText("Upload Real Media")).toBeVisible();
  await expect(page.getByRole("button", { name: /Render with video-use/ })).toBeDisabled();
  await expect(page.getByText("OpenCut Core 0.2.10")).toBeVisible();
  await expect(page.getByRole("button", { name: "Export OpenCut Snapshot" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Export OpenReel Project" })).toBeEnabled();

  await page.screenshot({
    path: testInfo.outputPath("aether-workbench.png"),
    fullPage: true,
  });
});

test("requires authentication and remains usable at a narrow viewport", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByText("Sign in to your protected workspace")).toBeVisible();
  await page.getByLabel("Email").fill("admin@aether.local");
  await page.getByLabel("Password").fill(testPassword);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("Library & Materials")).toBeVisible();
  await expect(page.getByPlaceholder("New project name")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("aether-workbench-mobile.png"),
    fullPage: true,
  });
});
