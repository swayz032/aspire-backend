import { test, expect } from './fixtures/auth';

/**
 * W9 Authenticated Page Screenshots
 *
 * Captures full-page screenshots of the 3 new/replaced pages with
 * an authenticated Supabase session, so the real UI content renders.
 */
test.describe('W9 Authenticated Screenshots', () => {
  test('screenshot: Front Desk Setup', async ({ authenticatedPage: page }) => {
    await page.goto('/session/calls/setup');
    await page.waitForTimeout(5000);

    await expect(page.locator('body')).toBeVisible();
    const bodyText = await page.locator('body').innerText();
    expect(bodyText.length).toBeGreaterThan(10);

    await page.screenshot({
      path: 'screenshots/w9-setup-auth.png',
      fullPage: true,
    });
  });

  test('screenshot: Return Calls', async ({ authenticatedPage: page }) => {
    await page.goto('/session/calls');
    await page.waitForTimeout(5000);

    await expect(page.locator('body')).toBeVisible();

    await page.screenshot({
      path: 'screenshots/w9-calls-auth.png',
      fullPage: true,
    });
  });

  test('screenshot: Text Messages', async ({ authenticatedPage: page }) => {
    await page.goto('/session/messages');
    await page.waitForTimeout(5000);

    await expect(page.locator('body')).toBeVisible();

    await page.screenshot({
      path: 'screenshots/w9-messages-auth.png',
      fullPage: true,
    });
  });

  test('screenshot: Home (Text Messages in menu)', async ({ authenticatedPage: page }) => {
    await page.goto('/');
    await page.waitForTimeout(5000);

    await expect(page.locator('body')).toBeVisible();

    await page.screenshot({
      path: 'screenshots/w9-home-auth.png',
      fullPage: true,
    });
  });
});
