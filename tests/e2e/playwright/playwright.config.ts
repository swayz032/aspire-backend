import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright configuration for Aspire Desktop E2E tests.
 *
 * Base URL: http://localhost:3100 (Express server serving Expo web build)
 * Projects: Chromium only (desktop web app)
 */
export default defineConfig({
  testDir: './specs',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  timeout: 30_000,
  globalTimeout: 60_000,

  reporter: [
    ['list'],
    ['html', { open: 'never' }],
  ],

  use: {
    baseURL: 'http://localhost:3100',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'on-first-retry',
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },

  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],

  webServer: {
    command: 'npm start',
    cwd: '../../../Aspire-desktop',
    port: 3100,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
