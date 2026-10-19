import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, registerAndQueue, signIn } from "./support";

/**
 * Taking money from the reception queue row: "Get payment" and "Paid".
 *
 * The endpoint's rules (one transaction, no typed amount, refusal when the
 * bill moved) have their own backend tests. This spec exists to catch the
 * screen drifting from them — a strip that never appears, a QR missing from
 * the dialog, a row that still says "due" after the money is in.
 */
async function rowFor(page: Page, name: string) {
  await page.goto("/reception");
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.scrollIntoViewIfNeeded();
  return row;
}

test("a UPI payment is taken from the row's dialog and the row shows it paid", async ({
  page,
}) => {
  const name = await registerAndQueue(page);
  await signIn(page, ACCOUNTS.reception);
  const row = await rowFor(page, name);

  await expect(row.getByTestId("amount-due")).toContainText("500");
  await row.getByTestId("get-payment").click();

  const dialog = page.getByRole("dialog");
  // What was booked, and a QR for exactly that.
  await expect(dialog.getByText("OPD consultation")).toBeVisible();
  await expect(dialog.getByTestId("collect-total")).toContainText("500");
  await expect(dialog.getByRole("img", { name: /UPI QR code/ })).toBeVisible();

  // No reference, no receipt: UPI is reconciled by its UTR.
  const confirm = dialog.getByTestId("confirm-received");
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel("UPI transaction ID (UTR)").fill("612345678901");
  await confirm.click();

  await expect(page.getByText(/received\. Receipt/)).toBeVisible();
  await expect(row.getByTestId("paid-chip")).toContainText("500");
  await expect(row.getByTestId("collect-strip")).toHaveCount(0);
});

test("cash takes two taps on Paid — the first only arms it", async ({ page }) => {
  const name = await registerAndQueue(page);
  await signIn(page, ACCOUNTS.reception);
  const row = await rowFor(page, name);

  const paid = row.getByTestId("mark-paid");
  await paid.click();
  await expect(paid).toHaveText(/Confirm cash/);
  // Armed, not recorded.
  await expect(row.getByTestId("amount-due")).toBeVisible();

  await paid.click();
  await expect(row.getByTestId("paid-chip")).toContainText("500");
});
