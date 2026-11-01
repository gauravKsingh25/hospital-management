import { expect, test } from "@playwright/test";

import { deskAdmits, releaseTrackedBeds, sentForAdmission } from "./support";

/**
 * The stay screen: a sticky header (who, where, the safety banner, every act
 * on the stay), then what needs doing and the stay's facts in two columns.
 * Anything "Not recorded" is filled in where it shows.
 */
releaseTrackedBeds();

test("the stay screen keeps the patient and the actions in view, and details are edited in place", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const name = await sentForAdmission(page);
  const { bed } = await deskAdmits(page, name);

  const header = page.getByTestId("stay-header");
  await expect(header).toContainText(name);
  await expect(header).toContainText(bed);
  await expect(header.getByTestId("stay-day")).toHaveText("Day 1");
  await expect(header.getByRole("heading", { level: 1, name })).toBeVisible();
  // The stay's pages are linked from the header, so they never need a scroll.
  await expect(header.getByRole("link", { name: "Medication chart" })).toBeVisible();
  await expect(header.getByRole("link", { name: "Discharge summary" })).toBeVisible();

  // A missing diagnosis is recorded where it shows.
  const clinical = page.getByTestId("clinical-panel");
  await clinical.getByRole("button", { name: "Edit Provisional diagnosis" }).click();
  await clinical.getByLabel("Provisional diagnosis").fill("Community-acquired pneumonia");
  await clinical.getByRole("button", { name: "Save" }).click();
  await expect(clinical).toContainText("Community-acquired pneumonia");

  // So is the attendant, and the expected stay drives the header's day count.
  const stay = page.getByTestId("stay-panel");
  await stay.getByRole("button", { name: "Edit Attendant" }).click();
  await stay.getByLabel("Attendant's name").fill("Ravi Kumar");
  await stay.getByLabel("Relation").fill("Son");
  await stay.getByLabel("Phone").fill("9876543210");
  await stay.getByRole("button", { name: "Save" }).click();
  await expect(stay).toContainText("Ravi Kumar (Son)");
  await expect(stay).toContainText("9876543210");

  await stay.getByRole("button", { name: "Edit Expected stay" }).click();
  await stay.getByLabel("Expected stay").fill("5");
  await stay.getByRole("button", { name: "Save" }).click();
  await expect(header.getByTestId("stay-day")).toHaveText("Day 1 of ~5");

  // Survives a reload: it was saved, not just shown.
  await page.reload();
  await expect(page.getByTestId("clinical-panel")).toContainText("Community-acquired pneumonia");
  await expect(page.getByTestId("discharge-readiness")).toContainText("Not yet marked by the doctor.");

  // The header stays in view at the bottom of the page.
  await page.getByTestId("discharge-readiness").scrollIntoViewIfNeeded();
  await page.mouse.wheel(0, 4000);
  await expect(page.getByTestId("stay-header")).toBeInViewport();
});
