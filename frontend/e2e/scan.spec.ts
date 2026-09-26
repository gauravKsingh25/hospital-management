import { expect, test } from "@playwright/test";

import { ACCOUNTS, registerAndReadSlip, signIn, signOut } from "./support";

/**
 * QR-based patient lookup (CLAUDE.md §7b).
 *
 * The payload is the UHID as plain text, which is what makes the common case
 * free: **a counter barcode scanner is a keyboard.** It types the payload into
 * whatever has focus and presses Enter, so a scan into the existing search box
 * needs no client code at all.
 *
 * That is why these tests drive the keyboard rather than a camera. Playwright
 * typing into the search box *is* a HID scanner, faithfully — same events, same
 * order. What no automated test can cover is whether a thermal-printed code is
 * physically scannable; that needs paper and a scanner, and it is the one step
 * on this feature's critical path that is not code.
 */

test.describe("the token slip", () => {
  test("says who it belongs to and carries a scannable code", async ({ page }) => {
    /**
     * The slip used to carry a token, a doctor and a room — and nothing that
     * identified the person walking away with it.
     */
    const { name, uhid } = await registerAndReadSlip(page);

    // Scoped to the slip: the success toast repeats the name and the UHID, so
    // a page-wide text match would pass on the toast alone and prove nothing
    // about what was printed.
    const slip = page.getByTestId("token-slip");
    await expect(slip).toContainText(name);
    await expect(slip).toContainText(uhid);
    // `CODE-YY-NNNNNN`, the shape a scanner will send back.
    expect(uhid).toMatch(/^[A-Z0-9]{2,12}-\d{2}-\d{6}$/);
  });

  test("prints the slip and nothing else on the page", async ({ page }) => {
    /**
     * `emulateMedia` applies the print stylesheet without a printer, which is
     * the only way to test this at all — and it needs testing, because what
     * prints is what a patient carries home.
     *
     * Two things had to be kept off it: the screen's own heading ("Register a
     * patient — 4 fields. Everything else can be added later." is not a
     * caption for somebody's token) and Next's development overlay, which
     * mounts outside the React tree and landed on top of the slip.
     */
    const { uhid } = await registerAndReadSlip(page);
    await page.emulateMedia({ media: "print" });

    const slip = page.getByTestId("token-slip");
    await expect(slip).toBeVisible();
    await expect(slip.getByRole("img")).toHaveAttribute("aria-label", uhid);

    // The screen's chrome, gone.
    await expect(page.getByRole("heading", { name: "Register a patient" })).toBeHidden();
    await expect(page.getByRole("button", { name: /Print slip/ })).toBeHidden();
    await expect(page.getByRole("button", { name: /Register another/ })).toBeHidden();

    // Next's dev overlay. Present in the DOM in `next dev` and absent in a
    // production build, so this asserts "not visible" rather than "not there".
    const overlay = page.locator("nextjs-portal");
    if (await overlay.count()) await expect(overlay.first()).toBeHidden();

    await page.emulateMedia({ media: null });
  });
});

test.describe("scanning a card", () => {
  test("a scanned UHID opens the patient's record", async ({ page }) => {
    /**
     * The whole feature, end to end, through the path a counter actually uses:
     * focus the search box, scan, open. Fourteen typed characters become one
     * physical action.
     */
    const { name, uhid } = await registerAndReadSlip(page);

    await page.keyboard.press("F3");
    // A HID scanner types the payload and presses Enter. This is that.
    await page.getByRole("combobox").fill(uhid);

    const hit = page.getByRole("option").filter({ hasText: name }).first();
    await expect(hit).toBeVisible({ timeout: 45_000 });
    await hit.click();

    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  });

  test("a scan resolves in under two seconds", async ({ page }) => {
    // The point of the feature is speed at a counter. Measured from the
    // keystroke a scanner sends to the patient being on screen.
    const { name, uhid } = await registerAndReadSlip(page);

    await page.keyboard.press("F3");
    const started = Date.now();
    await page.getByRole("combobox").fill(uhid);
    const hit = page.getByRole("option").filter({ hasText: name }).first();
    await expect(hit).toBeVisible({ timeout: 45_000 });
    const elapsed = Date.now() - started;

    console.log(`scan resolved in ${(elapsed / 1000).toFixed(1)}s`);
    expect(elapsed).toBeLessThan(2_000);
  });

  test("a card from another hospital finds nothing", async ({ page }) => {
    // Tenant isolation, from the one direction a stranger can actually push
    // on: a real card, presented at the wrong hospital. RLS makes it
    // indistinguishable from a UHID that never existed.
    await signIn(page, ACCOUNTS.reception);
    await page.keyboard.press("F3");
    await page.getByRole("combobox").fill("OTHER-26-000001");

    await expect(page.getByRole("option")).toHaveCount(0, { timeout: 45_000 });
  });
});

