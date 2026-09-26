import { expect, test } from "@playwright/test";

import {
  ACCOUNTS,
  chooseGender,
  releaseTrackedBeds,
  signIn,
  signOut,
  trackBed,
  uniquePatient,
} from "./support";

/**
 * The OPD → admission desk hand-off, end to end.
 *
 * Reception marks a patient seen and sends them for admission; the admission
 * desk — its own role, its own screen — admits them into a bed, and the IPD
 * stay begins. Each side sees only its own half, and both agree afterwards
 * about where the patient is.
 */
releaseTrackedBeds();

test("reception sends a seen patient and the admission desk admits them", async ({ page }) => {
  const patient = uniquePatient();

  // --- reception: register, queue, mark seen, send for admission -------------
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception/register");
  await page.getByLabel("Full name", { exact: true }).fill(patient.name);
  await page.getByLabel("Mobile number", { exact: true }).fill(patient.phone);
  await page.getByLabel("Age", { exact: true }).fill("55");
  await chooseGender(page, "Male");
  await page.getByLabel("Doctor", { exact: true }).click();
  await page.getByRole("option", { name: /Vikram/ }).click();
  await page.getByRole("button", { name: /Register and send to a doctor/ }).click();
  await expect(page.getByText("Token issued")).toBeVisible({ timeout: 45_000 });

  await page.goto("/reception");
  const row = page.getByTestId("queue-row").filter({ hasText: patient.name });
  await expect(row).toBeVisible({ timeout: 30_000 });
  await row.getByTestId("mark-seen").click();

  const seenRow = page.getByTestId("seen-row").filter({ hasText: patient.name });
  await expect(seenRow).toBeVisible({ timeout: 20_000 });
  await seenRow.getByTestId("send-to-admission").click();

  const send = page.getByRole("dialog");
  await send.getByLabel(/Note for the admission desk/).fill("Doctor advised admission");
  await send.getByRole("button", { name: "Send", exact: true }).click();
  await expect(seenRow.getByTestId("admission-status")).toHaveText("At admission desk", {
    timeout: 15_000,
  });
  // Sent once: the button is gone, so nobody sends them twice.
  await expect(seenRow.getByTestId("send-to-admission")).toHaveCount(0);

  // Reception does not work the desk.
  await page.goto("/admissions");
  await expect(page.getByText("You do not have permission to open this.")).toBeVisible();
  await signOut(page);

  // --- admission desk: its own home screen, the patient waiting --------------
  await signIn(page, ACCOUNTS.admissionDesk);
  await expect(page).toHaveURL(/\/admissions$/, { timeout: 30_000 });

  const waiting = page.getByTestId("admission-request").filter({ hasText: patient.name });
  await expect(waiting).toBeVisible({ timeout: 30_000 });
  await expect(waiting).toContainText("Doctor advised admission");
  await expect(waiting).toContainText("From Dr Vikram Rao");

  await waiting.getByTestId("desk-admit").click();
  const admit = page.getByRole("dialog", { name: "Admit to a ward" });
  const bed = admit.locator("label:has(input[name='bed']:not([disabled]))").first();
  await expect(bed).toBeVisible({ timeout: 30_000 });
  const bedCode = (await bed.innerText()).trim();
  await bed.click();
  await admit.getByRole("button", { name: "Admit", exact: true }).click();

  // The IPD flow begins: the admission's own page.
  await expect(page).toHaveURL(/\/admissions\/[0-9a-f-]{36}$/, { timeout: 45_000 });
  trackBed({ url: page.url(), bed: bedCode });
  await expect(page.getByRole("heading", { level: 1 })).toContainText(patient.name, {
    timeout: 30_000,
  });

  // Off the waiting list, onto "handled today".
  await page.goto("/admissions");
  await expect(page.getByTestId("admission-request").filter({ hasText: patient.name })).toHaveCount(
    0,
    { timeout: 30_000 },
  );
  await expect(
    page.getByTestId("handled-requests").getByRole("listitem").filter({ hasText: patient.name }),
  ).toContainText("Admitted");
  await signOut(page);

  // --- reception sees the same answer -----------------------------------------
  await signIn(page, ACCOUNTS.reception);
  await page.goto("/reception");
  await expect(
    page.getByTestId("seen-row").filter({ hasText: patient.name }).getByTestId("admission-status"),
  ).toHaveText("Admitted", { timeout: 30_000 });
});
