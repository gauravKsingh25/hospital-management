import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, findOnBoard, registerAndQueue, signIn, signOut } from "./support";

/**
 * The clearance desks, and the loop they close (CLAUDE.md §6).
 *
 * The state machine says a visit becomes `COMPLETED` "only when the last
 * pending item is cleared by the relevant staff (cashier / lab tech /
 * pharmacist)". Until these screens existed the doctor could put a visit into
 * `PENDING_CLEARANCE` and nothing in the browser could ever get it out again —
 * the happy path dead-ended one click after the consultation.
 *
 * The long test below is the one that matters. It walks a single patient
 * through four sessions as four different members of staff, and asserts what
 * §6 promises: that nobody chooses `COMPLETED`, and that taking the last rupee
 * closes the visit by itself.
 */

/** Put a patient in front of the doctor, order a blood test, finish the visit. */
async function consultAndOrderBloods(page: Page): Promise<string> {
  const name = await registerAndQueue(page);

  await signIn(page, ACCOUNTS.doctor);
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await row.getByRole("link", { name: /Start consultation/ }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });

  await page.getByRole("button", { name: "Start consultation" }).click();
  const note = page.getByRole("textbox", { name: "Clinical note" });
  await expect(note).toBeEnabled({ timeout: 45_000 });
  await note.fill("Tired for a month. Pale conjunctivae. Query anaemia.");
  await note.press("Control+Enter");

  await page.getByRole("textbox", { name: "Item" }).fill("CBC");
  await page.getByRole("button", { name: "Place order" }).click();
  const orders = page.getByRole("region", { name: "Orders" });
  await expect(orders.getByText("CBC", { exact: true })).toBeVisible({ timeout: 45_000 });

  await page.getByTestId("complete-visit").click();
  await expect(page).toHaveURL(/\/doctor/, { timeout: 45_000 });

  await signOut(page);
  return name;
}

test.describe("the diagnostics bench", () => {
  test("an unaccessioned request is already on the worklist", async ({ page }) => {
    // The reason the board is built from orders rather than reports: before
    // anyone accessions it there is no report to list, and that is exactly the
    // request most likely to be forgotten.
    const name = await consultAndOrderBloods(page);

    await signIn(page, ACCOUNTS.lab);
    await expect(page).toHaveURL(/\/lab/);

    await findOnBoard(page, "worklist-row", name);
    const row = page.getByTestId("worklist-row").filter({ hasText: name });
    await expect(row).toContainText(/DEMO-/); // the UHID, not a UUID
    await expect(row).toContainText("Accessioning");
    await expect(row.getByRole("button", { name: "Accession" })).toBeVisible();
  });

  test("the row's one action follows the sample, then the report", async ({ page }) => {
    const name = await consultAndOrderBloods(page);
    await signIn(page, ACCOUNTS.lab);

    await findOnBoard(page, "worklist-row", name);
    const row = page.getByTestId("worklist-row").filter({ hasText: name });

    await row.getByRole("button", { name: "Accession" }).click();
    await page.getByLabel("Catalogue entry").click();
    await page.getByRole("option", { name: /Complete Blood Count/ }).click();
    await page.getByRole("button", { name: "Accession" }).last().click();

    // Accessioned: the sample now exists and has to be drawn.
    await expect(row).toContainText("Sample", { timeout: 45_000 });
    await row.getByRole("button", { name: "Sample taken" }).click();

    await expect(row).toContainText("Receipt at lab", { timeout: 45_000 });
    await row.getByRole("button", { name: "Received" }).click();

    // Only now is there anything to measure.
    await expect(row).toContainText("Results", { timeout: 45_000 });
    await expect(row.getByRole("link", { name: "Enter results" })).toBeVisible();
  });

  test("the report screen shows the age and sex its ranges depend on", async ({ page }) => {
    const name = await registerAndQueue(page, "62");

    await signIn(page, ACCOUNTS.doctor);
    const queued = page.getByTestId("queue-row").filter({ hasText: name });
    await queued.getByRole("link", { name: /Start consultation/ }).click();
    await page.getByRole("button", { name: "Start consultation" }).click();
    const note = page.getByRole("textbox", { name: "Clinical note" });
    await expect(note).toBeEnabled({ timeout: 45_000 });
    await note.fill("Routine bloods.");
    await note.press("Control+Enter");
    await page.getByRole("textbox", { name: "Item" }).fill("CBC");
    await page.getByRole("button", { name: "Place order" }).click();
    await page.getByTestId("complete-visit").click();
    await expect(page).toHaveURL(/\/doctor/, { timeout: 45_000 });
    await signOut(page);

    await signIn(page, ACCOUNTS.lab);
    await findOnBoard(page, "worklist-row", name);
    const row = page.getByTestId("worklist-row").filter({ hasText: name });
    await row.getByRole("button", { name: "Accession" }).click();
    await page.getByLabel("Catalogue entry").click();
    await page.getByRole("option", { name: /Complete Blood Count/ }).click();
    await page.getByRole("button", { name: "Accession" }).last().click();
    await row.getByRole("button", { name: "Sample taken" }).click();
    await row.getByRole("button", { name: "Received" }).click();
    await row.getByRole("link", { name: "Enter results" }).click();

    // A number means nothing without the band it was judged against, and the
    // band is chosen by sex and age. Both are on the screen, above the values.
    await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });
    await expect(page.getByText(/62 yrs/)).toBeVisible();
    await expect(page.getByText(/FEMALE/)).toBeVisible();
  });

  test("a technician cannot sign off their own work", async ({ page }) => {
    // The separation the backend enforces (`assert_may_verify`). The screen
    // does not offer the button rather than offering it and being refused.
    const name = await consultAndOrderBloods(page);
    await signIn(page, ACCOUNTS.lab);

    await findOnBoard(page, "worklist-row", name);
    const row = page.getByTestId("worklist-row").filter({ hasText: name });
    await row.getByRole("button", { name: "Accession" }).click();
    await page.getByLabel("Catalogue entry").click();
    await page.getByRole("option", { name: /Complete Blood Count/ }).click();
    await page.getByRole("button", { name: "Accession" }).last().click();
    await row.getByRole("button", { name: "Sample taken" }).click();
    await row.getByRole("button", { name: "Received" }).click();
    await row.getByRole("link", { name: "Enter results" }).click();

    await page.getByRole("textbox", { name: "Haemoglobin" }).fill("9.4");
    await page.getByRole("button", { name: "Save results" }).click();

    // Flagged by the server against the female band — not typed by the person
    // who took the measurement.
    await expect(page.getByText("Low", { exact: true })).toBeVisible({ timeout: 45_000 });
    await expect(page.getByRole("button", { name: /Verify and release/ })).toHaveCount(0);
  });
});

