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
  await expect(pane.getByRole("button", { name: /Print bill/ })).toBeVisible();
  await expect(pane.getByRole("button", { name: /Edit bill/ })).toBeVisible();
  await expect(page.getByTestId("visit-item").first()).toContainText(/Final|Paid/);

  // A wrong final bill is corrected in the product: cancel (with a reason),
  // and the stay's bill is editable again.
  await pane.getByRole("button", { name: "Cancel final bill" }).click();
  const cancel = page.getByRole("dialog", { name: "Cancel final bill" });
  await cancel.getByLabel("Reason for cancelling").fill("Oxygen charged twice");
  await cancel.getByRole("button", { name: "Cancel final bill" }).click();
  await expect(page.getByText(/Final bill .* cancelled/)).toBeVisible();
  await expect(pane.getByRole("button", { name: "Add charge" })).toBeVisible();
});

test("a background reload cannot slip a stale save through, and refused changes are kept", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const loaded = page.waitForResponse((response) => /\/ipd-bill$/.test(response.url()));
  await admittedForAccounts(page);
  const encounterId = (await loaded).url().match(/accounts\/([0-9a-f-]+)\/ipd-bill$/)?.[1];
  expect(encounterId).toBeTruthy();

  await addFreeTextCharge(page, "Dressing kit", "240");
  await expect(page.getByTestId("unsaved-count")).toHaveText("1 unsaved change");

  // Another accountant saves first.
  const bill = `/bff/billing/accounts/${encounterId}/ipd-bill`;
  const { version } = (await (await page.request.get(bill)).json()) as { version: string };
  const other = await page.request.post(`${bill}/changes`, {
    data: {
      expected_version: version,
      ops: [
        {
          op: "add",
          description: "Oxygen cylinder",
          category: "OTHER",
          unit_price: "1000.00",
          reason: "Night shift",
        },
      ],
    },
  });
  expect(other.ok()).toBeTruthy();

  // This tab reloads the bill in the background, as it does on focus.
  await page.waitForTimeout(11_000);
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
  await expect(page.getByTestId("ipd-line").filter({ hasText: "Oxygen cylinder" })).toBeVisible();

  // The staged change was made against the old bill: refused, and kept.
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText(/Someone else changed this bill/)).toBeVisible();
  await expect(page.getByTestId("unsaved-count")).toHaveText("1 unsaved change");

  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Bill saved.")).toBeVisible();
  await expect(page.getByTestId("ipd-line").filter({ hasText: "Dressing kit" })).toBeVisible();
});
