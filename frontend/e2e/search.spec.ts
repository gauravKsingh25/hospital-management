import { expect, test } from "@playwright/test";

import { ACCOUNTS, registerAndQueue, signIn } from "./support";

/**
 * The universal search box, and the patient index (CLAUDE.md §7b).
 *
 * §7b asks for "one box [that] resolves UHID, name, mobile, token, doctor, or
 * appointment number". Until the `/search` endpoint existed the component
 * could only do the first three, because the rest live in other modules and
 * merging them client-side would have re-ranked an exact match on a printed
 * number below a near miss on a name.
 *
 * The token case is the one worth watching: it is the only identifier a
 * receptionist holds that is neither typed by the patient nor printed on a
 * card, and it was the one that did not work.
 */
test.describe("the universal search box", () => {
  test("F3 opens it from anywhere", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/reception");

    await page.keyboard.press("F3");
    await expect(page.getByRole("dialog")).toBeVisible();
  });

  test("a name finds the patient", async ({ page }) => {
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.reception);
    await page.keyboard.press("F3");
    await page.getByRole("combobox").fill(name.split(" ")[1]);

    const hit = page.getByRole("option").filter({ hasText: name });
    await expect(hit).toBeVisible({ timeout: 45_000 });
    await expect(hit).toContainText("Patient");
  });

  test("a token number finds today's patient and opens their chart", async ({ page }) => {
    /**
     * The identifier §7b named that no endpoint could answer before. A
     * receptionist holding a token slip has a small integer and nothing else,
     * and it has to resolve to a person *and* to the chart they are waiting
     * for — which only a server-side composition can do.
     */
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/queue");

    const firstRow = page.getByTestId("queue-row").first();
    await expect(firstRow).toBeVisible({ timeout: 45_000 });
    const token = (await firstRow.locator("td").first().innerText()).trim();
    const patientName = (await firstRow.locator("td").nth(1).innerText()).split("\n")[0].trim();

    await page.keyboard.press("F3");
    await page.getByRole("combobox").fill(token);

    const hit = page.getByRole("option").filter({ hasText: patientName }).first();
    await expect(hit).toBeVisible({ timeout: 45_000 });
    await expect(hit).toContainText("Token");

    await hit.click();
    await expect(page).toHaveURL(/\/consultation\//, { timeout: 45_000 });
  });

  test("a doctor's name finds the doctor", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await page.keyboard.press("F3");
    await page.getByRole("combobox").fill("Vikram");

    const hit = page.getByRole("option").filter({ hasText: "Vikram" }).first();
    await expect(hit).toBeVisible({ timeout: 45_000 });
    await expect(hit).toContainText("Doctor");
  });

  test("a single letter waits rather than returning noise", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await page.keyboard.press("F3");
    await page.getByRole("combobox").fill("a");

    // The hint inside the results list, not a result. Scoped to the listbox
    // because the dialog's own description says the same thing — matching both
    // would be a strict-mode violation, and matching either would be luck.
    await expect(
      page.getByRole("listbox").getByText(/Type at least/),
    ).toBeVisible();
    await expect(page.getByRole("option")).toHaveCount(0);
  });
});

test.describe("the patient index", () => {
  test("lists patients without anyone having to search first", async ({ page }) => {
    // The gap the index fills: search answers "where is this person", and
    // cannot answer "who came in yesterday afternoon".
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/patients");

    await expect(page.getByRole("heading", { name: "Patients", level: 1 })).toBeVisible();
    await expect(page.getByTestId("patient-row").first()).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("patient-row").first()).toContainText(/DEMO-/);
  });

  test("a row opens the record", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/patients");

    const row = page.getByTestId("patient-row").first();
    await expect(row).toBeVisible({ timeout: 45_000 });
    await row.getByRole("link", { name: "Open" }).click();

    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });
  });

  test("searching narrows the list and resets to the first page", async ({ page }) => {
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.reception);
    await page.goto("/patients");
    await page.getByLabel("Search").fill(name);

    const row = page.getByTestId("patient-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 45_000 });
  });
});
