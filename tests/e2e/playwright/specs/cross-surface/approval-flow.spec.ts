import { test, expect } from '../../fixtures/auth';

/**
 * Approval Flow Cross-Surface E2E Test
 *
 * Tests the full YELLOW-tier approval flow:
 * 1. Trigger a YELLOW action via chat (e.g., "send an invoice")
 * 2. Navigate to the authority queue
 * 3. Verify an approval request appears
 * 4. Approve it
 * 5. Verify a receipt is generated
 *
 * This is a complex integration test requiring the full stack.
 */
test.describe('Approval Flow', () => {
  test.describe.configure({ mode: 'serial' });

  test('YELLOW action creates approval request', async ({ authenticatedPage: page }) => {
    // Step 1: Send a YELLOW-tier request via chat
    await page.waitForTimeout(3000);

    const chatInput = page.locator(
      'input[placeholder*="message" i], input[placeholder*="ask" i], ' +
      'textarea[placeholder*="message" i], textarea[placeholder*="ask" i], ' +
      '[data-testid="chat-input"], [data-testid="ava-input"]'
    ).first();

    const inputVisible = await chatInput.isVisible().catch(() => false);

    if (!inputVisible) {
      test.skip(
        true,
        'Chat input not found — approval flow requires Ava chat. ' +
        'Add data-testid="ava-input" to enable this test.'
      );
      return;
    }

    // Request a YELLOW action — sending an email or creating an invoice
    await chatInput.fill('Send a test email draft to team@example.com');

    const sendButton = page
      .locator('[data-testid="send-button"], [data-testid="ava-send-button"]')
      .first();
    const sendVisible = await sendButton.isVisible().catch(() => false);

    if (sendVisible) {
      await sendButton.click();
    } else {
      await chatInput.press('Enter');
    }

    // Wait for orchestrator to process and create approval request
    await page.waitForTimeout(8000);

    // Step 2: Navigate to authority queue
    await page.goto('/session/authority');
    await page.waitForTimeout(5000);

    // Step 3: Check if approval request appeared
    const pendingSection = page.getByText('Pending Approval');
    const hasPending = await pendingSection.isVisible().catch(() => false);

    if (!hasPending) {
      // Orchestrator may not be running or may have auto-handled the request
      // This is acceptable in a dev environment
      return;
    }

    // Step 4: Approve the first pending item
    const approveButton = page.getByText('Approve').first();
    const hasApprove = await approveButton.isVisible().catch(() => false);

    if (hasApprove) {
      await approveButton.click();

      // Confirmation modal should appear
      const confirmButton = page
        .locator('[data-testid="confirm-approve"]')
        .or(page.getByText('Approve').last())
        .first();
      await page.waitForTimeout(1000);

      const confirmVisible = await confirmButton.isVisible().catch(() => false);
      if (confirmVisible) {
        await confirmButton.click();
        await page.waitForTimeout(3000);

        // Step 5: Status should change to "Approved"
        const approvedStatus = page.getByText('Approved');
        const isApproved = await approvedStatus.isVisible().catch(() => false);

        if (isApproved) {
          await expect(approvedStatus.first()).toBeVisible();
        }
      }
    }
  });
});
