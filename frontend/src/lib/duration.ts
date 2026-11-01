/**
 * How long somebody has been waiting, in units a person reads at a glance
 * (UX-04). "Waiting 4173 min" was on the admission desk's first row; nobody at
 * a counter converts that to "nearly three days" in their head, and nearly
 * three days is the point.
 *
 * Minutes under an hour, hours and minutes under a day, then days and hours —
 * the minutes stop mattering once it is days.
 */

export type WaitParts = { days: number; hours: number; minutes: number };

export function splitWait(totalMinutes: number): WaitParts {
  const total = Number.isFinite(totalMinutes) ? Math.max(0, Math.round(totalMinutes)) : 0;
  return {
    days: Math.floor(total / 1440),
    hours: Math.floor((total % 1440) / 60),
    minutes: total % 60,
  };
}

/** "45m", "2h 5m", "2d 21h" — for tables where every character counts. */
export function compactWait(totalMinutes: number): string {
  const { days, hours, minutes } = splitWait(totalMinutes);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

/** A wait of a day or more: shown in red wherever it appears. */
export const LONG_WAIT_MINUTES = 24 * 60;
