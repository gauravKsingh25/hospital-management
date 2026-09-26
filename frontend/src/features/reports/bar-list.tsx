import { shareOf } from "@/features/reports/format";
import { cn } from "@/lib/utils";

/**
 * A ranked list of things, each with a bar showing its share of the largest.
 *
 * This is the shape most of the dashboard actually wants — busiest department,
 * fullest ward, where the money came from — and it beats a pie chart on every
 * axis that matters here: it stays readable at ten rows, it works in a narrow
 * column on a nursing-station monitor, and the exact figure sits next to the
 * bar instead of in a tooltip nobody hovers.
 *
 * The bar is scaled against the **largest row**, not against the total, and
 * that is deliberate: the question a ward list answers is "which is fullest",
 * which is a comparison between rows.
 *
 * ## The unnamed row
 *
 * `label` is nullable upstream — an encounter can have no department, a walk-in
 * no doctor — and those rows are the interesting ones. They are shown with an
 * explicit "Not recorded" rather than dropped, because a busy "unassigned" row
 * is a data-entry problem the dashboard exists to make visible.
 */
export type BarRow = {
  key: string;
  label: string;
  /** Drives the bar. */
  value: number;
  /** Shown on the right — usually the value, sometimes a rate or an amount. */
  display: string;
  /** A second line under the label: average wait, bed counts, whatever qualifies it. */
  detail?: string;
  tone?: "default" | "warn" | "bad";
};

const TONES = {
  default: "bg-primary/70",
  warn: "bg-caution",
  bad: "bg-destructive/80",
} as const;

export function BarList({ rows, testId }: { rows: BarRow[]; testId?: string }) {
  const largest = rows.reduce((max, row) => Math.max(max, row.value), 0);

  return (
    <ul className="space-y-2.5 rounded-lg border p-4" data-testid={testId}>
      {rows.map((row) => (
        <li key={row.key} data-testid="bar-row" data-label={row.label}>
          <div className="flex items-baseline justify-between gap-3">
            <span className="min-w-0 truncate text-sm font-medium">{row.label}</span>
            <span className="tabular shrink-0 text-sm font-semibold">{row.display}</span>
          </div>

          {/*
            `aria-hidden` on the bar itself: it is a second rendering of the
            number already beside it, and a screen reader announcing an
            unlabelled graphic here would add noise, not information.
          */}
          <div aria-hidden className="bg-muted mt-1 h-1.5 overflow-hidden rounded-full">
            <div
              className={cn("h-full rounded-full", TONES[row.tone ?? "default"])}
              style={{ width: shareOf(row.value, largest) }}
            />
          </div>

          {row.detail ? (
            <p className="text-muted-foreground tabular mt-1 text-xs">{row.detail}</p>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
