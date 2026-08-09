import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "production.spec.ts",
  outputDir: "test-results-production",
  fullyParallel: false,
  retries: 0,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report-production", open: "never" }],
  ],
  use: {
    baseURL: process.env.AETHER_PRODUCTION_E2E_URL || "http://127.0.0.1",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
});
