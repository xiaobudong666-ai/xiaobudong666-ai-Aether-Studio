import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "g72-normal-entry.spec.mjs",
  outputDir: "test-results-g72",
  timeout: 420_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  forbidOnly: true,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report-g72", open: "never" }],
  ],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: "VITE_DEV_API_TARGET=http://127.0.0.1:18100 pnpm --filter @aether/web dev --host 127.0.0.1",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
