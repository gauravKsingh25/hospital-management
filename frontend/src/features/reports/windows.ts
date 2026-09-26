/**
 * How far back the dashboard looks.
 *
 * **No `"use client"` here, and that is the whole reason this file exists.**
 * A plain constant exported from a client module is not a constant on the
 * server — it is a client-reference stub, and interpolating one into a URL
 * produces a query string containing a thrown error rather than a number.
 * This bit the pagination constants once already (see `lib/pagination.ts`),
 * and `reports/page.tsx` builds its fetch URL from `DEFAULT_TRAILING_DAYS`
 * during a server render, so it would have bitten again.
 *
 * The values themselves are bounded by the API: `trailing_days` is `ge=1,
 * le=90`.
 */

/** The windows offered on the dashboard, in the order they are shown. */
export const REPORT_WINDOWS = [7, 30, 90] as const;

/** What the dashboard opens on, and the only window that is server-rendered. */
export const DEFAULT_TRAILING_DAYS = 30;
