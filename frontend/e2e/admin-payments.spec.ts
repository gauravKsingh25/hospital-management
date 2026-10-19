import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, registerAndQueue, signIn, signOut } from "./support";

/**
 * Administration → Payments: the UPI ID every reception QR pays.
 *
 * The demo tenant is shared by every spec, so the ID is put back after the
 * test whatever happened in it — in `afterEach`, which still runs when the
 * test fails or times out. The seeded value is no bank's handle on purpose.
 */
const SEEDED = "demo.hospital@example";
const CHANGED = "demo.counter@example";

async function setUpiId(page: Page, value: string): Promise<void> {
  await page.goto("/admin/payments");
  await page.getByLabel("UPI ID", { exact: true }).fill(value);
  await page.getByRole("button", { name: "Save", exact: true }).click();
}

test.afterEach(async ({ page }) => {
  await page.context().clearCookies();
  await signIn(page, ACCOUNTS.admin);
  await page.goto("/admin/payments");
  const field = page.getByLabel("UPI ID", { exact: true });
  if ((await field.inputValue()) !== SEEDED) {
    await setUpiId(page, SEEDED);
    await expect(page.getByText("UPI ID saved.")).toBeVisible();
  }
});

test("an administrator sets the UPI ID and reception's QR pays it", async ({ page }) => {
  // Registering a patient through the form is most of the minute.
  test.setTimeout(120_000);
  await signIn(page, ACCOUNTS.admin);

  // Reachable from the hub, like every other area.
  await page.goto("/admin");
  await page.getByRole("heading", { name: "Payments", exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/payments$/);
  await expect(page.getByLabel("UPI ID", { exact: true })).toHaveValue(SEEDED);

  // A typo is refused at the form, not discovered by a patient.
  await setUpiId(page, "demo hospital");
  await expect(page.getByText(/looks like name@bank/)).toBeVisible();

  await setUpiId(page, CHANGED);
  await expect(page.getByText("UPI ID saved.")).toBeVisible();
  // The test QR, so the admin can scan it before any patient does.
  await expect(page.getByRole("img", { name: new RegExp(CHANGED) })).toBeVisible();
  await signOut(page);

  const name = await registerAndQueue(page);
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception");
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.getByTestId("get-payment").click();
  await expect(page.getByRole("dialog").getByText(CHANGED)).toBeVisible();
});