test.describe("scan anywhere", () => {
  /**
   * The scanner recognised by its typing speed, with no box focused first.
   *
   * `page.keyboard.type` with a per-character delay is exactly what a HID
   * scanner emits — the delay is the only thing that distinguishes a device
   * from a person, and it is the thing under test.
   */
  const SCANNER_DELAY = 8;

  test("a scan from a plain screen opens the patient", async ({ page }) => {
    const { name, uhid } = await registerAndReadSlip(page);

    await page.goto("/queue");
    // Wait for a row, not the heading. Rows arrive from a client query, so
    // seeing one proves React has hydrated and the key listener is attached —
    // whereas the heading is server-rendered and present before any of that.
    // A real person is slower than hydration; a test is not.
    await expect(page.getByTestId("queue-row").first()).toBeVisible({ timeout: 45_000 });
    // Nothing focused, no dialog open — the case this feature exists for.
    await page.keyboard.type(uhid, { delay: SCANNER_DELAY });
    await page.keyboard.press("Enter");

    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  });

  test("an unknown card says so rather than doing nothing", async ({ page }) => {
    // A scan that silently fails is the worst outcome: the receptionist scans
    // again, harder, and concludes the software is broken.
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/queue");
    await expect(page.getByTestId("queue-row").first()).toBeVisible({ timeout: 45_000 });
    await page.keyboard.type("NOPE-99-000001", { delay: SCANNER_DELAY });
    await page.keyboard.press("Enter");

    await expect(page.getByText("No patient has that card.")).toBeVisible({ timeout: 45_000 });
  });

  test("it cannot eat a clinical note", async ({ page }) => {
    /**
     * The risk that made this the last thing built. A global key handler that
     * misfires while a doctor is writing costs a sentence of a clinical note,
     * which is worse than the typing this saves.
     *
     * The guard is absolute rather than statistical: the handler is inert
     * whenever a text control has focus. So a burst typed *into the note* has
     * to land in the note, in full, and navigate nowhere.
     */
    const { name, uhid } = await registerAndReadSlip(page);
    await signOut(page);

    await signIn(page, ACCOUNTS.doctor);
    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await expect(row).toBeVisible({ timeout: 45_000 });
    await row.getByRole("link", { name: /Start consultation/ }).click();
    await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });
    await page.getByRole("button", { name: "Start consultation" }).click();

    const note = page.getByRole("textbox", { name: "Clinical note" });
    await expect(note).toBeEnabled({ timeout: 45_000 });
    await note.click();
    await note.type(uhid, { delay: SCANNER_DELAY });

    // Every character still there, and the doctor is still on the chart.
    await expect(note).toHaveValue(uhid);
    await expect(page).toHaveURL(/\/consultation\//);
  });
});

test.describe("the patient card", () => {
  test("prints the name, the UHID and a code big enough to scan", async ({ page }) => {
    const { name, uhid } = await registerAndReadSlip(page);

    await page.keyboard.press("F3");
    await page.getByRole("combobox").fill(uhid);
    await page.getByRole("option").filter({ hasText: name }).first().click();
    await expect(page).toHaveURL(/\/patients\/[0-9a-f-]{36}/, { timeout: 45_000 });

    await page.getByRole("link", { name: "Patient card" }).click();
    const card = page.getByTestId("patient-card");
    await expect(card).toBeVisible({ timeout: 45_000 });
    await expect(card).toContainText(name);
    await expect(card).toContainText(uhid);

    // Sized in millimetres, because the only dimension that matters is the
    // printed one — and below about 20 mm a version-1 code stops reading
    // reliably off a thermal head.
    const code = card.getByRole("img");
    await expect(code).toHaveAttribute("aria-label", uhid);
    const width = await code.getAttribute("width");
    expect(width).toMatch(/^\d+mm$/);
    expect(Number.parseInt(width as string, 10)).toBeGreaterThanOrEqual(20);
  });
});
