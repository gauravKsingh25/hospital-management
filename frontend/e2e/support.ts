import { expect, test, type Page } from "@playwright/test";

/**
 * Shared helpers for the end-to-end specs.
 *
 * The accounts are the ones `backend/scripts/seed_demo.py` creates. Hard-coded
 * rather than created per run, because creating a user needs an admin token,
 * and a suite that spends its first thirty seconds bootstrapping identity is a
 * suite people stop running.
 */

export const PASSWORD = process.env.DEMO_PASSWORD ?? "demo-password-2026";

export const ACCOUNTS = {
  admin: "admin@demo.hospital",
  reception: "reception@demo.hospital",
  doctor: "doctor@demo.hospital",
  nurse: "nurse@demo.hospital",
  lab: "lab@demo.hospital",
  cashier: "cashier@demo.hospital",
  admissionDesk: "admission@demo.hospital",
  accountant: "accountant@demo.hospital",
} as const;

export async function signIn(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 30_000 });
}

export async function signOut(page: Page): Promise<void> {
  await page
    .getByRole("button", { name: /@demo\.hospital|Priya|Vikram|Asha|Lakshmi|Ravi|Meera|Ritu/ })
    .click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
}

/**
 * Register a patient, send them to the doctor's queue, and sign out again.
 *
 * The one-click OPD path (CLAUDE.md §7b) — the same one a receptionist takes,
 * which is why the specs drive the form rather than posting to the API. Signs
 * out at the end so what follows is a genuine session for the next role, not
 * reception wearing somebody else's permissions.
 */
export async function registerAndQueue(page: Page, age = "34"): Promise<string> {
  const patient = uniquePatient();

  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception/register");
  await page.getByLabel("Full name", { exact: true }).fill(patient.name);
  await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
  await page.getByLabel("Age", { exact: true }).fill(age);
  await chooseGender(page, "Female");
  await page.getByLabel("Doctor", { exact: true }).click();
  await page.getByRole("option", { name: /Vikram/ }).click();
  await page.getByRole("button", { name: /Register and send to a doctor/ }).click();
  await expect(page.getByText("Token issued")).toBeVisible({ timeout: 45_000 });

  await page.goto("/reception");
  await signOut(page);

  return patient.name;
}

/**
 * Register a patient, let reception mark them seen, and send them to the
 * admission desk — the OPD half of the hand-off, as reception does it.
 *
 * The doctor sees the patient in person and enters nothing; there is no
 * consultation screen. Ends signed out.
 */
export async function sentForAdmission(page: Page): Promise<string> {
  const name = await registerAndQueue(page);

  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception");
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.getByTestId("mark-seen").click();

  // The undo window passes, then the row moves to "seen".
  const seen = page.getByTestId("seen-row").filter({ hasText: name });
  await expect(seen).toBeVisible({ timeout: 20_000 });
  await seen.getByTestId("send-to-admission").click();
  await page.getByRole("dialog").getByRole("button", { name: "Send", exact: true }).click();
  await expect(seen.getByTestId("admission-status")).toHaveText("At admission desk", {
    timeout: 15_000,
  });

  await signOut(page);
  return name;
}

/**
 * The admission desk admits a waiting patient into a free bed, optionally
 * matching `bed` (e.g. `/^GW1-/`). The bed is tracked, so `releaseTrackedBeds`
 * gives it back after the test. Ends signed in as the desk, on the stay.
 */
export async function deskAdmits(
  page: Page,
  name: string,
  { bed = /./, attendant }: { bed?: RegExp; attendant?: string } = {},
): Promise<OpenStay> {
  await signIn(page, ACCOUNTS.admissionDesk);
  await page.goto("/admissions");
  const waiting = page.getByTestId("admission-request").filter({ hasText: name });
  await expect(waiting).toBeVisible({ timeout: 30_000 });
  await waiting.getByTestId("desk-admit").click();

  const dialog = page.getByRole("dialog", { name: "Admit to a ward" });
  // The label, not the radio: the input is `sr-only` inside it, so the label
  // is what a person clicks. Its text is exactly the bed code, which is what
  // teardown needs to give the bed back.
  const choice = dialog
    .locator("label:has(input[name='bed']:not([disabled]))")
    .filter({ hasText: bed })
    .first();
  await expect(choice).toBeVisible({ timeout: 30_000 });
  const code = (await choice.innerText()).trim();
  await choice.click();
  if (attendant) await dialog.getByLabel(/Attendant$/).fill(attendant);
  await dialog.getByRole("button", { name: "Admit", exact: true }).click();

  await expect(page).toHaveURL(/\/admissions\/[0-9a-f-]{36}$/, { timeout: 45_000 });
  const stay = { url: page.url(), bed: code };
  trackBed(stay);
  return stay;
}

