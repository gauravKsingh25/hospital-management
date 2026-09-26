import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, chooseGender, signIn, uniquePatient } from "./support";

/**
 * The doctor's workflow (CLAUDE.md §7, §7b).
 *
 * The behaviour worth protecting here is the one from §6: **the doctor never
 * chooses a status.** They say they are finished, and the state machine
 * decides whether the visit is closed or is waiting on somebody else. The
 * last two tests assert both branches of that, because a well-meaning future
 * change that adds a status dropdown to this screen would look like a feature
 * and would quietly give the state machine a second author.
 */

/** Register a patient and put them in the doctor's queue. Returns the name. */
async function queueAPatient(page: Page): Promise<string> {
  const patient = uniquePatient();

  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception/register");
  await page.getByLabel("Full name", { exact: true }).fill(patient.name);
  await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
  await page.getByLabel("Age", { exact: true }).fill("47");
  await chooseGender(page, "Male");
  await page.getByLabel("Doctor", { exact: true }).click();
  await page.getByRole("option", { name: /Vikram/ }).click();
  await page.getByRole("button", { name: /Register and send to a doctor/ }).click();
  await expect(page.getByText("Token issued")).toBeVisible({ timeout: 45_000 });

  // Sign out so the next step is a genuine doctor session, not reception
  // wearing a doctor's permissions.
  await page.goto("/reception");
  await page.getByRole("button", { name: /Priya/ }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);

  return patient.name;
}

test.describe("the doctor's worklist", () => {
  test("shows waiting patients by name, not by identifier", async ({ page }) => {
    const name = await queueAPatient(page);

    await signIn(page, ACCOUNTS.doctor);
    await expect(page).toHaveURL(/\/doctor/);

    // The whole reason `QueueBoardEntry` carries patient identity: a worklist
    // of UUIDs is not a worklist.
    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 45_000 });
    await expect(row).toContainText(/DEMO-/); // the UHID
    await expect(row).toContainText("47"); // the age
  });

  test("one click from the list to the chart", async ({ page }) => {
    const name = await queueAPatient(page);
    await signIn(page, ACCOUNTS.doctor);

    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await row.getByRole("link", { name: /Start consultation/ }).click();

    await expect(page).toHaveURL(/\/consultation\//);
    await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });
  });
});

test.describe("the consultation", () => {
  test("the chart opens with the patient's identity already on it", async ({ page }) => {
    const name = await queueAPatient(page);
    await signIn(page, ACCOUNTS.doctor);

    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await row.getByRole("link", { name: /Start consultation/ }).click();

    // Server-rendered from the single `/chart` call, so the name and UHID are
    // in the first paint rather than arriving a request later.
    await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });
    await expect(page.getByText(/UHID DEMO-/)).toBeVisible();
  });

  test("the doctor finishes, and the state machine decides where the visit goes", async ({
    page,
  }) => {
    /**
     * This test used to be called "a visit with nothing outstanding closes",
     * and it was only ever true because the demo tenant had no price list: an
     * unpriced consultation charge is zero, a zero balance is not a pending
     * item, and the visit closed. Once `seed_demo.py` prices the consultation
     * — which is what a configured hospital looks like — the money itself is
     * the pending item, and the visit correctly goes to the counter instead.
     *
     * The behaviour under test is unchanged and is the one that matters: the
     * doctor pressed one button that does not name a status, and something
     * else worked out what that meant.
     */
    const name = await queueAPatient(page);
    await signIn(page, ACCOUNTS.doctor);

    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await row.getByRole("link", { name: /Start consultation/ }).click();
    await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });

    await page.getByRole("button", { name: "Start consultation" }).click();

    // Nothing can be recorded until the visit is open, so the note editor
    // only becomes usable at this point.
    const note = page.getByRole("textbox", { name: "Clinical note" });
    await expect(note).toBeEnabled({ timeout: 45_000 });
    await note.fill("Fever three days. Chest clear. Viral illness. Advised rest and fluids.");

    // Ctrl+Enter — a doctor typing a note has both hands on the keyboard.
    await note.press("Control+Enter");

    const diagnosis = page.getByRole("textbox", { name: "Diagnosis" });
    await diagnosis.fill("Viral fever");
    await diagnosis.press("Enter");
    await expect(page.getByText("Viral fever")).toBeVisible({ timeout: 45_000 });

    // Nothing clinical is outstanding — no test was ordered — but the
    // consultation fee is, so the counter is the next stop. The doctor is told
    // that in the pending list, and is still not asked to choose.
    await expect(page.getByText("Waiting on")).toBeVisible({ timeout: 45_000 });
    await expect(page.getByText(/Payment: .* outstanding at the counter/)).toBeVisible();
    await expect(page.getByRole("combobox", { name: /status/i })).toHaveCount(0);

    await page.getByTestId("complete-visit").click();

    // The status came back from the server, and the doctor is returned to the
    // next patient rather than to a confirmation screen.
    await expect(page.getByText(/Visit moved to PENDING_CLEARANCE/)).toBeVisible({
      timeout: 45_000,
    });
    await expect(page).toHaveURL(/\/doctor/, { timeout: 45_000 });
  });

  test("an order the patient must act on keeps the visit open", async ({ page }) => {
    const name = await queueAPatient(page);
    await signIn(page, ACCOUNTS.doctor);

    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await row.getByRole("link", { name: /Start consultation/ }).click();
    await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });
    await page.getByRole("button", { name: "Start consultation" }).click();

    const note = page.getByRole("textbox", { name: "Clinical note" });
    await expect(note).toBeEnabled({ timeout: 45_000 });
    await note.fill("Query anaemia. Bloods requested.");
    await note.press("Control+Enter");

    await page.getByRole("textbox", { name: "Item" }).fill("Complete blood count");
    await page.getByRole("button", { name: "Place order" }).click();

    // It lands in the orders list...
    const orders = page.getByRole("region", { name: "Orders" });
    await expect(orders.getByText("Complete blood count", { exact: true })).toBeVisible({
      timeout: 45_000,
    });

    // ...and, because it blocks closure, in what the visit is waiting on. The
    // doctor is told, in the backend's own words, and is still not asked to
    // choose a status because of it.
    await expect(page.getByText("Waiting on")).toBeVisible({ timeout: 45_000 });
    await expect(page.getByText("Lab: Complete blood count")).toBeVisible();
    await expect(page.getByRole("button", { name: /Complete visit/ })).toBeVisible();

    // There is deliberately no status picker anywhere on this screen.
    await expect(page.getByRole("combobox", { name: /status/i })).toHaveCount(0);
  });
});
