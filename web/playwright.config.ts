import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for Voice AI browser journeys.
 * Implements the verification plan constraints:
 * - 1 worker
 * - 0 retries
 * - forbidOnly: true
 * - Chromium only
 * - JSON reporting
 * - Trace on failure
 * - Explicit base URL (injected, never assumed)
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: [
    ['json', { outputFile: process.env.PLAYWRIGHT_JSON_OUTPUT || 'playwright.json' }],
    ['list'],
  ],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:3001',
    trace: 'retain-on-failure',
    video: 'off',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: undefined,
});