/**
 * A patient admitted the way the hospital now does it — reception sends, the
 * desk admits — returning their name and admission URL. Ends signed in as the
 * doctor, who still runs the inpatient chart.
 */
export async function admittedPatient(page: Page): Promise<{ name: string; url: string }> {
  const name = await sentForAdmission(page);
  const { url } = await deskAdmits(page, name);
  await signOut(page);
  await signIn(page, ACCOUNTS.doctor);
  return { name, url };
}

/**
 * The doctor finishes a queued patient's visit, optionally having ordered a
 * lab test — through the API, as the removed consultation screen did.
 *
 * There is deliberately no screen for this any more (the doctor sees the
 * patient in person and enters nothing, and test ordering is not yet built
 * for staff). The lab worklist, the counter and messaging still run off visits
 * finished this way, so their specs drive it here. Ends signed out.
 */
export async function doctorFinishesVisit(
  page: Page,
  name: string,
  { order }: { order?: string } = {},
): Promise<void> {
  await signIn(page, ACCOUNTS.doctor);
  const queue = await page.request.get("/bff/queue");
  expect(queue.ok()).toBeTruthy();
  const entry = ((await queue.json()) as { patient_name: string | null; encounter_id: string | null }[]).find(
    (row) => row.patient_name === name,
  );
  expect(entry?.encounter_id, `${name} is not in the queue`).toBeTruthy();
  const visit = `/bff/encounters/${entry!.encounter_id}`;

  const started = await page.request.post(`${visit}/start`, { data: {} });
  expect(started.ok(), await started.text()).toBeTruthy();
  if (order) {
    const placed = await page.request.post(`${visit}/orders`, {
      data: { order_type: "LAB", item_code: order, item_name: order },
    });
    expect(placed.ok(), await placed.text()).toBeTruthy();
  }
  const completed = await page.request.post(`${visit}/complete`, { data: {} });
  expect(completed.ok(), await completed.text()).toBeTruthy();

  await signOut(page);
}

