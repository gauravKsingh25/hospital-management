import { expect, test } from "@playwright/test";

import { ACCOUNTS, chooseGender, openLine, showDoctor, signIn, uniquePatient } from "./support";

/**
 * Reception's per-doctor queue board (CLAUDE.md §7b).
 *
 * Reception balances the clinics, so the board has a chip per doctor with the
 * waiting count, busiest first, and two doctors' lines side by side — a Move
 * action under each waiting line. The registration form's doctor picker shows
 * the same counts, so the choice is made where the choice is made.
 *
 * The move itself needs two doctors. The demo seed creates one, so that step
 * skips — visibly, not silently — on a fresh seed and runs wherever a second
 * doctor profile exists.
 */
test.describe("reception's queue board", () => {
  test("shows two doctors side by side, with counts and a move action", async ({ page }) => {
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

    // Never more than two panes, however many doctors there are; the rest
    // are a chip away.
    const panes = page.getByTestId("doctor-queue");
    expect(await panes.count()).toBeLessThanOrEqual(2);
    const chips = page.getByTestId("doctor-chip");
    if ((await chips.count()) > 0) {
      await expect(chips.filter({ hasText: "Dr Vikram Rao" })).toHaveCount(1);
    }

    await showDoctor(page, "Dr Vikram Rao");
    const raoColumn = panes.filter({ hasText: "Dr Vikram Rao" });
    await expect(raoColumn).toHaveCount(1);
    await expect(raoColumn).toContainText("Waiting");
    await expect(raoColumn).toContainText("With the doctor:");

    const row = raoColumn.getByTestId("queue-row").filter({ hasText: patient.name });
    await expect(row).toBeVisible();
    // A quiet line: the action is under it, one tap away.
    await expect(row.getByRole("button", { name: /^Move / })).toHaveCount(0);
    await openLine(row);
    await expect(row.getByRole("button", { name: /^Move / })).toBeVisible();
  });

  test("a doctor's chip brings their line on screen", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/reception");
    await expect(page.getByTestId("doctor-queues")).toBeVisible({ timeout: 30_000 });

    const chips = page.getByTestId("doctor-chip");
    const doctorCount = await chips.count();
    test.skip(doctorCount < 3, "every doctor is already on screen with fewer than three");

    // The first chip not on screen; tapping it puts that doctor in a pane.
    let hidden = chips.first();
    for (let index = 0; index < doctorCount; index += 1) {
      if ((await chips.nth(index).getAttribute("aria-pressed")) !== "true") {
        hidden = chips.nth(index);
        break;
      }
    }
    // The chip is labelled "Dr Name, n waiting".
    const name = (await hidden.getAttribute("aria-label"))?.split(",")[0] ?? "";
    await hidden.click();
    await expect(hidden).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("doctor-queue").filter({ hasText: name })).toHaveCount(1);
    await expect(page.getByTestId("doctor-queue")).toHaveCount(2);
  });

  test("a waiting patient can be moved to another doctor's queue", async ({ page }) => {
    const patient = uniquePatient();

    await signIn(page, ACCOUNTS.reception);
    await page.goto("/reception");
    await expect(page.getByTestId("doctor-queues")).toBeVisible({ timeout: 30_000 });

    const columns = page.getByTestId("doctor-queue");
    // A chip per doctor once there is more than one.
    const doctorCount = await page.getByTestId("doctor-chip").count();
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
    await openLine(row);
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

    // The destination may not be on screen; its chip brings it.
    await showDoctor(page, destinationName);
    const target = columns.filter({ hasText: destinationName });
    await expect(target.getByTestId("queue-row").filter({ hasText: patient.name })).toBeVisible({
      timeout: 15_000,
    });
    const source = columns.filter({ hasText: "Dr Vikram Rao" });
    await expect(source.getByTestId("queue-row").filter({ hasText: patient.name })).toHaveCount(0);
  });
});
