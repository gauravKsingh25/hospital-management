/**
 * Rendering the numbers on a dashboard.
 *
 * One rule runs through all of this and it is worth stating plainly, because
 * getting it wrong is how a dashboard becomes something staff quote in a
 * meeting and later regret: **a number that does not exist is not zero.**
 *
 * The backend is deliberate about the distinction — `reporting/schemas.py`
 * returns `None` for every average with no denominator and `0` for every count
 * that genuinely counted nothing. An average length of stay of `null` means
 * nobody was discharged in the window; rendering it as "0.0 days" invents the
 * best possible result out of no data at all. So `null` becomes an em dash
 * here, everywhere, and the caller never gets to default it.
 *
 * The other rule is that rates arrive as fractions (`0.83`) and are shown as
 * percentages. Doing that conversion in one place keeps a stray `* 100` from
 * turning an occupancy of 83% into 8300%.
 */

/** What an absent number looks like. Never "0", never blank. */
export const DASH = "—";

const INTEGER = new Intl.NumberFormat("en-IN");
const ONE_DECIMAL = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

/** A count. Always a number — counts are zero-filled server-side. */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return INTEGER.format(value);
}

/** A fraction (0–1) as a whole-percent string, or an em dash. */
export function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return `${Math.round(value * 100)}%`;
}

/**
 * A duration in minutes, rounded to whole minutes.
 *
 * Waits are quoted to patients in whole minutes and nobody has ever cared
 * about the seconds, so the extra precision would only make the number look
 * more authoritative than it is.
 */
export function formatMinutes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return `${Math.round(value)}m`;
}

/** A day count to one decimal — ALOS is genuinely read as "4.2 days". */
export function formatDays(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return ONE_DECIMAL.format(value);
}

/** A per-hundred rate that is already scaled, e.g. mortality per 100 discharges. */
export function formatPerHundred(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return ONE_DECIMAL.format(value);
}

/**
 * A `YYYY-MM-DD` from the API, in the reader's own date format.
 *
 * Built from the parts rather than `new Date(iso)`, which parses a bare date
 * as UTC midnight — west of Greenwich that renders every date one day early.
 * The API's dates are already the *hospital's* local dates (see
 * `reporting.local_today`), so shifting them by a timezone at all is wrong.
 */
export function formatLocalDate(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  if (!year || !month || !day) return iso;
  return new Date(year, month - 1, day).toLocaleDateString();
}

/** The same, abbreviated for a crowded axis: "16 Aug". */
export function formatAxisDate(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  if (!year || !month || !day) return iso;
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
  });
}

/** The share one row takes of the largest row, as a CSS width. Never NaN. */
export function shareOf(value: number, largest: number): string {
  if (!Number.isFinite(value) || !Number.isFinite(largest) || largest <= 0) return "0%";
  return `${Math.max(0, Math.min(100, (value / largest) * 100))}%`;
}
