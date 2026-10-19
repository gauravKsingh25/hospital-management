import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, registerAndQueue, signIn, signOut } from "./support";

/**
 * Administration → Hospital profile: the letterhead on every bill — logo,
 * name, address, GSTIN — and the UPI ID every reception QR pays.
 *
 * The demo tenant is shared by every spec. The UPI ID is put back after each
 * test (in `afterEach`, which still runs when a test fails); the address and
 * GSTIN written here are the demo hospital's own, so leaving them is harmless
 * and makes every later bill print properly.
 */
const SEEDED_UPI = "demo.hospital@example";
const CHANGED_UPI = "demo.counter@example";
const ADDRESS = "12 MG Road, Civil Lines";
const GSTIN = "09AAACD1234F1Z5";
const LOGO = path.join(__dirname, "fixtures", "logo.png");

async function saveUpi(page: Page, value: string): Promise<void> {
  await page.goto("/admin/hospital");
  await page.getByLabel("UPI ID", { exact: true }).fill(value);
  await page.getByRole("button", { name: "Save profile" }).click();
}

test.afterEach(async ({ page }) => {
  await page.context().clearCookies();
  await signIn(page, ACCOUNTS.admin);
  await page.goto("/admin/hospital");
  const field = page.getByLabel("UPI ID", { exact: true });
  if ((await field.inputValue()) !== SEEDED_UPI) {
    await saveUpi(page, SEEDED_UPI);
    await expect(page.getByText("Hospital profile saved.")).toBeVisible();
  }
});

test("an administrator sets the letterhead and logo", async ({ page }) => {
  await signIn(page, ACCOUNTS.admin);

  // Reachable from the hub; the old Payments address still lands here.
  await page.goto("/admin");
  await page.getByRole("heading", { name: "Hospital profile", exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/hospital$/);
  await page.goto("/admin/payments");
  await expect(page).toHaveURL(/\/admin\/hospital$/);

  // Line 2 differs on every run, so Save always has something to save.
  const block = `Block ${Date.now() % 100_000}`;
  await page.getByLabel("Address line 1").fill(ADDRESS);
  await page.getByLabel("Address line 2").fill(block);
  await page.getByLabel("GSTIN").fill(GSTIN);
  await page.getByRole("button", { name: "Save profile" }).click();
  await expect(page.getByText("Hospital profile saved.")).toBeVisible();

  await page.getByLabel("Upload logo").setInputFiles(LOGO);
  await expect(page.getByText("Logo uploaded.")).toBeVisible();
  const preview = page.getByRole("img", { name: "Hospital logo" });
  await expect(preview).toBeVisible();
  // The preview is the stored file, fetched back through the app.
  await expect
    .poll(() => preview.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth))
    .toBeGreaterThan(0);

  await page.reload();
  await expect(page.getByLabel("Address line 1")).toHaveValue(ADDRESS);
  await expect(page.getByLabel("Address line 2")).toHaveValue(block);
  await expect(page.getByLabel("GSTIN")).toHaveValue(GSTIN);
  await expect(page.getByRole("img", { name: "Hospital logo" })).toBeVisible();
});

test("a file that is not a PNG or JPEG is refused", async ({ page }) => {
  await signIn(page, ACCOUNTS.admin);
  await page.goto("/admin/hospital");

  await page.getByLabel("Upload logo").setInputFiles({
    name: "logo.gif",
    mimeType: "image/png",
    buffer: Buffer.from("GIF89a\x01\x00\x01\x00\x00\x00\x00;", "binary"),
  });

  await expect(page.getByText("The logo must be a PNG or JPEG image.")).toBeVisible();
});

test("the UPI ID set here is what reception's QR pays", async ({ page }) => {
  // Registering a patient through the form is most of the minute.
  test.setTimeout(120_000);
  await signIn(page, ACCOUNTS.admin);
  await page.goto("/admin/hospital");
  await expect(page.getByLabel("UPI ID", { exact: true })).toHaveValue(SEEDED_UPI);

  // A typo is refused at the form, not discovered by a patient.
  await saveUpi(page, "demo hospital");
  await expect(page.getByText(/looks like name@bank/)).toBeVisible();

  await saveUpi(page, CHANGED_UPI);
  await expect(page.getByText("Hospital profile saved.")).toBeVisible();
  // The test QR, so the admin can scan it before any patient does.
  await expect(page.getByRole("img", { name: new RegExp(CHANGED_UPI) })).toBeVisible();
  await signOut(page);

  const name = await registerAndQueue(page);
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception");
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.getByTestId("get-payment").click();
  await expect(page.getByRole("dialog").getByText(CHANGED_UPI)).toBeVisible();
});
