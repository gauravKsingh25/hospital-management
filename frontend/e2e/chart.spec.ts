import { expect, test, type Page } from "@playwright/test";

import {
  ACCOUNTS,
  registerAndQueue,
  releaseTrackedBeds,
  signIn,
  signOut,
  trackBed,
} from "./support";

/**
 * The medication chart, the drug round, and the discharge summary.
 *
 * The property worth protecting is a separation, and CLAUDE.md §13 step 9
 * states it: **a doctor prescribes and does not sign for a dose at the
 * bedside; a nurse gives the drug and does not prescribe it.** That is the
 * second pair of eyes, and it is most of what a medication chart is for. Two
 * tests assert it by name.
 *
 * The other is that a dose not given always carries a reason. The backend
 * refuses it without one; the screen is shaped so the friction lands exactly
 * there and nowhere else — "Given" stays a single tap.
 */

/** Admit a patient and return their name and admission URL. */
async function admittedPatient(page: Page): Promise<{ name: string; url: string }> {
  const name = await registerAndQueue(page);

  await signIn(page, ACCOUNTS.doctor);
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await row.getByRole("link", { name: /Start consultation/ }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Start consultation" }).click();

  const note = page.getByRole("textbox", { name: "Clinical note" });
  await expect(note).toBeEnabled({ timeout: 45_000 });
  await note.fill("Chest infection. For intravenous antibiotics.");
  await note.press("Control+Enter");

  await page.getByTestId("admit-patient").click();
  const dialog = page.getByRole("dialog", { name: "Admit to a ward" });
  // Any free bed in any ward. These tests are about the chart, not about
  // where the patient sleeps, and pinning them to one ward makes them fail
  // when that ward fills up — which says nothing about the code under test.
  //
  // The label, not the radio: the input is `sr-only` inside it (the accessible
  // pattern for a button-styled choice), so the label is what a person clicks
  // and what Playwright can click. Its text is exactly the bed code, which is
  // what teardown needs to give the bed back.
  const choice = dialog.getByRole("group", { name: "Choose a bed" }).locator("label").first();
  const bed = (await choice.innerText()).trim();
  await choice.click();
  await dialog.getByRole("button", { name: "Admit", exact: true }).click();
  await expect(page).toHaveURL(/\/admissions\/[0-9a-f-]{36}/, { timeout: 45_000 });

  trackBed({ url: page.url(), bed });
  return { name, url: page.url() };
}

// Six admissions a run, none of which used to end. Beds are a fixed pool, so
// that is a leak rather than clutter — see `releaseTrackedBeds`.
releaseTrackedBeds();

/** Prescribe one scheduled drug on the chart currently open. */
async function prescribe(page: Page, drug: string): Promise<void> {
  await page.getByRole("button", { name: "Prescribe" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Prescribe" });
  await dialog.getByLabel("Drug").fill(drug);
  await dialog.getByLabel("Dose", { exact: true }).fill("500 mg");
  await dialog.getByRole("button", { name: "Prescribe" }).click();
  await expect(page.getByText(`${drug} prescribed.`)).toBeVisible({ timeout: 45_000 });
}

test.describe("the medication chart", () => {
  test("a doctor prescribes and is not offered the bedside signature", async ({ page }) => {
    const { name, url } = await admittedPatient(page);

    await page.goto(`${url}/chart`);
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible({ timeout: 45_000 });

    await prescribe(page, "Amoxicillin");
    await expect(page.getByTestId("drug-row").filter({ hasText: "Amoxicillin" })).toBeVisible();

    // Dose slots are materialised in advance by the backend, so they are here
    // immediately — and the doctor cannot sign for any of them.
    await expect(page.getByTestId("dose-row").first()).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("dose-given")).toHaveCount(0);
  });

  test("a nurse signs for a dose and is not offered the prescription", async ({ page }) => {
    const { name, url } = await admittedPatient(page);
    await page.goto(`${url}/chart`);
    await prescribe(page, "Paracetamol");
    await signOut(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto(`${url}/chart`);
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible({ timeout: 45_000 });

    // The other half of the separation.
    await expect(page.getByRole("button", { name: "Prescribe" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Stop" })).toHaveCount(0);

    const dose = page.getByTestId("dose-row").first();
    await expect(dose).toHaveAttribute("data-status", "DUE");
    await dose.getByTestId("dose-given").click();

    await expect(page.getByText(/signed for/)).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("dose-row").first()).toHaveAttribute(
      "data-status",
      "GIVEN",
      { timeout: 45_000 },
    );
  });

  test("a dose not given always carries a reason", async ({ page }) => {
    // The rule the backend enforces, and the reason it does: a blank reason on
    // a missed antibiotic is the gap an incident review cannot close.
    const { url } = await admittedPatient(page);
    await page.goto(`${url}/chart`);
    await prescribe(page, "Metronidazole");
    await signOut(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto(`${url}/chart`);

    const dose = page.getByTestId("dose-row").first();
    await expect(dose).toBeVisible({ timeout: 45_000 });
    await dose.getByRole("button", { name: "No", exact: true }).click();

    const dialog = page.getByRole("dialog", { name: "Not given" });
    await expect(dialog.getByRole("button", { name: "Save" })).toBeDisabled();
    // The three ways of not giving are kept apart on purpose — refused, held,
    // ran out — so the picker has to be used rather than defaulted through.
    await dialog.getByLabel("What happened").click();
    await page.getByRole("option", { name: "Held" }).click();
    await dialog.getByLabel("Reason").fill("Patient nil by mouth for theatre");
    await dialog.getByRole("button", { name: "Save" }).click();

    await expect(page.getByText("Recorded.")).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("dose-row").first()).toHaveAttribute("data-status", "HELD", {
      timeout: 45_000,
    });
  });
});

test.describe("the drug round", () => {
  test("lists doses across the ward, naming the patient and the bed", async ({ page }) => {
    /**
     * The whole reason `DoseRead` gained identity. Checking the patient
     * against the chart before giving a drug is *the* check, and a round of
     * UUIDs does not support it.
     */
    const { name, url } = await admittedPatient(page);
    await page.goto(`${url}/chart`);
    await prescribe(page, "Ceftriaxone");
    await signOut(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/rounds");

    // Filtered by the patient rather than the drug: the ward is shared across
    // specs and drug names repeat, so "the Ceftriaxone row" is whichever
    // patient happened to be prescribed one first.
    const row = page.getByTestId("dose-row").filter({ hasText: name }).first();
    await expect(row).toBeVisible({ timeout: 45_000 });
    await expect(row).toContainText("Ceftriaxone");
    await expect(row).toContainText(/DEMO-/);
    // A bed code, whichever ward it is in — the point is that the round says
    // where to go, not which ward the test happened to use.
    await expect(row).toContainText(/-\d/);
  });

  test("outstanding is the default view", async ({ page }) => {
    // A round of already-signed doses is a list a nurse reads past to find
    // their work. The next thing to do belongs at the top.
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/rounds");

    await expect(page.getByRole("button", { name: "Outstanding" })).toBeVisible();
    for (const row of await page.getByTestId("dose-row").all()) {
      await expect(row).toHaveAttribute("data-status", "DUE");
    }
  });
});

test.describe("the discharge summary", () => {
  test("is compiled from the record, then signed and frozen", async ({ page }) => {
    /**
     * Compiled, never invented. A summary typed from memory at the end of a
     * shift is the least reliable record in the hospital — and it is the one
     * the patient takes home.
     */
    const { url } = await admittedPatient(page);
    await page.goto(`${url}/chart`);
    await prescribe(page, "Azithromycin");

    await page.goto(`${url}/summary`);
    // The draft already exists: it is compiled when the patient is admitted,
    // so it accumulates as the stay goes on rather than being written at the
    // end. Re-reading the record pulls in what has happened since.
    await expect(page.locator('[data-status="DRAFT"]')).toBeVisible({ timeout: 45_000 });
    await page.getByTestId("recompile-summary").click();
    await expect(page.getByText("Summary compiled from the record.")).toBeVisible({
      timeout: 45_000,
    });

    // Compiled, not invented — the doctor's own note is already in it.
    await expect(page.getByLabel("Course in hospital")).toContainText(
      "For intravenous antibiotics",
    );

    // Signing is refused without a diagnosis, and the screen says so rather
    // than letting the doctor press the button and be told no.
    await expect(page.getByTestId("sign-summary")).toBeDisabled();
    await expect(page.getByText(/A diagnosis is needed/)).toBeVisible();

    // The doctor supplies what the compiler could not know.
    await page.getByLabel("Diagnoses").fill("Community-acquired pneumonia");
    const condition = page.getByLabel("Condition at discharge");
    await condition.fill("Afebrile for 48 hours, chest clear.");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Summary saved.")).toBeVisible({ timeout: 45_000 });

    await page.getByLabel("NMC registration").fill("KMC-12345");
    await expect(page.getByTestId("sign-summary")).toBeEnabled();
    await page.getByTestId("sign-summary").click();
    await expect(page.getByText("Summary signed and released.")).toBeVisible({
      timeout: 45_000,
    });

    // Frozen: a correction after this is an amendment, which leaves both
    // versions standing.
    await expect(page.locator('[data-status="FINAL"]')).toBeVisible({ timeout: 45_000 });
    await expect(page.getByLabel("Condition at discharge")).toBeDisabled();
    await expect(page.getByTestId("sign-summary")).toHaveCount(0);
  });

  test("a nurse may write the summary but not sign it", async ({ page }) => {
    // The signature carries a registration number. `summary:sign` is checked
    // against the role as well as the permission, and a nurse holds neither.
    const { url } = await admittedPatient(page);
    await page.goto(`${url}/summary`);
    await expect(page.locator('[data-status="DRAFT"]')).toBeVisible({ timeout: 45_000 });
    await signOut(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto(`${url}/summary`);

    await expect(page.getByLabel("Condition at discharge")).toBeEnabled({ timeout: 45_000 });
    await expect(page.getByTestId("sign-summary")).toHaveCount(0);
  });
});
