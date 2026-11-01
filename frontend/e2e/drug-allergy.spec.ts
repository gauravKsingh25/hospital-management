import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, admittedPatient, releaseTrackedBeds, signIn, signOut } from "./support";

/**
 * SAFE-01: a recorded drug allergy stands between the patient and the drug.
 *
 * The audit's case was a CRITICAL "Aspirin — anaphylaxis" alert and an
 * Aspirin order that went through with no warning, onto a drug round that
 * never showed the allergy. These walk the two places it now stops: the
 * doctor prescribing, and the nurse at the bedside when the allergy was
 * reported after the drug was charted.
 */

releaseTrackedBeds();

const ALLERGY = "Aspirin — anaphylaxis";

/** Record a CRITICAL drug allergy on the admitted patient, as the doctor. */
async function recordAllergy(page: Page, url: string): Promise<void> {
  const admissionId = url.split("/admissions/")[1]?.split(/[/?#]/)[0];
  const stay = await page.request.get(`/bff/ipd/admissions/${admissionId}`);
  expect(stay.ok(), await stay.text()).toBeTruthy();
  const patientId = (await stay.json()).patient_id as string;

  const alert = await page.request.post(`/bff/patients/${patientId}/alerts`, {
    data: { alert_type: "DRUG_ALLERGY", label: ALLERGY, severity: "CRITICAL" },
  });
  expect(alert.ok(), await alert.text()).toBeTruthy();
}

async function openPrescribe(page: Page, drug: string) {
  await page.getByRole("button", { name: "Prescribe" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Prescribe" });
  await dialog.getByLabel("Drug").fill(drug);
  await dialog.getByLabel("Dose", { exact: true }).fill("300 mg");
  return dialog;
}

test.describe("drug allergies", () => {
  test("prescribing the drug a patient is allergic to needs a reason", async ({ page }) => {
    const { name, url } = await admittedPatient(page);
    await recordAllergy(page, url);
    await page.goto(`${url}/chart`);
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible({ timeout: 45_000 });

    const dialog = await openPrescribe(page, "Aspirin");
    await dialog.getByTestId("prescribe-submit").click();

    // Refused, in the dialog, naming the allergy.
    const conflict = dialog.getByTestId("allergy-conflict");
    await expect(conflict).toBeVisible({ timeout: 45_000 });
    await expect(conflict).toContainText(ALLERGY);
    const anyway = dialog.getByTestId("prescribe-submit");
    await expect(anyway).toHaveText("Prescribe anyway");
    await expect(anyway).toBeDisabled();

    await dialog.getByLabel("Reason for prescribing anyway").fill("Intolerance only; cardiology accepts.");
    await anyway.click();
    await expect(page.getByText("Aspirin prescribed.")).toBeVisible({ timeout: 45_000 });

    // The clash and the reason stay on the chart.
    const drug = page.getByTestId("drug-row").filter({ hasText: "Aspirin" });
    await expect(drug.getByTestId("order-allergy")).toContainText("cardiology accepts");
  });

  test("an allergy reported after charting stops the dose at the bedside", async ({ page }) => {
    const { name, url } = await admittedPatient(page);
    await page.goto(`${url}/chart`);
    const dialog = await openPrescribe(page, "Aspirin");
    await dialog.getByTestId("prescribe-submit").click();
    await expect(page.getByText("Aspirin prescribed.")).toBeVisible({ timeout: 45_000 });

    // The family mentions it the next morning.
    await recordAllergy(page, url);
    await signOut(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/rounds");
    const row = page.getByTestId("dose-row").filter({ hasText: name }).first();
    await expect(row).toBeVisible({ timeout: 45_000 });
    await expect(row.getByTestId("allergy-clash")).toContainText("Aspirin");
    await expect(row.getByTestId("dose-allergies")).toContainText(ALLERGY);
    await expect(row.getByTestId("allergy-hold")).toBeVisible();
    await expect(row.getByTestId("dose-given")).toBeDisabled();
  });
});
