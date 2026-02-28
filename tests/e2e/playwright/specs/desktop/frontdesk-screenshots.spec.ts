import { test, expect } from '../../fixtures/auth';

/**
 * Front Desk / SMS / Voicemail Visual Screenshots
 *
 * Captures full-page screenshots of the 3 new/replaced W9 pages
 * so we can visually verify the premium dark design system.
 */
test.describe('W9 Front Desk Pages - Visual', () => {
  test('screenshot: Front Desk Setup', async ({ authenticatedPage: page }) => {
    await page.goto('/session/calls/setup');
    await page.waitForTimeout(5000);

    // Verify page loaded (not blank or error)
    await expect(page.locator('body')).toBeVisible();
    const bodyText = await page.locator('body').innerText();
    expect(bodyText.length).toBeGreaterThan(10);

    await page.screenshot({
      path: 'screenshots/w9-setup.png',
      fullPage: true,
    });
  });

  test('screenshot: Return Calls (Call Center)', async ({ authenticatedPage: page }) => {
    await page.goto('/session/calls');
    await page.waitForTimeout(5000);

    await expect(page.locator('body')).toBeVisible();
    const bodyText = await page.locator('body').innerText();
    expect(bodyText.length).toBeGreaterThan(10);

    await page.screenshot({
      path: 'screenshots/w9-calls.png',
      fullPage: true,
    });
  });

  test('screenshot: Text Messages', async ({ authenticatedPage: page }) => {
    await page.goto('/session/messages');
    await page.waitForTimeout(5000);

    await expect(page.locator('body')).toBeVisible();
    const bodyText = await page.locator('body').innerText();
    expect(bodyText.length).toBeGreaterThan(10);

    await page.screenshot({
      path: 'screenshots/w9-messages.png',
      fullPage: true,
    });
  });

  test('screenshot: Home page (verify Text Messages in menu)', async ({ authenticatedPage: page }) => {
    await page.goto('/');
    await page.waitForTimeout(5000);

    await expect(page.locator('body')).toBeVisible();

    // Verify Text Messages appears in the interaction mode menu
    const textMessagesEntry = page.getByText('Text Messages');
    const hasEntry = await textMessagesEntry.isVisible().catch(() => false);
    expect(hasEntry).toBeTruthy();

    await page.screenshot({
      path: 'screenshots/w9-home-menu.png',
      fullPage: true,
    });
  });
});
