import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, admittedPatient, releaseTrackedBeds, signIn, signOut } from "./support";

/**
 * The medicine list, and the cost of an inpatient stay.
 *
 * Two paths put the list on a patient's bill, and each gets a test:
 *
 * - the admission desk adds an item with a price and records it used on a
 *   stay — and a reversal takes it back off;
 * - a doctor prescribes from the list, a nurse signs a dose given, and the
 *   dose appears in what the stay has cost.
 */
releaseTrackedBeds();

/** A name nobody else's run will have used. */
function uniqueItem(): string {
  return `E2E drip ${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`;
}

async function addItem(page: Page, name: string, price: string): Promise<void> {
  await page.goto("/medicines");
  await page.getByTestId("add-medicine").click();
  const dialog = page.getByRole("dialog", { name: "Add to the medicine list" });
  await dialog.getByLabel("Name").fill(name);
  await dialog.getByLabel("Type").click();
  await page.getByRole("option", { name: "IV fluid" }).click();
  // Choosing the type filled in the usual unit.
  await expect(dialog.getByLabel("Unit")).toHaveValue("bottle");
  await dialog.getByLabel("Dose").fill("500 ml");
  await dialog.getByLabel(/Price per bottle/).fill(price);
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(page.getByText(`${name} added to the medicine list.`)).toBeVisible({
    timeout: 30_000,
  });
}

test("the admission desk prices an item, records it on a stay, and the cost shows it", async ({
  page,
}) => {
  const { url } = await admittedPatient(page);
  await signOut(page);

  const item = uniqueItem();
  await signIn(page, ACCOUNTS.admissionDesk);
  await addItem(page, item, "47");
  const row = page.getByTestId("medicine-row").filter({ hasText: item });
  await expect(row).toContainText("₹47.00");
  await expect(row).toContainText("IV fluid");

  // On the stay: record two used.
  await page.goto(url);
  await page.getByTestId("record-item").click();
  const record = page.getByRole("dialog", { name: "Record item used" });
  await record.getByPlaceholder(/Search by name/).fill(item);
  await record.getByTestId("medicine-option").filter({ hasText: item }).click();
  await expect(record.getByTestId("picked-medicine")).toContainText("₹47.00 / bottle");
  await record.getByRole("button", { name: "One more" }).click();
  await record.getByRole("button", { name: "Add to bill" }).click();
  await expect(page.getByText(`${item} × 2 added to the bill.`)).toBeVisible({ timeout: 30_000 });

  await expect(page.getByTestId("item-used").filter({ hasText: item })).toContainText("× 2");
  const line = page.getByTestId("cost-line").filter({ hasText: item });
  await expect(line).toContainText("₹94.00", { timeout: 30_000 });

  // Recorded in error: reversed with a reason, and off the bill.
  await page
    .getByTestId("item-used")
    .filter({ hasText: item })
    .getByRole("button", { name: /Reverse/ })
    .click();
  const reverse = page.getByRole("dialog", { name: "Reverse this entry" });
  await reverse.getByLabel("Reason").fill("Recorded against the wrong patient");
  await reverse.getByRole("button", { name: "Reverse", exact: true }).click();
  await expect(page.getByTestId("item-used").filter({ hasText: item })).toContainText(
    "Reversed: Recorded against the wrong patient",
    { timeout: 30_000 },
  );
  await expect(page.getByTestId("cost-line").filter({ hasText: item })).toHaveCount(0, {
    timeout: 30_000,
  });

  // Tidy the shared demo list: switch the test item off.
  await page.goto("/medicines");
  await page.getByRole("button", { name: `Edit ${item}` }).click();
  const edit = page.getByRole("dialog", { name: "Edit item" });
  await edit.getByLabel(/In use/).uncheck();
  await edit.getByRole("button", { name: "Save" }).click();
  await expect(page.getByTestId("medicine-row").filter({ hasText: item })).toHaveCount(0, {
    timeout: 30_000,
  });
});

test("a dose prescribed from the list and given shows up in the cost of the stay", async ({
  page,
}) => {
  const { name, url } = await admittedPatient(page);

  // The doctor, still signed in, prescribes from the list.
  await page.goto(`${url}/chart`);
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Prescribe" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Prescribe" });
  await dialog.getByPlaceholder(/Search by name/).fill("Ceftriaxone");
  await dialog.getByTestId("medicine-option").filter({ hasText: "Ceftriaxone 1 g" }).click();
  // Picking filled the prescription in.
  await expect(dialog.getByLabel("Drug")).toHaveValue("Ceftriaxone 1 g");
  await expect(dialog.getByLabel("Dose", { exact: true })).toHaveValue("1 g");
  await dialog.getByRole("button", { name: "Prescribe" }).click();
  await expect(page.getByText("Ceftriaxone 1 g prescribed.")).toBeVisible({ timeout: 45_000 });
  await expect(
    page.getByTestId("drug-row").filter({ hasText: "Ceftriaxone 1 g" }).getByTestId("billing-line"),
  ).toContainText("Billed per dose given");
  await signOut(page);

  // The nurse signs one dose given…
  await signIn(page, ACCOUNTS.nurse);
  await page.goto(`${url}/chart`);
  const dose = page.getByTestId("dose-row").first();
  await expect(dose).toHaveAttribute("data-status", "DUE", { timeout: 45_000 });
  await dose.getByTestId("dose-given").click();
  await expect(page.getByTestId("dose-row").first()).toHaveAttribute("data-status", "GIVEN", {
    timeout: 45_000,
  });

  // …and it is on what the stay has cost.
  await page.goto(url);
  await expect(
    page.getByTestId("cost-line").filter({ hasText: "Ceftriaxone 1 g" }),
  ).toContainText("× 1", { timeout: 30_000 });

  // A nurse reads the list, and cannot change it.
  await page.goto("/medicines");
  await expect(page.getByTestId("medicine-row").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("add-medicine")).toHaveCount(0);
});
