import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, findOnBoard, registerAndQueue, signIn, signOut } from "./support";

/**
 * The outbox, the block list, and the standing copy.
 *
 * Two properties carry the weight.
 *
 * **A message not sent is not a message that failed.** The backend keeps
 * `SUPPRESSED` and `FAILED` apart deliberately — "we chose not to" and "we
 * tried and could not" answer different questions, and an auditor asks the
 * first — so the screen must not collapse them into one grey "not delivered".
 *
 * **A death suppression is never liftable.** CLAUDE.md §14 names it an
 * invariant, and `service.lift_suppression` refuses it whatever the caller
 * holds, deliberately outside RBAC so an administrator editing permission rows
 * cannot grant it back. Until these screens existed nobody in the hospital
 * could see that rule working, and an invariant nobody can inspect is one
 * people quietly stop believing in.
 */

/** Find a patient in the index and open their record. */
async function openPatient(page: Page, name: string): Promise<void> {
  await page.goto("/patients");
  await page.getByLabel("Search").fill(name);
  const row = page.getByTestId("patient-row").filter({ hasText: name });
  await expect(row).toBeVisible({ timeout: 45_000 });
  await row.getByRole("link", { name: "Open" }).click();
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible({ timeout: 45_000 });
}

/** Register, queue, and let the doctor close the visit — which triggers messages. */
async function visitWithMessages(page: Page): Promise<string> {
  const name = await registerAndQueue(page);

  await signIn(page, ACCOUNTS.doctor);
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await row.getByRole("link", { name: /Start consultation/ }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });
  await page.getByRole("button", { name: "Start consultation" }).click();

  const note = page.getByRole("textbox", { name: "Clinical note" });
  await expect(note).toBeEnabled({ timeout: 45_000 });
  await note.fill("Seen and well. Review in a month.");
  await note.press("Control+Enter");
  await page.getByRole("button", { name: /Complete/ }).first().click();

  await signOut(page);
  return name;
}

test.describe("the outbox", () => {
  test("names the patient who says they never got it", async ({ page }) => {
    // The sentence this screen exists to answer is said by a named person at a
    // counter. It is why `NotificationSummary` gained a name and a UHID.
    const name = await visitWithMessages(page);

    await signIn(page, ACCOUNTS.reception);
    await page.goto("/messages");
    await findOnBoard(page, "message-row", name);
  });

  test("a message carries every channel that was tried", async ({ page }) => {
    /**
     * "We sent it" is not an answer to a patient who received nothing. The
     * ladder is: WhatsApp first, then SMS, then email (CLAUDE.md §9), and each
     * rung records what the gateway said.
     */
    const name = await visitWithMessages(page);

    await signIn(page, ACCOUNTS.reception);
    await page.goto("/messages");
    await findOnBoard(page, "message-row", name);
    await page.getByTestId("message-row").filter({ hasText: name }).first().click();

    const ladder = page.getByTestId("attempt-ladder");
    await expect(ladder).toBeVisible({ timeout: 45_000 });
    await expect(ladder.getByTestId("attempt-row").first()).toHaveAttribute(
      "data-channel",
      "WHATSAPP",
    );
  });
});

