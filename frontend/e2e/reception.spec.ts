import { expect, test } from "@playwright/test";

import { ACCOUNTS, chooseGender, signIn, uniquePatient } from "./support";

/**
 * The counter workflow, end to end (CLAUDE.md §7b).
 *
 * The acceptance gate for this module is a registration in under thirty
 * seconds, and the last test in this file measures it rather than trusting
 * that the design achieved it. That number is the module's definition of done
 * (§15), so it belongs in a test that fails when it regresses — not in a
 * paragraph of a README.
 */
test.describe("registering a patient", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/reception/register");
  });

  test("the whole form is four fields", async ({ page }) => {
    // Anything beyond these four must be optional and out of the way (§7b).
    await expect(page.getByLabel("Full name", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Mobile number", { exact: true })).toBeVisible();
    await expect(page.getByLabel("Age", { exact: true })).toBeVisible();
    await expect(page.getByRole("group", { name: "Gender" })).toBeVisible();

    // Address and guardian exist but are collapsed.
    await expect(page.getByLabel("Address", { exact: true })).toBeHidden();
    await expect(page.getByRole("button", { name: /Add address/ })).toBeVisible();
  });

  test("the cursor starts in the first field", async ({ page }) => {
    // A counter clerk should begin by typing, not by aiming a mouse.
    await expect(page.getByLabel("Full name", { exact: true })).toBeFocused();
  });

  test("a patient is registered and given a token in one action", async ({ page }) => {
    const patient = uniquePatient();

    await page.getByLabel("Full name", { exact: true }).fill(patient.name);
    await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
    await page.getByLabel("Age", { exact: true }).fill("42");
    await chooseGender(page, "Female");

    // Choosing a doctor turns registration into §7b's one-click OPD: the
    // appointment, the check-in, the token and the encounter all happen from
    // this one button.
    await page.getByLabel("Doctor", { exact: true }).click();
    await page.getByRole("option", { name: /Vikram/ }).click();

    await page.getByRole("button", { name: /Register and send to a doctor/ }).click();

    // The token slip, with everything needed to direct the patient.
    await expect(page.getByText("Token issued")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/Dr Vikram Rao/)).toBeVisible();

    // The token number is the largest thing on screen because it gets read
    // out across a counter.
    const token = page.locator("p.text-7xl");
    await expect(token).toBeVisible();
    await expect(token).toHaveText(/^\d+$/);
  });

  test("a likely duplicate is surfaced before the record is created", async ({ page }) => {
    const patient = uniquePatient();

    // Register once.
    await page.getByLabel("Full name", { exact: true }).fill(patient.name);
    await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
    await page.getByLabel("Age", { exact: true }).fill("30");
    await chooseGender(page, "Male");
    await page.getByRole("button", { name: "Register", exact: true }).click();
    await expect(page.getByRole("main").getByText(/registered as/)).toBeVisible({
      timeout: 30_000,
    });

    // Then try the same person again.
    await page.goto("/reception/register");
    await page.getByLabel("Full name", { exact: true }).fill(patient.name);
    await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
    await page.getByLabel("Age", { exact: true }).fill("30");

    const warning = page.getByTestId("duplicate-warning");
    await expect(warning).toBeVisible({ timeout: 30_000 });
    // The reason is shown in words. Staff can act on "same mobile"; nobody
    // can calibrate a confidence score.
    await expect(warning).toContainText(/mobile|name/i);

    // Same name AND same mobile is an identity collision, so this one blocks
    // until somebody actively says these are different people.
    await expect(warning).toHaveAttribute("data-blocking", "true");
    const submit = page.getByRole("button", { name: "Register", exact: true });
    await expect(submit).toBeDisabled();

    await warning.getByRole("checkbox").check();
    await expect(submit).toBeEnabled();
  });

  test("a shared name alone warns but does not block", async ({ page }) => {
    // The distinction that keeps the check meaningful. In a hospital full of
    // Sharmas and Kumars, blocking on name similarity would have staff
    // confirming "different person" all day, and a box everyone ticks by
    // reflex protects nobody.
    const first = uniquePatient();
    const second = uniquePatient();

    await page.getByLabel("Full name", { exact: true }).fill(first.name);
    await page.getByLabel("Mobile number", { exact: true }).fill(first.phone);
    await page.getByLabel("Age", { exact: true }).fill("28");
    await chooseGender(page, "Female");
    await page.getByRole("button", { name: "Register", exact: true }).click();
    await expect(page.getByRole("main").getByText(/registered as/)).toBeVisible({
      timeout: 30_000,
    });

    // A similar name, a different mobile: a hint, not a collision.
    await page.goto("/reception/register");
    await page.getByLabel("Full name", { exact: true }).fill(first.name);
    await page.getByLabel("Mobile number", { exact: true }).fill(second.phone);
    await page.getByLabel("Age", { exact: true }).fill("28");

    const warning = page.getByTestId("duplicate-warning");
    await expect(warning).toBeVisible({ timeout: 30_000 });
    await expect(warning).toHaveAttribute("data-blocking", "false");
    await expect(page.getByRole("button", { name: "Register", exact: true })).toBeEnabled();
  });

  test("a bad mobile number is caught before the server is asked", async ({ page }) => {
    await page.getByLabel("Full name", { exact: true }).fill("Test Patient");
    await page.getByLabel("Mobile number", { exact: true }).fill("12345");
    await page.getByLabel("Age", { exact: true }).fill("30");
    await page.getByLabel("Mobile number", { exact: true }).blur();

    await expect(page.getByRole("alert").filter({ hasText: /valid 10-digit/ })).toBeVisible();
  });
});

