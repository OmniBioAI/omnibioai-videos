import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  outputDir: '/tmp/n11a-videos-playwright',
  use: {
    headless: true,
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : {},
  },
  webServer: {
    command: 'python3 -m http.server 4178 --bind 127.0.0.1 --directory portal',
    url: 'http://127.0.0.1:4178/',
    reuseExistingServer: false,
  },
});
