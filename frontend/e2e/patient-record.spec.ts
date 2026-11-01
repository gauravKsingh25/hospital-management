import { expect, test } from "@playwright/test";

import { ACCOUNTS, openPatient, registerAndQueue, signIn, signOut } from "./support";

/**
 * The patient record and the directory — one place for everything the
 * hospital knows about a person.
 *
 * What these protect is the record answering its two questions: "how often
 * has this person been here?" and "what were they treated for?" — and the
 * second one only for the roles allowed to read it.
 */

test.describe("the patient record", () => {
  test("a diagnosis recorded on the visit becomes the patient's history", async ({ page }) => {
    /**
     * With the consultation screen gone, the visit card is where a condition
     * gets onto the record. The problem list is built from those, so a
     * diagnosis added here must show up under "Treated for" straight away.
     */
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.doctor);
    await openPatient(page, name);

    await expect(page.getByTestId("stat-visits")).toContainText("1");
    const visit = page.getByTestId("visit-card").first();
    await expect(visit).toContainText("Visit 1");
    await expect(page.getByTestId("problem-list")).toContainText("No diagnoses recorded yet.");

    await visit.getByTestId("add-diagnosis").click();
    const dialog = page.getByRole("dialog", { name: "Add diagnosis" });
    await dialog.getByLabel("Diagnosis", { exact: true }).fill("Type 2 diabetes mellitus");
    await dialog.getByLabel(/ICD-10/).fill("e11");
    await dialog.getByRole("button", { name: "Save" }).click();

    const problem = page.getByTestId("problem-row").filter({ hasText: "Type 2 diabetes mellitus" });
    await expect(problem).toBeVisible({ timeout: 45_000 });
    await expect(problem).toContainText("E11");
    await expect(visit.getByTestId("visit-diagnosis")).toContainText("Type 2 diabetes mellitus");

    // And on the full history, numbered.
    await page.getByTestId("record-tab-visits").click();
    await expect(page.getByTestId("record-panel-visits")).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("visit-card").first()).toContainText("Visit 1");
  });

  test("reception sees the visits but not what the patient was treated for", async ({ page }) => {
    /**
     * Reception holds `encounter:read` and not `diagnosis:read`. The record
     * says so in words — never an empty list, which would tell a receptionist
     * the patient has no history.
     */
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.reception);
    await openPatient(page, name);

    await expect(page.getByTestId("visit-card").first()).toContainText("Visit 1");
    await expect(page.getByTestId("problem-list")).toContainText("Not shown to your role.");
    await expect(page.getByTestId("add-diagnosis")).toHaveCount(0);
    // Lab and imaging is a clinical tab reception does not hold.
    await expect(page.getByTestId("record-tab-reports")).toHaveCount(0);
  });

  test("every section is reachable by its own address", async ({ page }) => {
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.doctor);
    await openPatient(page, name);
    const base = page.url().split("?")[0];

    for (const tab of ["visits", "admissions", "reports", "billing", "profile"]) {
      await page.goto(`${base}?tab=${tab}`);
      await expect(page.getByTestId(`record-panel-${tab}`)).toBeVisible({ timeout: 45_000 });
      await expect(page.getByTestId(`record-tab-${tab}`)).toHaveAttribute("aria-current", "page");
    }
    await expect(page.getByTestId("profile-panel")).toContainText("Contact");
    await expect(page.getByTestId("record-name")).toHaveText(name);
  });
});

test.describe("the patient directory", () => {
  test("shows each patient's visit count and last visit", async ({ page }) => {
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.reception);
    await page.goto("/patients");
    await page.getByLabel("Search").fill(name);

    const row = page.getByTestId("patient-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 45_000 });
    await expect(row.getByTestId("visit-count")).toHaveText("1", { timeout: 45_000 });
    await expect(row).toContainText("Registered");
  });

  test("filters narrow the list and say so when nothing matches", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/patients");
    await expect(page.getByTestId("patient-row").first()).toBeVisible({ timeout: 45_000 });

    await page.getByLabel("Gender").selectOption("MALE");
    await expect(page.getByTestId("patient-row").first()).toContainText("· M", { timeout: 45_000 });
    for (const text of await page.getByTestId("patient-row").allInnerTexts()) {
      expect(text).toContain("· M");
    }

    // A registration window in the future has nobody in it.
    await page.getByLabel("Registered from").fill("2099-01-01");
    await expect(page.getByText("No patients match these filters.")).toBeVisible({
      timeout: 45_000,
    });

    await page.getByRole("button", { name: "Clear filters" }).click();
    await expect(page.getByTestId("patient-row").first()).toBeVisible({ timeout: 45_000 });
    await signOut(page);
  });
});
