import { expect, test } from "@playwright/test";

import { ACCOUNTS, signIn } from "./support";

/**
 * Reception manages the queue from its own screen — one column per doctor,
 * with Seen, Move and payment on every row. A second "Queue" screen in its
 * sidebar was the same patients in a flatter, less useful shape, so it is
 * gone for anyone who works the Reception screen.
 *
 * Nurses and doctors keep it: for them `/queue` is home — the flat list a
 * nurse works for vitals across every clinic.
 */
const sidebar = (page: import("@playwright/test").Page) =>
  page.getByRole("navigation").filter({ has: page.getByRole("link", { name: /Reception|Queue/ }) });

test("reception has no Queue link, and /queue takes them to Reception", async ({ page }) => {
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception");

  const nav = sidebar(page).first();
  await expect(nav.getByRole("link", { name: "Reception", exact: true })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Queue", exact: true })).toHaveCount(0);

  await page.goto("/queue");
  await expect(page).toHaveURL(/\/reception$/);
});

test("a nurse still has the Queue screen", async ({ page }) => {
  await signIn(page, ACCOUNTS.nurse);
  await page.goto("/queue");

  await expect(page).toHaveURL(/\/queue$/);
  await expect(
    sidebar(page).first().getByRole("link", { name: "Queue", exact: true }),
  ).toBeVisible();
});
