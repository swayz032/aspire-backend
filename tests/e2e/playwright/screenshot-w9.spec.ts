/**
 * Quick W9 page screenshot capture — standalone script, no auth fixture needed.
 * Run: npx playwright test screenshot-w9.ts --project=chromium
 *
 * Captures the page layout/design. Pages may redirect to login if auth is required,
 * but that still verifies the routes are wired correctly.
 */
import { test, expect } from '@playwright/test';

const BASE = 'http://localhost:5000';

test.describe('W9 Visual Screenshots', () => {
  test('capture setup page', async ({ page }) => {
    await page.goto(`${BASE}/session/calls/setup`);
    await page.waitForTimeout(4000);
    await page.screenshot({ path: 'screenshots/w9-setup.png', fullPage: true });
    console.log('Saved: screenshots/w9-setup.png');
  });

  test('capture calls page', async ({ page }) => {
    await page.goto(`${BASE}/session/calls`);
    await page.waitForTimeout(4000);
    await page.screenshot({ path: 'screenshots/w9-calls.png', fullPage: true });
    console.log('Saved: screenshots/w9-calls.png');
  });

  test('capture messages page', async ({ page }) => {
    await page.goto(`${BASE}/session/messages`);
    await page.waitForTimeout(4000);
    await page.screenshot({ path: 'screenshots/w9-messages.png', fullPage: true });
    console.log('Saved: screenshots/w9-messages.png');
  });

  test('capture home page with Text Messages menu', async ({ page }) => {
    await page.goto(`${BASE}/`);
    await page.waitForTimeout(4000);
    await page.screenshot({ path: 'screenshots/w9-home.png', fullPage: true });
    console.log('Saved: screenshots/w9-home.png');
  });
});
