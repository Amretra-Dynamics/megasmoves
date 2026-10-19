import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 30000,
  use: {
    headless: true,
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.MEGAS_TEST_CHROMIUM
      ? { executablePath: process.env.MEGAS_TEST_CHROMIUM }
      : {},
  },
});
