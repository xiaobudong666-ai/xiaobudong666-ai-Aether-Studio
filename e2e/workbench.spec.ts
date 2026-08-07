import { expect, test } from "@playwright/test";

test("creates a project and exposes the real-media workbench", async ({
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

  await page.screenshot({
    path: testInfo.outputPath("aether-workbench.png"),
    fullPage: true,
  });
});
