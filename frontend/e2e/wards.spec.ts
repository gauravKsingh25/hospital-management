import { expect, test, type Page } from "@playwright/test";

import {
  ACCOUNTS,
  bedTile,
  registerAndQueue,
  releaseTrackedBeds,
  signIn,
  signOut,
  trackBed,
} from "./support";

/**
 * Inpatient care: the bed board, and one stay end to end.
 *
 * The behaviour worth protecting here is the `CLEANING` rung. Most bed boards
 * leave it out, and leaving it out is what makes a board lie: the moment a
 * patient is discharged the bed looks free, admissions sends somebody to it,
 * they arrive to find it unmade, and within a week the ward is back on a
 * whiteboard. The last test walks a bed through occupied → cleaning → free and
 * asserts it is *not* offered for admission in between.
 *
 * The other one is `DISCHARGE_INITIATED`. The doctor writing the discharge
 * does not free the bed — the patient is still in it settling the bill — and a
 * system that frees it at the signature double-books it.
 */

/** Put a patient in front of the doctor and open the consultation. */
async function consultingRoom(page: Page): Promise<string> {
  const name = await registerAndQueue(page);

  await signIn(page, ACCOUNTS.doctor);
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await row.getByRole("link", { name: /Start consultation/ }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Start consultation" }).click();

  const note = page.getByRole("textbox", { name: "Clinical note" });
  await expect(note).toBeEnabled({ timeout: 45_000 });
  await note.fill("Breathless at rest. Needs inpatient care.");
  await note.press("Control+Enter");

  return name;
}

/**
 * Admit the patient currently on screen.
 *
 * Returns the stay's URL *and* the bed taken, because the suite shares one
 * ward: assertions have to follow the specific bed this test used rather than
 * "the first one in cleaning", which belongs to whichever test ran before.
 */
async function admit(page: Page, name: string): Promise<{ url: string; bed: string }> {
  await page.getByTestId("admit-patient").click();

  const dialog = page.getByRole("dialog", { name: "Admit to a ward" });
  await expect(dialog).toBeVisible();
  const choice = dialog.getByRole("group", { name: "Choose a bed" }).getByText(/^GW1-/).first();
  const bed = (await choice.innerText()).trim();
  await choice.click();
  await dialog.getByLabel(/Attendant$/).fill("Sita Devi");
  await dialog.getByRole("button", { name: "Admit", exact: true }).click();

  await expect(page).toHaveURL(/\/admissions\/[0-9a-f-]{36}/, { timeout: 45_000 });
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible({ timeout: 45_000 });

  const stay = { url: page.url(), bed };
  trackBed(stay);
  return stay;
}

// Hand every bed back when the test ends — see `releaseTrackedBeds`. Without
// it these specs are not idempotent, and the ward fills up after three runs.
releaseTrackedBeds();

test.describe("the bed board", () => {
  test("shows every bed, its state, and who is in it", async ({ page }) => {
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/wards");

    // Scoped to the ward, not the whole board: other specs create wards of
    // their own, and a count over every tile in the hospital is a count that
    // breaks whenever somebody adds a bed.
    const ward = page.getByRole("region", { name: /General Ward/ });
    await expect(ward).toBeVisible({ timeout: 45_000 });
    await expect(ward.getByTestId("bed-tile")).toHaveCount(12);
    // Occupancy is the number management asks for at nine in the morning.
    await expect(page.getByText("Occupancy")).toBeVisible();
  });

  test("a bed can be taken out of service, with a reason, and restored", async ({ page }) => {
    // `bed:block` — an administrator's permission, not a nurse's. Taking a bed
    // off the board changes what the hospital can sell, which is why it sits
    // with the people who own the estate rather than with the ward.
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/wards");

    const free = page.locator('[data-testid="bed-tile"][data-status="AVAILABLE"]').first();
    await expect(free).toBeVisible({ timeout: 45_000 });
    const code = (await free.getAttribute("data-code")) ?? "";

    await free.getByRole("button", { name: "Out of service" }).click();
    const dialog = page.getByRole("dialog", { name: "Out of service" });
    // The backend requires a reason, and rightly: a bed that vanishes from the
    // board with no explanation is a bed nobody dares bring back.
    await expect(dialog.getByRole("button", { name: "Save" })).toBeDisabled();
    await dialog.getByLabel("Reason").fill("Broken side rail");
    await dialog.getByRole("button", { name: "Save" }).click();

    await expect(page.getByText("Bed taken out of service.")).toBeVisible({ timeout: 45_000 });
    await expect(bedTile(page, code)).toHaveAttribute("data-status", "OUT_OF_SERVICE", {
      timeout: 45_000,
    });

    await bedTile(page, code).getByRole("button", { name: "Back in service" }).click();
    // `CLEANING`, not `AVAILABLE` — a bed coming back from maintenance is made
    // up before anybody is put in it. The same rung that stops the board lying
    // after a discharge applies to a repair.
    await expect(bedTile(page, code)).toHaveAttribute("data-status", "CLEANING", {
      timeout: 45_000,
    });

    // Finish the round trip rather than stopping at `CLEANING`. It asserts one
    // more real rung — a repaired bed does come back to the pool — and it is
    // also the difference between this test costing the ward a bed per run and
    // costing it nothing.
    await bedTile(page, code).getByRole("button", { name: "Cleaned" }).click();
    await expect(bedTile(page, code)).toHaveAttribute("data-status", "AVAILABLE", {
      timeout: 45_000,
    });
  });

  test("a doctor sees the board but is not offered its controls", async ({ page }) => {
    // The separation the RBAC states in words: a consultant can see the board,
    // because "is there an ICU bed" changes a decision, and does not run it.
    await signIn(page, ACCOUNTS.doctor);
    await page.goto("/wards");

    await expect(page.getByTestId("bed-tile").first()).toBeVisible({ timeout: 45_000 });
    await expect(page.getByRole("button", { name: "Out of service" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Cleaned" })).toHaveCount(0);
  });
});

test.describe("admitting", () => {
  test("the doctor admits from the consultation and lands on the stay", async ({ page }) => {
    const name = await consultingRoom(page);
    await admit(page, name);

    // The screen where somebody will later sign a discharge names the patient
    // it is about — the whole reason `AdmissionRead` gained identity.
    await expect(page.getByText(/DEMO-/)).toBeVisible();
    await expect(page.getByText("General Ward", { exact: false })).toBeVisible();
    await expect(page.locator('[data-status="ADMITTED"]')).toBeVisible();
  });

  test("the patient appears on the board, in a bed, by name", async ({ page }) => {
    const name = await consultingRoom(page);
    await admit(page, name);
    await signOut(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/wards");

    const tile = page.getByTestId("bed-tile").filter({ hasText: name });
    await expect(tile).toBeVisible({ timeout: 45_000 });
    await expect(tile).toHaveAttribute("data-status", "OCCUPIED");

    // One click from the board to the stay.
    await tile.getByRole("link").click();
    await expect(page).toHaveURL(/\/admissions\//, { timeout: 45_000 });
  });
});

test.describe("the stay ends", () => {
  test("fit for discharge does not free the bed; discharge sends it to cleaning", async ({
    page,
  }) => {
    /**
     * The two rungs this module exists to model, asserted together.
     *
     * `DISCHARGE_INITIATED` keeps the bed: the patient is still in it settling
     * the bill. `CLEANING` keeps it out of the admissions pool until somebody
     * has actually turned it around. Skip either and the board starts lying.
     */
    const name = await consultingRoom(page);
    const { url: stayUrl, bed } = await admit(page, name);

    // The doctor writes the discharge.
    await page.getByRole("button", { name: "Mark fit for discharge" }).click();
    await expect(page.getByText("Marked fit for discharge.")).toBeVisible({ timeout: 45_000 });
    await expect(page.locator('[data-status="DISCHARGE_INITIATED"]')).toBeVisible();

    // The bed is still theirs — asserted on the board, not just the status.
    await page.goto("/wards");
    await expect(bedTile(page, bed)).toHaveAttribute("data-status", "OCCUPIED", {
      timeout: 45_000,
    });
    await expect(bedTile(page, bed)).toContainText(name);

    // Now the patient actually leaves.
    await page.goto(stayUrl);
    await page.getByTestId("discharge").click();
    const dialog = page.getByRole("dialog", { name: "Discharge" });
    // No default outcome: a system that records a referral as a recovery
    // because nobody changed a dropdown is worse than one that asks.
    await expect(dialog.getByRole("button", { name: "Discharge", exact: true })).toBeDisabled();
    await dialog.getByLabel("Outcome").click();
    await page.getByRole("option", { name: "Recovered" }).click();
    await dialog.getByRole("button", { name: "Discharge", exact: true }).click();

    await expect(page.getByText(/Patient discharged/)).toBeVisible({ timeout: 45_000 });
    await expect(page.locator('[data-status="DISCHARGED"]')).toBeVisible({ timeout: 45_000 });

    // And *that* bed is in cleaning — not free.
    await page.goto("/wards");
    await expect(bedTile(page, bed)).toHaveAttribute("data-status", "CLEANING", {
      timeout: 45_000,
    });
    // The doctor can see the turnaround and cannot do it: `bed:clean` belongs
    // to the ward and to housekeeping.
    await expect(bedTile(page, bed).getByRole("button", { name: "Cleaned" })).toHaveCount(0);

    await signOut(page);
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/wards");
    await expect(bedTile(page, bed)).toBeVisible({ timeout: 45_000 });
    await bedTile(page, bed).getByRole("button", { name: "Cleaned" }).click();
    await expect(bedTile(page, bed)).toHaveAttribute("data-status", "AVAILABLE", {
      timeout: 45_000,
    });
  });

  test("a death is not offered as a discharge outcome", async ({ page }) => {
    // Both terminal outcomes are recorded against the *visit*, where CLAUDE.md
    // §6's metadata is captured, and the admission follows. Offering them here
    // would be a second, thinner way to record a death.
    const name = await consultingRoom(page);
    await admit(page, name);

    await page.getByTestId("discharge").click();
    await page.getByLabel("Outcome").click();

    await expect(page.getByRole("option", { name: "Recovered" })).toBeVisible();
    await expect(page.getByRole("option", { name: /Deceased|Died/i })).toHaveCount(0);
    await expect(page.getByRole("option", { name: /against medical advice/i })).toHaveCount(0);
  });
});
