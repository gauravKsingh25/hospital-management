import { expect, test, type Page } from "@playwright/test";

import {
  ACCOUNTS,
  deskAdmits,
  openLine,
  openPatientBills,
  registerAndQueue,
  releaseTrackedBeds,
  sentForAdmission,
  signIn,
  signOut,
} from "./support";

/**
 * The printed bill: an A4 page with the hospital's letterhead, the GST
 * columns, the total in words and the receipts — the page "PDF" and "Print"
 * print in place. Browsers save it as a PDF; no PDF library is involved.
 */
releaseTrackedBeds();

async function paidOpdBill(page: Page): Promise<string> {
  const name = await registerAndQueue(page);
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception");
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await openLine(row);
  const paid = row.getByTestId("mark-paid");
  await paid.click();
  await paid.click();
  await expect(row.getByTestId("paid-chip")).toContainText("500");
  await signOut(page);
  return name;
}

test("the printed bill carries the letterhead, GST columns and amount in words", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const name = await paidOpdBill(page);

  await signIn(page, ACCOUNTS.accountant);
  await page.goto("/billing");
  const bill = page.getByTestId("bill-row").filter({ hasText: name });
  await expect(bill).toBeVisible();
  const number = (await bill.locator("td").nth(1).innerText()).trim();
  const open = await bill.getByRole("link", { name: `Open bill ${number}` }).getAttribute("href");
  const invoiceId = open?.match(/bill=([0-9a-f-]+)/)?.[1];
  await page.goto(`/bills/${invoiceId}`);
  const sheet = page.getByTestId("printed-bill");
  await expect(sheet.getByTestId("letterhead")).toBeVisible();
  await expect(sheet.getByRole("heading", { name: "BILL OF SUPPLY" })).toBeVisible();
  await expect(sheet).toContainText(number);
  await expect(sheet).toContainText(name);
  for (const column of ["SAC/HSN", "Taxable", "GST %", "CGST", "SGST"]) {
    await expect(sheet.getByRole("columnheader", { name: column, exact: true })).toBeVisible();
  }
  await expect(sheet).toContainText("Rupees Five Hundred Only");
  await expect(sheet.getByTestId("receipts")).toContainText("Cash");
  // No app chrome on paper.
  await expect(page.getByRole("navigation")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Print" })).toBeVisible();
});

test("a stay's running bill prints as interim", async ({ page }) => {
  test.setTimeout(180_000);
  const name = await sentForAdmission(page);
  await deskAdmits(page, name);
  await signOut(page);
  await signIn(page, ACCOUNTS.accountant);
  await openPatientBills(page, name);

  await page.getByRole("button", { name: "Print running bill" }).click();

  // Printed in place: the accountant stays on the patient's page.
  await expect(page).toHaveURL(/\/billing\/patients\//);
  await expect(
    page.frameLocator('[data-testid="print-frame"]').last().getByTestId("printed-bill"),
  ).toContainText("INTERIM — NOT A FINAL BILL");
});
