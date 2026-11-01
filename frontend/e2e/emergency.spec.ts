import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, registerAndReadSlip, showDoctor, signIn, uniquePatient } from "./support";

/**
 * The emergency door (UX-01).
 *
 * An unconscious patient with no name and no mobile: F9 from anywhere, one
 * gender tap, a doctor, and they are first in that doctor's line as
 * `Unknown Male EM-…`. Reception sees them under "needs identification" and
 * names them later — as a new person, or by linking the record they already
 * have.
 */

/** Signed in already, as reception. */
async function registerUnknownMale(page: Page): Promise<string> {
  await page.goto("/reception");
  await expect(page.getByTestId("emergency-link")).toBeVisible({ timeout: 30_000 });

  // F9 works from any screen, not only from a button somebody has to find.
  await page.keyboard.press("F9");
  await expect(page).toHaveURL(/\/reception\/emergency$/);

  await page.getByTestId("emergency-gender-MALE").click();
  await page.getByLabel(/Approximate age/).fill("40");
  await page.getByLabel("What happened").fill("RTA, unconscious");
  const doctor = page.getByLabel("Doctor", { exact: true });
  // The device may remember the last emergency doctor; pick explicitly.
  await doctor.click();
  await page.getByRole("option", { name: /Vikram/ }).click();
  await page.getByTestId("emergency-submit").click();

  await expect(page.getByTestId("emergency-issued")).toBeVisible({ timeout: 45_000 });
  const slip = page.getByTestId("token-slip");
  await expect(slip).toContainText(/Unknown Male EM-\d{4}/);
  const name = (await slip.textContent())?.match(/Unknown Male EM-\d{4}/)?.[0];
  expect(name, "the slip names the patient by their emergency tag").toBeTruthy();
  return name as string;
}

async function openIdentify(page: Page, name: string): Promise<void> {
  await page.goto("/reception");
  const strip = page.getByTestId("needs-identification");
  await expect(strip).toBeVisible({ timeout: 30_000 });
  const row = strip.getByRole("listitem").filter({ hasText: name });
  await expect(row).toBeVisible();
  await row.getByTestId("identify-button").click();
  await expect(page.getByRole("dialog")).toBeVisible();
}

test.describe("emergency registration", () => {
  test("an unknown patient is first in line, then named as a new person", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    const name = await registerUnknownMale(page);

    await page.goto("/reception");
    await showDoctor(page, "Dr Vikram Rao");
    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 30_000 });
    await expect(row.getByTestId("emergency-tag")).toBeVisible();
    await expect(row.getByTestId("unidentified-tag")).toBeVisible();

    const real = uniquePatient();
    await openIdentify(page, name);
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Full name", { exact: true }).fill(real.name);
    await dialog.getByLabel("Mobile number", { exact: true }).fill(real.phone);
    await dialog.getByRole("button", { name: "Male", exact: true }).click();
    await dialog.getByTestId("identify-save").click();
    await expect(dialog).toBeHidden({ timeout: 30_000 });

    // Off the list, and the board calls the patient by their name.
    await expect(
      page.getByTestId("unidentified-name").filter({ hasText: name }),
    ).toHaveCount(0);
    const named = page.getByTestId("queue-row").filter({ hasText: real.name });
    await expect(named).toBeVisible({ timeout: 30_000 });
    await expect(named.getByTestId("unidentified-tag")).toHaveCount(0);
    await expect(named.getByTestId("emergency-tag")).toBeVisible();
  });

  test("an unknown patient is linked to the record they already have", async ({ page }) => {
    // Signs in as reception.
    const existing = await registerAndReadSlip(page);
    const name = await registerUnknownMale(page);

    await openIdentify(page, name);
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel(/Find their record/).fill(existing.uhid);
    const candidate = dialog
      .getByTestId("identify-candidates")
      .getByRole("listitem")
      .filter({ hasText: existing.name });
    await expect(candidate).toBeVisible({ timeout: 30_000 });
    await candidate.getByRole("button", { name: "This is the patient" }).click();
    await expect(dialog.getByTestId("identify-confirm-link")).toContainText(existing.name);
    await dialog.getByTestId("identify-link-confirm").click();
    await expect(dialog).toBeHidden({ timeout: 30_000 });

    await expect(
      page.getByTestId("unidentified-name").filter({ hasText: name }),
    ).toHaveCount(0);
    await showDoctor(page, "Dr Vikram Rao");
    // The emergency visit now carries the existing patient's name.
    const rows = page.getByTestId("queue-row").filter({ hasText: existing.name });
    await expect(rows.filter({ has: page.getByTestId("emergency-tag") })).toBeVisible({
      timeout: 30_000,
    });
  });

  test("the record of an unknown patient says so, and nothing is messaged", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    const name = await registerUnknownMale(page);

    await page.goto("/reception");
    const strip = page.getByTestId("needs-identification");
    await strip.getByTestId("unidentified-name").filter({ hasText: name }).click();
    await expect(page.getByTestId("unidentified-banner")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("unidentified-banner")).toContainText(
      "No messages are sent to this patient",
    );

    // Named from the record too — which also leaves the demo's Reception
    // strip as it found it.
    const real = uniquePatient();
    await page.getByTestId("unidentified-banner").getByRole("button", { name: "Identify" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Full name", { exact: true }).fill(real.name);
    await dialog.getByLabel("Mobile number", { exact: true }).fill(real.phone);
    await dialog.getByRole("button", { name: "Male", exact: true }).click();
    await dialog.getByTestId("identify-save").click();
    await expect(page.getByTestId("unidentified-banner")).toBeHidden({ timeout: 30_000 });
    await expect(page.getByRole("heading", { name: real.name })).toBeVisible();
  });
});
