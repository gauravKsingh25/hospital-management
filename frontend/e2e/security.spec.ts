import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, chooseGender, signIn, uniquePatient } from "./support";

/**
 * The browser-side security headers, and the things they quietly broke.
 *
 * These exist because a Content-Security-Policy failure is silent by
 * construction: the browser refuses something, the page keeps rendering, and
 * nobody finds out until a user says a screen "looks wrong". That is exactly
 * what happened here — the policy blocked Sonner's stylesheet and every toast
 * in the application rendered unstyled and in the document flow, for as long
 * as toasts have existed, while the whole e2e suite stayed green because a
 * toast's *text* is present either way.
 *
 * So the tests below assert the two halves separately: that the policy says
 * what we think it says, and that the things it governs actually look right.
 */

/** Collect CSP violations the page reports, with their source. */
async function watchViolations(page: Page): Promise<() => Promise<string[]>> {
  await page.addInitScript(() => {
    (window as unknown as { __csp: string[] }).__csp = [];
    document.addEventListener("securitypolicyviolation", (event) => {
      (window as unknown as { __csp: string[] }).__csp.push(
        `${event.violatedDirective} | ${event.sourceFile}`,
      );
    });
  });
  return () => page.evaluate(() => (window as unknown as { __csp: string[] }).__csp ?? []);
}

test.describe("the content security policy", () => {
  test("uses a nonce, and does not claim to allow inline styles", async ({ page }) => {
    /**
     * The regression that hid a real bug for months.
     *
     * `style-src` used to read `'self' 'nonce-…' 'unsafe-inline'`, which looks
     * permissive and is not: the CSP spec says a nonce in a directive makes
     * the browser **ignore** `'unsafe-inline'` in that same directive. Chrome
     * says so in the violation text itself. Listing it described a permission
     * the browser never granted, so anyone reading the policy would conclude
     * that a blocked inline style was impossible.
     */
    const response = await page.goto("/login");
    const csp = response?.headers()["content-security-policy"] ?? "";

    expect(csp, "a CSP must be sent at all").not.toBe("");

    const styleSrc = csp.split(";").map((part) => part.trim()).find((p) => p.startsWith("style-src"));
    expect(styleSrc).toBeTruthy();
    expect(styleSrc).toMatch(/'nonce-[^']+'/);
    expect(styleSrc, "a nonce makes 'unsafe-inline' inert — listing it only misleads").not.toContain(
      "'unsafe-inline'",
    );

    // The rest of the header, asserted because each one is load-bearing and
    // each is a single word away from being switched off by accident.
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("connect-src 'self'");
    expect(response?.headers()["x-content-type-options"]).toBe("nosniff");
    expect(response?.headers()["referrer-policy"]).toBe("no-referrer");
  });

  test("blocks no inline style except the one library that injects its own", async ({ page }) => {
    /**
     * A budget rather than a blanket ban, because one violation is expected
     * and permanent: Sonner injects its stylesheet at runtime with a bare
     * `createElement("style")` that carries no nonce, so CSP refuses it. That
     * is correct behaviour on both sides — and harmless, because `globals.css`
     * now imports the same stylesheet through the bundler, where it is served
     * from `self` and trusted.
     *
     * The budget is what makes this worth having: any *new* source of blocked
     * inline styles pushes the count over and fails here, instead of being
     * lost in the noise the way the Sonner one was.
     *
     * Next's development overlay is filtered out rather than counted. It is
     * responsible for about thirty violations a page load, it is the reason
     * the real one was invisible, and it does not exist in a production build
     * — verified by running one: production reports exactly the two below.
     */
    const readViolations = await watchViolations(page);
    await signIn(page, ACCOUNTS.reception);

    for (const path of ["/reception", "/queue", "/patients"]) {
      await page.goto(path);
      await page.waitForTimeout(1_500);
    }

    const ours = (await readViolations()).filter((v) => !v.includes("next-devtools"));
    const SONNER_INJECTIONS = 2;

    expect(
      ours.length,
      `expected at most ${SONNER_INJECTIONS} blocked inline styles (Sonner's own), got:\n${ours.join("\n")}`,
    ).toBeLessThanOrEqual(SONNER_INJECTIONS);
  });
});

test.describe("a toast", () => {
  test("floats above the page instead of landing in the middle of it", async ({ page }) => {
    /**
     * The bug the policy caused, asserted where a person would notice it.
     *
     * With Sonner's stylesheet blocked, the toaster computed to
     * `position: static` with a transparent background — so every
     * confirmation in the application rendered as bare text in the document
     * flow rather than a card in the corner. Asserting the computed position
     * is the cheapest thing that fails when that returns; asserting the text
     * is what the rest of the suite already does, and it never noticed.
     */
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

    const toaster = page.locator("[data-sonner-toaster]");
    await expect(toaster).toHaveCount(1, { timeout: 45_000 });
    await expect(toaster).toHaveCSS("position", "fixed");

    const toast = page.locator("[data-sonner-toast]").first();
    await expect(toast).toBeVisible();
    // Opaque: an unstyled toast is transparent, and transparent text over the
    // form underneath is unreadable even though it is technically "visible".
    await expect(toast).not.toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
  });
});

test.describe("a link that looks like a button", () => {
  test("is still a link", async ({ page }) => {
    /**
     * These used to be `<Button render={<Link />}>`, which Base UI warned
     * about on every render — correctly. It rendered an `<a>` while treating
     * it as a native button, which put a meaningless `type="button"` on the
     * anchor (verified: it really was emitted) and skipped Base UI's own link
     * handling.
     *
     * The warning's suggested remedy, `nativeButton={false}`, is worse: it
     * silences the warning by adding `role="button"` (also verified). These
     * controls *navigate*, so that announces a link as a button, drops it out
     * of the browser's link list, and would break every `getByRole("link")`
     * in this suite.
     *
     * `LinkButton` takes the third option — button styling on a plain anchor,
     * with Base UI not involved at all.
     */
    await signIn(page, ACCOUNTS.reception);

    for (const path of ["/reception", "/queue", "/patients", "/wards"]) {
      await page.goto(path);
      await page.waitForTimeout(1_000);

      const junk = await page.$$eval("a", (anchors) =>
        anchors
          .filter((a) => a.hasAttribute("type") || a.getAttribute("role") === "button")
          .map((a) => `${a.getAttribute("href")} type=${a.getAttribute("type")} role=${a.getAttribute("role")}`),
      );
      expect(junk, `on ${path}, anchors should carry neither type nor role="button"`).toEqual([]);
    }

    // And the styling still arrives: a link-button is not a bare underlined link.
    await page.goto("/reception");
    const register = page.getByRole("link", { name: /Register a patient/ });
    await expect(register).toBeVisible({ timeout: 45_000 });
    await expect(register).toHaveClass(/inline-flex/);
  });
});
