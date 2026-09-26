import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, registerAndQueue, signIn, signOut } from "./support";

/**
 * The three unplanned endings: death, referral out, and leaving against
 * medical advice.
 *
 * CLAUDE.md §6 makes these **first-class terminal states, not afterthoughts**,
 * and they were the last part of the state machine with no screen — recordable
 * only by an API call, which in practice means not recordable at all. A
 * hospital that cannot record a death in its own system keeps a paper register
 * beside it, and then the two disagree about who is alive.
 *
 * Each one is asserted twice over: that it is recorded with the metadata the
 * state machine demands, and that the metadata is *readable afterwards*. The
 * second half is the point — these fields are required precisely because
 * somebody asks for them later.
 */

/** Open a fresh visit as the doctor and return the consultation URL. */
async function consultingRoom(page: Page): Promise<{ name: string; url: string }> {
  const name = await registerAndQueue(page);

  await signIn(page, ACCOUNTS.doctor);
  const row = page.getByTestId("queue-row").filter({ hasText: name });
  await row.getByRole("link", { name: /Start consultation/ }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible({ timeout: 45_000 });

  return { name, url: page.url() };
}

test.describe("recording how a visit ended", () => {
  test("a death is recorded, and the record names the doctor who certified it", async ({
    page,
  }) => {
    /**
     * The identity gap that mattered most. `death_certified_by_id` alone makes
     * the record unreadable by everyone who needs it — the records officer,
     * the registrar, the family — and unlike the other worklists in this
     * system, this one ends up on a document somebody is handed.
     */
    await consultingRoom(page);

    await page.getByTestId("record-outcome").click();
    const dialog = page.getByRole("dialog", { name: "How did this visit end?" });
    await dialog.getByLabel("What happened").click();
    await page.getByRole("option", { name: "The patient died" }).click();

    await dialog.getByLabel("Certified by").click();
    await page.getByRole("option", { name: /Vikram/ }).click();
    await dialog.getByLabel("Cause of death").fill("Cardiac arrest.");
    await dialog.getByLabel("Where").fill("Casualty");

    // Stated before the button, not after: nothing reopens a terminal visit.
    await expect(dialog.getByText(/closes the visit permanently/i)).toBeVisible();
    await dialog.getByTestId("confirm-outcome").click();

    const summary = page.getByTestId("outcome-summary");
    await expect(summary).toBeVisible({ timeout: 45_000 });
    await expect(summary).toHaveAttribute("data-status", "DECEASED");
    await expect(summary).toContainText("Cardiac arrest.");
    // The name, not the id.
    await expect(summary).toContainText("Dr Vikram Rao");
  });

  test("a death stops the hospital messaging the patient", async ({ page }) => {
    /**
     * CLAUDE.md §14's invariant, now reachable the way a hospital would
     * actually reach it. This used to be asserted by POSTing to the API,
     * because there was no screen — which was the clearest evidence that the
     * screen needed to exist.
     */
    const { name } = await consultingRoom(page);

    await page.getByTestId("record-outcome").click();
    const dialog = page.getByRole("dialog", { name: "How did this visit end?" });
    await dialog.getByLabel("What happened").click();
    await page.getByRole("option", { name: "The patient died" }).click();
    await dialog.getByLabel("Certified by").click();
    await page.getByRole("option", { name: /Vikram/ }).click();
    await dialog.getByLabel("Cause of death").fill("Respiratory failure.");
    await dialog.getByTestId("confirm-outcome").click();
    await expect(page.getByTestId("outcome-summary")).toBeVisible({ timeout: 45_000 });

    await signOut(page);
    await signIn(page, ACCOUNTS.reception);
    await page.goto("/messages/blocked");

    const blocked = page.getByTestId("suppression-row").filter({ hasText: name }).first();
    await expect(blocked).toBeVisible({ timeout: 45_000 });
    await expect(blocked).toHaveAttribute("data-reason", "DECEASED");
    // And it is the block nobody can release.
    await expect(blocked.getByRole("button", { name: "Release" })).toHaveCount(0);
  });

  test("a referral records where the patient went and why", async ({ page }) => {
    // The receiving hospital reads the reason before the patient arrives, so
    // it is a letter rather than a form field.
    await consultingRoom(page);

    await page.getByTestId("record-outcome").click();
    const dialog = page.getByRole("dialog", { name: "How did this visit end?" });
    await dialog.getByLabel("What happened").click();
    await page.getByRole("option", { name: "Referred to another hospital" }).click();

    await dialog.getByLabel("Referred to").fill("District Hospital, Varanasi");
    await dialog.getByLabel("Reason for referral").fill("Needs a CT scan we cannot do here.");
    await dialog.getByLabel("Transport").fill("Hospital ambulance");
    await dialog.getByTestId("confirm-outcome").click();

    const summary = page.getByTestId("outcome-summary");
    await expect(summary).toBeVisible({ timeout: 45_000 });
    await expect(summary).toHaveAttribute("data-status", "REFERRED_OUT");
    await expect(summary).toContainText("District Hospital, Varanasi");
    await expect(summary).toContainText("Needs a CT scan");
  });

  test("an unsigned LAMA form says so rather than being left blank", async ({ page }) => {
    /**
     * The checkbox is recorded either way and defaults to unticked. An
     * unsigned LAMA is the hospital's legal exposure, and a field that is
     * quietly always "yes" helps nobody the day it is examined.
     */
    await consultingRoom(page);

    await page.getByTestId("record-outcome").click();
    const dialog = page.getByRole("dialog", { name: "How did this visit end?" });
    await dialog.getByLabel("What happened").click();
    await page.getByRole("option", { name: "Left against medical advice" }).click();

    await expect(dialog.getByTestId("lama-form-signed")).not.toBeChecked();
    await dialog.getByLabel("What the patient said").fill("Wants to go to a hospital nearer home.");
    await dialog.getByTestId("confirm-outcome").click();

    const summary = page.getByTestId("outcome-summary");
    await expect(summary).toBeVisible({ timeout: 45_000 });
    await expect(summary).toHaveAttribute("data-status", "LAMA");
    await expect(summary).toContainText("Not signed");
  });

  test("nothing can be recorded without the metadata the state machine demands", async ({
    page,
  }) => {
    // `REQUIRED_METADATA` refuses the transition without these, and the screen
    // refuses to send it — so the doctor is not told no after the fact.
    await consultingRoom(page);

    await page.getByTestId("record-outcome").click();
    const dialog = page.getByRole("dialog", { name: "How did this visit end?" });
    await dialog.getByLabel("What happened").click();
    await page.getByRole("option", { name: "The patient died" }).click();

    await expect(dialog.getByTestId("confirm-outcome")).toBeDisabled();
    await dialog.getByLabel("Certified by").click();
    await page.getByRole("option", { name: /Vikram/ }).click();
    // Still short of a cause.
    await expect(dialog.getByTestId("confirm-outcome")).toBeDisabled();

    await dialog.getByLabel("Cause of death").fill("Sepsis.");
    await expect(dialog.getByTestId("confirm-outcome")).toBeEnabled();
  });
});

test.describe("the queue empties", () => {
  test("a terminal outcome takes the token off the board", async ({ page }) => {
    /**
     * The bug this found. Queue entries were closed only by cancelling or
     * no-showing the appointment, so every visit that actually *finished* left
     * its token on the live board as `WAITING` — completed, admitted, deceased,
     * referred, absconded, all of them. The OPD queue never drained.
     *
     * It hid because every screen that reads the queue looks for a named
     * patient rather than at the whole list. It became impossible to miss the
     * day these screens landed and a patient recorded as deceased was still
     * showing first in line for a doctor.
     */
    const { name } = await consultingRoom(page);

    await page.getByTestId("record-outcome").click();
    const dialog = page.getByRole("dialog", { name: "How did this visit end?" });
    await dialog.getByLabel("What happened").click();
    await page.getByRole("option", { name: "Left against medical advice" }).click();
    await dialog.getByLabel("What the patient said").fill("Went home.");
    await dialog.getByTestId("confirm-outcome").click();
    await expect(page.getByTestId("outcome-summary")).toBeVisible({ timeout: 45_000 });

    await page.goto("/queue");
    await expect(page.getByTestId("queue-row").filter({ hasText: name })).toHaveCount(0, {
      timeout: 45_000,
    });
  });
});

test.describe("who may record one", () => {
  test("a nurse reads the chart and is not offered the outcomes", async ({ page }) => {
    /**
     * Three permissions rather than one (§8), and a nurse holds none of them.
     * They still get the whole chart — a nurse who cannot read the visit
     * cannot nurse the patient — which is exactly why the control is gated
     * separately from the screen.
     */
    const { name, url } = await consultingRoom(page);
    await signOut(page);

    await signIn(page, ACCOUNTS.nurse);
    await page.goto(url);
    await expect(page.getByText(name).first()).toBeVisible({ timeout: 45_000 });

    await expect(page.getByTestId("record-outcome")).toHaveCount(0);
  });
});