/** Find a patient in the index and open their record. */
export async function openPatient(page: Page, name: string): Promise<void> {
  await page.goto("/patients");
  await page.getByLabel("Search").fill(name);
  const row = page.getByTestId("patient-row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 45_000 });
  await row.getByRole("link", { name: "Open" }).click();
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible({ timeout: 45_000 });
}

/**
 * Record a death against the open visit on the patient page now showing —
 * where outcomes live since the consultation screen was removed.
 */
export async function recordDeath(page: Page, cause: string): Promise<void> {
  await page.getByTestId("record-outcome").first().click();
  const dialog = page.getByRole("dialog", { name: "How did this visit end?" });
  await dialog.getByLabel("What happened").click();
  await page.getByRole("option", { name: "The patient died" }).click();
  await dialog.getByLabel("Certified by").click();
  await page.getByRole("option", { name: /Vikram/ }).click();
  await dialog.getByLabel("Cause of death").fill(cause);
  await dialog.getByTestId("confirm-outcome").click();
  await expect(page.getByTestId("outcome-summary")).toBeVisible({ timeout: 45_000 });
}

/** A stay a spec opened, and the bed it took. */
export type OpenStay = { url: string; bed: string };

/**
 * Beds the running test has taken, so teardown can hand them back.
 *
 * The suite shares one tenant and, by design, mostly does not clean up after
 * itself — the README says to reset the demo data periodically, because
 * abandoned visits pile up on the boards and push new ones off the first page.
 *
 * **Beds are different, and this is why they get a lifecycle of their own.**
 * They are a fixed, exhaustible pool rather than a list that grows: General
 * Ward has twelve, and the specs took ten a run across `wards` and `chart` and
 * gave one back. So the suite passed twice and then failed on a full ward,
 * with a locator timeout inside admissions that reads exactly like a
 * regression in the code under test. It cost an hour to diagnose, twice.
 *
 * "Reset more often" is not the fix. A suite that only passes on freshly reset
 * data fails at the worst possible moment and blames the wrong thing.
 */
let bedsTaken: OpenStay[] = [];

/** Record a bed this test took. Call it the moment an admission succeeds. */
export function trackBed(stay: OpenStay): void {
  bedsTaken.push(stay);
}

/** One bed tile on the board, by its code. */
export function bedTile(page: Page, code: string) {
  return page.locator(`[data-testid="bed-tile"][data-code="${code}"]`);
}

/**
 * Discharge the stay and turn its bed around, so the next run has a ward.
 *
 * Done as the hospital admin because it is the one role holding both
 * `admission:discharge` and `bed:clean`. The split those two permissions
 * describe is real and worth keeping — a doctor may discharge and may not
 * clean — but teardown should not need two sign-ins to work around it.
 *
 * Tolerant of a stay already closed and a bed already made up: the discharge
 * spec walks its own bed all the way back to `AVAILABLE`, and teardown has to
 * be a no-op after it rather than a second failure.
 */
async function releaseBed(page: Page, stay: OpenStay): Promise<void> {
  // Cookies rather than the sign-out menu. Teardown runs after failures too,
  // and a test that died with a dialog open cannot reach the user menu —
  // teardown that needs the UI in a good state stops working exactly when it
  // is needed.
  await page.context().clearCookies();
  await signIn(page, ACCOUNTS.admin);

  await dischargeOpenStay(page, stay.url);

  await page.goto("/wards");
  const tile = bedTile(page, stay.bed);
  await expect(tile).toBeVisible({ timeout: 45_000 });

  const cleaned = tile.getByRole("button", { name: "Cleaned" });
  if ((await cleaned.count()) > 0) await cleaned.click();
  await expect(tile).toHaveAttribute("data-status", "AVAILABLE", { timeout: 45_000 });
}

/** Discharge a stay as the signed-in user, if it is still open. */
async function dischargeOpenStay(page: Page, url: string): Promise<void> {
  await page.goto(url);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 45_000 });

  // Absent once the patient is out: the whole actions section is gated on the
  // stay still being live.
  const discharge = page.getByTestId("discharge");
  if ((await discharge.count()) > 0) {
    await discharge.click();
    const dialog = page.getByRole("dialog", { name: "Discharge" });
    await dialog.getByLabel("Outcome").click();
    await page.getByRole("option", { name: "Recovered" }).click();
    await dialog.getByRole("button", { name: "Discharge", exact: true }).click();
    await expect(page.getByText(/Patient discharged/)).toBeVisible({ timeout: 45_000 });
  }
}

/**
 * Discharge a stay the way the ward does, as the hospital admin. Ends signed
 * out, so the next role signs in to a clean session.
 */
export async function dischargeStay(page: Page, url: string): Promise<void> {
  await page.context().clearCookies();
  await signIn(page, ACCOUNTS.admin);
  await dischargeOpenStay(page, url);
  await page.context().clearCookies();
}

/**
 * The accountant's way into a patient's bills: Billing → Accounts → Patients,
 * searched by name. Ends on the patient's bill page.
 */
export async function openPatientBills(page: Page, name: string): Promise<void> {
  await page.goto("/billing");
  await page.getByRole("tab", { name: "Patients" }).click();
  await page.getByRole("searchbox", { name: "Search accounts" }).fill(name);
  const row = page.getByTestId("patient-row").filter({ hasText: name });
  await expect(row).toHaveCount(1, { timeout: 15_000 });
  await row.getByRole("link", { name }).click();
  await expect(page).toHaveURL(/\/billing\/patients\/[0-9a-f-]+$/);
  await expect(page.getByRole("heading", { level: 1, name })).toBeVisible();
}

/**
 * Register the teardown that hands every tracked bed back.
 *
 * Called once at the top of any spec that admits. Releasing in an `afterEach`
 * rather than at the end of each test body is deliberate: a test that fails
 * *after* admitting still gives the bed up, and a leak on the failure path is
 * how the pool drained in the first place.
 */
export function releaseTrackedBeds(): void {
  test.afterEach(async ({ page }) => {
    const stays = bedsTaken;
    bedsTaken = [];
    for (const stay of stays) await releaseBed(page, stay);
  });
}

