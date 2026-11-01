import { expect, test } from "@playwright/test";

import { ACCOUNTS, PASSWORD, signIn } from "./support";

/**
 * SEC-04: a shared tablet locks itself when nobody is using it.
 *
 * The clock is Playwright's, so ten idle minutes pass in a moment. What is
 * pinned: the lock hides the page completely, the same person gets their page
 * back with their password, a reload does not get past it, and "Switch user"
 * hands the tablet over.
 */

async function idleUntilLocked(page: import("@playwright/test").Page): Promise<void> {
  await page.clock.fastForward("11:00");
  await expect(page.getByTestId("idle-lock")).toBeVisible({ timeout: 15_000 });
}

test.describe("the idle lock", () => {
  test("locks after ten idle minutes and gives the same page back", async ({ page }) => {
    await page.clock.install();
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/rounds");
    const heading = page.getByRole("heading", { level: 1 });
    await expect(heading).toBeVisible({ timeout: 30_000 });
    const title = await heading.textContent();

    await idleUntilLocked(page);
    // Opaque and covering the whole screen: the round underneath is not
    // readable while locked.
    const cover = await page.getByTestId("idle-lock").evaluate((element) => {
      const box = element.getBoundingClientRect();
      const colour = getComputedStyle(element).backgroundColor;
      return { width: box.width, height: box.height, colour };
    });
    const viewport = page.viewportSize();
    expect(cover.width).toBe(viewport?.width);
    expect(cover.height).toBe(viewport?.height);
    // Not see-through: no alpha below 1, not transparent.
    expect(cover.colour).not.toMatch(/rgba\(.*,\s*0(\.\d+)?\)|transparent/);
    // The page under the lock is the same page, still there.
    await expect(page).toHaveURL(/\/rounds$/);

    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Unlock" }).click();
    await expect(page.getByTestId("idle-lock")).toBeHidden({ timeout: 30_000 });
    await expect(page).toHaveURL(/\/rounds$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(title ?? "");
  });

  test("a reload does not get past the lock", async ({ page }) => {
    await page.clock.install();
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/rounds");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 30_000 });

    await idleUntilLocked(page);
    // Give the lock a moment to end the session on the server.
    await expect(async () => {
      await page.reload();
      await expect(page).toHaveURL(/\/login/, { timeout: 5_000 });
    }).toPass({ timeout: 30_000 });
  });

  test("switch user hands the tablet over", async ({ page }) => {
    await page.clock.install();
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/rounds");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 30_000 });

    await idleUntilLocked(page);
    await page.getByRole("button", { name: "Switch user" }).click();
    await expect(page).toHaveURL(/\/login/, { timeout: 30_000 });
  });

  test("a tablet in use does not lock", async ({ page }) => {
    await page.clock.install();
    await signIn(page, ACCOUNTS.nurse);
    await page.goto("/rounds");
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible({ timeout: 30_000 });

    for (let minute = 0; minute < 3; minute += 1) {
      await page.clock.fastForward("04:00");
      await page.mouse.click(5, 300);
    }
    await page.clock.fastForward("00:30");
    await expect(page.getByTestId("idle-lock")).toBeHidden();
  });
});

test("another site cannot lock or unlock a tablet", async ({ page }) => {
  await signIn(page, ACCOUNTS.nurse);
  for (const path of ["/auth/lock", "/auth/unlock"]) {
    const response = await page.request.post(path, {
      headers: { "sec-fetch-site": "cross-site", origin: "https://evil.example" },
      data: { email: "nurse@demo.hospital", password: "x" },
    });
    expect(response.status(), path).toBe(403);
  }
});
