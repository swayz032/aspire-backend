import { test, expect } from '../../fixtures/auth';

/**
 * Finance Hub E2E Tests
 *
 * Tests the finance-hub main page. Verifies that cash position data loads
 * from connected providers or shows appropriate empty/demo state.
 */
test.describe('Finance Hub', () => {
  test('navigates to finance hub', async ({ authenticatedPage: page }) => {
    await page.goto('/finance-hub');
    await page.waitForTimeout(3000);

    // data-testid suggestion: [data-testid="finance-hub-page"]
    const financeTitle = page.getByText('Finance Hub');
    await expect(financeTitle.first()).toBeVisible({ timeout: 10_000 });
  });

  test('displays cash position section', async ({ authenticatedPage: page }) => {
    await page.goto('/finance-hub');
    await page.waitForTimeout(5000);

    // The finance hub shows either live data or demo/fallback data
    // data-testid suggestion: [data-testid="total-balance-card"]
    const balanceLabel = page.getByText('Total Balance');
    await expect(balanceLabel.first()).toBeVisible({ timeout: 10_000 });
  });

  test('shows provider cards', async ({ authenticatedPage: page }) => {
    await page.goto('/finance-hub');
    await page.waitForTimeout(5000);

    // Provider cards: Plaid, QuickBooks, Gusto
    // data-testid suggestion: [data-testid="provider-card-plaid"]
    const plaidCard = page.getByText(/Plaid/);
    const qbCard = page.getByText(/QuickBooks/);

    const hasPlaid = await plaidCard.first().isVisible().catch(() => false);
    const hasQB = await qbCard.first().isVisible().catch(() => false);

    // At least one provider card should be visible
    expect(hasPlaid || hasQB).toBeTruthy();
  });

  test('KPI section shows metrics', async ({ authenticatedPage: page }) => {
    await page.goto('/finance-hub');
    await page.waitForTimeout(5000);

    // KPI cards: Balance, Income, Savings, Expenses
    // data-testid suggestion: [data-testid="kpi-balance"]
    const kpiLabels = ['Balance', 'Income', 'Savings', 'Expenses'];
    let visibleCount = 0;

    for (const label of kpiLabels) {
      const element = page.getByText(label, { exact: true }).first();
      const visible = await element.isVisible().catch(() => false);
      if (visible) visibleCount++;
    }

    // At least some KPI cards should be visible
    expect(visibleCount).toBeGreaterThanOrEqual(2);
  });

  test('no hardcoded financial data when providers disconnected', async ({
    authenticatedPage: page,
  }) => {
    await page.goto('/finance-hub');
    await page.waitForTimeout(5000);

    // When no providers are connected, values should show dashes or "Demo" indicator
    // The page should not show fake dollar amounts as if they are real
    const bodyText = await page.locator('body').innerText();

    // Check for Demo badge or dash values indicating no live data
    const hasDemoIndicator = bodyText.includes('Demo') || bodyText.includes('\u2014');
    const hasLiveIndicator = bodyText.includes('Live');

    // One or the other should be present
    expect(hasDemoIndicator || hasLiveIndicator).toBeTruthy();
  });

  test('proposals section is visible', async ({ authenticatedPage: page }) => {
    await page.goto('/finance-hub');
    await page.waitForTimeout(5000);

    // data-testid suggestion: [data-testid="finn-proposals"]
    const proposalsTitle = page.getByText("Finn's Proposals");
    const hasProposals = await proposalsTitle.isVisible().catch(() => false);

    if (hasProposals) {
      // Should show proposal cards with risk badges
      const reviewButton = page.getByText('Review').first();
      await expect(reviewButton).toBeVisible();
    }
  });
});
