import { expect, test } from "@playwright/test";

import { ACCOUNTS, registerAndQueue, signIn, signOut } from "./support";

/**
 * Open on a dashboard bill row: the patient's bills, that bill selected and
 * locked. Edit unlocks it; "Issue corrected bill" asks why, then issues a new
 * bill under a new number with the receipt moved onto it, and cancels the old
 * one. An issued bill is never changed in place.
 */
test("Open shows the bill locked, and Edit issues a corrected bill under a new number", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const name = await registerAndQueue(page);
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

  await signIn(page, ACCOUNTS.accountant);
  await page.goto("/billing");
  const billRow = page.getByTestId("bill-row").filter({ hasText: name });
  const number = (await billRow.locator("td").nth(1).innerText()).trim();
  await billRow.getByRole("link", { name: `Open bill ${number}` }).click();

  await expect(page).toHaveURL(/\/billing\/patients\/[0-9a-f-]+\?bill=[0-9a-f-]+$/);
  const bill = page.getByTestId("issued-bill").filter({ hasText: number });
  await expect(bill).toContainText("OPD consultation");
  // Locked: no line can be changed until Edit.
  await expect(bill.getByRole("button", { name: /^Edit OPD consultation/ })).toHaveCount(0);

  await bill.getByRole("button", { name: `Edit bill ${number}` }).click();
  await bill.getByRole("button", { name: "Add charge" }).click();
  const add = page.getByRole("dialog", { name: "Add charge" });
  await add.getByLabel("Description").fill("Dressing kit");
  await add.getByLabel("Unit price").fill("240");
  await add.getByLabel("Reason").fill("Missed at the counter");
  await add.getByRole("button", { name: "Add to bill" }).click();
  await expect(page.getByTestId("unsaved-count")).toHaveText("1 unsaved change");

  await page.getByRole("button", { name: "Issue corrected bill" }).click();
  const confirm = page.getByRole("dialog", { name: "Issue corrected bill" });
  await confirm.getByLabel("Reason for the correction").fill("Dressing missed at the counter");
  await confirm.getByRole("button", { name: "Issue corrected bill" }).click();

  await expect(page.getByText(new RegExp(`Corrected bill .* issued; ${number} cancelled`))).toBeVisible();
  const pane = page.getByTestId("visit-pane");
  await expect(pane.getByTestId("issued-bill").filter({ hasText: number })).toContainText("Cancelled");
  await expect(pane.getByTestId("issued-bill").filter({ hasNotText: number })).toContainText("Part paid");
});
