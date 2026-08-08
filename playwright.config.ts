import { defineConfig, devices } from "@playwright/test";
import { randomUUID } from "node:crypto";

const python = process.env.AETHER_PYTHON || "python3";
const testPassword = process.env.AETHER_PLAYWRIGHT_PASSWORD || `local-${randomUUID()}`;
process.env.AETHER_PLAYWRIGHT_PASSWORD = testPassword;
if (!/^[A-Za-z0-9_-]{12,128}$/.test(testPassword)) {
  throw new Error("AETHER_PLAYWRIGHT_PASSWORD must use 12-128 safe characters");
}

export default defineConfig({
  testDir: "./e2e",
  outputDir: "test-results",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command:
        "PYTHONPATH=apps/api DATABASE_URL=sqlite:////tmp/aether-playwright.db " +
        "AETHER_BOOTSTRAP_ADMIN_EMAIL=admin@aether.local " +
        `AETHER_BOOTSTRAP_ADMIN_PASSWORD=${testPassword} ` +
        "AETHER_COOKIE_SECURE=false AETHER_ENFORCE_CSRF=true " +
        `${python} -m uvicorn app.main:app --host 127.0.0.1 --port 8000`,
      url: "http://127.0.0.1:8000/health",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: "pnpm --filter @aether/web dev --host 127.0.0.1",
      url: "http://127.0.0.1:5173",
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
