import { expect, test } from "@playwright/test";

import {
  DEFAULT_PERIOD,
  parsePeriod,
  PERIODS,
  periodDays,
} from "@/features/reports/windows";

/**
 * The Reports page's period filter — Today, 7, 30 or 90 days.
 *
 * Unit tests; no page fixture, so no browser starts (see
 * `camera-scan-support.spec.ts`). The period lives in the address
 * (`/reports?period=7`) so a refresh, the Back button and a shared link all
 * keep it, which makes the address bar user input: anything can arrive.
 */

test.describe("reading the period from the address", () => {
  test("no period opens on today", () => {
    // The end-of-day question — what came in today — is the common one.
    expect(DEFAULT_PERIOD).toBe("today");
    expect(parsePeriod(undefined)).toBe("today");
    expect(parsePeriod(null)).toBe("today");
    expect(parsePeriod("")).toBe("today");
  });

  test("each offered period reads as itself", () => {
    for (const period of PERIODS) expect(parsePeriod(period)).toBe(period);
  });

  test("anything else falls back to today rather than failing", () => {
    /**
     * A hand-edited or stale link must still open the page. It must never
     * reach the API as an arbitrary number either — the server caps the
     * window at 90 days and would answer `?period=365` with an error page.
     */
    for (const junk of ["365", "0", "-7", "7.5", "week", "TODAY ", "<script>"]) {
      expect(parsePeriod(junk)).toBe("today");
    }
  });

  test("a repeated parameter uses the first", () => {
    // `?period=7&period=90` arrives from Next as an array.
    expect(parsePeriod(["7", "90"])).toBe("7");
  });
});

test.describe("how many days a period covers", () => {
  test("today is one day, the hospital's own", () => {
    expect(periodDays("today")).toBe(1);
  });

  test("the others are trailing windows ending today", () => {
    expect(periodDays("7")).toBe(7);
    expect(periodDays("30")).toBe(30);
    expect(periodDays("90")).toBe(90);
  });

  test("every period fits the API's limit", () => {
    // `trailing_days` is `ge=1, le=90` on `/reports/dashboard`.
    for (const period of PERIODS) {
      expect(periodDays(period)).toBeGreaterThanOrEqual(1);
      expect(periodDays(period)).toBeLessThanOrEqual(90);
    }
  });
});