test.describe("the §7b speed gate", () => {
  test("a registration takes under 30 seconds", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);

    // The clock starts where a receptionist's does: at the counter screen,
    // with a patient in front of them. Sign-in is not part of the task.
    await page.goto("/reception");
    const patient = uniquePatient();

    const started = Date.now();

    // F2 — the shortcut §7b asks for, and the fastest way in.
    await page.keyboard.press("F2");
    await expect(page.getByLabel("Full name", { exact: true })).toBeFocused({ timeout: 15_000 });

    // Typed the way a clerk types: keyboard only, no mouse, tabbing between
    // fields. If this needs a pointer, the gate is not really being met.
    await page.keyboard.type(patient.name);
    await page.keyboard.press("Tab");
    await page.keyboard.type(patient.phone);
    await page.keyboard.press("Tab");
    await page.keyboard.type("35");

    await chooseGender(page, "Male");
    await page.getByRole("button", { name: "Register", exact: true }).click();
    await expect(page.getByRole("main").getByText(/registered as/)).toBeVisible({
      timeout: 30_000,
    });

    const elapsed = (Date.now() - started) / 1000;
    console.log(`registration completed in ${elapsed.toFixed(1)}s`);

    /**
     * The gate from CLAUDE.md §7b and §15, measured end to end against a real
     * database — so it includes real network time, which is the point.
     *
     * If this fails, check the *database's* round-trip latency before
     * touching the UI. Registration costs roughly nine SQL statements, and
     * every one of them is a round trip: at 60ms that is well under a second,
     * at 870ms (which is what a Neon project in `us-east-2` costs from India)
     * it is eight seconds before the backend has done any thinking. No amount
     * of frontend work recovers that. See the README's note on region choice.
     *
     * Overridable so a deliberately distant environment can still run the
     * rest of the suite without a permanent red mark.
     */
    const budget = Number(process.env.E2E_REGISTRATION_BUDGET_SECONDS ?? 30);
    expect(
      elapsed,
      `registration took ${elapsed.toFixed(1)}s against a ${budget}s budget — ` +
        "if this is far over, measure the database round-trip latency first",
    ).toBeLessThan(budget);
  });
});
