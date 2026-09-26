import { expect, test } from "@playwright/test";

import { ACCOUNTS, chooseGender, signIn, uniquePatient } from "./support";

/**
 * Reception's per-doctor queue board (CLAUDE.md §7b).
 *
 * Reception balances the clinics, so the board is one column per doctor with
 * the waiting count in the header, busiest first, and a Move action on each
 * waiting row. The registration form's doctor picker shows the same counts,
 * so the choice is made where the choice is made.
 *
 * The move itself needs two doctors. The demo seed creates one, so that step
 * skips — visibly, not silently — on a fresh seed and runs wherever a second
 * doctor profile exists.
 */
test.describe("reception's queue board", () => {
  test("shows one column per doctor, with counts and a move action", async ({ page }) => {
    const patient = uniquePatient();

    await signIn(page, ACCOUNTS.reception);
    await page.goto("/reception/register");
    await page.getByLabel("Full name", { exact: true }).fill(patient.name);
    await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
    await page.getByLabel("Age", { exact: true }).fill("36");
    await chooseGender(page, "Female");

    // The picker carries the load — "n waiting" next to every name.
    await page.getByLabel("Doctor", { exact: true }).click();
    const rao = page.getByRole("option", { name: /Vikram/ });
    await expect(rao).toContainText(/waiting/);
    await rao.click();
    // And names the choice, rather than echoing the id behind it.
    await expect(page.getByLabel("Doctor", { exact: true })).toContainText(/Vikram Rao/);
    await page.getByRole("button", { name: /Register and send to a doctor/ }).click();
    await expect(page.getByText("Token issued")).toBeVisible({ timeout: 45_000 });

    await page.goto("/reception");
    await expect(page.getByTestId("doctor-queues")).toBeVisible({ timeout: 30_000 });

    const raoColumn = page.getByTestId("doctor-queue").filter({ hasText: "Dr Vikram Rao" });
    await expect(raoColumn).toHaveCount(1);
    await expect(raoColumn).toContainText("Waiting");
    await expect(raoColumn).toContainText("With the doctor:");

    const row = raoColumn.getByTestId("queue-row").filter({ hasText: patient.name });
    await expect(row).toBeVisible();
    await expect(row.getByRole("button", { name: /^Move / })).toBeVisible();
  });

  test("a waiting patient can be moved to another doctor's queue", async ({ page }) => {
    const patient = uniquePatient();

    await signIn(page, ACCOUNTS.reception);
    await page.goto("/reception");
    await expect(page.getByTestId("doctor-queues")).toBeVisible({ timeout: 30_000 });

    const columns = page.getByTestId("doctor-queue");
    const doctorCount = await columns.count();
    test.skip(doctorCount < 2, "the move needs a second doctor profile; the demo seed has one");

    await page.goto("/reception/register");
    await page.getByLabel("Full name", { exact: true }).fill(patient.name);
    await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
    await page.getByLabel("Age", { exact: true }).fill("52");
    await chooseGender(page, "Male");
    await page.getByLabel("Doctor", { exact: true }).click();
    await page.getByRole("option", { name: /Vikram/ }).click();
    await page.getByRole("button", { name: /Register and send to a doctor/ }).click();
    await expect(page.getByText("Token issued")).toBeVisible({ timeout: 45_000 });

    await page.goto("/reception");
    const row = page.getByTestId("queue-row").filter({ hasText: patient.name });
    await expect(row).toBeVisible({ timeout: 30_000 });
    await row.getByRole("button", { name: /^Move / }).click();

    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("Move to another doctor");
    await dialog.getByLabel("Doctor", { exact: true }).click();
    // Any doctor who is not Dr Rao; the destination list never offers the
    // queue the patient is already in.
    const options = page.getByRole("option");
    await expect(options.filter({ hasText: /Vikram Rao/ })).toHaveCount(0);
    const destination = options.first();
    const destinationName = (await destination.textContent())?.split(" · ")[0] ?? "";
    await destination.click();
    await dialog.getByRole("button", { name: "Move", exact: true }).click();

    // The toast names the new token, because somebody has to tell the patient.
    await expect(
      page.getByText(new RegExp(`moved to ${destinationName} — new token \\d+`)),
    ).toBeVisible({
      timeout: 15_000,
    });

    const target = columns.filter({ hasText: destinationName });
    await expect(target.getByTestId("queue-row").filter({ hasText: patient.name })).toBeVisible({
      timeout: 15_000,
    });
    const source = columns.filter({ hasText: "Dr Vikram Rao" });
    await expect(source.getByTestId("queue-row").filter({ hasText: patient.name })).toHaveCount(0);
  });
});
