import { expect, test } from "@playwright/test";

import { admittedPatient, releaseTrackedBeds } from "./support";

/**
 * The discharge summary on paper: the Print button waits for the three
 * sections the page is built around, prints in place, and the page carries
 * the hospital's own name and details — "DRAFT" until a doctor signs.
 */
releaseTrackedBeds();

test("the discharge summary prints on the hospital's letterhead once its sections are filled", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const { name, url } = await admittedPatient(page);
  const admissionId = url.split("/").pop() ?? "";

  await page.goto(`${url}/summary`);
  await expect(page.locator('[data-status="DRAFT"]')).toBeVisible({ timeout: 45_000 });

  // Nothing to print yet: the screen names what is missing.
  const print = page.getByTestId("print-summary");
  const hint = page.getByTestId("print-summary-hint");
  await expect(print).toBeDisabled();
  await expect(hint).toContainText("Diagnoses");

  await page.getByLabel("Diagnoses").fill("Communicating hydrocephalus");
  await page.getByLabel("Presenting complaint").fill("Nausea and intermittent vomiting");
  await page.getByLabel("Course in hospital").fill("Recovered well; ambulant at discharge.");
  // Printing reads what is saved, so unsaved edits hold it back.
  await expect(hint).toHaveText("Save your changes before printing.");
  await expect(print).toBeDisabled();

  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Summary saved.")).toBeVisible({ timeout: 45_000 });
  await expect(print).toBeEnabled();

  // Printed in place: the document loads in a hidden frame and prints itself.
  await print.click();
  const printed = page.frameLocator('[data-testid="print-frame"]').last().getByTestId("printed-summary");
  await expect(printed).toContainText(name, { timeout: 45_000 });
  await expect(printed).toContainText("Discharge Summary");
  await expect(printed).toContainText("Communicating hydrocephalus");
  await expect(printed).toContainText("Nausea and intermittent vomiting");
  await expect(printed).toContainText("Recovered well; ambulant at discharge.");
  await expect(printed.getByTestId("summary-draft")).toBeVisible();
  // The hospital running the system, not a fixed name.
  await expect(printed.getByTestId("letterhead")).not.toBeEmpty();
  await expect(printed.getByTestId("summary-hospital")).not.toBeEmpty();

  // Signed, the draft mark gives way to the signature.
  await page.getByLabel("NMC registration").fill("KMC-12345");
  await page.getByTestId("sign-summary").click();
  await expect(page.getByText("Summary signed and released.")).toBeVisible({ timeout: 45_000 });

  await page.goto(`/discharge-summaries/${admissionId}`);
  const signed = page.getByTestId("printed-summary");
  await expect(signed.getByTestId("summary-signature")).toContainText("KMC-12345");
  await expect(signed.getByTestId("summary-draft")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Print" })).toBeVisible();
});
