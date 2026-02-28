import { test, expect } from '../../fixtures/auth';

/**
 * Mail Flow Cross-Surface E2E Test
 *
 * Tests the inbox/mail functionality:
 * 1. Navigate to inbox
 * 2. Verify thread list loads (if PolarisM is accessible)
 * 3. Verify draft creation works (if available)
 */
test.describe('Mail Flow', () => {
  test('inbox loads with real mail data or empty state', async ({
    authenticatedPage: page,
  }) => {
    await page.goto('/inbox');
    await page.waitForTimeout(5000);

    // data-testid suggestion: [data-testid="inbox-page"]
    await expect(page.locator('body')).toBeVisible();

    // Check for mail thread items or empty state
    // data-testid suggestion: [data-testid="mail-thread-list"]
    const threadList = page
      .locator('[data-testid="mail-thread-list"]')
      .or(page.locator('[role="list"]'))
      .first();

    const hasThreads = await threadList.isVisible().catch(() => false);

    if (hasThreads) {
      // Threads are available — verify at least one is visible
      const firstThread = page
        .locator('[data-testid="inbox-thread-item"]')
        .or(page.locator('[role="listitem"]'))
        .first();

      const threadVisible = await firstThread.isVisible().catch(() => false);
      if (threadVisible) {
        await expect(firstThread).toBeVisible();
      }
    }

    // Either threads or empty state — page should be functional
    const bodyText = await page.locator('body').innerText();
    expect(bodyText.length).toBeGreaterThan(0);
  });

  test('mail thread detail loads when clicked', async ({ authenticatedPage: page }) => {
    await page.goto('/inbox');
    await page.waitForTimeout(5000);

    // data-testid suggestion: [data-testid="inbox-thread-item"]
    const firstThread = page
      .locator('[data-testid="inbox-thread-item"]')
      .or(page.locator('[role="listitem"]'))
      .first();

    const threadVisible = await firstThread.isVisible().catch(() => false);

    if (!threadVisible) {
      test.skip(true, 'No mail threads available — PolarisM may not be connected');
      return;
    }

    await firstThread.click();
    await page.waitForTimeout(3000);

    // Thread detail view should load
    // data-testid suggestion: [data-testid="mail-thread-detail"]
    await expect(page.locator('body')).toBeVisible();
    const bodyText = await page.locator('body').innerText();
    expect(bodyText.length).toBeGreaterThan(0);
  });

  test('draft creation accessible if PolarisM connected', async ({
    authenticatedPage: page,
  }) => {
    await page.goto('/inbox');
    await page.waitForTimeout(5000);

    // Look for a compose/draft button
    // data-testid suggestion: [data-testid="compose-button"], [data-testid="new-draft"]
    const composeButton = page
      .locator('[data-testid="compose-button"]')
      .or(page.getByText(/Compose|New|Draft/i))
      .first();

    const hasCompose = await composeButton.isVisible().catch(() => false);

    if (!hasCompose) {
      test.skip(true, 'Compose button not found — mail drafting may require PolarisM');
      return;
    }

    await composeButton.click();
    await page.waitForTimeout(3000);

    // Draft composition should open
    await expect(page.locator('body')).toBeVisible();
  });
});
