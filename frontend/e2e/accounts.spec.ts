import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, registerAndQueue, signIn, signOut } from "./support";

/**
 * Billing → Accounts: the accountant's own dashboard.
 *
 * Totals for the period, then every bill, receipt, outstanding bill and
 * patient — newest first, read only. The cash counter is untouched and stays
 * the cashier's and reception's screen.
 */
const LOGO = path.join(__dirname, "fixtures", "logo.png");

async function collectCash(page: Page, name: string): Promise<void> {
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception");
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.scrollIntoViewIfNeeded();
  const paid = row.getByTestId("mark-paid");
  await paid.click();
  await paid.click();
  await expect(row.getByTestId("paid-chip")).toContainText("500");
  await signOut(page);
}

test("the accountant opens Billing on the Accounts dashboard, newest bill first", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const name = await registerAndQueue(page);
  await collectCash(page, name);

  await signIn(page, ACCOUNTS.accountant);
  await page.goto("/billing");

  await expect(page.getByRole("tab", { name: "Accounts", selected: true })).toBeVisible();
  const totals = page.getByTestId("totals-strip");
  await expect(totals.getByText("Collected")).toBeVisible();
  await expect(totals.getByTestId("total-collected")).not.toHaveText("₹0.00");

  const firstBill = page.getByTestId("bill-row").first();
  await expect(firstBill).toContainText(name);
  await expect(firstBill).toContainText("OPD");
  // Open goes to the patient's bills with this one selected.
  await expect(firstBill.getByRole("link", { name: /Open bill/ })).toHaveAttribute(
    "href",
    /\/billing\/patients\/[0-9a-f-]+\?bill=[0-9a-f-]+$/,
  );
  // Print prints in place: the page stays, the A4 bill loads in a hidden frame.
  await firstBill.getByRole("button", { name: /Print bill/ }).click();
  await expect(page).toHaveURL(/\/billing$/);
  await expect(
    page.frameLocator('[data-testid="print-frame"]').last().getByTestId("printed-bill"),
  ).toContainText(name);

  await page.getByRole("tab", { name: "Payments received" }).click();
  await expect(page.getByTestId("payment-row").first()).toContainText(name);

  await page.getByRole("tab", { name: "Patients" }).click();
  const patient = page.getByTestId("patient-row").first();
  await expect(patient).toContainText(name);
  await patient.getByRole("link", { name }).click();
  await expect(page).toHaveURL(/\/billing\/patients\/[0-9a-f-]+$/);
});

test("reception keeps the cash counter and has no Accounts tab", async ({ page }) => {
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/billing");

  await expect(page.getByRole("heading", { name: "Cash counter" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Accounts" })).toHaveCount(0);
});

test("a banner asks for the hospital profile while the address is missing", async ({
  page,
}) => {
  await signIn(page, ACCOUNTS.admin);
  await page.goto("/admin/hospital");
  const line1 = page.getByLabel("Address line 1");
  const address = (await line1.inputValue()) || "12 MG Road, Civil Lines";

  try {
    await line1.fill("");
    await page.getByRole("button", { name: "Save profile" }).click();
    await expect(page.getByText("Hospital profile saved.")).toBeVisible();

    await page.goto("/billing");
    const banner = page.getByTestId("profile-banner");
    await expect(banner).toBeVisible();
    await banner.getByRole("link", { name: /Complete the hospital profile/ }).click();
    await expect(page).toHaveURL(/\/admin\/hospital$/);
  } finally {
    await page.goto("/admin/hospital");
    await page.getByLabel("Address line 1").fill(address);
    await page.getByRole("button", { name: "Save profile" }).click();
    await expect(page.getByText("Hospital profile saved.")).toBeVisible();
  }
  await page.getByLabel("Upload logo").setInputFiles(LOGO);
  await expect(page.getByText("Logo uploaded.")).toBeVisible();

  // Address and logo both present: nothing to ask for.
  await page.goto("/billing");
  await expect(page.getByRole("tab", { name: "Accounts", selected: true })).toBeVisible();
  await expect(page.getByTestId("profile-banner")).toHaveCount(0);
});