test.describe("the cash counter", () => {
  test("a visit with charges nobody has invoiced is on the board", async ({ page }) => {
    // Not the invoice list: this visit has no invoice at all, and it is still
    // the hospital's money.
    const name = await consultAndOrderBloods(page);

    await signIn(page, ACCOUNTS.cashier);
    await page.goto("/billing");

    await findOnBoard(page, "account-row", name);
    const row = page.getByTestId("account-row").filter({ hasText: name });
    await expect(row).toContainText(/DEMO-/);
    // The consultation and the blood test, priced by the seeded rate card.
    await expect(row).toContainText("₹850.00");
  });

  test("the counter assembles, issues and takes payment", async ({ page }) => {
    const name = await consultAndOrderBloods(page);

    await signIn(page, ACCOUNTS.cashier);
    await page.goto("/billing");
    await findOnBoard(page, "account-row", name);
    const row = page.getByTestId("account-row").filter({ hasText: name });
    await row.getByRole("link", { name: "Open account" }).click();

    await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });

    // The cashier never builds the bill — the charges are already there,
    // captured from the consultation and the order by the event bus.
    const charges = page.getByRole("region", { name: "Charges not yet invoiced" });
    await expect(charges.getByText("OPD consultation")).toBeVisible();
    await expect(charges.getByText(/Complete Blood Count|CBC/)).toBeVisible();

    await page.getByRole("button", { name: "Assemble invoice" }).click();
    // On the chip's `data-status`, not its label: the success toast says
    // "Draft invoice assembled." and a text match finds both.
    await expect(page.locator('[data-status="DRAFT"]')).toBeVisible({ timeout: 45_000 });

    await page.getByRole("button", { name: "Issue", exact: true }).click();
    await expect(page.locator('[data-status="ISSUED"]')).toBeVisible({ timeout: 45_000 });

    await page.getByRole("button", { name: "Take payment" }).click();

    // UPI is the default, and it demands a reference — an unmatched digital
    // payment cannot be reconciled the next morning (CLAUDE.md §9).
    const record = page.getByRole("button", { name: "Record payment" });
    await expect(record).toBeDisabled();
    await page.getByLabel("Transaction reference").fill("UPI-E2E-0001");
    await expect(record).toBeEnabled();
    await record.click();

    await expect(page.getByText(/Receipt RCP-/)).toBeVisible({ timeout: 45_000 });
    await expect(page.locator('[data-status="PAID"]')).toBeVisible({ timeout: 45_000 });
  });
});

