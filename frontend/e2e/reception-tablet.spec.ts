import { expect, test } from "@playwright/test";

import { ACCOUNTS, signIn } from "./support";

/**
 * The app on a portrait tablet (768 × 1024), the device in the UX-01/UX-02
 * reports.
 *
 * The sidebar used to appear at 768 px and leave the page ~520 px: two doctor
 * panes a quarter-screen wide, names broken after two letters, and every page
 * a few pixels wider than the screen. Below 1024 px the sidebar is now the
 * menu button, and the board draws one doctor at a time.
 */
test.use({ viewport: { width: 768, height: 1024 } });

test("a portrait tablet shows one readable doctor pane, switched by the chips", async ({
  page,
}) => {
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception");
  const board = page.getByTestId("doctor-queues");
  await expect(board).toBeVisible({ timeout: 30_000 });

  const visible = board.getByTestId("doctor-queue").filter({ visible: true });
  await expect(visible).toHaveCount(1);
  const box = await visible.boundingBox();
  // Wide enough that "Unknown Male EM-0034" fits on a line.
  expect(box?.width ?? 0).toBeGreaterThan(600);

  // The emergency door is on screen without scrolling.
  await expect(page.getByTestId("emergency-link")).toBeInViewport();

  const chips = page.getByTestId("doctor-chip");
  test.skip((await chips.count()) < 2, "needs two doctors to switch between");
  const current = (await visible.textContent()) ?? "";
  for (let index = 0; index < (await chips.count()); index += 1) {
    const chip = chips.nth(index);
    const name = (await chip.locator("span").first().textContent()) ?? "";
    if (name && !current.includes(name)) {
      await chip.click();
      await expect(visible).toHaveCount(1);
      await expect(visible).toContainText(name);
      return;
    }
  }
});

test("the menu replaces the sidebar, and nothing scrolls sideways", async ({ page }) => {
  await signIn(page, ACCOUNTS.admin);

  for (const path of [
    "/reception",
    "/reception/register",
    "/reception/emergency",
    "/queue",
    "/admissions",
    "/lab",
    "/billing",
    "/wards",
    "/rounds",
    "/patients",
    "/messages",
    "/reports",
    "/admin",
  ]) {
    await page.goto(path);
    await page.getByRole("main").waitFor();
    // Let the client-fetched boards draw before measuring them.
    await page.waitForLoadState("load");
    await page.waitForTimeout(1500);

    await expect(page.getByRole("button", { name: "Open menu" }), path).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(overflow, `${path} scrolls sideways by ${overflow}px`).toBeLessThanOrEqual(0);
  }
});

test("every control is a full tap target and no text is below 13 px", async ({ page }) => {
  // UX-03: counter and ward staff tap with a finger and read at arm's length.
  await signIn(page, ACCOUNTS.reception);

  for (const path of ["/reception", "/reception/register", "/reception/emergency"]) {
    await page.goto(path);
    await page.waitForLoadState("load");
    await page.waitForTimeout(1500);

    const problems = await page.evaluate(() => {
      const found: string[] = [];
      const controls = document.querySelectorAll<HTMLElement>(
        "a, button, [role=button], [role=combobox], [role=tab], input:not([type=hidden]):not([type=checkbox]):not([type=radio])",
      );
      for (const element of Array.from(controls)) {
        const box = element.getBoundingClientRect();
        if (box.width === 0 || box.height === 0 || element.closest("[aria-hidden=true]")) continue;
        if (box.height < 44 || box.width < 44) {
          found.push(`${Math.round(box.width)}x${Math.round(box.height)} ${element.textContent?.trim().slice(0, 30)}`);
        }
      }
      for (const element of Array.from(document.querySelectorAll<HTMLElement>("main *"))) {
        const hasText = [...element.childNodes].some(
          (node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim(),
        );
        if (!hasText || element.getBoundingClientRect().width === 0) continue;
        const size = parseFloat(getComputedStyle(element).fontSize);
        if (size < 13) found.push(`${size}px text "${element.textContent?.trim().slice(0, 30)}"`);
      }
      return found;
    });
    expect(problems, path).toEqual([]);
  }
});
