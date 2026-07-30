import { expect, test } from "@playwright/test";

test("creates a project and receives render progress in the three-panel workbench", async ({
  page,
}, testInfo) => {
  await page.goto("/");

  await expect(page.getByText("Library & Materials")).toBeVisible();
  await expect(page.getByText("Canvas Monitor (480p Proxy Target)")).toBeVisible();
  await expect(page.getByText("Property Inspector & Tasks")).toBeVisible();
  await expect(page.getByText(/Timeline tracks \(Canonical v1\.1\)/)).toBeVisible();

  const projectName = `Playwright M0-0 ${Date.now()}`;
  await page.getByPlaceholder("New project name").fill(projectName);
  await page.getByRole("button", { name: "Create Project" }).click();

  await expect(
    page.getByRole("option", { name: `${projectName} (r1)` }),
  ).toBeVisible();

  await page.getByRole("button", { name: /Trigger AI Proxy Render/ }).click();
  await expect(page.getByText(/Task: [a-f0-9]{8}/)).toBeVisible({
    timeout: 10_000,
  });
  await expect(
    page.getByText(/pending|processing|completed/, { exact: true }),
  ).toBeVisible();

  await page.screenshot({
    path: testInfo.outputPath("aether-workbench.png"),
    fullPage: true,
  });
});
