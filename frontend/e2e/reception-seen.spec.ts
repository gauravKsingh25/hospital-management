import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, chooseGender, signIn, uniquePatient } from "./support";

/**
 * Reception's "Seen" button (CLAUDE.md §7b).
 *
 * The click strikes the name through at once and holds the request behind an
 * Undo for a few seconds, then records the patient as seen on the server —
 * where the row drops into the doctor's "Seen today" list, still struck
 * through, and stays there across a reload.
 */
async function queueWithRao(page: Page): Promise<string> {
  const patient = uniquePatient();
  await page.goto("/reception/register");
  await page.getByLabel("Full name", { exact: true }).fill(patient.name);
  await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
  await page.getByLabel("Age", { exact: true }).fill("38");
  await chooseGender(page, "Female");
  await page.getByLabel("Doctor", { exact: true }).click();
  await page.getByRole("option", { name: /Vikram/ }).click();
  await page.getByRole("button", { name: /Register and send to a doctor/ }).click();
  await expect(page.getByText("Token issued")).toBeVisible({ timeout: 45_000 });
  return patient.name;
}

test.describe("marking a patient as seen", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
  });

  test("the name is struck through, and Undo takes it back", async ({ page }) => {
    const name = await queueWithRao(page);
    await page.goto("/reception");

    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 30_000 });

    await row.getByTestId("mark-seen").click();
    await expect(row.getByTestId("patient-name")).toHaveCSS("text-decoration-line", "line-through");

    await row.getByRole("button", { name: /^Undo marking/ }).click();
    await expect(row.getByTestId("patient-name")).not.toHaveCSS(
      "text-decoration-line",
      "line-through",
    );
    await expect(row.getByTestId("mark-seen")).toBeVisible();

    // Still in the line after the undo window would have closed.
    await page.waitForTimeout(6_000);
    await page.reload();
    await expect(page.getByTestId("queue-row").filter({ hasText: name })).toBeVisible({
      timeout: 30_000,
    });
  });

  test("once the undo window passes, the patient is recorded as seen", async ({ page }) => {
    const name = await queueWithRao(page);
    await page.goto("/reception");

    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 30_000 });
    await row.getByTestId("mark-seen").click();

    // Out of the line and into "Seen today", struck through.
    const column = page.getByTestId("doctor-queue").filter({ hasText: "Dr Vikram Rao" });
    const seenRow = column.getByTestId("seen-row").filter({ hasText: name });
    await expect(seenRow).toBeVisible({ timeout: 20_000 });
    await expect(seenRow.getByText(name)).toHaveCSS("text-decoration-line", "line-through");
    await expect(column.getByTestId("queue-row").filter({ hasText: name })).toHaveCount(0);

    // Stored, not just drawn.
    await page.reload();
    await expect(
      page
        .getByTestId("doctor-queue")
        .filter({ hasText: "Dr Vikram Rao" })
        .getByTestId("seen-row")
        .filter({ hasText: name }),
    ).toBeVisible({ timeout: 30_000 });

    // And gone from the nurse's live queue.
    await page.goto("/queue");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByTestId("queue-row").filter({ hasText: name })).toHaveCount(0);
  });
});
