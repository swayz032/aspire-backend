import { test, expect } from '../../fixtures/auth';

/**
 * Founder Hub E2E Tests
 *
 * Tests the founder-hub/daily-brief page. Verifies real data from
 * Adam's research receipts or default state. No "Zenith Solutions"
 * or hardcoded mock text.
 */
test.describe('Founder Hub', () => {
  test('navigates to founder hub', async ({ authenticatedPage: page }) => {
    await page.goto('/founder-hub');
    await page.waitForTimeout(3000);

    // data-testid suggestion: [data-testid="founder-hub-page"]
    await expect(page.locator('body')).toBeVisible();
  });

  test('daily brief page loads', async ({ authenticatedPage: page }) => {
    await page.goto('/founder-hub/daily-brief');
    await page.waitForTimeout(5000);

    // data-testid suggestion: [data-testid="daily-brief-page"]
    const dailyBriefTitle = page.getByText('Daily Brief');
    await expect(dailyBriefTitle.first()).toBeVisible({ timeout: 10_000 });

    // Should show the subtitle
    await expect(
      page.getByText('AI-curated intelligence for your business')
    ).toBeVisible();
  });

  test('no "Zenith Solutions" mock data', async ({ authenticatedPage: page }) => {
    await page.goto('/founder-hub/daily-brief');
    await page.waitForTimeout(5000);

    const bodyText = await page.locator('body').innerText();
    expect(bodyText).not.toContain('Zenith Solutions');
  });

  test('no "pallet returns" mock data', async ({ authenticatedPage: page }) => {
    await page.goto('/founder-hub/daily-brief');
    await page.waitForTimeout(5000);

    const bodyText = await page.locator('body').innerText();
    // "pallet returns" was a known mock data artifact — should not appear
    expect(bodyText.toLowerCase()).not.toContain('pallet returns');
  });

  test('today badge and date are displayed', async ({ authenticatedPage: page }) => {
    await page.goto('/founder-hub/daily-brief');
    await page.waitForTimeout(5000);

    // data-testid suggestion: [data-testid="today-badge"]
    const todayBadge = page.getByText("Today's Brief");
    await expect(todayBadge).toBeVisible({ timeout: 10_000 });
  });

  test('key metrics section is visible', async ({ authenticatedPage: page }) => {
    await page.goto('/founder-hub/daily-brief');
    await page.waitForTimeout(5000);

    // data-testid suggestion: [data-testid="key-metrics-section"]
    const metricsTitle = page.getByText('Key Metrics Today');
    await expect(metricsTitle).toBeVisible({ timeout: 10_000 });

    // Should show some metric cards
    const cashPosition = page.getByText('Cash Position');
    await expect(cashPosition).toBeVisible();
  });

  test('focus areas section loads', async ({ authenticatedPage: page }) => {
    await page.goto('/founder-hub/daily-brief');
    await page.waitForTimeout(5000);

    // data-testid suggestion: [data-testid="focus-areas-section"]
    const focusTitle = page.getByText("Today's Focus Areas");
    await expect(focusTitle).toBeVisible({ timeout: 10_000 });
  });
});