/**
 * Register a patient through the form and read the UHID back off the slip.
 *
 * Shared by both scan specs because both need the same thing: a real patient
 * and the exact string their card carries. The QR's accessible name *is* the
 * UHID it encodes — which is both the contract the QR feature rests on and the
 * only way to read the payload back out without shipping a decoder into the
 * test suite.
 *
 * Does not sign out, unlike `registerAndQueue`: the scan specs go on to use
 * the same reception session.
 */
export async function registerAndReadSlip(page: Page): Promise<{ name: string; uhid: string }> {
  const patient = uniquePatient();

  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception/register");
  await page.getByLabel("Full name", { exact: true }).fill(patient.name);
  await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
  await page.getByLabel("Age", { exact: true }).fill("41");
  await chooseGender(page, "Female");
  await page.getByLabel("Doctor", { exact: true }).click();
  await page.getByRole("option", { name: /Vikram/ }).click();
  await page.getByRole("button", { name: /Register and send to a doctor/ }).click();
  await expect(page.getByText("Token issued")).toBeVisible({ timeout: 45_000 });

  const uhid = await page
    .getByTestId("token-slip")
    .getByRole("img")
    .first()
    .getAttribute("aria-label");
  expect(uhid, "the slip should carry a QR labelled with the UHID").toBeTruthy();

  return { name: patient.name, uhid: uhid as string };
}

/**
 * Pick a gender on the registration form.
 *
 * The radio input is visually hidden inside its `<label>` — the standard
 * accessible pattern for a button-styled choice: the input stays in the
 * accessibility tree and stays keyboard-operable, while the whole box is the
 * click target. Playwright's `.check()` refuses because the label sits over
 * the input, so this clicks the label, which is what a person does.
 */
export async function chooseGender(page: Page, gender: "Male" | "Female" | "Other"): Promise<void> {
  await page.getByRole("group", { name: "Gender" }).getByText(gender, { exact: true }).click();
  await expect(page.getByRole("radio", { name: gender, exact: true })).toBeChecked();
}

/**
 * A phone number and name unique to this run.
 *
 * The suite runs against a persistent database, so a fixed "9876543210" would
 * trip the duplicate detector on the second run and every run after — the
 * tests would start failing for a reason that has nothing to do with the code.
 */
let counter = 0;

export function uniquePatient(): { name: string; phone: string } {
  // A counter as well as the clock: two calls inside the same millisecond are
  // easy in a single test, and two "unique" patients that turn out to be
  // identical would silently make a duplicate-detection test assert nothing.
  counter += 1;
  const stamp = `${Date.now().toString().slice(-7)}${counter % 10}`;
  return {
    name: `Testpatient ${stamp}`,
    // Indian mobile numbers start 6-9; the backend validates that.
    phone: `9${stamp.padStart(9, "0").slice(0, 9)}`,
  };
}

/**
 * Find a row on a paginated board, paging forward until it appears.
 *
 * The cash counter is ordered by oldest debt first, so a visit created seconds
 * ago is on the last page of a tenant with any history — which is what a
 * cashier faces mid-morning, and what makes the pager load-bearing rather than
 * decorative. Bounded, so a genuinely missing row fails rather than hanging.
 */
export async function findOnBoard(
  page: Page,
  testId: string,
  text: string,
  maxPages = 20,
): Promise<void> {
  const rows = page.getByTestId(testId);

  for (let visited = 0; visited < maxPages; visited += 1) {
    // Let the board render before deciding the row is not on this page.
    // Swallowed on timeout so a genuinely empty board still falls through.
    await rows
      .first()
      .waitFor({ state: "visible", timeout: 15_000 })
      .catch(() => {});

    if (await rows.filter({ hasText: text }).count()) {
      await expect(rows.filter({ hasText: text })).toBeVisible();
      return;
    }

    const next = page.getByRole("button", { name: "Next", exact: true });
    if ((await next.count()) === 0 || (await next.isDisabled())) break;

    // Remember what is on screen, then wait for it to actually change. Without
    // this the loop clicks Next again while React is still showing the old
    // page, skipping straight past the page it was looking for — which fails
    // as "not found on any page" while the row sits one click away.
    const before = (await rows.count()) ? await rows.first().innerText() : "";
    await next.click();
    await expect
      .poll(async () => ((await rows.count()) ? await rows.first().innerText() : ""), {
        timeout: 30_000,
      })
      .not.toBe(before);
  }

  throw new Error(`"${text}" was not found on any page of ${testId}`);
}
