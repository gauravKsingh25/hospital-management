// Run with `npm run test:unit`. UX-04: "waiting 4173 min" is a number nobody
// at a counter can read at a glance; "2 d 21 h" is.
import assert from "node:assert/strict";
import { test } from "node:test";

import { compactWait, splitWait } from "./duration.ts";

test("under an hour stays in minutes", () => {
  assert.deepEqual(splitWait(0), { days: 0, hours: 0, minutes: 0 });
  assert.deepEqual(splitWait(59), { days: 0, hours: 0, minutes: 59 });
});

test("hours and minutes under a day", () => {
  assert.deepEqual(splitWait(125), { days: 0, hours: 2, minutes: 5 });
});

test("days and hours beyond, the audit's 4173 minutes", () => {
  assert.deepEqual(splitWait(4173), { days: 2, hours: 21, minutes: 33 });
});

test("nonsense is nothing, not a negative wait", () => {
  assert.deepEqual(splitWait(-5), { days: 0, hours: 0, minutes: 0 });
  assert.deepEqual(splitWait(Number.NaN), { days: 0, hours: 0, minutes: 0 });
});

test("the compact form for dense report tables", () => {
  assert.equal(compactWait(45), "45m");
  assert.equal(compactWait(125), "2h 5m");
  assert.equal(compactWait(4173), "2d 21h");
});
