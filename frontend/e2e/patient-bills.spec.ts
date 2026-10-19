import { expect, test } from "@playwright/test";

import {
  ACCOUNTS,
  deskAdmits,
  openPatientBills,
  releaseTrackedBeds,
  sentForAdmission,
  signIn,
  signOut,
} from "./support";

/**
 * A patient's bills, as the accountant reads them: every bill-bearing visit
 * newest first, the newest one open, and the whole journey's cost itemised.
 *
 * A patient admitted from a still-open OPD visit is the case worth driving:
 * it shows two rows for one visit — the consultation as its own OPD bill, and
 * the stay.
 */
releaseTrackedBeds();

test("a patient's bills are listed newest first and the newest is open", async ({ page }) => {
  test.setTimeout(180_000);
  const name = await sentForAdmission(page);
  await deskAdmits(page, name);
  await signOut(page);

  await signIn(page, ACCOUNTS.accountant);
  await openPatientBills(page, name);

  const visits = page.getByTestId("visit-item");
  await expect(visits).toHaveCount(2);
  await expect(visits.nth(0)).toContainText("IPD");
  await expect(visits.nth(0)).toHaveAttribute("aria-current", "true");
  await expect(visits.nth(1)).toContainText("OPD");
  await expect(visits.nth(1)).toContainText("₹500.00");

  // The open pane is the stay.
  await expect(page.getByTestId("visit-pane")).toContainText(/IPD-\d{4}-\d+/);

  // The older visit opens its own bill: the consultation, frozen.
  await visits.nth(1).click();
  const pane = page.getByTestId("visit-pane");
  await expect(pane.getByText("OPD consultation")).toBeVisible();
  await expect(pane.getByRole("button", { name: "Add charge" })).toHaveCount(0);
});

test("the cost journey lists what was given, newest first", async ({ page }) => {
  test.setTimeout(180_000);
  const name = await sentForAdmission(page);
  await deskAdmits(page, name);
  await signOut(page);

  await signIn(page, ACCOUNTS.accountant);
  await openPatientBills(page, name);
  await page.getByRole("tab", { name: "Cost journey" }).click();

  const lines = page.getByTestId("journey-line");
  await expect(lines.first()).toBeVisible();
  await expect(lines.last()).toContainText("OPD consultation");
  await expect(page.getByTestId("journey-totals")).toContainText("Consultation");

  await page.getByLabel("Category", { exact: true }).selectOption("CONSULTATION");
  await expect(lines).toHaveCount(1);
  await expect(lines.first()).toContainText("OPD consultation");
});

test("the cashier cannot open the accountant's patient bill page", async ({ page }) => {
  await signIn(page, ACCOUNTS.cashier);
  await page.goto("/billing/patients/00000000-0000-0000-0000-000000000000");

  await expect(page.getByText("You do not have permission to open this.")).toBeVisible();
});
