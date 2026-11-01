import { createHmac } from "node:crypto";

import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, PASSWORD, signIn, signOut } from "./support";

/**
 * SEC-05: two-step sign-in with an authenticator app.
 *
 * Run on a throwaway account the test creates, so no demo account is ever
 * left with two-step on behind a secret only this test knew. The test plays
 * the authenticator app itself — RFC 6238, the same arithmetic the phone does.
 */

function base32Decode(text: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of text.replace(/[\s=]/g, "").toUpperCase()) {
    bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let index = 0; index + 8 <= bits.length; index += 8) {
    bytes.push(parseInt(bits.slice(index, index + 8), 2));
  }
  return Buffer.from(bytes);
}

/** The code an authenticator shows, `offsetSteps` windows from now. */
function totp(secret: string, offsetSteps = 0): string {
  const step = Math.floor(Date.now() / 1000 / 30) + offsetSteps;
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const digest = createHmac("sha1", base32Decode(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const value = (digest.readUInt32BE(offset) & 0x7fffffff) % 1_000_000;
  return String(value).padStart(6, "0");
}

async function signInAs(page: Page, email: string, password: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
}

/** Sign the throwaway user out: their menu is named after them, not a demo account. */
async function signOutTestUser(page: Page): Promise<void> {
  await page
    .getByRole("button", { name: "Two Step Test" })
    .and(page.locator("[aria-haspopup=menu]"))
    .click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login/);
}

test("someone turns two-step sign-in on, signs in with a code, and is remembered", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const email = `twostep.${Date.now()}@demo.hospital`;
  const password = `${PASSWORD}-${Date.now()}`;

  // A throwaway receptionist, created by the administrator.
  await signIn(page, ACCOUNTS.admin);
  const created = await page.request.post("/bff/users", {
    data: { email, full_name: "Two Step Test", password, role_codes: ["RECEPTIONIST"] },
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  await signOut(page);

  // First sign-in: a new account sets its own password.
  await signInAs(page, email, password);
  await expect(page).toHaveURL(/\/change-password/, { timeout: 30_000 });
  const fresh = `${password}-new`;
  await page.getByLabel("Current password").fill(password);
  await page.getByLabel("New password", { exact: true }).fill(fresh);
  await page.getByLabel("Confirm new password").fill(fresh);
  await page.getByRole("button", { name: /password/i }).click();
  // A changed password ends the session; sign in again with the new one.
  await expect(page).toHaveURL(/\/login/, { timeout: 30_000 });
  await signInAs(page, email, fresh);
  await expect(page).not.toHaveURL(/\/login/, { timeout: 30_000 });

  // Turn it on: scan (here, read) the secret, prove the app works.
  await page.goto("/account/two-factor");
  await page.getByRole("button", { name: "Set up" }).click();
  const secret = ((await page.getByTestId("totp-secret").textContent()) ?? "").replace(/\s/g, "");
  expect(secret.length).toBeGreaterThanOrEqual(32);
  await page.getByLabel("Six-digit code").fill(totp(secret));
  await page.getByRole("button", { name: "Turn on" }).click();
  await expect(page.getByTestId("two-factor-status")).toHaveText("Two-step sign-in is on.", {
    timeout: 30_000,
  });
  await signOutTestUser(page);

  // Next sign-in asks for the code; a wrong one is refused.
  await signInAs(page, email, fresh);
  await expect(page).toHaveURL(/\/login\/two-factor$/, { timeout: 30_000 });
  await page.getByLabel("Six-digit code").fill("000000");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("That code is not right.")).toBeVisible({ timeout: 30_000 });

  // The next window's code: the current one was spent turning it on.
  await page.getByLabel("Six-digit code").fill(totp(secret, 1));
  await page.getByLabel("Remember this device for 12 hours").check();
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login/, { timeout: 30_000 });
  await signOutTestUser(page);

  // A remembered device goes straight in.
  await signInAs(page, email, fresh);
  await expect(page).not.toHaveURL(/\/login/, { timeout: 30_000 });
});

test("the code page cannot be opened without a password first", async ({ page }) => {
  await page.goto("/login/two-factor");
  await expect(page).toHaveURL(/\/login$/);
  await page.goto("/login/two-factor/setup");
  await expect(page).toHaveURL(/\/login$/);
});
