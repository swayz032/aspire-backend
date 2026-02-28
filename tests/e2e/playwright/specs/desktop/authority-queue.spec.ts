import { test, expect } from '../../fixtures/auth';

/**
 * Authority Queue E2E Tests
 *
 * Tests the session/authority page where YELLOW/RED risk tier actions
 * await approval. Verifies the approval/deny flow with confirmation modals.
 */
test.describe('Authority Queue', () => {
  test('navigates to authority queue', async ({ authenticatedPage: page }) => {
    await page.goto('/session/authority');
    await page.waitForTimeout(3000);

    // data-testid suggestion: [data-testid="authority-queue-page"]
    const authorityTitle = page.getByText('Authority Queue');
    await expect(authorityTitle).toBeVisible({ timeout: 10_000 });
  });

  test('shows pending items or empty state', async ({ authenticatedPage: page }) => {
    await page.goto('/session/authority');
    await page.waitForTimeout(5000);

    // Either shows "Pending Approval" section or "No items" message
    // data-testid suggestion: [data-testid="authority-pending-section"]
    const pendingSection = page.getByText('Pending Approval');
    const emptyState = page.getByText('No items in the authority queue');
    const loadingState = page.getByText('Loading authority queue');

    // Wait for loading to finish
    await page.waitForFunction(
      () => !document.body.innerText.includes('Loading authority queue'),
      { timeout: 10_000 }
    ).catch(() => {
      // Loading may have already completed
    });

    const hasPending = await pendingSection.isVisible().catch(() => false);
    const hasEmpty = await emptyState.isVisible().catch(() => false);

    // One of these states should be visible
    expect(hasPending || hasEmpty).toBeTruthy();
  });

  test('pending items have approve and deny buttons', async ({ authenticatedPage: page }) => {
    await page.goto('/session/authority');
    await page.waitForTimeout(5000);

    const pendingSection = page.getByText('Pending Approval');
    const hasPending = await pendingSection.isVisible().catch(() => false);

    if (!hasPending) {
      test.skip(true, 'No pending items in authority queue — skipping button test');
      return;
    }

    // data-testid suggestions: [data-testid="authority-approve-btn"], [data-testid="authority-deny-btn"]
    const approveButton = page.getByText('Approve').first();
    const denyButton = page.getByText('Deny').first();

    await expect(approveButton).toBeVisible();
    await expect(denyButton).toBeVisible();
  });

  test('approve button opens confirmation modal', async ({ authenticatedPage: page }) => {
    await page.goto('/session/authority');
    await page.waitForTimeout(5000);

    const pendingSection = page.getByText('Pending Approval');
    const hasPending = await pendingSection.isVisible().catch(() => false);

    if (!hasPending) {
      test.skip(true, 'No pending items — skipping approval modal test');
      return;
    }

    // Click approve on first pending item
    const approveButton = page.getByText('Approve').first();
    await approveButton.click();

    // Confirmation modal should appear
    // data-testid suggestion: [data-testid="confirmation-modal"]
    await expect(page.getByText('Approve Request')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText(/Are you sure you want to approve/)).toBeVisible();
  });

  test('deny button opens confirmation modal', async ({ authenticatedPage: page }) => {
    await page.goto('/session/authority');
    await page.waitForTimeout(5000);

    const pendingSection = page.getByText('Pending Approval');
    const hasPending = await pendingSection.isVisible().catch(() => false);

    if (!hasPending) {
      test.skip(true, 'No pending items — skipping deny modal test');
      return;
    }

    // Click deny on first pending item
    const denyButton = page.getByText('Deny').first();
    await denyButton.click();

    // Confirmation modal should appear
    await expect(page.getByText('Deny Request')).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText(/Are you sure you want to deny/)).toBeVisible();
  });

  test('shows resolved items section when items have been approved/denied', async ({
    authenticatedPage: page,
  }) => {
    await page.goto('/session/authority');
    await page.waitForTimeout(5000);

    // Resolved section shows approved/denied items
    // data-testid suggestion: [data-testid="authority-resolved-section"]
    const resolvedSection = page.getByText('Resolved');
    const hasResolved = await resolvedSection.isVisible().catch(() => false);

    if (hasResolved) {
      // Approved or Denied status badges should be visible
      const approvedBadge = page.getByText('Approved');
      const deniedBadge = page.getByText('Denied');
      const hasStatus =
        (await approvedBadge.isVisible().catch(() => false)) ||
        (await deniedBadge.isVisible().catch(() => false));
      expect(hasStatus).toBeTruthy();
    }
    // If no resolved items, that is acceptable
  });
});
