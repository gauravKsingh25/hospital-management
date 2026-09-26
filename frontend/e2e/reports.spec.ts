import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, registerAndQueue, signIn } from "./support";

/**
 * The KPI dashboards.
 *
 * The property that carries the weight here is **who sees which numbers**.
 * CLAUDE.md §8 splits reporting into three permissions on purpose — a single
 * `report:read` would have meant that giving a nurse the bed board also handed
 * her the hospital's monthly collections — and the whole point of returning one
 * dashboard shaped by permission is that the split is invisible to the user and
 * absolute to the server. Four of these tests are that matrix, asserted by role.
 *
 * The rest check the two things a dashboard has to get right to be worth
 * opening: the numbers move when the hospital does, and the chart actually
 * loads (it is behind a dynamic import, so a broken lazy chunk would show a
 * skeleton forever and nothing would fail at build time).
 */

/** The `data-section` of every panel the dashboard can render. */
const SECTIONS = {
  today: "report-today",
  footfall: "report-footfall",
  occupancy: "report-occupancy",
  inpatient: "report-inpatient",
  followUp: "report-follow-up",
  revenue: "report-revenue",
} as const;

async function openReports(page: Page, account: string): Promise<void> {
  await signIn(page, account);
  await page.goto("/reports");
  await expect(page.getByRole("heading", { name: "Reports", level: 1 })).toBeVisible({
    timeout: 45_000,
  });
}

function section(page: Page, name: string) {
  return page.locator(`[data-section="${name}"]`);
}

test.describe("who sees which numbers", () => {
  test("a nurse gets operations and is shown no money at all", async ({ page }) => {
    await openReports(page, ACCOUNTS.nurse);

    await expect(section(page, SECTIONS.today)).toBeVisible();
    await expect(section(page, SECTIONS.occupancy)).toBeVisible();
    await expect(section(page, SECTIONS.footfall)).toBeVisible();

    // The reason the permission is split three ways rather than one.
    await expect(section(page, SECTIONS.revenue)).toHaveCount(0);
    await expect(section(page, SECTIONS.inpatient)).toHaveCount(0);
    await expect(section(page, SECTIONS.followUp)).toHaveCount(0);
  });

  test("a cashier gets money and no clinical outcomes", async ({ page }) => {
    await openReports(page, ACCOUNTS.cashier);

    await expect(section(page, SECTIONS.revenue)).toBeVisible();
    // Earned, collected and outstanding are three separate tiles. They are
    // never summed, and the screen never offers a total.
    await expect(page.getByTestId("stat-earned")).toBeVisible();
    await expect(page.getByTestId("stat-collected")).toBeVisible();
    await expect(page.getByTestId("stat-outstanding")).toBeVisible();

    await expect(section(page, SECTIONS.inpatient)).toHaveCount(0);
    await expect(section(page, SECTIONS.followUp)).toHaveCount(0);
  });

  test("a doctor gets clinical outcomes and no money", async ({ page }) => {
    await openReports(page, ACCOUNTS.doctor);

    await expect(section(page, SECTIONS.inpatient)).toBeVisible();
    await expect(section(page, SECTIONS.followUp)).toBeVisible();
    await expect(section(page, SECTIONS.revenue)).toHaveCount(0);
  });

  test("an administrator gets every section", async ({ page }) => {
    await openReports(page, ACCOUNTS.admin);

    for (const name of Object.values(SECTIONS)) {
      await expect(section(page, name)).toBeVisible();
    }
  });
});

test.describe("the dashboard", () => {
  test("counts a patient who was registered a moment ago", async ({ page }) => {
    /**
     * The dashboard reads views over the operational tables rather than a copy
     * of them, so a patient who reaches the queue is in the numbers on the next
     * refresh. This is also the test that would have caught the queue's date
     * bug: `check_in` stored a UTC date while the report asked for the
     * hospital's local one, so every evening's arrivals were counted under
     * yesterday and today's board read zero.
     */
    await registerAndQueue(page);

    await openReports(page, ACCOUNTS.admin);
    const waiting = page.getByTestId("stat-waiting");
    await expect(waiting).toBeVisible();
    await expect(waiting).not.toHaveText("0");

    // And in the window's totals, not only in today's live strip.
    await expect(page.getByTestId("stat-visits")).not.toHaveText("0");
  });

  test("draws the footfall trend", async ({ page }) => {
    // The chart is behind `next/dynamic`, which nothing at build time can
    // prove resolves. If the chunk fails to load the skeleton simply stays.
    await openReports(page, ACCOUNTS.admin);
    await expect(page.locator(".recharts-wrapper")).toBeVisible({ timeout: 45_000 });
  });

  test("narrowing the window refetches the report", async ({ page }) => {
    await openReports(page, ACCOUNTS.admin);

    const dates = section(page, SECTIONS.footfall).locator("p").first();
    const beforeText = await dates.innerText();

    await page.getByTestId("window-7").click();
    // The window is rendered from the response, so a changed date range means
    // the server answered the new question rather than the client relabelling
    // a cached one.
    await expect(dates).not.toHaveText(beforeText, { timeout: 45_000 });
  });

  test("is reachable from the sidebar by a role that holds the permission", async ({ page }) => {
    await signIn(page, ACCOUNTS.nurse);
    await page.getByRole("link", { name: "Reports" }).click();
    await expect(page).toHaveURL(/\/reports/);
    await expect(page.getByRole("heading", { name: "Reports", level: 1 })).toBeVisible({
      timeout: 45_000,
    });
  });
});