test.describe("blocking", () => {
  test("an opt-out is recorded on the patient's own record and stops messages", async ({
    page,
  }) => {
    // The request arrives as a sentence said to whoever has the record open,
    // so that is where the control is.
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.reception);
    await openPatient(page, name);
    await page.getByTestId("block-messaging").click();

    const dialog = page.getByRole("dialog", { name: "Block messaging" });
    await dialog.getByLabel("Note").fill("Asked at the counter.");
    await dialog.getByRole("button", { name: "Save" }).click();

    await expect(page.getByText("Recorded.")).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("patient-suppression")).toHaveAttribute(
      "data-reason",
      "OPTED_OUT",
    );

    // And they are on the hospital-wide list, by name.
    await page.goto("/messages/blocked");
    await findOnBoard(page, "suppression-row", name);
  });

  test("a block that can be released offers a release, and asks why", async ({ page }) => {
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.reception);
    await openPatient(page, name);
    await page.getByTestId("block-messaging").click();
    await page
      .getByRole("dialog", { name: "Block messaging" })
      .getByRole("button", { name: "Save" })
      .click();
    await expect(page.getByText("Recorded.")).toBeVisible({ timeout: 45_000 });

    await page.goto("/messages/blocked");
    await findOnBoard(page, "suppression-row", name);
    const row = page.getByTestId("suppression-row").filter({ hasText: name }).first();
    await row.getByRole("button", { name: "Release" }).click();

    // Re-enabling messaging to somebody who opted out is a consent decision
    // under the DPDP Act. It has to say who decided and why, so Save stays
    // disabled until it does.
    const dialog = page.getByRole("dialog", { name: "Release this block" });
    await expect(dialog.getByRole("button", { name: "Save" })).toBeDisabled();
    await dialog.getByLabel("Reason").fill("Patient asked to be contacted again.");
    await dialog.getByRole("button", { name: "Save" }).click();

    // Asserted on the list rather than on the toast: "Released." also appears
    // inside every already-released row's own caption, so matching the text
    // alone proves nothing. The block leaving the default view is the outcome.
    await expect(page.getByTestId("suppression-row").filter({ hasText: name })).toHaveCount(0, {
      timeout: 45_000,
    });

    // And it is still on the record — released, not erased.
    await page.getByTestId("toggle-lifted").click();
    await findOnBoard(page, "suppression-row", name);
    await expect(
      page.getByTestId("suppression-row").filter({ hasText: name }).first(),
    ).toHaveAttribute("data-lifted", "true");
  });

  test("a death is never offered a release", async ({ page }) => {
    /**
     * The invariant, on the screen — recorded the way a hospital records it.
     *
     * This test used to POST to the API because recording a death had no
     * screen, which was the clearest evidence that it needed one.
     */
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.doctor);
    const row = page.getByTestId("queue-row").filter({ hasText: name });
    await row.getByRole("link", { name: /Start consultation/ }).click();
    await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });

    await page.getByTestId("record-outcome").click();
    const outcome = page.getByRole("dialog", { name: "How did this visit end?" });
    await outcome.getByLabel("What happened").click();
    await page.getByRole("option", { name: "The patient died" }).click();
    await outcome.getByLabel("Certified by").click();
    await page.getByRole("option", { name: /Vikram/ }).click();
    await outcome.getByLabel("Cause of death").fill("Cardiac arrest.");
    await outcome.getByTestId("confirm-outcome").click();
    await expect(page.getByTestId("outcome-summary")).toBeVisible({ timeout: 45_000 });

    await signOut(page);

    await signIn(page, ACCOUNTS.reception);
    await page.goto("/messages/blocked");
    await findOnBoard(page, "suppression-row", name);

    const blocked = page.getByTestId("suppression-row").filter({ hasText: name }).first();
    await expect(blocked).toHaveAttribute("data-reason", "DECEASED");
    // No button at all — not a disabled one. A control that can never be used
    // is still a control somebody asks why they cannot use.
    await expect(blocked.getByRole("button", { name: "Release" })).toHaveCount(0);
    await expect(blocked).toContainText(/never released/i);
  });

  test("a death cannot be typed in as a reason", async ({ page }) => {
    // One way in, and it is the clinical death entry. Two ways to record a
    // death would mean two answers to whether somebody is dead.
    const name = await registerAndQueue(page);

    await signIn(page, ACCOUNTS.reception);
    await openPatient(page, name);
    await page.getByTestId("block-messaging").click();

    const dialog = page.getByRole("dialog", { name: "Block messaging" });
    await expect(dialog.getByRole("button", { name: "Opted out" })).toBeVisible();
    await expect(dialog.getByRole("button", { name: /deceased|died/i })).toHaveCount(0);
    await expect(dialog).toContainText(/never typed in here/i);
  });
});

test.describe("message templates", () => {
  test("says up front that messages go out without any templates", async ({ page }) => {
    /**
     * Writing no templates is not a misconfiguration — every message has
     * shipped English wording behind it, so a hospital that never opens this
     * screen still reminds patients of their appointments. Saying so is the
     * difference between an administrator ignoring this screen and treating an
     * empty list as an outage.
     *
     * Asserted on the standing description rather than on the empty state: the
     * empty state is only reachable on a tenant with no templates, and a test
     * that quietly stops asserting once one exists is worse than no test. The
     * sentence that has to be there is there either way.
     */
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/admin/templates");

    await expect(page.getByRole("heading", { name: "Message templates" })).toBeVisible({
      timeout: 45_000,
    });
    await expect(page.getByText(/shipped English wording as a fallback/i)).toBeVisible();
  });

  test("a template is written, then previewed before anybody receives it", async ({ page }) => {
    /**
     * The alternative way to discover a placeholder typo is to read it on a
     * patient's phone — and the failure is quiet: a name the renderer does not
     * recognise is substituted with nothing, so the message goes out with a
     * hole in it rather than with visible gibberish. The preview is what makes
     * that visible before anybody receives it.
     *
     * Note the syntax: `{{name}}`, which is what `PLACEHOLDER_PATTERN` matches.
     * Writing single braces produced exactly the silent no-substitution this
     * test now guards, and the screen's own hint had it wrong.
     */
    await signIn(page, ACCOUNTS.admin);
    await page.goto("/admin/templates");
    await page.getByRole("button", { name: "Add a template" }).click();

    // A language unique to this run. Templates are *configuration*, so
    // `reset_demo_data.py` rightly leaves them alone — which means a fixed
    // code/channel/language would collide with the previous run and 409. The
    // same reasoning as `uniquePatient`.
    const language = `q${Date.now().toString().slice(-2)}`;

    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Message").click();
    await page.getByRole("option").first().click();
    await dialog.getByLabel("Language").fill(language);
    await dialog
      .getByLabel("Wording")
      .fill("Namaste {{patient_name}}, aapka {{nonexistent}} taiyar hai.");
    await dialog.getByTestId("save-template").click();
    await expect(page.getByText("Template saved.")).toBeVisible({ timeout: 45_000 });

    // Re-open this one and render it: nothing supplies `nonexistent`, so it is
    // dropped and the sentence goes out with a hole where a word should be.
    await page.locator(`[data-testid="template-row"][data-language="${language}"]`).click();
    await page.getByTestId("preview-template").click();
    await expect(page.getByTestId("missing-placeholders")).toBeVisible({ timeout: 45_000 });
    await expect(page.getByTestId("missing-placeholders")).toContainText("nonexistent");
  });
});
