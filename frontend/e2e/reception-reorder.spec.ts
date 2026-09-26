import { expect, test, type Page } from "@playwright/test";

import { ACCOUNTS, chooseGender, signIn, signOut, uniquePatient } from "./support";

/**
 * Re-ranking a doctor's line by drag and drop (CLAUDE.md §7b).
 *
 * The drag is the order, not a picture of it: what reception drops is what
 * the server stores, what survives a reload, and what the doctor's own list
 * shows. So the spec checks all three, and drives a real pointer rather than
 * calling the endpoint — the endpoint has its own tests; this one exists to
 * catch a handle that stopped picking up.
 */
async function queueWithRao(page: Page, name: string, phone: string): Promise<void> {
  await page.goto("/reception/register");
  await page.getByLabel("Full name", { exact: true }).fill(name);
  await page.getByLabel("Mobile number", { exact: true }).fill(phone);
  await page.getByLabel("Age", { exact: true }).fill("40");
  await chooseGender(page, "Male");
  await page.getByLabel("Doctor", { exact: true }).click();
  await page.getByRole("option", { name: /Vikram/ }).click();
  await page.getByRole("button", { name: /Register and send to a doctor/ }).click();
  await expect(page.getByText("Token issued")).toBeVisible({ timeout: 45_000 });
}

test("dragging a patient to the top changes who the doctor sees first", async ({ page }) => {
  const first = uniquePatient();
  const second = uniquePatient();

  await signIn(page, ACCOUNTS.reception);
  await queueWithRao(page, first.name, first.phone);
  await queueWithRao(page, second.name, second.phone);

  await page.goto("/reception");
  const column = page.getByTestId("doctor-queue").filter({ hasText: "Dr Vikram Rao" });
  const rows = column.getByTestId("queue-row");
  await expect(rows.filter({ hasText: second.name })).toBeVisible({ timeout: 30_000 });

  // Arrival order to begin with: `first` above `second`.
  const names = async () => rows.getByTestId("patient-name").allInnerTexts();
  const before = await names();
  expect(before.indexOf(first.name)).toBeLessThan(before.indexOf(second.name));

  // Pick up `second` by its handle and carry it above `first`. A real
  // pointer, in steps, so the sensor's activation distance is crossed the
  // way a hand crosses it.
  const handle = rows.filter({ hasText: second.name }).getByTestId("drag-handle");
  const target = rows.filter({ hasText: first.name });
  await handle.scrollIntoViewIfNeeded();
  // The delay alert above the board arrives on its own poll and pushes the
  // rows down when it lands. Measure until two readings agree, so the
  // pointer goes where the handle *is*, not where it was.
  let from = await handle.boundingBox();
  let to = await target.boundingBox();
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await page.waitForTimeout(400);
    const nextFrom = await handle.boundingBox();
    const nextTo = await target.boundingBox();
    if (nextFrom?.y === from?.y && nextTo?.y === to?.y) break;
    from = nextFrom;
    to = nextTo;
  }
  if (!from || !to) throw new Error("rows not laid out");

  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2 - 10, { steps: 5 });
  await page.mouse.move(to.x + to.width / 2, to.y + 4, { steps: 15 });
  await page.mouse.up();

  // Optimistic: the row is above straight away…
  await expect
    .poll(async () => {
      const order = await names();
      return order.indexOf(second.name) < order.indexOf(first.name);
    })
    .toBe(true);

  // The big number is the place in line, so after the drop it still reads
  // 1, 2, 3… down the column — never tokens out of order.
  const places = (await rows.getByTestId("place").allInnerTexts())
    .map((text) => text.replace(/\D/g, ""))
    .filter(Boolean)
    .map(Number);
  expect(places).toEqual(places.map((_, index) => index + 1));

  // …and stored: a fresh load shows the same order.
  await page.reload();
  await expect(rows.filter({ hasText: second.name })).toBeVisible({ timeout: 30_000 });
  const after = await names();
  expect(after.indexOf(second.name)).toBeLessThan(after.indexOf(first.name));

  // The doctor's own list honours it — the reason the rank is server-side.
  await signOut(page);
  await signIn(page, ACCOUNTS.doctor);
  await page.goto("/doctor");
  const mine = page.getByTestId("queue-row");
  await expect(mine.filter({ hasText: second.name })).toBeVisible({ timeout: 30_000 });
  const doctorOrder = await mine.allInnerTexts();
  const indexOf = (name: string) => doctorOrder.findIndex((text) => text.includes(name));
  expect(indexOf(second.name)).toBeLessThan(indexOf(first.name));
});
