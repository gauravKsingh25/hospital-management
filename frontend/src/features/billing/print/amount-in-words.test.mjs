// Run with `npm run test:unit` (Node's own test runner; Node 22.6+ loads the
// TypeScript module directly). No test framework is added for one helper.
import assert from "node:assert/strict";
import { test } from "node:test";

import { amountInWords } from "./amount-in-words.ts";

test("thousands, the Indian way", () => {
  assert.equal(amountInWords("24600.00"), "Rupees Twenty-Four Thousand Six Hundred Only");
});

test("lakhs with paise", () => {
  assert.equal(
    amountInWords("150000.50"),
    "Rupees One Lakh Fifty Thousand and Fifty Paise Only",
  );
});

test("crores", () => {
  assert.equal(
    amountInWords("12345678.00"),
    "Rupees One Crore Twenty-Three Lakh Forty-Five Thousand Six Hundred Seventy-Eight Only",
  );
});

test("small and round amounts", () => {
  assert.equal(amountInWords("0.00"), "Rupees Zero Only");
  assert.equal(amountInWords("500"), "Rupees Five Hundred Only");
  assert.equal(amountInWords("0.05"), "Rupees Zero and Five Paise Only");
  assert.equal(amountInWords("1019.00"), "Rupees One Thousand Nineteen Only");
});

test("a malformed amount is not guessed at", () => {
  assert.equal(amountInWords("abc"), "");
});
