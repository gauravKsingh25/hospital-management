import { expect, test } from "@playwright/test";

import {
  isMachineSpeed,
  isSafeScanTarget,
  looksLikeScanInProgress,
  readScan,
  type ScanKey,
} from "@/lib/scanner";

/**
 * The scan-anywhere heuristic, in isolation.
 *
 * These are unit tests, not end-to-end ones — none of them requests the `page`
 * fixture, so no browser starts. They live here because the alternative was
 * adding a second test runner and its config for one pure module, and
 * CLAUDE.md §14 says to question a dependency that earns that little.
 *
 * They are also the tests that matter most in the QR feature. A global key
 * handler that guesses wrong swallows real typing, and in a hospital that
 * means a clinical note losing a sentence — worse than the problem
 * scan-anywhere solves. Every threshold gets a passing case and a failing one.
 */

/** A burst of keys at a fixed interval, the way a device emits them. */
function typed(text: string, gapMs: number, start = 1_000): ScanKey[] {
  return [...text].map((key, index) => ({ key, at: start + index * gapMs }));
}

const UHID = "DEMO-26-000042";

test.describe("telling a scanner from a person", () => {
  test("a scanner burst is recognised", () => {
    // 8 ms per character is typical for a commodity USB scanner.
    expect(readScan(typed(UHID, 8))).toBe(UHID);
  });

  test("a fast human typist is not", () => {
    /**
     * The number that matters. Sustained professional typing runs about
     * 100 ms per character and the world record is near 60 ms; the threshold
     * sits at 30 ms with a scanner an order of magnitude below it. There is
     * no overlap, which is why this is not a tuning exercise.
     */
    expect(readScan(typed(UHID, 60))).toBeNull();
    expect(readScan(typed(UHID, 35))).toBeNull();
  });

  test("one stalled keystroke does not lose a real scan", () => {
    // The median, not the mean: an OS hiccup mid-burst is ordinary, and a mean
    // would be dragged over the threshold by a single 300 ms gap.
    const keys = typed(UHID, 8);
    for (let index = 7; index < keys.length; index += 1) keys[index].at += 300;

    expect(isMachineSpeed(keys)).toBe(true);
    expect(readScan(keys)).toBe(UHID);
  });

  test("keys minutes apart never accumulate into a scan", () => {
    // A buffer that is never flushed must not become a false positive.
    const stale = typed(UHID, 8);
    stale[stale.length - 1].at += 120_000;
    expect(readScan(stale)).toBeNull();
  });
});

test.describe("what counts as a card", () => {
  test("something the right speed but the wrong shape is discarded", () => {
    expect(readScan(typed("hello world!!", 8))).toBeNull();
    expect(readScan(typed("9876543210987", 8))).toBeNull();
  });

  test("a UHID typed in lower case still resolves", () => {
    // Some scanners are configured with a case-inverting keyboard layout.
    expect(readScan(typed(UHID.toLowerCase(), 8))).toBe(UHID);
  });

  test("too few characters is not a scan", () => {
    expect(readScan(typed("AB-26-01", 8))).toBeNull();
    expect(readScan(typed("X", 8))).toBeNull();
    expect(readScan([])).toBeNull();
  });

  test("another hospital's card is still shaped like a card", () => {
    // Recognised here and refused by the server, which is where tenant
    // isolation belongs. This module decides "is that a UHID", never "is it
    // one of ours".
    expect(readScan(typed("OTHER-26-000001", 8))).toBe("OTHER-26-000001");
  });
});

test.describe("suppressing keys mid-scan", () => {
  test("kicks in before the terminator arrives", () => {
    /**
     * It has to. If the decision waited for the Enter, the Enter itself would
     * reach the page — and Enter on whatever button holds focus is a click
     * nobody asked for.
     */
    expect(looksLikeScanInProgress(typed(UHID, 8).slice(0, 4))).toBe(true);
  });

  test("does not kick in for a person typing", () => {
    expect(looksLikeScanInProgress(typed(UHID, 60).slice(0, 4))).toBe(false);
  });

  test("does not kick in on too little evidence", () => {
    expect(looksLikeScanInProgress(typed(UHID, 8).slice(0, 3))).toBe(false);
  });
});

test.describe("where it is safe to listen", () => {
  test("never inside anything a person types into", () => {
    /**
     * The strong guard, and the one that makes the dangerous case impossible
     * rather than unlikely. A scan into a focused field already works — the
     * characters simply land there — so there is nothing to gain by
     * intercepting, and a clinical note to lose.
     */
    for (const tag of ["INPUT", "TEXTAREA", "SELECT"]) {
      expect(isSafeScanTarget({ tagName: tag } as Element)).toBe(false);
    }
  });

  test("nothing focused is the case this exists for", () => {
    expect(isSafeScanTarget(null)).toBe(true);
    expect(isSafeScanTarget({ tagName: "BODY" } as Element)).toBe(true);
  });

  test("a focused button is watched, which is why keys get suppressed", () => {
    // Focus usually sits on a button when somebody reaches for a scanner —
    // they just clicked something. Watching there is the point; suppressing
    // the terminator is what stops the button being pressed.
    expect(isSafeScanTarget({ tagName: "BUTTON" } as Element)).toBe(true);
  });
});
