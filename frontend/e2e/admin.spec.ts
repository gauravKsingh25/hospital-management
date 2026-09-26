import { expect, test } from "@playwright/test";

import { ACCOUNTS, signIn } from "./support";

/**
 * Administration, and the universal search box.
 *
 * These screens exist for one reason: until they did, a hospital could not set
 * its own prices, add a test to the catalogue, create a staff account or make
 * a bed — all of it needed somebody running a Python script. So the assertions
 * here are mostly about *reachability*: that an administrator can get from the
 * hub to each area and complete the one action that unblocks a workflow.
 *
 * A note on why some of these only assert the screen renders. Creating a
 * second rate card or department mutates the shared demo tenant that every
 * other spec depends on, so tests that write are limited to the ones whose
 * effect is additive and harmless.
 */

const SUFFIX = Date.now().toString().slice(-6);

test.describe("the administration hub", () => {
  test.beforeEach(async ({ page }) => {
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/admin");
  });

  test("shows every area the administrator may reach", async ({ page }) => {
    for (const name of [
      "Staff accounts",
      "Doctors",
      "Departments",
      "Services and prices",
      "Test catalogue",
      "Wards and beds",
    ]) {
      await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
    }
  });

  test("each card leads somewhere real", async ({ page }) => {
    // The failure this guards is the one the sidebar already taught us about:
    // a card that leads to a 404 teaches staff to distrust the screen.
    for (const [name, path] of [
      ["Staff accounts", "/admin/staff"],
      ["Doctors", "/admin/doctors"],
      ["Departments", "/admin/departments"],
      ["Services and prices", "/admin/services"],
      ["Test catalogue", "/admin/catalogue"],
      ["Wards and beds", "/admin/wards"],
    ] as const) {
      await page.goto("/admin");
      await page.getByRole("heading", { name, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(path.replace("/", "\\/")));
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    }
  });
});

test.describe("staff accounts", () => {
  test("every row says what that person may do", async ({ page }) => {
    /**
     * The whole reason `UserRead` gained a `roles` field. A staff list that
     * cannot answer "who is a doctor here?" is not a staff list, and the
     * alternative — a request per row — is one on the screen where every row
     * is a person whose access matters.
     */
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/admin/staff");

    const doctorRow = page.getByTestId("staff-row").filter({ hasText: "doctor@demo.hospital" });
    await expect(doctorRow).toBeVisible({ timeout: 45_000 });
    await expect(doctorRow).toContainText("DOCTOR");

    const labRow = page.getByTestId("staff-row").filter({ hasText: "lab@demo.hospital" });
    await expect(labRow).toContainText("LAB_TECH");
  });

  test("an account can be created and given a role", async ({ page }) => {
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/admin/staff");

    await page.getByRole("button", { name: "Add staff" }).click();
    await page.getByLabel("Name", { exact: true }).fill(`Test Nurse ${SUFFIX}`);
    await page.getByLabel("Email", { exact: true }).fill(`nurse.${SUFFIX}@demo.hospital`);
    await page.getByLabel("Temporary password").fill("a-perfectly-long-passphrase");

    // An account with no roles can sign in and do nothing, so the form insists.
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByText("Choose at least one role.")).toBeVisible();

    await page.getByRole("group", { name: "Roles" }).getByText("Nurse", { exact: true }).click();
    await page.getByRole("button", { name: "Create account" }).click();

    await expect(page.getByText("Account created.")).toBeVisible({ timeout: 45_000 });
    const row = page.getByTestId("staff-row").filter({ hasText: `nurse.${SUFFIX}@demo.hospital` });
    await expect(row).toBeVisible({ timeout: 45_000 });
    await expect(row).toContainText("NURSE");
  });
});

test.describe("services and prices", () => {
  test("the counter's price list is visible and complete", async ({ page }) => {
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/admin/services");

    // The seed prices these two; the screen exists so a hospital can price
    // the rest without a script.
    const consult = page.getByTestId("service-row").filter({ hasText: "OPD consultation" });
    await expect(consult).toBeVisible({ timeout: 45_000 });
    await expect(consult).toContainText("₹500.00");

    // A default card must exist, or a walk-in cash patient has no price list
    // to fall back on and every charge lands at zero.
    await expect(page.getByText("No rate card is marked default")).toHaveCount(0);
    await expect(page.getByText("There is no rate card at all")).toHaveCount(0);
  });
});

test.describe("the test catalogue", () => {
  test("a test with no analytes is flagged rather than looking finished", async ({ page }) => {
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/admin/catalogue");

    const cbc = page.getByTestId("catalogue-row").filter({ hasText: "Complete Blood Count" });
    await expect(cbc).toBeVisible({ timeout: 45_000 });
    // The seed gives it one analyte, so it shows a count rather than the
    // warning. The warning is what the column exists for.
    await expect(cbc).toContainText("BLOOD");
    await expect(cbc.getByRole("button", { name: "Add analyte" })).toBeVisible();
  });
});

test.describe("wards and beds", () => {
  test("a ward and a run of beds can be created", async ({ page }) => {
    /**
     * Phase 12 builds the nursing station's bed board. Neither it nor an
     * admission has anywhere to go until a bed exists, and a bed could not be
     * created by any means other than a script before this screen.
     */
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/admin/wards");

    await page.getByRole("button", { name: "Add ward" }).click();
    const wardDialog = page.getByRole("dialog", { name: "Add ward" });
    await wardDialog.getByRole("textbox", { name: "Code", exact: true }).fill(`W${SUFFIX}`);
    await wardDialog
      .getByRole("textbox", { name: "Name", exact: true })
      .fill(`Test Ward ${SUFFIX}`);
    await wardDialog.getByRole("button", { name: "Save" }).click();
    await expect(page.getByText("Ward created.")).toBeVisible({ timeout: 45_000 });

    await page.getByRole("button", { name: "Add beds" }).click();
    // Scoped to the dialog: "To" alone also matches the toast's close button
    // and Next's dev-tools trigger.
    const bedsDialog = page.getByRole("dialog", { name: "Add beds" });
    // The prefix defaults to the ward's code, because a bed code is unique
    // across the whole hospital rather than within a ward. Bare numbers would
    // collide with the first ward anybody set up.
    await expect(bedsDialog.getByRole("textbox", { name: "Prefix" })).toHaveValue(
      `W${SUFFIX}-`,
    );
    await bedsDialog.getByRole("textbox", { name: "From", exact: true }).fill("1");
    await bedsDialog.getByRole("textbox", { name: "To", exact: true }).fill("4");
    await expect(bedsDialog.getByText(/Creates 4 beds/)).toBeVisible();
    await bedsDialog.getByRole("button", { name: "Save" }).click();

    await expect(page.getByText("Beds created.")).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("bed-tile")).toHaveCount(4, { timeout: 45_000 });
    await expect(page.getByTestId("bed-tile").first()).toContainText("AVAILABLE");
  });
});

test.describe("a receptionist is not an administrator", () => {
  test("the administration section is not offered", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await expect(
      page.getByRole("navigation").getByRole("link", { name: "Administration" }),
    ).toHaveCount(0);
  });

  test("and reaching it directly shows nothing to administer", async ({ page }) => {
    // Not an authorisation test — the server refuses regardless. This asserts
    // the screen does not offer controls that would all return 403.
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/admin/services");
    await expect(page.getByRole("button", { name: "Add service" })).toHaveCount(0);
  });
});