test.describe("the loop closes", () => {
  test("four desks, one patient, and nobody picks the final status", async ({ page }) => {
    /**
     * The whole point of CLAUDE.md §6, exercised end to end.
     *
     * Reception registers. The doctor consults and orders. The lab verifies.
     * The counter takes the money. At no stage does any screen offer a status
     * dropdown, and the visit reaches `COMPLETED` because the last pending
     * item was cleared — not because anybody decided it had.
     */
    const name = await consultAndOrderBloods(page);

    // --- the bench -------------------------------------------------------
    await signIn(page, ACCOUNTS.lab);
    await findOnBoard(page, "worklist-row", name);
    const benchRow = page.getByTestId("worklist-row").filter({ hasText: name });

    await benchRow.getByRole("button", { name: "Accession" }).click();
    await page.getByLabel("Catalogue entry").click();
    await page.getByRole("option", { name: /Complete Blood Count/ }).click();
    await page.getByRole("button", { name: "Accession" }).last().click();
    await benchRow.getByRole("button", { name: "Sample taken" }).click();
    await benchRow.getByRole("button", { name: "Received" }).click();
    await benchRow.getByRole("link", { name: "Enter results" }).click();

    await page.getByRole("textbox", { name: "Haemoglobin" }).fill("13.2");
    await page.getByRole("button", { name: "Save results" }).click();
    await expect(page.getByText("Results saved.")).toBeVisible({ timeout: 45_000 });
    await signOut(page);

    // --- the doctor signs the report -------------------------------------
    await signIn(page, ACCOUNTS.doctor);
    await page.goto("/lab");
    await findOnBoard(page, "worklist-row", name);
    const toVerify = page.getByTestId("worklist-row").filter({ hasText: name });
    await expect(toVerify).toContainText("Verification", { timeout: 45_000 });
    await toVerify.getByRole("link", { name: "Open report" }).click();
    await page.getByRole("button", { name: /Verify and release/ }).click();

    // Signing off clears the doctor's order and takes the row off the bench.
    await expect(page).toHaveURL(/\/lab$/, { timeout: 45_000 });

    // Reloaded, so this asserts what the *server* now lists rather than what
    // the client happened to have cached before the signature.
    await page.reload();
    await expect(findOnBoard(page, "worklist-row", name)).rejects.toThrow(
      /not found on any page/,
    );
    await signOut(page);

    // --- the counter -----------------------------------------------------
    // The lab is done and the visit is still open, because money is still a
    // pending item. This is the half that had no screen at all before.
    await signIn(page, ACCOUNTS.cashier);
    await page.goto("/billing");
    await findOnBoard(page, "account-row", name);
    const counterRow = page.getByTestId("account-row").filter({ hasText: name });
    await counterRow.getByRole("link", { name: "Open account" }).click();

    await page.getByRole("button", { name: "Assemble invoice" }).click();
    await page.getByRole("button", { name: "Issue", exact: true }).click();
    await page.getByRole("button", { name: "Take payment" }).click();
    await page.getByLabel("Transaction reference").fill("UPI-E2E-LOOP");
    await page.getByRole("button", { name: "Record payment" }).click();
    await expect(page.getByText(/Receipt RCP-/)).toBeVisible({ timeout: 45_000 });

    // --- and the visit closed itself -------------------------------------
    // Asserted on the chip's `data-status`, not its label: the label is
    // translated ("Closed"), and a test that reads it would break the day
    // somebody improves the wording rather than the day the behaviour breaks.
    await expect(page.locator('[data-status="COMPLETED"]')).toBeVisible({ timeout: 45_000 });
    await expect(page.getByText("₹0.00")).toBeVisible();

    // Off the counter's board, because there is nothing left to collect.
    // Asserted against the whole board, not page one — a settled visit must
    // not merely have moved to a later page.
    await page.goto("/billing");
    await expect(
      findOnBoard(page, "account-row", name),
    ).rejects.toThrow(/not found on any page/);
  });
});

test.describe("the nurse's quick action", () => {
  test("vitals are recorded from the queue without leaving it", async ({ page }) => {
    // CLAUDE.md §7b gives a nurse fifteen seconds for a common action. A
    // dialog on the row rather than a screen of its own is most of how that
    // is met: they keep their place in the list.
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/queue");

    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 45_000 });

    const started = Date.now();
    await row.getByRole("button", { name: "Vitals" }).click();
    await page.getByLabel(/Temperature/).fill("38.4");
    await page.getByLabel(/Pulse/).fill("104");
    await page.getByLabel(/Systolic/).fill("118");
    await page.getByLabel(/Diastolic/).fill("76");
    await page.getByRole("button", { name: "Save" }).click();

    await expect(page.getByText(/Vitals recorded/)).toBeVisible({ timeout: 45_000 });
    const elapsed = (Date.now() - started) / 1000;
    console.log(`vitals recorded in ${elapsed.toFixed(1)}s`);

    // Still on the queue, still looking at the same list.
    await expect(page).toHaveURL(/\/queue/);
    await expect(row).toBeVisible();
  });

  test("half a blood pressure is refused before the round trip", async ({ page }) => {
    // The backend requires both values or neither. Telling the nurse after a
    // round trip is telling them after they have moved on.
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/queue");
    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 45_000 });
    await row.getByRole("button", { name: "Vitals" }).click();

    await page.getByLabel(/Systolic/).fill("130");
    await expect(page.getByText(/both blood pressure values/i)).toBeVisible();
    await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();

    await page.getByLabel(/Diastolic/).fill("85");
    await expect(page.getByRole("button", { name: "Save" })).toBeEnabled();
  });
});
