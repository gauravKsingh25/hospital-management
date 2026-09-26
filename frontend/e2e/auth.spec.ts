import { expect, test } from "@playwright/test";

import { ACCOUNTS, PASSWORD, signIn } from "./support";

/**
 * Authentication, and the property the whole BFF design exists for.
 *
 * The last test in this file is the important one: it asserts that no JWT is
 * reachable from the page's JavaScript. If that ever starts failing, the
 * architecture described in `src/lib/session.ts` has been undone and an XSS
 * anywhere in the dependency tree becomes an exfiltration of the hospital's
 * patient list.
 */
test.describe("signing in", () => {
  test("an unauthenticated visitor is sent to the login screen", async ({ page }) => {
    await page.goto("/reception");
    await expect(page).toHaveURL(/\/login/);
  });

  test("it remembers where you were going", async ({ page }) => {
    await page.goto("/reception/register");
    await expect(page).toHaveURL(/next=%2Freception%2Fregister/);

    await page.getByLabel("Email").fill(ACCOUNTS.reception);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();

    // Back to the page they asked for, not to a home screen — the difference
    // between "sign in again" and "sign in again and find that patient again".
    //
    // The longer timeout is for sign-in specifically: argon2 is deliberately
    // slow (CLAUDE.md §12) and Neon's free tier may be cold-starting
    // underneath it (§5). Neither is a failure.
    await expect(page).toHaveURL(/\/reception\/register/, { timeout: 30_000 });
  });

  test("a wrong password says so without saying which half was wrong", async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill(ACCOUNTS.reception);
    await page.getByLabel("Password").fill("not-the-password");
    await page.getByRole("button", { name: "Sign in" }).click();

    // Scoped to the form: Next's own route announcer is also `role="alert"`,
    // so an unscoped query is ambiguous.
    const alert = page.locator("form").getByRole("alert");
    await expect(alert).toBeVisible({ timeout: 30_000 });
    // Naming which half failed tells an attacker which hospital addresses are
    // real, which is step one of a targeted phishing campaign against staff.
    await expect(alert).toContainText("do not match");
  });

  test("each role lands on its own screen", async ({ page }) => {
    await signIn(page, ACCOUNTS.doctor);
    await expect(page).toHaveURL(/\/doctor/);
  });

  test("reception lands on the counter screen", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);
    await expect(page).toHaveURL(/\/reception/);
  });
});

test.describe("the session", () => {
  test("the token is never reachable from page JavaScript", async ({ page, context }) => {
    await signIn(page, ACCOUNTS.reception);

    // Nothing in the browser's own storage.
    const storage = await page.evaluate(() => ({
      local: JSON.stringify(window.localStorage),
      session: JSON.stringify(window.sessionStorage),
    }));
    expect(storage.local).not.toContain("eyJ"); // a JWT always starts "eyJ"
    expect(storage.session).not.toContain("eyJ");

    // And the session cookie is httpOnly, so `document.cookie` cannot see it.
    const readable = await page.evaluate(() => document.cookie);
    expect(readable).not.toContain("hms_session");

    const [session] = (await context.cookies()).filter((c) => c.name === "hms_session");
    expect(session, "the session cookie should exist").toBeTruthy();
    expect(session.httpOnly, "the session cookie must be httpOnly").toBe(true);
    expect(session.sameSite).toBe("Lax");
  });

  test("the browser never talks to the API directly", async ({ page }) => {
    const direct: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      // Anything not served by this origin is a request that escaped the BFF.
      if (url.port === "8000" || url.pathname.startsWith("/api/v1")) direct.push(request.url());
    });

    await signIn(page, ACCOUNTS.reception);
    await page.waitForLoadState("networkidle");

    expect(direct, "browser requests bypassing the BFF proxy").toEqual([]);
  });

  test("signing out ends the session", async ({ page }) => {
    await signIn(page, ACCOUNTS.reception);

    await page.getByRole("button", { name: /Priya/ }).click();
    await page.getByRole("menuitem", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login/);

    // And the back button does not get you back in.
    await page.goto("/reception");
    await expect(page).toHaveURL(/\/login/);
  });
});
