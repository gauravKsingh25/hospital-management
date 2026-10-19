import { expect, test, type Page } from "@playwright/test";

import {
  ACCOUNTS,
  deskAdmits,
  dischargeStay,
  openPatientBills,
  releaseTrackedBeds,
  sentForAdmission,
  signIn,
  signOut,
} from "./support";

/**
 * The accountant's IPD bill editor.
 *
 * Changes are staged in the browser and marked unsaved; the total shown is
 * the last *saved* one until Save, when the server applies everything at
 * once and recalculates. After discharge the accountant generates the final
 * bill, and from then on it is frozen.
 */
releaseTrackedBeds();

async function admittedForAccounts(page: Page): Promise<{ name: string; url: string }> {
  const name = await sentForAdmission(page);
  const { url } = await deskAdmits(page, name);
  await signOut(page);
  await signIn(page, ACCOUNTS.accountant);
  await openPatientBills(page, name);
  return { name, url };
}

async function addFreeTextCharge(page: Page, description: string, price: string): Promise<void> {
  await page.getByRole("button", { name: "Add charge" }).click();
  const dialog = page.getByRole("dialog", { name: "Add charge" });
  await dialog.getByLabel("Description").fill(description);
  await dialog.getByLabel("Unit price").fill(price);
  await dialog.getByLabel("Reason").fill("Used on the ward");
  await dialog.getByRole("button", { name: "Add to bill" }).click();
  await expect(dialog).toBeHidden();
}

test("the accountant edits an IPD bill and the total changes only on Save", async ({ page }) => {
  test.setTimeout(180_000);
  await admittedForAccounts(page);
  const total = page.getByTestId("ipd-total");
  const before = await total.innerText();

  await addFreeTextCharge(page, "Dressing kit", "240");
  await expect(page.getByTestId("unsaved-count")).toHaveText("1 unsaved change");
  // Staged, not applied: the saved total has not moved.
  await expect(total).toHaveText(before);

  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Bill saved.")).toBeVisible();
  await expect(total).not.toHaveText(before);
  const afterAdd = await total.innerText();

  const line = page.getByTestId("ipd-line").filter({ hasText: "Dressing kit" });
  await line.getByRole("button", { name: /Edit/ }).click();
  const edit = page.getByRole("dialog", { name: "Edit charge" });
  await edit.getByLabel("Quantity").fill("2");
  await edit.getByLabel("Reason").fill("Two kits used");
  await edit.getByRole("button", { name: "Stage change" }).click();
  await expect(total).toHaveText(afterAdd);

  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Bill saved.")).toBeVisible();
  await expect(total).not.toHaveText(afterAdd);
  await expect(line).toContainText("2");
});

test("after discharge the accountant generates the final bill and it can no longer be edited", async ({
  page,
}) => {
  test.setTimeout(240_000);
  const { name, url } = await admittedForAccounts(page);
  await addFreeTextCharge(page, "Oxygen cylinder", "1000");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Bill saved.")).toBeVisible();
  // Not offered while the patient is in a bed.
  await expect(page.getByRole("button", { name: "Generate final bill" })).toHaveCount(0);

  await dischargeStay(page, url);
  await signIn(page, ACCOUNTS.accountant);
  await openPatientBills(page, name);

  await page.getByRole("button", { name: "Generate final bill" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Generate final bill" }).click();
  await expect(page.getByText(/Final bill .* generated/)).toBeVisible();

  const pane = page.getByTestId("visit-pane");
  await expect(pane.getByRole("button", { name: "Add charge" })).toHaveCount(0);
  await expect(pane.getByRole("link", { name: /Print bill/ })).toBeVisible();
  await expect(page.getByTestId("visit-item").first()).toContainText(/Final|Paid/);
});
